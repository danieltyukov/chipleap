"""Find the open-source tools the verification steps use."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None
    version: str
    purpose: str
    # Why a tool that seems to be there is not used: a relative CHIPLEAP_<TOOL>, or a copy
    # found only where ChipLeap does not run tools from.
    note: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None

    def missing(self, label: str) -> str:
        """``label is not installed``, with the reason a copy that is there was not used."""
        return f"{label} is not installed" + (f" ({self.note})" if self.note else "")


# Directories no tool may come from in this context: the project a step runs on (see
# ``untrusted``). A context variable, so that MCP calls on two projects in two threads
# each keep their own.
_UNTRUSTED: ContextVar[tuple[Path, ...]] = ContextVar("chipleap_untrusted", default=())


@contextmanager
def untrusted(*dirs: str | Path) -> Iterator[None]:
    """Within the block, ``search_path``, ``which`` and ``clean_env`` also leave out
    ``dirs`` and everything below them. A step wraps its run in this with its project's
    directory (``chipleap.rtl.project.runs_tools``), so a tool the project ships is not
    found even through an absolute PATH entry that leads into it."""
    token = _UNTRUSTED.set((*_UNTRUSTED.get(), *(Path(os.path.realpath(d)) for d in dirs)))
    try:
        yield
    finally:
        _UNTRUSTED.reset(token)


def _banned(exclude: Iterable[str | Path]) -> list[Path]:
    return [*_UNTRUSTED.get(), *(Path(os.path.realpath(e)) for e in exclude)]


def search_path(exclude: Iterable[str | Path] = (), path: str | None = None) -> str:
    """``path`` (this process's PATH by default) cut down to the directories a tool may
    be found in: absolute ones outside ``exclude`` and ``untrusted`` directories. An
    empty entry, ``.`` or any other relative entry names the current directory or one
    below it, and during a run that is a project or its build directory: a project that
    ships its own z3, yosys or sby would have it run in place of the real one. An
    absolute entry inside the project does the same."""
    raw = os.environ.get("PATH", os.defpath) if path is None else path
    banned = _banned(exclude)
    keep: list[str] = []
    for entry in raw.split(os.pathsep):
        if not entry or not os.path.isabs(entry) or entry in keep:
            continue
        if any(Path(os.path.realpath(entry)).is_relative_to(b) for b in banned):
            continue
        keep.append(entry)
    return os.pathsep.join(keep)


def which(name: str, exclude: Iterable[str | Path] = (), path: str | None = None) -> str | None:
    """The absolute path of the program ``name`` on ``search_path``, or None. A name with
    a directory in it is taken as it is when absolute and refused when relative, since
    a relative one is looked up from wherever ChipLeap happens to run."""
    exclude = list(exclude)
    if os.sep in name or (os.altsep and os.altsep in name):
        found = shutil.which(name) if os.path.isabs(name) else None
    else:
        found = shutil.which(name, path=_usable_path(exclude, path))
    # shutil.which tries the current directory first on Windows; never take that.
    if found is None or not os.path.isabs(found):
        return None
    if any(Path(os.path.realpath(found)).is_relative_to(b) for b in _banned(exclude)):
        return None
    return found


def _usable_path(exclude: Iterable[str | Path], path: str | None) -> str:
    """``search_path``, or the system's default directories when nothing is left: an empty
    PATH would make a program started by name resolve in its working directory."""
    exclude = list(exclude)
    return search_path(exclude, path) or search_path(exclude, os.defpath)


def clean_env(
    prepend: Iterable[str | Path] = (),
    exclude: Iterable[str | Path] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``base`` (this process's environment by default) with PATH made of ``prepend``
    (directories ChipLeap chose) and ``search_path(exclude)``, both held to the same
    rule. Every program ChipLeap starts gets an environment from here, so a tool that
    starts another by name (SymbiYosys starts yosys and z3, cocotb starts the simulator,
    a script's ``#!/usr/bin/env python3`` starts Python) never finds one in its working
    directory or the project."""
    exclude = list(exclude)
    env = dict(os.environ if base is None else base)
    first = search_path(exclude, os.pathsep.join(str(d) for d in prepend))
    rest = _usable_path(exclude, env.get("PATH", os.defpath))
    env["PATH"] = os.pathsep.join(p for p in (first, rest) if p)
    return env


def probe(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    """Run a short query of a tool (its version, where its Python is) in an empty
    directory of its own: run in a project, ``python -c "import cocotb"`` would import a
    ``cocotb`` package the project ships, since Python looks in the current directory
    first."""
    with tempfile.TemporaryDirectory(prefix="chipleap-probe-") as empty:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=empty,
            env=clean_env(exclude=[empty]),
        )


def _version(cmd: list[str], pattern: str) -> str:
    try:
        out = probe(cmd)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(pattern, out.stdout + out.stderr)
    return m.group(1) if m else ""


def is_wrapper(path: str) -> bool:
    """Shell scripts that start a container are tools too, but checking their version
    would pull an image, so they are reported without one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(400)
    except OSError:
        return False
    return head.startswith(b"#!") and b"docker" in head


def locate(name: str, env: str | None = None) -> str | None:
    """Where the program ``name`` is: the absolute path the variable ``env`` names, else
    ``which(name)``. A variable that holds a bare name is looked up the same way; one
    that holds a relative path is not used (``located`` says why)."""
    return located(name, env)[0]


def located(name: str, env: str | None = None) -> tuple[str | None, str]:
    """``locate``, and when it finds nothing although something is there, why."""
    given = os.environ.get(env, "") if env else ""
    if given and not os.path.isabs(given) and os.sep in given:
        return None, f"{env} is a relative path, {given}; give an absolute one"
    path = which(given or name)
    if path is not None:
        return path, ""
    if given and os.path.isabs(given) and shutil.which(given):
        return None, f"{env} names {given}, inside the project, where ChipLeap runs no tools"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        copy = Path(entry or os.curdir) / (given or name)
        if copy.is_file() and os.access(copy, os.X_OK):
            return None, (
                f"the only {copy.name} found is {copy}, from a PATH entry ChipLeap does not "
                "run tools from: relative, or inside the project"
            )
    return None, ""


def find(
    name: str, version_args: list[str], pattern: str, purpose: str, env: str | None = None
) -> Tool:
    path, note = located(name, env)
    if path is None:
        return Tool(name, None, "", purpose, note)
    if is_wrapper(path):
        return Tool(name, path, "container wrapper", purpose)
    return Tool(name, path, _version([path, *version_args], pattern), purpose)


def verilator() -> Tool:
    return find(
        "verilator",
        ["--version"],
        r"Verilator (\S+)",
        "cycle-accurate simulation and lint",
        "CHIPLEAP_VERILATOR",
    )


def yosys() -> Tool:
    return find("yosys", ["-V"], r"Yosys (\S+)", "logic synthesis", "CHIPLEAP_YOSYS")


def openroad() -> Tool:
    return find("openroad", ["-version"], r"(\S+)", "static timing (OpenSTA)", "CHIPLEAP_OPENROAD")


def sta() -> Tool:
    return find("sta", ["-version"], r"(\S+)", "static timing (standalone OpenSTA)", "CHIPLEAP_STA")


def librelane() -> Tool:
    return find(
        "librelane",
        ["--version"],
        r"LibreLane v?(\S+)",
        "placement, routing, DRC and LVS (LibreLane with OpenROAD, KLayout, Magic, Netgen)",
        "CHIPLEAP_LIBRELANE",
    )


def iverilog() -> Tool:
    return find(
        "iverilog",
        ["-V"],
        r"Icarus Verilog version (\S+)",
        "four-state simulation of RTL projects (with cocotb); 13 or newer for IHP gate level",
        "CHIPLEAP_IVERILOG",
    )


def sby() -> Tool:
    # SymbiYosys built from a yosys-0.xx tag has no --version; it is listed without one.
    return find(
        "sby", ["--help"], r"SBY (\S+)", "formal proofs of a project's properties", "CHIPLEAP_SBY"
    )


def eqy() -> Tool:
    return find(
        "eqy",
        ["--help"],
        r"EQY (\S+)",
        "equivalence of RTL, synthesized and routed netlists",
        "CHIPLEAP_EQY",
    )


def solver_path(tool: str | None, exclude: Iterable[str | Path] = ()) -> str:
    """The search path a formal tool's run gets: the tool's own directory first, where
    scripts/install_formal_tools.sh puts a z3 newer than the distributions' (z3 4.8.12,
    in Ubuntu 22.04 and 24.04, ran out of memory on the UART example's proof, which z3
    4.15 finishes in a second), then ``search_path(exclude)``."""
    first = [str(Path(tool).parent)] if tool and os.path.isabs(tool) else []
    return clean_env(first, exclude, {"PATH": os.environ.get("PATH", os.defpath)})["PATH"]


def z3() -> Tool:
    """The z3 SymbiYosys runs with: the one beside it, else the one on the path."""
    path = which("z3", path=solver_path(locate("sby", "CHIPLEAP_SBY")))
    purpose = "SMT solver for formal and equivalence"
    if path is None:
        return Tool("z3", None, "", purpose)
    return Tool("z3", path, _version([path, "--version"], r"Z3 version (\S+)"), purpose)


def slang() -> Tool:
    return find(
        "slang",
        ["--version"],
        r"slang version (\S+)",
        "second SystemVerilog front end for linting RTL projects",
        "CHIPLEAP_SLANG",
    )


def sv2v() -> Tool:
    return find(
        "sv2v",
        ["--version"],
        r"sv2v v?(\S+)",
        "SystemVerilog front end for older Yosys",
        "CHIPLEAP_SV2V",
    )


def riscv_gcc() -> Tool:
    for name in (
        "riscv32-unknown-elf-gcc",
        "riscv32-corev-elf-gcc",
        "riscv64-unknown-elf-gcc",
        "riscv-none-elf-gcc",
    ):
        t = find(
            name, ["--version"], r"\) (\S+)", "compile the firmware driver", "CHIPLEAP_RISCV_GCC"
        )
        if t.found:
            return t
    return Tool("riscv-gcc", None, "", "compile the firmware driver")


def cxx() -> Tool:
    return find("c++", ["--version"], r"(\d+\.\d+\.\d+)", "build the Verilator testbench")


def all_tools() -> list[Tool]:
    return [
        verilator(),
        cxx(),
        yosys(),
        openroad(),
        sta(),
        librelane(),
        riscv_gcc(),
        iverilog(),
        slang(),
        sby(),
        eqy(),
        z3(),
    ]


# Commercial tools the backends drive (chipleap.verify.backends). They are only looked up
# on the path, never started here, so listing them checks out no licence.
COMMERCIAL = (
    ("xrun", "simulation (Cadence Xcelium)"),
    ("vcs", "simulation (Synopsys VCS)"),
    ("vsim", "simulation (Siemens Questa)"),
    ("genus", "synthesis (Cadence Genus)"),
    ("dc_shell", "synthesis (Synopsys Design Compiler)"),
    ("lc_shell", "liberty to .db for Design Compiler (Synopsys Library Compiler)"),
    ("innovus", "place and route scripts (Cadence Innovus)"),
)


def commercial() -> list[Tool]:
    return [Tool(n, which(n), "", purpose) for n, purpose in COMMERCIAL]


def liberty_settings_problem() -> str:
    """What is wrong with CHIPLEAP_LIBERTY or PDK_ROOT, if anything: a relative path would
    be read from wherever ChipLeap runs, which may be a project, so neither is used."""
    for var in ("CHIPLEAP_LIBERTY", "PDK_ROOT"):
        value = os.environ.get(var, "")
        if value and not os.path.isabs(value):
            return f"{var} is a relative path, {value}; give an absolute one"
    lib = os.environ.get("CHIPLEAP_LIBERTY", "")
    if lib and not Path(lib).is_file():
        return f"CHIPLEAP_LIBERTY names {lib}, which is not a file"
    return ""


def ihp_liberty() -> Path | None:
    """The IHP SG13G2 typical-corner standard-cell library, if a PDK is installed.
    CHIPLEAP_LIBERTY and PDK_ROOT count only as absolute paths
    (``liberty_settings_problem``)."""
    candidates = []
    env = os.environ.get("CHIPLEAP_LIBERTY")
    if env:
        return Path(env) if os.path.isabs(env) and Path(env).is_file() else None
    pdk_root = os.environ.get("PDK_ROOT", "")
    roots = [
        pdk_root if os.path.isabs(pdk_root) else "",
        str(Path.home() / ".volare"),
        str(Path.home() / ".ciel"),
        str(Path.home() / ".local/share/pdk"),
        "/usr/local/share/pdk",
        "/opt/pdk",
    ]
    name = "sg13g2_stdcell_typ_1p20V_25C.lib"
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        candidates += list(Path(root).rglob(name))
        if candidates:
            break
    # Several copies are common (a PDK checkout plus the one LibreLane pins); take the
    # newest library revision, so the choice does not depend on directory names.
    return max(candidates, key=lambda c: (_lib_revision(c), str(c))) if candidates else None


def _lib_revision(path: Path) -> tuple[int, ...]:
    try:
        head = path.open(errors="replace").read(4000)
    except OSError:
        return ()
    m = re.search(r"revision\s*:\s*\"\$Revision:\s*([\d.]+)", head)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()
