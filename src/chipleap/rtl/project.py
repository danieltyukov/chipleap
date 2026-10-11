"""The project file for user RTL: ``project.yaml``.

A spec says what a workload needs and a design says how the generated accelerator is
built; a project describes a design someone else wrote. It names the sources, the top,
the clocks and resets, the technology and the cocotb tests, and the verification and
implementation steps read everything else from the RTL itself. Paths are relative to the
project file. Every key is checked, and an error names the key, so a typo fails with a
message that says where it is.

Later milestones add sections (formal properties, macros, a pad ring). Their names are
reserved here and rejected with a message that says so. To add one: a frozen dataclass
and a ``_parse_<name>`` function, a field on ``Project`` with a default, an entry in
``SECTIONS``, and the key removed from ``RESERVED``.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chipleap.hw.design import SPEC_NAME_MAX, is_safe_name, safe_name
from chipleap.hw.tech import TECHNOLOGIES

PROJECT_FILE = "project.yaml"
SCHEMA = "project/v1"
SIMULATORS = ("auto", "icarus", "verilator")
HDL_SUFFIXES = (".sv", ".v", ".svh", ".vh")
# Files an include directory may hold that change what the RTL means.
INCLUDE_SUFFIXES = (*HDL_SUFFIXES, ".h", ".inc", ".vlt", ".mem", ".hex")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
TIME_RE = re.compile(r"(1|10|100)(s|ms|us|ns|ps|fs)")
# Sections later milestones will read. A file written for them fails clearly here.
RESERVED = {
    "formal": "D2",
    "equivalence": "D2",
    "cdc": "D2",
    "gate_sim": "D2",
    "macros": "D3",
    "dft": "D3",
    "power": "D3",
    "ir_drop": "D3",
    "upf": "D3",
    "eco": "D3",
    "chip": "D4",
}
SKIP_DIRS = {"build", "sim_build", "runs", "obj_dir", "__pycache__", "node_modules"}


class ProjectError(ValueError):
    pass


@dataclass(frozen=True)
class Clock:
    port: str
    period_ns: float

    @property
    def mhz(self) -> float:
        return 1000.0 / self.period_ns


@dataclass(frozen=True)
class Reset:
    port: str
    active_low: bool = True
    sync: bool = False


@dataclass(frozen=True)
class Tests:
    """The project's cocotb tests. ``dir`` is None when the project has none."""

    dir: Path | None = None
    modules: tuple[str, ...] = ()
    simulator: str = "auto"
    seed: int = 1
    toplevel: str = ""
    sources: tuple[Path, ...] = ()  # testbench-only HDL, simulated but never synthesized


@dataclass(frozen=True)
class LintConfig:
    waivers: tuple[Path, ...] = ()  # Verilator configuration files
    slang: bool = True


@dataclass(frozen=True)
class PnrConfig:
    utilisation: float = 45.0  # FP_CORE_UTIL, percent
    density: float = 55.0  # PL_TARGET_DENSITY_PCT, percent
    die_um: tuple[float, float] | None = None
    librelane: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Project:
    root: Path  # the directory of the project file; relative paths start here
    file: Path
    name: str
    top: str
    sources: tuple[Path, ...]
    include_dirs: tuple[Path, ...] = ()
    defines: dict[str, str | None] = field(default_factory=dict)
    parameters: dict[str, int] = field(default_factory=dict)
    timescale: tuple[str, str] = ("1ns", "1ps")
    clocks: tuple[Clock, ...] = ()
    resets: tuple[Reset, ...] = ()
    tech: str = "ihp-sg13g2"
    tests: Tests = field(default_factory=Tests)
    golden: Path | None = None
    lint: LintConfig = field(default_factory=LintConfig)
    sdc: Path | None = None
    pnr: PnrConfig = field(default_factory=PnrConfig)
    description: str = ""
    filelists: tuple[Path, ...] = ()  # the filelist and every file it includes

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def fastest_clock(self) -> Clock | None:
        return min(self.clocks, key=lambda c: c.period_ns) if self.clocks else None

    def rel(self, path: str | Path) -> str:
        """``path`` relative to the project when it is inside it, else as given."""
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def define_args(self) -> list[str]:
        """Defines as ``NAME`` or ``NAME=VALUE``, for a tool's -D or +define+ option."""
        return [k if v is None else f"{k}={v}" for k, v in self.defines.items()]

    def input_files(self) -> list[Path]:
        """Every file whose content can change a result: the project file, the sources,
        include files, waivers, constraints, the tests and the golden model."""
        files: list[Path] = [self.file, *self.filelists, *self.sources, *self.tests.sources]
        for d in self.include_dirs:
            files += [
                p for p in sorted(d.rglob("*")) if p.is_file() and p.suffix in INCLUDE_SUFFIXES
            ]
        files += [*self.lint.waivers, *([self.sdc] if self.sdc else [])]
        if self.tests.dir is not None:
            files += [
                p
                for p in sorted(self.tests.dir.rglob("*.py"))
                if not SKIP_DIRS & set(p.relative_to(self.tests.dir).parts)
            ]
        if self.golden is not None:
            files += sorted(self.golden.parent.glob("*.py"))
        seen: dict[Path, None] = {}
        for f in files:
            seen.setdefault(f.resolve(), None)
        return list(seen)

    def inputs_digest(self) -> str:
        """SHA-256 over every input file's project-relative name and content, so a run log
        can tell a changed design from a changed tool."""
        h = hashlib.sha256()
        for f in sorted(self.input_files(), key=lambda p: self.rel(p)):
            h.update(self.rel(f).encode() + b"\0")
            h.update(hashlib.sha256(f.read_bytes()).digest() if f.is_file() else b"missing")
        return h.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """The project as a mapping with project-relative paths (what describe_project
        returns and what ``init_project`` writes)."""
        out: dict[str, Any] = {"schema": SCHEMA, "name": self.name}
        if self.description:
            out["description"] = self.description
        out["top"] = self.top
        out["sources"] = [self.rel(s) for s in self.sources]
        if self.include_dirs:
            out["include_dirs"] = [self.rel(d) for d in self.include_dirs]
        if self.defines:
            out["defines"] = dict(self.defines)
        if self.parameters:
            out["parameters"] = dict(self.parameters)
        out["timescale"] = "/".join(self.timescale)
        out["clocks"] = [{"port": c.port, "period_ns": c.period_ns} for c in self.clocks]
        out["resets"] = [
            {"port": r.port, "active": "low" if r.active_low else "high", "sync": r.sync}
            for r in self.resets
        ]
        out["tech"] = self.tech
        if self.tests.dir is not None:
            t: dict[str, Any] = {
                "dir": self.rel(self.tests.dir),
                "modules": list(self.tests.modules),
                "simulator": self.tests.simulator,
                "seed": self.tests.seed,
            }
            if self.tests.toplevel != self.top:
                t["toplevel"] = self.tests.toplevel
            if self.tests.sources:
                t["sources"] = [self.rel(s) for s in self.tests.sources]
            out["tests"] = t
        if self.golden is not None:
            out["golden"] = self.rel(self.golden)
        if self.lint != LintConfig():
            out["lint"] = {
                "waivers": [self.rel(w) for w in self.lint.waivers],
                "slang": self.lint.slang,
            }
        if self.sdc is not None:
            out["constraints"] = {"sdc": self.rel(self.sdc)}
        if self.pnr != PnrConfig():
            p: dict[str, Any] = {
                "utilisation": self.pnr.utilisation,
                "density": self.pnr.density,
            }
            if self.pnr.die_um:
                p["die_um"] = list(self.pnr.die_um)
            if self.pnr.librelane:
                p["librelane"] = dict(self.pnr.librelane)
            out["pnr"] = p
        return out


# Parsing. Each helper takes the key path it reports in errors.


def _mapping(value: Any, where: str, keys: Iterable[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectError(f"{where} must be a mapping")
    bad = set(value) - set(keys)
    if bad:
        raise ProjectError(f"{where}: unknown keys {', '.join(sorted(map(str, bad)))}")
    return value


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ProjectError(f"{where} must be a path or a list of paths")
    return list(value)


def _ident(value: Any, where: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise ProjectError(f"{where} must be a Verilog identifier, not {value!r}")
    return value


def _positive(value: Any, where: str, upper: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProjectError(f"{where} must be a positive number")
    if upper is not None and value > upper:
        raise ProjectError(f"{where} must be at most {upper:g}")
    return float(value)


def _expand(text: str, where: str) -> str:
    """``$VAR`` and ``${VAR}`` from the environment; an unset variable is an error."""
    out = os.path.expandvars(text)
    if "$" in out:
        raise ProjectError(f"{where}: {text!r} uses an environment variable that is not set")
    return out


def _path(root: Path, text: str, where: str, *, kind: str = "file") -> Path:
    p = Path(_expand(text, where)).expanduser()
    p = p if p.is_absolute() else root / p
    ok = p.is_dir() if kind == "dir" else p.is_file()
    if not ok:
        raise ProjectError(f"{where}: {text} is not a {'directory' if kind == 'dir' else 'file'}")
    return p


def _sources(root: Path, items: list[str], where: str) -> list[Path]:
    """Files, with glob patterns expanded in sorted order."""
    out: list[Path] = []
    for item in items:
        text = _expand(item, where)
        if any(ch in text for ch in "*?["):
            pattern = text if Path(text).is_absolute() else str(root / text)
            matches = sorted(Path(m) for m in glob.glob(pattern, recursive=True))
            matches = [m for m in matches if m.is_file()]
            if not matches:
                raise ProjectError(f"{where}: {item} matches no files")
            out += matches
        else:
            out.append(_path(root, text, where))
    return out


@dataclass
class _Filelist:
    sources: list[Path] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    files: list[Path] = field(default_factory=list)


def parse_filelist(path: Path, where: str = "filelist", _depth: int = 0) -> _Filelist:
    """A simulator filelist: source files, ``-f``/``-F`` (nested, relative to the file
    that names them), ``+incdir+``, ``-I``, ``+define+`` and ``-D``. Comments start with
    ``//`` or ``#``. Library options (``-v``, ``-y``) are not supported."""
    if _depth > 8:
        raise ProjectError(f"{where}: filelists nest more than 8 deep")
    out = _Filelist(files=[path])
    base = path.parent
    words: list[str] = []
    for raw in path.read_text(errors="replace").splitlines():
        line = re.split(r"//|#", raw, maxsplit=1)[0]
        try:
            words += shlex.split(line)
        except ValueError as exc:
            raise ProjectError(f"{where}: {path.name}: {exc}") from exc
    it = iter(words)
    for w in it:
        here = f"{where} ({path.name})"
        if w in ("-f", "-F"):
            arg = next(it, None)
            if arg is None:
                raise ProjectError(f"{here}: {w} needs a file")
            sub = parse_filelist(_path(base, arg, here), where, _depth + 1)
            out.sources += sub.sources
            out.include_dirs += sub.include_dirs
            out.defines.update(sub.defines)
            out.files += sub.files
        elif w.startswith("+incdir+"):
            out.include_dirs += [
                _path(base, d, here, kind="dir") for d in w[len("+incdir+") :].split("+") if d
            ]
        elif w.startswith("-I"):
            arg = w[2:] or next(it, "")
            out.include_dirs.append(_path(base, arg, here, kind="dir"))
        elif w.startswith("+define+"):
            for d in w[len("+define+") :].split("+"):
                if d:
                    k, _, v = d.partition("=")
                    out.defines[k] = v if "=" in d else None
        elif w.startswith("-D") and len(w) > 2:
            k, _, v = w[2:].partition("=")
            out.defines[k] = v if "=" in w else None
        elif w.startswith(("-", "+")):
            raise ProjectError(f"{here}: option {w} is not supported in a filelist")
        else:
            out.sources += _sources(base, [w], here)
    return out


def _parse_clocks(value: Any) -> tuple[Clock, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProjectError("clocks must be a list of {port, period_ns}")
    out = []
    for i, c in enumerate(value):
        where = f"clocks[{i}]"
        c = _mapping(c, where, ("port", "period_ns", "freq_mhz"))
        if ("period_ns" in c) == ("freq_mhz" in c):
            raise ProjectError(f"{where} needs period_ns or freq_mhz (one of them)")
        period = (
            _positive(c["period_ns"], f"{where}.period_ns")
            if "period_ns" in c
            else 1000.0 / _positive(c["freq_mhz"], f"{where}.freq_mhz")
        )
        out.append(Clock(_ident(c.get("port"), f"{where}.port"), round(period, 6)))
    if len({c.port for c in out}) != len(out):
        raise ProjectError("clocks: a port is listed twice")
    return tuple(out)


def _active_low_by_name(port: str) -> bool:
    return bool(re.search(r"(_n|_ni|n|_b)$", port))


def _parse_resets(value: Any) -> tuple[Reset, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProjectError("resets must be a list of {port, active, sync}")
    out = []
    for i, r in enumerate(value):
        where = f"resets[{i}]"
        r = _mapping(r, where, ("port", "active", "sync"))
        port = _ident(r.get("port"), f"{where}.port")
        active = r.get("active", "low" if _active_low_by_name(port) else "high")
        if active not in ("low", "high"):
            raise ProjectError(f"{where}.active must be low or high")
        sync = r.get("sync", False)
        if not isinstance(sync, bool):
            raise ProjectError(f"{where}.sync must be true or false")
        out.append(Reset(port, active == "low", sync))
    return tuple(out)


def _parse_defines(value: Any) -> dict[str, str | None]:
    if value is None:
        return {}
    if isinstance(value, list):  # [A, B=1] as on a command line
        value = {
            str(v).partition("=")[0]: (str(v).partition("=")[2] if "=" in str(v) else None)
            for v in value
        }
    if not isinstance(value, dict):
        raise ProjectError("defines must be a mapping of name to value (null for none)")
    out: dict[str, str | None] = {}
    for k, v in value.items():
        _ident(k, f"defines.{k}")
        if v is not None and (isinstance(v, bool) or not isinstance(v, (str, int, float))):
            raise ProjectError(f"defines.{k} must be a string, a number or null")
        out[str(k)] = None if v is None else str(v)
    return out


def _parse_parameters(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectError("parameters must be a mapping of name to integer")
    out = {}
    for k, v in value.items():
        _ident(k, f"parameters.{k}")
        # Every tool takes an integer the same way; strings need tool-specific quoting.
        if isinstance(v, bool) or not isinstance(v, int):
            raise ProjectError(
                f"parameters.{k} must be an integer (set other parameters in a wrapper module)"
            )
        out[str(k)] = v
    return out


def _parse_timescale(value: Any) -> tuple[str, str]:
    if value is None:
        return ("1ns", "1ps")
    parts = str(value).replace(" ", "").split("/")
    if len(parts) != 2 or not all(TIME_RE.fullmatch(p) for p in parts):
        raise ProjectError("timescale must look like 1ns/1ps")
    return parts[0], parts[1]


def _parse_tests(value: Any, root: Path, top: str) -> Tests:
    t = _mapping(value, "tests", ("dir", "modules", "simulator", "seed", "toplevel", "sources"))
    if not t:
        return Tests(toplevel=top)
    if "dir" not in t:
        raise ProjectError("tests.dir is required: the directory with the cocotb tests")
    d = _path(root, str(t["dir"]), "tests.dir", kind="dir")
    if "modules" in t:
        modules = _str_list(t["modules"], "tests.modules")
        for m in modules:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", m):
                raise ProjectError(f"tests.modules: {m!r} is not a Python module name")
            if not (d / f"{m.replace('.', '/')}.py").is_file():
                raise ProjectError(f"tests.modules: {m}.py not found in {t['dir']}")
    else:
        modules = sorted(p.stem for p in d.glob("test_*.py"))
        if not modules:
            raise ProjectError(f"tests.dir: no test_*.py files in {t['dir']}")
    sim = t.get("simulator", "auto")
    if sim not in SIMULATORS:
        raise ProjectError(f"tests.simulator must be one of {', '.join(SIMULATORS)}")
    seed = t.get("seed", 1)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ProjectError("tests.seed must be a non-negative integer")
    toplevel = _ident(t.get("toplevel", top), "tests.toplevel")
    srcs = _sources(root, _str_list(t.get("sources"), "tests.sources"), "tests.sources")
    return Tests(d, tuple(modules), str(sim), seed, toplevel, tuple(srcs))


def _parse_lint(value: Any, root: Path) -> LintConfig:
    lt = _mapping(value, "lint", ("waivers", "slang"))
    waivers = tuple(
        _path(root, w, "lint.waivers") for w in _str_list(lt.get("waivers"), "lint.waivers")
    )
    slang = lt.get("slang", True)
    if not isinstance(slang, bool):
        raise ProjectError("lint.slang must be true or false")
    return LintConfig(waivers, slang)


def _parse_pnr(value: Any) -> PnrConfig:
    p = _mapping(value, "pnr", ("utilisation", "density", "die_um", "librelane"))
    util = _positive(p.get("utilisation", 45), "pnr.utilisation", 95)
    density = _positive(p.get("density", 55), "pnr.density", 100)
    die = p.get("die_um")
    die_um = None
    if die is not None:
        if not isinstance(die, list) or len(die) != 2:
            raise ProjectError("pnr.die_um must be [width, height] in um")
        die_um = (_positive(die[0], "pnr.die_um[0]"), _positive(die[1], "pnr.die_um[1]"))
    extra = p.get("librelane") or {}
    if not isinstance(extra, dict) or not all(
        isinstance(k, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", k) for k in extra
    ):
        raise ProjectError("pnr.librelane must map LibreLane variable names (UPPER_CASE) to values")
    return PnrConfig(util, density, die_um, dict(extra))


# The top-level keys a project file may have. Later milestones add their sections here.
SECTIONS = (
    "schema",
    "name",
    "description",
    "top",
    "sources",
    "filelist",
    "include_dirs",
    "defines",
    "parameters",
    "timescale",
    "clocks",
    "resets",
    "tech",
    "tests",
    "golden",
    "lint",
    "constraints",
    "pnr",
)


def parse_project(data: Any, root: Path, file: Path | None = None) -> Project:
    """A project from its YAML mapping; relative paths start at ``root``."""
    if not isinstance(data, dict):
        raise ProjectError("a project file must be a YAML mapping")
    for key in data:
        if key in RESERVED:
            raise ProjectError(
                f"{key}: reserved for a later version of ChipLeap ({RESERVED[key]}), not read yet"
            )
    unknown = set(data) - set(SECTIONS)
    if unknown:
        raise ProjectError(f"unknown top-level keys: {', '.join(sorted(map(str, unknown)))}")
    schema = data.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise ProjectError(f"schema {schema!r} is not supported; this ChipLeap reads {SCHEMA}")
    root = root.resolve()
    top = _ident(data.get("top"), "top")
    name = data.get("name", top)
    if not is_safe_name(str(name), SPEC_NAME_MAX):
        raise ProjectError(
            f"name {name!r} must be 1 to {SPEC_NAME_MAX} letters, digits, '_', '.' or '-', "
            "starting with a letter or digit"
        )

    fl = _Filelist()
    if data.get("filelist") is not None:
        fl = parse_filelist(_path(root, str(data["filelist"]), "filelist"))
    sources = fl.sources + _sources(root, _str_list(data.get("sources"), "sources"), "sources")
    sources = list(dict.fromkeys(s.resolve() for s in sources))
    if not sources:
        raise ProjectError("sources: give at least one file (or a filelist)")
    for s in sources:
        if s.suffix not in HDL_SUFFIXES:
            raise ProjectError(f"sources: {s.name} is not Verilog or SystemVerilog")
    includes = fl.include_dirs + [
        _path(root, d, "include_dirs", kind="dir")
        for d in _str_list(data.get("include_dirs"), "include_dirs")
    ]
    defines = {**fl.defines, **_parse_defines(data.get("defines"))}

    tech = str(data.get("tech", "ihp-sg13g2"))
    if tech not in TECHNOLOGIES:
        raise ProjectError(f"tech: {tech!r} is not one of {', '.join(TECHNOLOGIES)}")
    golden = None
    if data.get("golden") is not None:
        golden = _path(root, str(data["golden"]), "golden")
        if golden.suffix != ".py":
            raise ProjectError("golden must be a Python file the tests import")
    cons = _mapping(data.get("constraints"), "constraints", ("sdc",))
    sdc = _path(root, str(cons["sdc"]), "constraints.sdc") if cons.get("sdc") else None
    clocks = _parse_clocks(data.get("clocks"))
    resets = _parse_resets(data.get("resets"))
    for r in resets:
        if r.port in {c.port for c in clocks}:
            raise ProjectError(f"resets: {r.port} is also a clock")
    return Project(
        root=root,
        file=(file or root / PROJECT_FILE).resolve(),
        name=str(name),
        top=top,
        sources=tuple(sources),
        include_dirs=tuple(dict.fromkeys(d.resolve() for d in includes)),
        defines=defines,
        parameters=_parse_parameters(data.get("parameters")),
        timescale=_parse_timescale(data.get("timescale")),
        clocks=clocks,
        resets=resets,
        tech=tech,
        tests=_parse_tests(data.get("tests"), root, top),
        golden=golden,
        lint=_parse_lint(data.get("lint"), root),
        sdc=sdc,
        pnr=_parse_pnr(data.get("pnr")),
        description=str(data.get("description", "")),
        filelists=tuple(fl.files),
    )


def find_project(path: str | Path) -> Path | None:
    """The project file ``path`` names, if it is one: a directory holding project.yaml, or
    a YAML file with ``schema: project/v1``."""
    p = Path(path)
    if p.is_dir():
        f = p / PROJECT_FILE
        return f if f.is_file() else None
    if p.is_file() and p.suffix in (".yaml", ".yml"):
        if p.name == PROJECT_FILE:
            return p
        try:
            data = yaml.safe_load(p.read_text())
        except (OSError, yaml.YAMLError):
            return None
        if isinstance(data, dict) and data.get("schema") == SCHEMA:
            return p
    return None


def load_project(path: str | Path) -> Project:
    """Read and check a project: a project file, or a directory that holds one."""
    p = Path(path)
    f = p / PROJECT_FILE if p.is_dir() else p
    try:
        data = yaml.safe_load(f.read_text())
    except yaml.YAMLError as exc:
        raise ProjectError(f"{f}: not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ProjectError(f"{f}: {exc.strerror}") from exc
    try:
        return parse_project(data, f.parent, f)
    except ProjectError as exc:
        raise ProjectError(f"{f}: {exc}") from exc


# Writing a project for existing RTL.

_MODULE_RE = re.compile(r"^\s*(?:macro)?module\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)", re.M)
_CLOCK_RE = re.compile(r"^(i_)?(clk|clock|aclk|clk_sys|sys_clk)(_i|_in)?$", re.I)
_RESET_RE = re.compile(r"^(i_)?(a?rst|a?reset)(_?n)?(_i|_ni|_in)?$", re.I)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def find_sources(directory: Path) -> list[Path]:
    """Verilog and SystemVerilog files under ``directory``, packages first, leaving out
    build directories, hidden directories and test directories."""
    found = []
    for p in sorted(directory.rglob("*")):
        rel = p.relative_to(directory).parts
        if (
            not p.is_file()
            or p.suffix not in (".sv", ".v")
            or any(part.startswith(".") or part in SKIP_DIRS for part in rel[:-1])
            or any(part in ("test", "tests", "tb", "sim") for part in rel[:-1])
        ):
            continue
        found.append(p)
    return sorted(found, key=lambda p: (not p.stem.endswith("_pkg"), str(p)))


def guess_top(sources: list[Path]) -> str:
    """The one module no other source instantiates."""
    texts = {s: _strip_comments(s.read_text(errors="replace")) for s in sources}
    modules = [m for t in texts.values() for m in _MODULE_RE.findall(t)]
    used = set()
    for m in modules:
        inst = re.compile(rf"\b{re.escape(m)}\s*(#\s*\(|[A-Za-z_]\w*\s*(\[[^\]]*\]\s*)?\()")
        for t in texts.values():
            body = _MODULE_RE.sub("", t)
            if inst.search(body):
                used.add(m)
                break
    tops = [m for m in dict.fromkeys(modules) if m not in used]
    if len(tops) != 1:
        found = ", ".join(tops) if tops else "none"
        raise ProjectError(f"cannot tell the top module (candidates: {found}); give --top")
    return str(tops[0])


def module_ports(sources: list[Path], top: str) -> list[str]:
    """The port names of ``top``, read from its header (ANSI or not)."""
    for s in sources:
        text = _strip_comments(s.read_text(errors="replace"))
        m = re.search(rf"\bmodule\s+{re.escape(top)}\b(.*?);", text, re.S)
        if not m:
            continue
        header = re.sub(r"#\s*\((?:[^()]|\([^()]*\))*\)", "", m.group(1))
        inner = header[header.find("(") + 1 : header.rfind(")")] if "(" in header else ""
        names = []
        for part in inner.split(","):
            words = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", " ", part))
            if words:
                names.append(words[-1])
        return names
    return []


def project_yaml(project: Project, notes: Iterable[str] = ()) -> str:
    """The text of a project file, with a comment that says what was guessed."""
    head = [
        "# ChipLeap project: chipleap verify . (docs/projects.md describes every key)",
        *[f"# {n}" for n in notes],
    ]
    body = yaml.safe_dump(project.to_dict(), sort_keys=False, default_flow_style=None)
    return "\n".join(head) + "\n" + body


def init_project(
    directory: str | Path,
    *,
    top: str | None = None,
    sources: Iterable[str] = (),
    filelist: str | None = None,
    clocks: Iterable[tuple[str, float]] = (),
    resets: Iterable[tuple[str, bool]] = (),
    tech: str = "ihp-sg13g2",
    tests_dir: str | None = None,
    name: str | None = None,
    force: bool = False,
) -> tuple[Path, list[str]]:
    """Write ``project.yaml`` for the RTL in ``directory``. Whatever is not given is
    guessed (sources, top, clock, reset, tests) and the guesses are returned as notes,
    which also go into the file as comments."""
    root = Path(directory).resolve()
    dest = root / PROJECT_FILE
    if dest.exists() and not force:
        raise ProjectError(f"{dest} exists; use --force to overwrite")
    notes: list[str] = []
    data: dict[str, Any] = {"schema": SCHEMA}
    srcs = list(sources)
    if filelist:
        data["filelist"] = filelist
        paths = parse_filelist(_path(root, filelist, "filelist")).sources
    elif srcs:
        paths = _sources(root, srcs, "sources")
    else:
        paths = find_sources(root)
        if not paths:
            raise ProjectError(f"no .sv or .v files under {root}; give --source")
        srcs = [p.relative_to(root).as_posix() for p in paths]
        notes.append(f"sources: found {len(srcs)} files")
    if srcs:
        data["sources"] = srcs
    if top is None:
        top = guess_top(paths)
        notes.append(f"top: {top}, the module no other source instantiates")
    data["top"] = top
    data["name"] = name or safe_name(top, "rtl")
    ports = module_ports(paths, top)
    clock_list = list(clocks)
    if not clock_list:
        clock_list = [(p, 10.0) for p in ports if _CLOCK_RE.match(p)][:1]
        if clock_list:
            notes.append(f"clock: {clock_list[0][0]} at 10 ns (100 MHz); set your own period")
    data["clocks"] = [{"port": p, "period_ns": float(t)} for p, t in clock_list]
    reset_list = list(resets)
    if not reset_list:
        reset_list = [(p, _active_low_by_name(p)) for p in ports if _RESET_RE.match(p)][:1]
        if reset_list:
            notes.append(
                f"reset: {reset_list[0][0]}, active {'low' if reset_list[0][1] else 'high'}"
            )
    data["resets"] = [
        {"port": p, "active": "low" if low else "high", "sync": False} for p, low in reset_list
    ]
    data["tech"] = tech
    if tests_dir is None:
        for cand in ("test", "tests", "tb"):
            if (root / cand).is_dir() and list((root / cand).glob("test_*.py")):
                tests_dir = cand
                notes.append(f"tests: cocotb tests in {cand}/")
                break
    if tests_dir is not None:
        data["tests"] = {"dir": tests_dir}
    project = parse_project(data, root, dest)
    dest.write_text(project_yaml(project, notes))
    return dest, notes
