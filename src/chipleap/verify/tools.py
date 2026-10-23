"""Find the open-source tools the verification steps use."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
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


# Search paths other than PATH that a tool resolves an empty or relative entry of against
# its working directory: Python's module path and the dynamic loader's.
LIBRARY_PATHS = ("PYTHONPATH", "LD_LIBRARY_PATH")


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
    directory or the project. ``PYTHONPATH`` and ``LD_LIBRARY_PATH`` are held to the same
    rule (one left empty is removed, since an empty one means the current directory), so
    a project's ``sitecustomize`` or shared library is not loaded either, and a
    ``PYTHONHOME`` inside the project is dropped."""
    exclude = list(exclude)
    env = dict(os.environ if base is None else base)
    first = search_path(exclude, os.pathsep.join(str(d) for d in prepend))
    rest = _usable_path(exclude, env.get("PATH", os.defpath))
    env["PATH"] = os.pathsep.join(p for p in (first, rest) if p)
    for var in LIBRARY_PATHS:
        if var in env:
            kept = search_path(exclude, env[var])
            if kept:
                env[var] = kept
            else:
                del env[var]
    home = env.get("PYTHONHOME")
    if home is not None and excluded(home, exclude):
        del env["PYTHONHOME"]
    return env


def excluded(path: str | Path, exclude: Iterable[str | Path] = ()) -> bool:
    """Whether ``path`` is relative, or inside ``exclude`` or an ``untrusted`` directory."""
    if not os.path.isabs(path):
        return True
    real = Path(os.path.realpath(path))
    return any(real.is_relative_to(b) for b in _banned(exclude))


def probe(
    cmd: list[str], timeout: float = 20, base: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a short query of a tool (its version, where its Python is) in an empty
    directory of its own: run in a project, ``python -c "import cocotb"`` would import a
    ``cocotb`` package the project ships, since Python looks in the current directory
    first. ``base`` is the environment it starts from (``clean_env``)."""
    with tempfile.TemporaryDirectory(prefix="chipleap-probe-") as empty:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=empty,
            env=clean_env(exclude=[empty], base=base),
        )


def _version(cmd: list[str], pattern: str, base: Mapping[str, str] | None = None) -> str:
    try:
        out = probe(cmd, base=base)
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
    name: str,
    version_args: list[str],
    pattern: str,
    purpose: str,
    env: str | None = None,
    base: Mapping[str, str] | None = None,
) -> Tool:
    """The tool ``name`` and its version, asked for in the environment ``base`` (this
    process's by default)."""
    path, note = located(name, env)
    if path is None:
        return Tool(name, None, "", purpose, note)
    if is_wrapper(path):
        return Tool(name, path, "container wrapper", purpose)
    return Tool(name, path, _version([path, *version_args], pattern, base), purpose)


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
    return find(
        "openroad",
        ["-version"],
        r"(\S+)",
        "static timing, scan insertion, power, IR drop, UPF and ECO (OpenROAD)",
        "CHIPLEAP_OPENROAD",
    )


def sta() -> Tool:
    return find("sta", ["-version"], r"(\S+)", "static timing (standalone OpenSTA)", "CHIPLEAP_STA")


def librelane() -> Tool:
    """LibreLane, its version asked for as a run would start it (``librelane_env``). For a
    wrapper ChipLeap does not ask (``is_wrapper``) given ``CHIPLEAP_LIBRELANE_IMAGE``, the
    version is the image's tag (``3.0.14`` of ``ghcr.io/librelane/librelane:3.0.14``); each
    run checks it is the one that ran (``librelane_ran``)."""
    try:
        base: Mapping[str, str] | None = librelane_env()
    except ValueError:  # a run reports the setting's problem
        base = None
    tool = find(
        "librelane",
        ["--version"],
        r"LibreLane v?(\S+)",
        "placement, routing, DRC and LVS (LibreLane with OpenROAD, KLayout, Magic, Netgen)",
        "CHIPLEAP_LIBRELANE",
        base,
    )
    image = librelane_image()
    if image and tool.found and tool.version == "container wrapper":
        tag = image.rpartition("/")[2].partition(":")[2]
        return replace(tool, version=tag or f"container wrapper, {image}")
    return tool


LIBRELANE_IMAGE_ENV = "CHIPLEAP_LIBRELANE_IMAGE"


def librelane_image() -> str:
    """The LibreLane container image ChipLeap asks a wrapper for (``CHIPLEAP_LIBRELANE_IMAGE``,
    passed on as ``LIBRELANE_IMAGE``); empty to leave the wrapper's own."""
    return os.environ.get(LIBRELANE_IMAGE_ENV, "").strip()


def librelane_env() -> dict[str, str]:
    """This process's environment with the settings a LibreLane container wrapper reads:
    ``LIBRELANE_IMAGE`` from ``CHIPLEAP_LIBRELANE_IMAGE`` (the image it starts) and
    ``LIBRELANE_PDK_ROOT`` from ``CHIPLEAP_LIBRELANE_PDK_ROOT`` (the PDK root it mounts), so
    a run gets the LibreLane and PDK ChipLeap was set to, whatever the wrapper's defaults."""
    env = dict(os.environ)
    if image := librelane_image():
        env["LIBRELANE_IMAGE"] = image
    if (root := librelane_pdk_root()) is not None:
        env["LIBRELANE_PDK_ROOT"] = str(root)
    return env


def librelane_ran(run: Path) -> str:
    """The LibreLane version that ran ``run`` (its ``resolved.json``), checked against the
    one ChipLeap expected (``librelane().version``, when that is a version): a wrapper that
    ignored ``CHIPLEAP_LIBRELANE_IMAGE`` raises ``ValueError``, since the run's
    configuration was written for the other. Empty when the run does not say."""
    try:
        meta = json.loads((run / "resolved.json").read_text()).get("meta") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    ran = str(meta.get("librelane_version") or "") if isinstance(meta, dict) else ""
    expected = librelane().version
    if ran and librelane_version_key(expected) != (0, 0, 0, 0, 0) and ran != expected:
        raise ValueError(
            f"LibreLane {ran} ran, not {expected}"
            + (
                f": the wrapper did not start {librelane_image()} ({LIBRELANE_IMAGE_ENV})"
                if librelane_image()
                else ""
            )
        )
    return ran


def librelane_version_key(version: str) -> tuple[int, int, int, int, int]:
    """Orders LibreLane versions: ``3.0.0.dev44`` before ``3.0.0rc1`` before ``3.0.0``
    before ``3.0.14``. An unreadable version sorts first."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:\.?(dev|rc)(\d+))?", version)
    if not m:
        return (0, 0, 0, 0, 0)
    stage = {"dev": 0, "rc": 1}.get(m[4] or "", 2)
    return (int(m[1]), int(m[2]), int(m[3]), stage, int(m[5] or 0))


def librelane_loads_pad_views() -> bool:
    """Whether the LibreLane here loads a PDK's pad library views itself (``PAD_LEFS`` and
    the rest, from 3.0.0 on; IHP-Open-PDK sets them from its LibreLane configuration of
    May 2026)."""
    tool = librelane()
    return tool.found and librelane_version_key(tool.version) >= (3, 0, 0, 2, 0)


LIBRELANE_PDK_ENV = "CHIPLEAP_LIBRELANE_PDK_ROOT"
LIBRELANE_PDK = "ihp-sg13g2"


def librelane_pdk_root() -> Path | None:
    """The PDK root LibreLane runs on, when ``CHIPLEAP_LIBRELANE_PDK_ROOT`` names one: a
    directory holding ``ihp-sg13g2`` (a ciel root with the commit of the chip's PDK
    checkout enabled). ``None`` when it is unset: LibreLane then installs the version it
    pins itself, through ciel. A setting that is not an absolute directory with
    ``ihp-sg13g2`` in it raises ``ValueError``, so a run never falls back silently."""
    value = os.environ.get(LIBRELANE_PDK_ENV, "")
    if not value:
        return None
    if not os.path.isabs(value):
        raise ValueError(f"{LIBRELANE_PDK_ENV} is a relative path, {value}; give an absolute one")
    root = Path(value)
    if not (root / LIBRELANE_PDK).is_dir():
        raise ValueError(f"{LIBRELANE_PDK_ENV} names {value}, which holds no {LIBRELANE_PDK}")
    return root


def librelane_pdk_args() -> list[str]:
    """LibreLane's PDK arguments: the PDK and, with ``CHIPLEAP_LIBRELANE_PDK_ROOT``,
    ``--manual-pdk --pdk-root`` it (``librelane_pdk_root``)."""
    root = librelane_pdk_root()
    args = ["--pdk", LIBRELANE_PDK]
    if root is not None:
        args += ["--manual-pdk", "--pdk-root", str(root)]
    return args


def librelane_pdk_commit(root: Path) -> str:
    """The commit of the PDK under a ciel root: ciel keeps each version in
    ``ciel/<family>/versions/<commit>``, which ``ihp-sg13g2`` links to. Empty when the
    path does not say."""
    real = (root / LIBRELANE_PDK).resolve()
    parts = real.parts
    if "versions" in parts:
        i = parts.index("versions")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


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


GL_ICARUS_MIN = (13,)
GL_PURPOSE = "gate-level simulation of IHP cells: scan patterns and power activity (13 or newer)"


def gl_iverilog() -> Tool:
    """An Icarus Verilog that simulates IHP SG13G2's cell models. Icarus 12 elaborates
    them but leaves every flip-flop at X, so it needs 13 or newer: ``CHIPLEAP_GL_IVERILOG``,
    else the ``iverilog`` ChipLeap uses (``CHIPLEAP_IVERILOG`` or the path) when it is new
    enough. Its ``vvp`` is the one beside it."""
    name = "iverilog-gl"
    path, note = located("iverilog", "CHIPLEAP_GL_IVERILOG")
    if path is None and not os.environ.get("CHIPLEAP_GL_IVERILOG"):
        path, note = located("iverilog", "CHIPLEAP_IVERILOG")
    if path is None:
        return Tool(name, None, "", GL_PURPOSE, note)
    version = _version([path, "-V"], r"Icarus Verilog version (\S+)")
    numbers = tuple(int(x) for x in re.findall(r"\d+", version)[:1])
    if numbers < GL_ICARUS_MIN:
        return Tool(name, None, version, GL_PURPOSE, f"{path} is Icarus {version or '?'}")
    return Tool(name, path, version, GL_PURPOSE)


def gl_vvp(tool: Tool) -> str | None:
    """The ``vvp`` that goes with a gate-level Icarus: the two must be the same version."""
    if tool.path is None:
        return None
    return which(str(Path(tool.path).parent / "vvp"))


def quaigh() -> Tool:
    return find(
        "quaigh",
        ["--version"],
        r"quaigh (\S+)",
        "test pattern generation for Fault (ATPG)",
        "CHIPLEAP_QUAIGH",
    )


def nl2bench() -> Tool:
    return find("nl2bench", ["--help"], r"(nl2bench)", "netlist to bench for Fault's ATPG")


def fault() -> Tool:
    path, note = located("fault", "CHIPLEAP_FAULT")
    purpose = "ATPG and stuck-at fault coverage (Fault, with Quaigh)"
    if path is None:
        return Tool("fault", None, "", purpose, note)
    try:
        with tempfile.TemporaryDirectory(prefix="chipleap-probe-") as empty:
            out = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=empty,
                env=fault_env(path),
            )
    except (OSError, subprocess.TimeoutExpired):
        return Tool("fault", path, "", purpose)
    m = re.search(r"^(\d+\.\d+\.\d+)\s*$", out.stdout, re.M)
    return Tool("fault", path, m.group(1) if m else "", purpose)


def _interpreter(script: str) -> str | None:
    """The Python a console script runs under, from its first line, as ``which`` finds
    it: a bare name after ``env`` is looked up on the trusted search path, not the
    script's."""
    try:
        with open(script, "rb") as fh:
            head = fh.readline(400).decode(errors="replace").strip()
    except OSError:
        return None
    if not head.startswith("#!"):
        return None
    words = head[2:].split()
    if not words:
        return None
    exe = words[-1] if Path(words[0]).name == "env" else words[0]
    return which(exe) if "python" in Path(exe).name else None


def fault_env(fault_path: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment Fault needs. Fault from PyPI embeds Python and does not see the
    virtual environment it was installed in, so its packages go on ``PYTHONPATH`` (and
    the base installation in ``PYTHONHOME``), asked of the interpreter Fault is installed
    with, in isolated mode (``-I``: no current directory, user site or ``PYTHON*``
    variables). It also runs ``vvp`` from the path, which must match the ``iverilog``
    that compiled the testbench, so both are named; and Quaigh goes first on the path.
    Everything goes through ``clean_env``."""
    env = dict(os.environ if base is None else base)
    py = _interpreter(fault_path)
    if py is not None:
        try:
            out = probe(
                [
                    py,
                    "-I",
                    "-c",
                    "import sys, sysconfig; print(sysconfig.get_paths()['purelib']); "
                    "print(sys.base_prefix)",
                ],
                timeout=60,
            )
            lines = out.stdout.splitlines()
            if out.returncode == 0 and len(lines) >= 2 and not excluded(lines[1]):
                env["PYTHONPATH"] = os.pathsep.join([lines[0], env.get("PYTHONPATH", "")])
                env["PYTHONHOME"] = lines[1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    first: list[str] = []
    gl = gl_iverilog()
    vvp = gl_vvp(gl)
    if gl.path and vvp:
        env["FAULT_IVERILOG"] = gl.path
        env["FAULT_VVP"] = vvp
        first.append(str(Path(gl.path).parent))
    q = quaigh()
    if q.path:
        first.append(str(Path(q.path).parent))
    return clean_env(first, base=env)


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


def fusesoc() -> Tool:
    return find(
        "fusesoc",
        ["--version"],
        r"(\S+)",
        "X-HEEP's sources for place and route of the SoC (X-HEEP's FuseSoC)",
        "CHIPLEAP_FUSESOC",
    )


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
        fusesoc(),
        iverilog(),
        slang(),
        sby(),
        eqy(),
        z3(),
        gl_iverilog(),
        fault(),
        quaigh(),
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


PDK_ROOTS = (
    "~/.volare",
    "~/.ciel",
    "~/.local/share/pdk",
    "/usr/local/share/pdk",
    "/opt/pdk",
)


def pdk_roots() -> tuple[Path, ...]:
    """Where PDKs live on this machine: ``PDK_ROOT``, the usual install directories and
    the PDK root LibreLane runs on (``librelane_pdk_root``), resolved. Files the
    implementation steps take from a PDK must be under one of them, or beside the liberty
    ``CHIPLEAP_LIBERTY`` names. Relative settings count for nothing
    (``liberty_settings_problem``)."""
    pdk_root = os.environ.get("PDK_ROOT", "")
    roots = [pdk_root if os.path.isabs(pdk_root) else "", *PDK_ROOTS]
    try:
        librelane_root = librelane_pdk_root()
    except ValueError:
        librelane_root = None
    if librelane_root is not None:
        roots.append(str(librelane_root))
    out = [Path(r).expanduser().resolve() for r in roots if r and Path(r).expanduser().is_dir()]
    env = os.environ.get("CHIPLEAP_LIBERTY")
    if env and os.path.isabs(env) and Path(env).is_file():
        # libs.ref/<library>/lib/<file>.lib: the library's own directory holds its views.
        out.append(Path(env).resolve().parent.parent)
    return tuple(dict.fromkeys(out))


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
