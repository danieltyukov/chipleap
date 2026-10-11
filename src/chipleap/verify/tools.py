"""Find the open-source tools the verification steps use.

A tool is found by an absolute path, never through the current directory. The search
path is ``PATH`` without its empty and relative entries (an empty entry is the current
directory, which during a step is the project or a work directory in its ``build/``),
and a ``CHIPLEAP_<TOOL>`` variable must name an absolute path or a command on that
search path. While a step runs on a project (``excluding``), neither a tool nor any
entry of the search paths ChipLeap hands its tools (``tool_env``: ``PATH``,
``PYTHONPATH``, ``LD_LIBRARY_PATH``) may lie inside the project, so a repository cannot
ship its own ``vvp``, ``fault`` or Python package and have a step run it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

# Directories no tool may come from while a step runs: the project's.
_EXCLUDED: ContextVar[tuple[Path, ...]] = ContextVar("chipleap_tools_excluded", default=())
# Search paths a tool reads from its environment: an empty or relative entry in any of
# them is resolved against the tool's working directory.
SEARCH_PATHS = ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH")


@contextmanager
def excluding(*dirs: Path) -> Iterator[None]:
    """Within the block, in this thread, find no tool in ``dirs`` and leave them out of
    every search path given to a tool. A step wraps its work in this with the project's
    directory, which holds ``build/``."""
    added = tuple(Path(d).resolve() for d in dirs)
    token = _EXCLUDED.set((*_EXCLUDED.get(), *added))
    try:
        yield
    finally:
        _EXCLUDED.reset(token)


def excluded(path: str | Path) -> bool:
    """Whether ``path`` is relative, or inside a directory excluded here."""
    if not os.path.isabs(path):
        return True
    try:
        real = Path(path).resolve()
    except (OSError, RuntimeError):
        return True
    return any(real == d or real.is_relative_to(d) for d in _EXCLUDED.get())


def trusted_entries(value: str | None) -> list[str]:
    """The entries of a search path (``PATH`` and the like) that a tool may use: absolute,
    and outside the excluded directories; empty and relative ones are dropped."""
    out: list[str] = []
    for entry in (value or "").split(os.pathsep):
        if entry and not excluded(entry) and entry not in out:
            out.append(entry)
    return out


def search_path() -> str:
    """``PATH`` as ChipLeap searches it for a tool."""
    return os.pathsep.join(trusted_entries(os.environ.get("PATH")))


def which(name: str) -> str | None:
    """The absolute path of the command ``name``: a path is taken as it is when it is
    absolute, executable and not excluded, a bare name is looked up on ``search_path``."""
    if not name:
        return None
    if os.sep in name or (os.altsep is not None and os.altsep in name):
        ok = not excluded(name) and os.path.isfile(name) and os.access(name, os.X_OK)
        return name if ok else None
    found = shutil.which(name, path=search_path())
    return found if found is not None and not excluded(found) else None


def from_env(var: str) -> str | None:
    """The tool a ``CHIPLEAP_<TOOL>`` variable names, through ``which``: None when the
    variable is unset or names nothing ChipLeap would run."""
    value = os.environ.get(var, "")
    return which(value) if value else None


def tool_env(base: dict[str, str] | None = None, prepend: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment for a tool: ``base`` (the process's own by default) with every
    search path reduced to its trusted entries, ``prepend`` first on ``PATH``. A search
    path left with no entry is removed."""
    env = dict(os.environ if base is None else base)
    for var in SEARCH_PATHS:
        extra = [*prepend] if var == "PATH" else []
        entries = trusted_entries(os.pathsep.join([*extra, env.get(var, "")]))
        if entries:
            env[var] = os.pathsep.join(entries)
        else:
            env.pop(var, None)
    home = env.get("PYTHONHOME")
    if home is not None and excluded(home):
        env.pop("PYTHONHOME")
    return env


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None
    version: str
    purpose: str

    @property
    def found(self) -> bool:
        return self.path is not None


def _version(cmd: list[str], pattern: str) -> str:
    # Out of the project, with the trusted search paths: a version probe runs nothing a
    # repository put there.
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            cwd=os.sep,
            env=tool_env(),
        )
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


def find(
    name: str, version_args: list[str], pattern: str, purpose: str, env: str | None = None
) -> Tool:
    path = (from_env(env) if env and os.environ.get(env) else which(name)) or ""
    if not path:
        return Tool(name, None, "", purpose)
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
    return find(
        "openroad",
        ["-version"],
        r"(\S+)",
        "timing, scan insertion, power, IR drop, UPF and ECO (OpenROAD)",
        "CHIPLEAP_OPENROAD",
    )


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
        "four-state simulation of RTL projects (with cocotb)",
        "CHIPLEAP_IVERILOG",
    )


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
    else ``iverilog`` on the path when it is new enough. Its ``vvp`` is the one beside it."""
    name = "iverilog-gl"
    var = "CHIPLEAP_GL_IVERILOG"
    path = (from_env(var) if os.environ.get(var) else which("iverilog")) or ""
    if not path:
        return Tool(name, None, "", GL_PURPOSE)
    version = _version([path, "-V"], r"Icarus Verilog version (\S+)")
    numbers = tuple(int(x) for x in re.findall(r"\d+", version)[:1])
    if numbers < GL_ICARUS_MIN:
        return Tool(name, None, version, GL_PURPOSE)
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
    var = "CHIPLEAP_FAULT"
    path = (from_env(var) if os.environ.get(var) else which("fault")) or ""
    purpose = "ATPG and stuck-at fault coverage (Fault, with Quaigh)"
    if not path:
        return Tool("fault", None, "", purpose)
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=os.sep,
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


def fault_env(fault_path: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment Fault needs. Fault from PyPI embeds Python and does not see the
    virtual environment it was installed in, so its packages go on ``PYTHONPATH`` (and
    the base installation in ``PYTHONHOME``). It also runs ``vvp`` from the path, which
    must match the ``iverilog`` that compiled the testbench, so both are named; and
    Quaigh goes first on the path."""
    env = dict(os.environ if base is None else base)
    py = _interpreter(fault_path)
    if py is not None:
        # Isolated (-I): no current directory, user site or PYTHON* variables, so only the
        # interpreter's own installation answers.
        try:
            out = subprocess.run(
                [
                    py,
                    "-I",
                    "-c",
                    "import sys, sysconfig; print(sysconfig.get_paths()['purelib']); "
                    "print(sys.base_prefix)",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=os.sep,
                env=tool_env(base),
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
    return tool_env(env, tuple(first))


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
    """Where PDKs live on this machine: ``PDK_ROOT`` and the usual install directories,
    resolved. Files the implementation steps take from a PDK must be under one of them,
    or beside the liberty ``CHIPLEAP_LIBERTY`` names."""
    roots = [os.environ.get("PDK_ROOT", ""), *PDK_ROOTS]
    out = [Path(r).expanduser().resolve() for r in roots if r and Path(r).expanduser().is_dir()]
    env = os.environ.get("CHIPLEAP_LIBERTY")
    if env and Path(env).is_file():
        # libs.ref/<library>/lib/<file>.lib: the library's own directory holds its views.
        out.append(Path(env).resolve().parent.parent)
    return tuple(dict.fromkeys(out))


def ihp_liberty() -> Path | None:
    """The IHP SG13G2 typical-corner standard-cell library, if a PDK is installed."""
    candidates = []
    env = os.environ.get("CHIPLEAP_LIBERTY")
    if env:
        return Path(env) if Path(env).is_file() else None
    roots = [os.environ.get("PDK_ROOT", ""), *(str(Path(r).expanduser()) for r in PDK_ROOTS)]
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
