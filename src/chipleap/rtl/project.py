"""The project file for user RTL: ``project.yaml``.

A spec says what a workload needs and a design says how the generated accelerator is
built; a project describes a design someone else wrote. It names the sources, the top,
the clocks and resets, the technology and the cocotb tests, and the verification and
implementation steps read everything else from the RTL itself. Paths are relative to the
project file. Every key is checked, and an error names the key, so a typo fails with a
message that says where it is.

A project file may come from someone else's repository, and its values end up in tool
scripts (Yosys, Tcl), on tool command lines and in results an assistant reads. So:

* Every path must resolve, symlinks followed, to a place inside the project directory,
  or inside a directory the user allowed (``--allow-path``, ``CHIPLEAP_ALLOW_PATHS``);
  nothing in the project file can widen that. Environment variables and ``~`` are not
  expanded. The project file itself, and every `include and $readmem file the RTL names
  in a string, are held to the same rule. Files are read through ``read_inside``, which
  checks the open file again. What a check of the text cannot see (an `include a macro
  builds), ``chipleap.rtl.confine`` stops at the tools.
* Names (top, ports, parameters, defines) are identifiers, parameter values are integers,
  define values a short literal; paths may not hold characters a script language reads
  as syntax. Such values are refused here, not escaped later.

Later milestones add sections (formal properties, macros, a pad ring). Their names are
reserved here and rejected with a message that says so. To add one: a frozen dataclass
and a ``_parse_<name>`` function, a field on ``Project`` with a default, an entry in
``SECTIONS``, and the key removed from ``RESERVED``.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Concatenate, ParamSpec, TypeVar

import yaml

from chipleap.chip.config import (
    CORE_MAX_UM,
    IO_DELAY_MAX_NS,
    IO_LOAD_MAX_PF,
    IO_TRANSITION_MAX_NS,
    MACROS_MAX,
    ORIENTATIONS,
    PAD_PITCH_MAX,
    PAD_PITCH_MIN,
    SIDES,
    SLOTS_MAX,
    SUPPLIES,
    ChipConfig,
    ChipMacro,
    IoBudget,
    Pad,
    port_bit,
)
from chipleap.hw.design import SPEC_NAME_MAX, is_safe_name, safe_name
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.rtl import builddir
from chipleap.verify.tools import untrusted

PROJECT_FILE = "project.yaml"
SCHEMA = "project/v1"
SIMULATORS = ("auto", "icarus", "verilator")
HDL_SUFFIXES = (".sv", ".v", ".svh", ".vh")
# Files an include directory may hold that change what the RTL means.
INCLUDE_SUFFIXES = (*HDL_SUFFIXES, ".h", ".inc", ".vlt", ".mem", ".hex")
# Verilog allows "$" in identifiers; Tcl would substitute it, so ChipLeap does not.
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TIME_RE = re.compile(r"(1|10|100)(s|ms|us|ns|ps|fs)")
# A define's value: a number or a short literal such as 8'hFF, never an option or a script.
DEFINE_VALUE_RE = re.compile(r"[A-Za-z0-9_.'][A-Za-z0-9_.'+-]*")
# Characters a path may not hold: control characters, and what a shell, Yosys or Tcl reads
# as syntax (quotes, backslash, braces, brackets, $, ;, backtick, #) or as a glob.
UNSAFE_PATH_RE = re.compile(r'[\x00-\x1f\x7f"\\{}\[\]$;`#*?]')
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# A LibreLane value given in the project: plain text that Tcl reads as one word.
LIBRELANE_VALUE_RE = re.compile(r"[A-Za-z0-9_./:+ -]*")
ALLOW_ENV = "CHIPLEAP_ALLOW_PATHS"
# Sections later milestones will read. A file written for them fails clearly here.
RESERVED = {
    "macros": "D3",
}
# IHP SG13G2's corners by short name: the liberty's suffix.
CORNERS = {"typ": "typ_1p20V_25C", "slow": "slow_1p08V_125C", "fast": "fast_1p32V_m40C"}
CLOCK_MIXING = ("no_mix", "clock_mix")
POWER_NETLISTS = ("auto", "routed", "synth")
# A hierarchical instance path in a power domain or a simulation scope: u_core/u_alu.
INSTANCE_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*")
# Characters a netlist name in an ECO may not hold. Names such as \u_rx.data_q[3] are fine:
# they reach OpenROAD as data, never as script text, and these keep them one plain word.
ECO_NAME_BAD = set("\"'{};`$")
SKIP_DIRS = {"build", "sim_build", "runs", "obj_dir", "__pycache__", "node_modules"}
FORMAL_MODES = ("prove", "bmc", "cover")
# SymbiYosys engines that work with the Yosys 0.33 toolchain. smtbmc with Boolector is left
# out: its induction step crashes there. abc pdr needs yices to write a counterexample.
FORMAL_ENGINES = ("smtbmc z3", "smtbmc yices", "smtbmc bitwuzla", "abc pdr")
FORMAL_DEPTH_MAX = 1000
EQUIV_CHECKS = ("rtl-synth", "synth-pnr")
EQUIV_ENGINES = ("yosys", "eqy")
GLS_NETLISTS = ("routed", "synth")
GLS_DELAYS = ("min", "typ", "max")
# A LibreLane corner name, as its SDF directories are named.
CORNER_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
# A register name pattern in a CDC waiver: names after flattening, "*" the only wildcard.
WAIVE_RE = re.compile(r"[A-Za-z0-9_.*\[\]/]{1,200}")


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
class DftConfig:
    """Scan insertion with OpenROAD and ATPG with Fault (``chipleap.rtl.dft``)."""

    max_chain_length: int = 100  # flip-flops per chain
    max_chains: int = 0  # 0: as many as the length needs
    clock_mixing: str = "no_mix"
    insert: bool = True  # place and route the scan netlist
    atpg: bool = True
    min_coverage: float | None = None  # percent
    patterns: int = 8  # ATPG patterns replayed through the chains in simulation


@dataclass(frozen=True)
class PowerConfig:
    """Gate-level power from the cocotb tests' activity (``chipleap.rtl.power``)."""

    corners: tuple[str, ...] = ("typ",)
    netlist: str = "auto"  # auto | routed | synth
    scope: str = ""  # the design's instance in the test top, when the tests wrap it
    input_activity: float = 0.1  # for the vectorless estimate


@dataclass(frozen=True)
class IrDropConfig:
    corner: str = "typ"
    max_drop_pct: float | None = None  # pass threshold, percent of the supply


@dataclass(frozen=True)
class PowerDomain:
    name: str
    elements: tuple[str, ...] = ()  # none: the top domain
    area_um: tuple[float, float, float, float] | None = None
    voltage: float | None = None


@dataclass(frozen=True)
class UpfConfig:
    domains: tuple[PowerDomain, ...] = ()

    @property
    def top(self) -> PowerDomain:
        return next(d for d in self.domains if not d.elements)


@dataclass(frozen=True)
class EcoChange:
    """One change to the routed design: ``resize`` an instance to ``cell``, put a
    ``buffer`` of ``cell`` at an instance's pin, or ``repair`` setup or hold."""

    kind: str  # resize | buffer | repair
    target: str  # the instance, INSTANCE/PIN, or setup | hold
    cell: str = ""
    margin_ns: float | None = None

    @property
    def instance(self) -> str:
        return self.target.rpartition("/")[0] if self.kind == "buffer" else self.target

    @property
    def pin(self) -> str:
        return self.target.rpartition("/")[2] if self.kind == "buffer" else ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {self.kind: self.target}
        if self.cell:
            out["cell"] = self.cell
        if self.margin_ns is not None:
            out["margin_ns"] = self.margin_ns
        return out

    @classmethod
    def parse(cls, text: str) -> EcoChange:
        """A change written on a command line: ``resize:INST=CELL``, ``buffer:INST/PIN=CELL``,
        ``repair:setup`` or ``repair:hold@MARGIN_NS``."""
        kind, _, rest = text.partition(":")
        item: dict[str, Any]
        if kind == "repair":
            what, _, margin = rest.partition("@")
            item = {"repair": what}
            if margin:
                try:
                    item["margin_ns"] = float(margin)
                except ValueError:
                    raise ProjectError(f"{text}: the margin is a number of ns") from None
        elif kind in ("resize", "buffer"):
            target, _, cell = rest.partition("=")
            item = {kind: target, "cell": cell}
        else:
            raise ProjectError(
                f"{text}: give resize:INST=CELL, buffer:INST/PIN=CELL or repair:setup|hold"
            )
        return _parse_eco_change(item, text)


@dataclass(frozen=True)
class FormalCheck:
    """One SymbiYosys run: a mode, a depth, the module to prove and an engine."""

    name: str
    mode: str = "prove"
    depth: int = 20
    top: str = ""  # the project's top when empty
    engine: str = "smtbmc z3"


@dataclass(frozen=True)
class FormalConfig:
    sources: tuple[Path, ...] = ()  # property files or a wrapper top, read with -formal
    checks: tuple[FormalCheck, ...] = (FormalCheck("prove"),)
    sby: tuple[Path, ...] = ()  # the project's own .sby files, run as they are


@dataclass(frozen=True)
class EquivConfig:
    checks: tuple[str, ...] = ()  # empty: rtl-synth, and synth-pnr when a routed run exists
    engine: str = "yosys"  # the prover: Yosys's equiv passes or EQY
    depth: int = 40  # cycles from reset of the bounded check of what induction leaves


@dataclass(frozen=True)
class CdcWaiver:
    source: str  # register name patterns, "*" a wildcard
    dest: str
    reason: str


@dataclass(frozen=True)
class CdcConfig:
    async_inputs: tuple[str, ...] = ()  # inputs no clock samples safely
    input_domains: dict[str, str] = field(default_factory=dict)  # input port -> clock port
    reset_synchronized: tuple[str, ...] = ()  # resets synchronized outside the design
    waive: tuple[CdcWaiver, ...] = ()


@dataclass(frozen=True)
class GateSimConfig:
    netlist: str = "routed"  # routed: LibreLane's final netlist with SDF; synth: Yosys's
    corner: str = ""  # the SDF corner; empty: the typical one
    delays: str = "typ"  # which value of the SDF triples


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
    formal: FormalConfig | None = None  # None: the project has no formal section
    equivalence: EquivConfig = field(default_factory=EquivConfig)
    cdc: CdcConfig = field(default_factory=CdcConfig)
    gate_sim: GateSimConfig = field(default_factory=GateSimConfig)
    chip: ChipConfig | None = None  # None: the project has no chip section
    description: str = ""
    filelists: tuple[Path, ...] = ()  # the filelist and every file it includes
    # Directories outside the project the user allowed files from (never the project file).
    allowed: tuple[Path, ...] = ()
    dft: DftConfig | None = None  # None: no scan
    power: PowerConfig = field(default_factory=PowerConfig)
    ir_drop: IrDropConfig = field(default_factory=IrDropConfig)
    upf: UpfConfig | None = None
    eco: tuple[EcoChange, ...] = ()

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    def work_dir(self, step: str | Path, *, empty: bool = False) -> Path:
        """A step's work directory in ``build/``: ``step`` is a path relative to it, or an
        absolute path inside it. ``build`` and every directory down to the step's must
        not be symlinks (``builddir.work_dir``); missing ones are created, symlinks inside
        are removed, and with ``empty`` everything inside is."""
        path = Path(os.path.normpath(self.build_dir / step))
        if path == self.build_dir or not path.is_relative_to(self.build_dir):
            raise builddir.BuildDirError(f"{step} is not a directory in {self.build_dir}")
        parts = path.relative_to(self.build_dir).parts
        return builddir.work_dir(self.root, "build", *parts, empty=empty)

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

    def inside(self, path: Path) -> bool:
        """Whether ``path`` resolves into the project or a directory the user allowed."""
        return _within(path.resolve(), (self.root, *self.allowed))

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.root, *self.allowed)

    def _files(self, directory: Path, suffixes: tuple[str, ...] = ()) -> list[tuple[Path, Path]]:
        return [
            (logical, real)
            for logical, real in _walk(directory, self.roots)
            if (not suffixes or real.suffix in suffixes) and not UNSAFE_PATH_RE.search(str(real))
        ]

    def files_in(self, directory: Path, suffixes: tuple[str, ...] = ()) -> list[Path]:
        """The files under ``directory`` that a run may read, copy or hash, resolved:
        reached without leaving the project (a symlink out is never followed, so nothing
        outside is even listed), outside build directories, with safe names."""
        return [real for _, real in self._files(directory, suffixes)]

    def read(self, path: Path) -> bytes:
        """The content of a project file, read only if the file opened is inside the
        project (``read_inside``)."""
        return read_inside(path, self.roots)

    def stage(self, path: Path, dest: Path) -> None:
        """Copy a file, or a directory's ``files_in``, to ``dest``. Each file is resolved
        and checked when it is copied, and checked again once open (``read_inside``)."""
        if path.is_dir():
            for logical, real in self._files(path):
                self.stage(real, dest / logical.relative_to(path))
            return
        data = self.read(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def input_files(self) -> list[Path]:
        """Every file whose content can change a result: the project file, the sources,
        include files, waivers, constraints, the tests and the golden model."""
        files: list[Path] = [self.file, *self.filelists, *self.sources, *self.tests.sources]
        for d in self.include_dirs:
            files += self.files_in(d, INCLUDE_SUFFIXES)
        files += [*self.lint.waivers, *([self.sdc] if self.sdc else [])]
        if self.formal is not None:
            files += [*self.formal.sources, *self.formal.sby]
        if self.chip is not None:
            for m in self.chip.macros:
                files += self.files_in(m.package)
        if self.tests.dir is not None:
            files += self.files_in(self.tests.dir, (".py",))
        if self.golden is not None:
            files += [p for p in sorted(self.golden.parent.glob("*.py")) if self.inside(p)]
        seen: dict[Path, None] = {}
        for f in files:
            seen.setdefault(f.resolve(), None)
        return list(seen)

    def inputs_digest(self) -> str:
        """SHA-256 over every input file's project-relative name and content, so a run log
        can tell a changed design from a changed tool. A file that is gone, or that now
        leads outside the project, counts as missing and is not read."""
        h = hashlib.sha256()
        for f in sorted(self.input_files(), key=lambda p: self.rel(p)):
            h.update(self.rel(f).encode() + b"\0")
            try:
                h.update(hashlib.sha256(self.read(f)).digest())
            except (ProjectError, OSError):
                h.update(b"missing")
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
        if self.formal is not None:
            f: dict[str, Any] = {
                "checks": [
                    {
                        "name": c.name,
                        "mode": c.mode,
                        "depth": c.depth,
                        **({"top": c.top} if c.top else {}),
                        "engine": c.engine,
                    }
                    for c in self.formal.checks
                ]
            }
            if self.formal.sources:
                f["sources"] = [self.rel(x) for x in self.formal.sources]
            if self.formal.sby:
                f["sby"] = [self.rel(x) for x in self.formal.sby]
            out["formal"] = f
        if self.equivalence != EquivConfig():
            e = self.equivalence
            out["equivalence"] = {"checks": list(e.checks), "engine": e.engine, "depth": e.depth}
        if self.cdc != CdcConfig():
            out["cdc"] = {
                "async_inputs": list(self.cdc.async_inputs),
                "input_domains": dict(self.cdc.input_domains),
                "reset_synchronized": list(self.cdc.reset_synchronized),
                "waive": [
                    {"from": w.source, "to": w.dest, "reason": w.reason} for w in self.cdc.waive
                ],
            }
        if self.gate_sim != GateSimConfig():
            g = self.gate_sim
            out["gate_sim"] = {"netlist": g.netlist, "corner": g.corner, "delays": g.delays}
        if self.dft is not None:
            out["dft"] = {k: v for k, v in asdict(self.dft).items() if v is not None}
        if self.power != PowerConfig():
            out["power"] = {**asdict(self.power), "corners": list(self.power.corners)}
        if self.ir_drop != IrDropConfig():
            out["ir_drop"] = {k: v for k, v in asdict(self.ir_drop).items() if v is not None}
        if self.upf is not None:
            domains = []
            for d in self.upf.domains:
                dom: dict[str, Any] = {"name": d.name}
                if d.elements:
                    dom["elements"] = list(d.elements)
                if d.area_um is not None:
                    dom["area_um"] = list(d.area_um)
                if d.voltage is not None:
                    dom["voltage"] = d.voltage
                domains.append(dom)
            out["upf"] = {"domains": domains}
        if self.eco:
            out["eco"] = [c.to_dict() for c in self.eco]
        if self.chip is not None:
            out["chip"] = _chip_dict(self.chip, self.rel)
        return out


# Parsing. Each helper takes the key path it reports in errors.


_P = ParamSpec("_P")
_R = TypeVar("_R")


def runs_tools(
    fn: Callable[Concatenate[Project, _P], _R],
) -> Callable[Concatenate[Project, _P], _R]:
    """For a step on the project in its first argument: while it runs, no tool is found
    in the project's directory or a directory the user allowed files from, and no program
    it starts gets a PATH entry into one (``chipleap.verify.tools.untrusted``). A
    repository can ship executables named like the tools, and an absolute PATH entry can
    lead into it as a relative one does."""

    @functools.wraps(fn)
    def step(project: Project, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with untrusted(*project.roots):
            return fn(project, *args, **kwargs)

    return step


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


def _within(path: Path, roots: Iterable[Path]) -> bool:
    """Whether ``path`` (resolved) is one of ``roots`` or below one, compared by path
    components, so /proj-evil is not inside /proj."""
    return any(path.is_relative_to(r) for r in roots)


def read_inside(path: Path, roots: Iterable[Path]) -> bytes:
    """The content of ``path`` if the file it opens is inside ``roots`` (resolved). The
    path is resolved and checked first, so nothing outside is opened; it is then opened
    without following a symlink in its last part, and the open file's own path is checked
    again, so a directory swapped for a symlink after the first check is caught, not read."""
    roots = tuple(roots)
    real = path.resolve()
    if not _within(real, roots):
        raise ProjectError(f"{path} is outside the project directory")
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ProjectError(f"{_shown(real, roots)}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        opened = _opened(fd)
        if opened is not None and not _within(opened, roots):
            raise ProjectError(f"{path} is outside the project directory")
        return fh.read()


def _opened(fd: int) -> Path | None:
    """Where an open file is, as the kernel sees it (Linux); None where it cannot say."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _walk(directory: Path, roots: tuple[Path, ...]) -> list[tuple[Path, Path]]:
    """(path under ``directory``, resolved path) of every file reachable from it without
    leaving ``roots``, sorted. A symlinked directory is entered only if it resolves inside
    them, each directory once; build directories never. Nothing outside is listed."""
    out: list[tuple[Path, Path]] = []
    start = directory.resolve()
    if not _within(start, roots):
        return out
    seen: set[Path] = set()
    stack = [(directory, start)]
    while stack:
        logical, real = stack.pop()
        if real in seen:
            continue
        seen.add(real)
        try:
            entries = sorted(os.scandir(real), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            target = Path(e.path).resolve()
            if not _within(target, roots):
                continue
            if target.is_dir():
                if e.name not in SKIP_DIRS:
                    stack.append((logical / e.name, target))
            elif target.is_file():
                out.append((logical / e.name, target))
    return sorted(out)


def _glob_regex(parts: tuple[str, ...]) -> re.Pattern[str]:
    """A regular expression for a glob over path components: ``*``, ``?``, ``[...]`` and
    ``**`` (any number of directories). Like glob, a wildcard does not match a leading
    dot."""
    out = []
    for k, part in enumerate(parts):
        last = k == len(parts) - 1
        if part == "**":
            out.append("(?:[^/]+/)*[^/]+" if last else "(?:[^/]+/)*")
            continue
        rx = "" if part.startswith(".") else r"(?!\.)"
        i = 0
        while i < len(part):
            c = part[i]
            if c == "*":
                rx += "[^/]*"
            elif c == "?":
                rx += "[^/]"
            elif c == "[" and part.find("]", i + 1) > i + 1:
                j = part.find("]", i + 1)
                body = part[i + 1 : j]
                neg = body.startswith("!")
                # Only "-" keeps a meaning (a range); anything else is the character itself.
                chars = "".join(c if c == "-" else re.escape(c) for c in body[int(neg) :])
                rx += "[" + ("^" if neg else "") + chars + "]"
                i = j
            else:
                rx += re.escape(c)
            i += 1
        out.append(rx if last else rx + "/")
    return re.compile("".join(out))


def _lexer(triple: bool, escaped: bool) -> re.Pattern[str]:
    """Verilog as a preprocessor reads it: string literals and comments whole, so a "/*"
    in a string cannot hide the `include after it; optionally SystemVerilog 2023's
    triple-quoted strings and escaped identifiers (``\\name``, which may hold a quote)."""
    return re.compile(
        "|".join(
            [
                *([r'"""(?:[^\\]|\\.)*?(?:"""|\Z)'] if triple else []),
                r'"(?:[^"\\\n]|\\.)*"?',  # to the end of the line when not closed
                r"//[^\n]*",
                r"/\*.*?(?:\*/|\Z)",
                *([r"\\[^\s`]*"] if escaped else []),
                r"`include\b",
                r"\$readmem[hb]\b",
            ]
        ),
        re.S,
    )


# Tools differ on those two constructs, and what is a comment in one reading can be code
# in another. So the text is read all four ways, and an `include counts unless every
# reading puts it in a comment; one inside a string counts too.
_LEXERS = [_lexer(t, e) for t in (False, True) for e in (False, True)]
_GAP_RE = re.compile(r"(?:\s|//[^\n]*|/\*.*?\*/)*", re.S)
_INCLUDE_ARG_RE = re.compile(r'"([^"\n]*)"|<([^>\n]*)>')
_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def _skip(text: str, pos: int) -> int:
    """The position after the white space and comments at ``pos``."""
    gap = _GAP_RE.match(text, pos)
    return gap.end() if gap else pos


def references(text: str) -> list[tuple[str, str | None, int]]:
    """The files a Verilog text makes a tool read, as (kind, name, line): ``include`` for
    every `include, its name None unless it is a plain quoted name, and ``readmem`` for a
    $readmemh or $readmemb whose file is a string literal."""
    found: dict[int, str] = {}
    for lexer in _LEXERS:
        for m in lexer.finditer(text):
            tok = m.group()
            if tok.startswith('"'):
                for inc in re.finditer(r"`include\b", tok):
                    found.setdefault(m.start() + inc.start(), "include")
            elif tok == "`include":
                found.setdefault(m.start(), "include")
            elif tok.startswith("$readmem"):
                found.setdefault(m.start(), "readmem")
    out: list[tuple[str, str | None, int]] = []
    for pos, kind in sorted(found.items()):
        line = text.count("\n", 0, pos) + 1
        after = _skip(text, pos + len("`include" if kind == "include" else "$readmemh"))
        if kind == "include":
            arg = _INCLUDE_ARG_RE.match(text, after)
            name = None
            if arg is not None:
                name = arg.group(1) if arg.group(1) is not None else arg.group(2)
            out.append(("include", name, line))
        elif text.startswith("(", after):
            lit = _STRING_RE.match(text, _skip(text, after + 1))
            if lit is not None:
                out.append(("readmem", lit.group(1), line))
    return out


def allowed_roots(extra: Iterable[str | Path] = ()) -> tuple[Path, ...]:
    """Directories outside a project its files may come from: the ones given (a command
    line option) and the ones in ``CHIPLEAP_ALLOW_PATHS``. Both are the user's own choice;
    a project file cannot add to them."""
    env = [d for d in os.environ.get(ALLOW_ENV, "").split(os.pathsep) if d]
    return tuple(dict.fromkeys(Path(d).expanduser().resolve() for d in [*extra, *env]))


class _Paths:
    """Resolves the paths a project names, and refuses any that leave the project."""

    def __init__(self, root: Path, allowed: Iterable[Path] = ()) -> None:
        self.root = root.resolve()
        self.roots = (self.root, *(Path(a).resolve() for a in allowed))

    def _text(self, text: str, where: str) -> None:
        if CONTROL_RE.search(text):
            raise ProjectError(f"{where}: {text!r} holds a control character")
        if "$" in text or text.startswith("~"):
            raise ProjectError(
                f"{where}: {text!r}: environment variables and ~ are not expanded; give a "
                "path relative to the project"
            )

    def check(self, path: Path, text: str, where: str) -> Path:
        """``path`` resolved, if it is inside the project (or an allowed directory) and
        its name is safe to put in a script."""
        real = path.resolve()
        if not _within(real, self.roots):
            raise ProjectError(
                f"{where}: {text} is outside the project directory; allow its directory "
                f"with --allow-path DIR or {ALLOW_ENV}"
            )
        bad = UNSAFE_PATH_RE.search(str(real))
        if bad:
            raise ProjectError(
                f"{where}: {text}: {bad.group()!r} is not allowed in a path, because tool "
                "scripts would read it as syntax"
            )
        for part in real.relative_to(next(r for r in self.roots if _within(real, [r]))).parts:
            if part.startswith(("-", "+")):
                raise ProjectError(
                    f"{where}: {text}: a name starting with - or + reads as an option"
                )
        return real

    def path(self, base: Path, text: str, where: str, kind: str = "file") -> Path:
        self._text(text, where)
        p = Path(text)
        # Confinement first, so an error never says whether a file outside the project exists.
        real = self.check(p if p.is_absolute() else base / p, text, where)
        if not (real.is_dir() if kind == "dir" else real.is_file()):
            raise ProjectError(
                f"{where}: {text} is not a {'directory' if kind == 'dir' else 'file'}"
            )
        return real

    def glob(self, base: Path, pattern: str, where: str) -> list[Path]:
        """The files a glob pattern matches, sorted. The pattern may not be absolute or
        hold "..", and the walk never leaves the project, so nothing outside is listed."""
        parts = PurePosixPath(pattern).parts
        if PurePosixPath(pattern).is_absolute() or ".." in parts:
            raise ProjectError(
                f"{where}: {pattern}: a pattern stays inside the project (no absolute path, no ..)"
            )
        fixed = next(k for k, part in enumerate(parts) if any(c in part for c in "*?["))
        top = base.joinpath(*parts[:fixed])
        if not _within(top.resolve(), self.roots):
            raise ProjectError(f"{where}: {pattern} is outside the project directory")
        rx = _glob_regex(parts[fixed:])
        found = [
            self.check(real, pattern, where)
            for logical, real in _walk(top, self.roots)
            if rx.fullmatch(logical.relative_to(top).as_posix())
        ]
        return sorted(dict.fromkeys(found))

    def files(self, base: Path, items: list[str], where: str) -> list[Path]:
        """Files, with glob patterns expanded in sorted order; every match is checked."""
        out: list[Path] = []
        for item in items:
            self._text(item, where)
            if any(ch in item for ch in "*?["):
                matches = self.glob(base, item, where)
                if not matches:
                    raise ProjectError(f"{where}: {item} matches no files")
                out += matches
            else:
                out.append(self.path(base, item, where))
        return out

    def check_references(self, files: Iterable[Path], include_dirs: list[Path]) -> None:
        """Follow every `include from ``files`` (sources, testbench sources and Verilator
        waiver files, which Verilator preprocesses too) through every place a tool could
        find it, and refuse one that names an absolute path, holds "..", is built by a
        macro, or resolves (through a symlink) outside the project; and a $readmemh or
        $readmemb file, named by a string, that resolves outside. The tools would read
        whatever these name, and their messages could quote it. A file name a macro or a
        parameter computes is beyond a check of the text: ``chipleap.rtl.confine`` is
        what stops the tools reading it."""
        todo = list(files)
        seen: set[Path] = set()
        while todo:
            f = todo.pop()
            if f in seen:
                continue
            seen.add(f)
            text = read_inside(f, self.roots).decode(errors="replace")
            for kind, name, line in references(text):
                here = f"{_shown(f, self.roots)}:{line}"
                if kind == "readmem":
                    self._check_readmem(f, name or "", here)
                    continue
                if name is None:
                    raise ProjectError(
                        f"{here}: an `include whose file is not a name in quotes (a macro) "
                        "cannot be checked; name the file in quotes"
                    )
                ref = PurePosixPath(name)
                if UNSAFE_PATH_RE.search(name) or name.startswith("~"):
                    raise ProjectError(f"{here}: `include {name!r} is not a plain file name")
                if ref.is_absolute() or ".." in ref.parts:
                    raise ProjectError(
                        f'{here}: `include "{name}": absolute paths and .. are not allowed in '
                        "an include; add its directory to include_dirs"
                    )
                # Tools search the including file's directory, the include directories and
                # their working directory (the project) in different orders, so every
                # place counts, and every file found is followed in turn.
                for d in (f.parent, *include_dirs, self.root):
                    c = (d / name).resolve()
                    if not _within(c, self.roots):
                        raise ProjectError(
                            f'{here}: `include "{name}" leads outside the project directory'
                        )
                    if c.is_file():
                        todo.append(c)

    def _check_readmem(self, f: Path, name: str, here: str) -> None:
        """A memory file a source names: Yosys looks for it from its working directory
        (the project) and then beside the source."""
        if UNSAFE_PATH_RE.search(name) or name.startswith("~"):
            raise ProjectError(f"{here}: $readmem file {name!r} is not a plain file name")
        p = PurePosixPath(name)
        places = [Path(name)] if p.is_absolute() else [self.root / name, f.parent / name]
        if not all(_within(c.resolve(), self.roots) for c in places):
            raise ProjectError(f'{here}: $readmem file "{name}" is outside the project directory')


def _shown(path: Path, roots: tuple[Path, ...]) -> str:
    """``path`` relative to the root it is in, for messages."""
    for r in roots:
        if _within(path, [r]):
            return path.relative_to(r).as_posix()
    return str(path)


@dataclass
class _Filelist:
    sources: list[Path] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    files: list[Path] = field(default_factory=list)


def parse_filelist(
    path: Path,
    where: str = "filelist",
    *,
    roots: Iterable[Path] | None = None,
    _depth: int = 0,
) -> _Filelist:
    """A simulator filelist: source files, ``-f``/``-F`` (nested, relative to the file
    that names them), ``+incdir+``, ``-I``, ``+define+`` and ``-D``. Comments start with
    ``//`` or ``#``. Library options (``-v``, ``-y``) are not supported. Every file and
    directory must be inside ``roots`` (the filelist's own directory by default)."""
    given = list(roots) if roots else [path.parent]
    paths = _Paths(given[0], given[1:])
    path = paths.check(path, path.name, where)
    if _depth > 8:
        raise ProjectError(f"{where}: filelists nest more than 8 deep")
    out = _Filelist(files=[path])
    base = path.parent
    words: list[str] = []
    for raw in read_inside(path, paths.roots).decode(errors="replace").splitlines():
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
            sub = parse_filelist(
                paths.path(base, arg, here), where, roots=paths.roots, _depth=_depth + 1
            )
            out.sources += sub.sources
            out.include_dirs += sub.include_dirs
            out.defines.update(sub.defines)
            out.files += sub.files
        elif w.startswith("+incdir+"):
            out.include_dirs += [
                paths.path(base, d, here, kind="dir") for d in w[len("+incdir+") :].split("+") if d
            ]
        elif w.startswith("-I"):
            arg = w[2:] or next(it, "")
            out.include_dirs.append(paths.path(base, arg, here, kind="dir"))
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
            out.sources += paths.files(base, [w], here)
    out.defines = _parse_defines(out.defines, f"{where} ({path.name}) defines")
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


def active_low_by_name(port: str) -> bool:
    """Whether a reset's name says it is active low: rst_n, rst_ni, RST_N, resetn."""
    return bool(re.search(r"(?i)(_n|_ni|_b|rstn|resetn)$", port))


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
        active = r.get("active", "low" if active_low_by_name(port) else "high")
        if active not in ("low", "high"):
            raise ProjectError(f"{where}.active must be low or high")
        sync = r.get("sync", False)
        if not isinstance(sync, bool):
            raise ProjectError(f"{where}.sync must be true or false")
        out.append(Reset(port, active == "low", sync))
    return tuple(out)


def _parse_defines(value: Any, where: str = "defines") -> dict[str, str | None]:
    if value is None:
        return {}
    if isinstance(value, list):  # [A, B=1] as on a command line
        value = {
            str(v).partition("=")[0]: (str(v).partition("=")[2] if "=" in str(v) else None)
            for v in value
        }
    if not isinstance(value, dict):
        raise ProjectError(f"{where} must be a mapping of name to value (null for none)")
    out: dict[str, str | None] = {}
    for k, v in value.items():
        _ident(k, f"{where}: a define name")
        if v is not None and (isinstance(v, bool) or not isinstance(v, (str, int, float))):
            raise ProjectError(f"{where}.{k} must be a string, a number or null")
        if v is not None and not DEFINE_VALUE_RE.fullmatch(str(v)):
            raise ProjectError(
                f"{where}.{k}: {str(v)!r} is not a plain value (letters, digits and _ . ' + -, "
                "not starting with - or +)"
            )
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


def _parse_tests(value: Any, paths: _Paths, top: str) -> Tests:
    t = _mapping(value, "tests", ("dir", "modules", "simulator", "seed", "toplevel", "sources"))
    if not t:
        return Tests(toplevel=top)
    if "dir" not in t:
        raise ProjectError("tests.dir is required: the directory with the cocotb tests")
    d = paths.path(paths.root, str(t["dir"]), "tests.dir", kind="dir")
    if "modules" in t:
        modules = _str_list(t["modules"], "tests.modules")
        for m in modules:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", m):
                raise ProjectError(f"tests.modules: {m!r} is not a Python module name")
            module = paths.check(d / f"{m.replace('.', '/')}.py", m, "tests.modules")
            if not module.is_file():
                raise ProjectError(f"tests.modules: {m}.py not found in {t['dir']}")
    else:
        found = (p for p in d.glob("test_*.py") if _within(p.resolve(), paths.roots))
        modules = sorted(p.stem for p in found)
        if not modules:
            raise ProjectError(f"tests.dir: no test_*.py files in {t['dir']}")
    sim = t.get("simulator", "auto")
    if sim not in SIMULATORS:
        raise ProjectError(f"tests.simulator must be one of {', '.join(SIMULATORS)}")
    seed = t.get("seed", 1)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ProjectError("tests.seed must be a non-negative integer")
    toplevel = _ident(t.get("toplevel", top), "tests.toplevel")
    srcs = paths.files(paths.root, _str_list(t.get("sources"), "tests.sources"), "tests.sources")
    return Tests(d, tuple(modules), str(sim), seed, toplevel, tuple(srcs))


def _parse_lint(value: Any, paths: _Paths) -> LintConfig:
    lt = _mapping(value, "lint", ("waivers", "slang"))
    waivers = tuple(
        paths.path(paths.root, w, "lint.waivers")
        for w in _str_list(lt.get("waivers"), "lint.waivers")
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
    for k, v in extra.items():
        items = v if isinstance(v, list) else [v]
        for item in items:
            ok = isinstance(item, (bool, int, float)) or (
                isinstance(item, str) and LIBRELANE_VALUE_RE.fullmatch(item)
            )
            if not ok:
                raise ProjectError(
                    f"pnr.librelane.{k}: values are numbers, booleans or plain text (letters, "
                    "digits, spaces and _ . / : + -), or lists of them"
                )
    return PnrConfig(util, density, die_um, dict(extra))


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectError(f"{where} must be true or false")
    return value


def _count(value: Any, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        kind = "a positive" if minimum > 0 else "a non-negative"
        raise ProjectError(f"{where} must be {kind} integer")
    return value


def _one_of(value: Any, where: str, choices: Iterable[str]) -> str:
    choices = tuple(choices)
    if value not in choices:
        raise ProjectError(f"{where} must be one of {', '.join(choices)}")
    return str(value)


def _parse_dft(value: Any) -> DftConfig:
    d = _mapping(
        value,
        "dft",
        (
            "max_chain_length",
            "max_chains",
            "clock_mixing",
            "insert",
            "atpg",
            "min_coverage",
            "patterns",
        ),
    )
    cov = d.get("min_coverage")
    return DftConfig(
        max_chain_length=_count(d.get("max_chain_length", 100), "dft.max_chain_length", 1),
        max_chains=_count(d.get("max_chains", 0), "dft.max_chains", 0),
        clock_mixing=_one_of(d.get("clock_mixing", "no_mix"), "dft.clock_mixing", CLOCK_MIXING),
        insert=_bool(d.get("insert", True), "dft.insert"),
        atpg=_bool(d.get("atpg", True), "dft.atpg"),
        min_coverage=None if cov is None else _positive(cov, "dft.min_coverage", 100),
        patterns=_count(d.get("patterns", 8), "dft.patterns", 0),
    )


def _corner(value: Any, where: str) -> str:
    return _one_of(value, where, CORNERS)


def _parse_power(value: Any) -> PowerConfig:
    p = _mapping(value, "power", ("corners", "netlist", "scope", "input_activity"))
    corners = p.get("corners", ["typ"])
    if isinstance(corners, str):
        corners = [corners]
    if not isinstance(corners, list):
        raise ProjectError("power.corners must be a list of typ, slow, fast")
    if not corners:
        raise ProjectError("power.corners needs at least one corner")
    for c in corners:
        if c not in CORNERS:
            raise ProjectError(f"power.corners: {c} is not one of {', '.join(CORNERS)}")
    scope = p.get("scope", "")
    if scope and (not isinstance(scope, str) or not INSTANCE_PATH_RE.fullmatch(scope)):
        raise ProjectError("power.scope must be instance names separated by /, such as tb/u_dut")
    return PowerConfig(
        corners=tuple(dict.fromkeys(str(c) for c in corners)),
        netlist=_one_of(p.get("netlist", "auto"), "power.netlist", POWER_NETLISTS),
        scope=str(scope),
        input_activity=_positive(p.get("input_activity", 0.1), "power.input_activity", 2),
    )


def _parse_ir_drop(value: Any) -> IrDropConfig:
    r = _mapping(value, "ir_drop", ("corner", "max_drop_pct"))
    limit = r.get("max_drop_pct")
    return IrDropConfig(
        corner=_corner(r.get("corner", "typ"), "ir_drop.corner"),
        max_drop_pct=None if limit is None else _positive(limit, "ir_drop.max_drop_pct", 100),
    )


def _parse_domain(value: Any, where: str) -> PowerDomain:
    d = _mapping(value, where, ("name", "elements", "area_um", "voltage"))
    name = _ident(d.get("name"), f"{where}.name")
    elements = _str_list(d.get("elements"), f"{where}.elements")
    for e in elements:
        if not INSTANCE_PATH_RE.fullmatch(e):
            raise ProjectError(
                f"{where}.elements: {e!r} is not an instance path (names separated by /)"
            )
    area = d.get("area_um")
    area_um = None
    if area is not None:
        if not elements:
            raise ProjectError(f"{where}: the top domain covers the core; it takes no area_um")
        ok = (
            isinstance(area, list)
            and len(area) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in area)
            and min(area) >= 0
            and area[2] > area[0]
            and area[3] > area[1]
        )
        if not ok:
            raise ProjectError(
                f"{where}.area_um must be [x0, y0, x1, y1] in um, with x1 > x0 and y1 > y0"
            )
        area_um = (float(area[0]), float(area[1]), float(area[2]), float(area[3]))
    voltage = d.get("voltage")
    return PowerDomain(
        name=name,
        elements=tuple(elements),
        area_um=area_um,
        voltage=None if voltage is None else _positive(voltage, f"{where}.voltage", 5),
    )


def _parse_upf(value: Any) -> UpfConfig:
    u = _mapping(value, "upf", ("domains",))
    raw = u.get("domains")
    if raw is None or (isinstance(raw, list) and not raw):
        raise ProjectError("upf.domains needs at least one domain")
    if not isinstance(raw, list):
        raise ProjectError("upf.domains must be a list of {name, elements, area_um, voltage}")
    domains = [_parse_domain(d, f"upf.domains[{i}]") for i, d in enumerate(raw)]
    if sum(not d.elements for d in domains) != 1:
        raise ProjectError(
            "upf.domains: exactly one domain has no elements: the top, which holds every "
            "instance the others do not"
        )
    seen: set[str] = set()
    owner: dict[str, str] = {}
    for d in domains:
        if d.name in seen:
            raise ProjectError(f"upf.domains: {d.name} is listed twice")
        seen.add(d.name)
        for e in d.elements:
            if e in owner:
                raise ProjectError(f"upf.domains: {e} is in two domains")
            owner[e] = d.name
    return UpfConfig(tuple(domains))


def _eco_name(value: Any, where: str) -> str:
    """A netlist name: printable, no white space, nothing a script could read as syntax."""
    ok = (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and all("!" <= c <= "~" for c in value)
        and not ECO_NAME_BAD.intersection(value)
    )
    if not ok:
        raise ProjectError(f"{where}: {value!r} is not a netlist name")
    return str(value)


def _parse_eco_change(value: Any, where: str) -> EcoChange:
    if not isinstance(value, dict):
        raise ProjectError(f"{where} must be a mapping such as {{resize: INST, cell: CELL}}")
    kinds = [k for k in ("resize", "buffer", "repair") if k in value]
    if len(kinds) != 1:
        raise ProjectError(f"{where} is one of resize, buffer or repair")
    kind = kinds[0]
    if kind == "repair":
        c = _mapping(value, where, ("repair", "margin_ns"))
        what = _one_of(c["repair"], f"{where}.repair", ("setup", "hold"))
        margin = c.get("margin_ns")
        if margin is not None and (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not 0 <= margin <= 10
        ):
            raise ProjectError(f"{where}.margin_ns must be between 0 and 10 ns")
        return EcoChange("repair", what, "", None if margin is None else float(margin))
    c = _mapping(value, where, (kind, "cell"))
    if not c.get("cell"):
        raise ProjectError(f"{where} needs a cell: the library cell to use")
    cell = _ident(c["cell"], f"{where}.cell")
    target = _eco_name(c[kind], f"{where}.{kind}")
    if kind == "buffer":
        inst, _, pin = target.rpartition("/")
        if not inst or not IDENT_RE.fullmatch(pin):
            raise ProjectError(f"{where}.buffer must be INSTANCE/PIN, such as _123_/A")
    return EcoChange(kind, target, cell)


def _parse_eco(value: Any) -> tuple[EcoChange, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProjectError("eco must be a list of changes ({resize: ...}, {buffer: ...}, ...)")
    return tuple(_parse_eco_change(c, f"eco[{i}]") for i, c in enumerate(value))


def _depth(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectError(f"{where} must be a positive integer")
    if value > FORMAL_DEPTH_MAX:
        raise ProjectError(f"{where} must be at most {FORMAL_DEPTH_MAX}")
    return value


def _choice(value: Any, choices: tuple[str, ...], where: str) -> str:
    if value not in choices:
        raise ProjectError(f"{where} must be one of {', '.join(choices)}, not {value!r}")
    return str(value)


def _parse_formal(value: Any, paths: _Paths) -> FormalConfig | None:
    if value is None:
        return None
    f = _mapping(value, "formal", ("sources", "checks", "sby"))
    sources = paths.files(
        paths.root, _str_list(f.get("sources"), "formal.sources"), "formal.sources"
    )
    for s in sources:
        if s.suffix not in HDL_SUFFIXES:
            raise ProjectError(f"formal.sources: {s.name} is not Verilog or SystemVerilog")
    sby = [paths.path(paths.root, x, "formal.sby") for x in _str_list(f.get("sby"), "formal.sby")]
    for x in sby:
        if x.suffix != ".sby":
            raise ProjectError(f"formal.sby: {_shown(x, paths.roots)} is not a .sby file")
    raw = f.get("checks")
    if raw is None:
        return FormalConfig(tuple(sources), sby=tuple(sby))
    if not isinstance(raw, list) or not raw:
        raise ProjectError("formal.checks must be a list of {name, mode, depth, top, engine}")
    checks: list[FormalCheck] = []
    for i, c in enumerate(raw):
        where = f"formal.checks[{i}]"
        c = _mapping(c, where, ("name", "mode", "depth", "top", "engine"))
        name = c.get("name")
        if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
            raise ProjectError(f"{where}.name must be an identifier, not {name!r}")
        if name in {x.name for x in checks}:
            raise ProjectError(f"formal.checks: {name} is listed twice")
        mode = _choice(c.get("mode", "prove"), FORMAL_MODES, f"{where}.mode")
        engine = _choice(c.get("engine", "smtbmc z3"), FORMAL_ENGINES, f"{where}.engine")
        if engine == "abc pdr" and mode != "prove":
            raise ProjectError(f"{where}: abc pdr only proves; use smtbmc for {mode}")
        top = _ident(c["top"], f"{where}.top") if "top" in c else ""
        checks.append(
            FormalCheck(name, mode, _depth(c.get("depth", 20), f"{where}.depth"), top, engine)
        )
    return FormalConfig(tuple(sources), tuple(checks), tuple(sby))


def _parse_equivalence(value: Any) -> EquivConfig:
    e = _mapping(value, "equivalence", ("checks", "engine", "depth"))
    checks = _str_list(e.get("checks"), "equivalence.checks")
    for c in checks:
        if c not in EQUIV_CHECKS:
            raise ProjectError(f"equivalence.checks: {c!r} is not one of {', '.join(EQUIV_CHECKS)}")
    engine = _choice(e.get("engine", "yosys"), EQUIV_ENGINES, "equivalence.engine")
    return EquivConfig(
        tuple(dict.fromkeys(checks)), engine, _depth(e.get("depth", 40), "equivalence.depth")
    )


def _parse_cdc(value: Any, clocks: tuple[Clock, ...], resets: tuple[Reset, ...]) -> CdcConfig:
    c = _mapping(value, "cdc", ("async_inputs", "input_domains", "reset_synchronized", "waive"))
    clock_ports = {k.port for k in clocks}
    async_inputs = []
    for i, port in enumerate(_str_list(c.get("async_inputs"), "cdc.async_inputs")):
        _ident(port, f"cdc.async_inputs[{i}]")
        if port in clock_ports:
            raise ProjectError(f"cdc.async_inputs: {port} is a clock")
        async_inputs.append(port)
    domains = c.get("input_domains") or {}
    if not isinstance(domains, dict):
        raise ProjectError("cdc.input_domains must map input ports to clock ports")
    for port, clock in domains.items():
        _ident(port, "cdc.input_domains: a port")
        if not isinstance(clock, str) or clock not in clock_ports:
            raise ProjectError(f"cdc.input_domains.{port}: {clock} is not one of the clocks")
    synced = _str_list(c.get("reset_synchronized"), "cdc.reset_synchronized")
    for r in synced:
        if r not in {x.port for x in resets}:
            raise ProjectError(f"cdc.reset_synchronized: {r} is not one of the resets")
    waive = []
    raw = c.get("waive") or []
    if not isinstance(raw, list):
        raise ProjectError("cdc.waive must be a list of {from, to, reason}")
    for i, w in enumerate(raw):
        where = f"cdc.waive[{i}]"
        w = _mapping(w, where, ("from", "to", "reason"))
        for key in ("from", "to"):
            pat = w.get(key, "*")
            if not isinstance(pat, str) or not WAIVE_RE.fullmatch(pat):
                raise ProjectError(
                    f"{where}.{key} must be a register name pattern (letters, digits, _ . / "
                    f"[ ] and * for any text), not {pat!r}"
                )
        reason = w.get("reason")
        if not isinstance(reason, str) or not reason.strip() or CONTROL_RE.search(reason):
            raise ProjectError(f"{where} needs a reason: a line saying why the finding is safe")
        waive.append(CdcWaiver(w.get("from", "*"), w.get("to", "*"), reason.strip()[:200]))
    return CdcConfig(tuple(async_inputs), dict(domains), tuple(synced), tuple(waive))


def _parse_gate_sim(value: Any) -> GateSimConfig:
    g = _mapping(value, "gate_sim", ("netlist", "corner", "delays"))
    netlist = _choice(g.get("netlist", "routed"), GLS_NETLISTS, "gate_sim.netlist")
    corner = g.get("corner", "")
    if corner and (not isinstance(corner, str) or not CORNER_RE.fullmatch(corner)):
        raise ProjectError(
            f"gate_sim.corner must be a corner name such as nom_typ_1p20V_25C, not {corner!r}"
        )
    delays = _choice(g.get("delays", "typ"), GLS_DELAYS, "gate_sim.delays")
    return GateSimConfig(netlist, str(corner), delays)


def _chip_pad(value: Any, where: str) -> Pad:
    if value is None:
        return Pad("empty")
    if isinstance(value, str):
        if value in SUPPLIES:
            return Pad(value)
        if port_bit(value) is None:
            raise ProjectError(
                f"{where}: {value!r} is not a port bit of the top (name or name[index]), "
                f"nor a supply ({', '.join(SUPPLIES)})"
            )
        return Pad("signal", value)
    a = _mapping(value, where, ("analog", "resistor"))
    if "analog" not in a:
        raise ProjectError(f"{where} must be a port bit, a supply, null or {{analog: NET}}")
    resistor = a.get("resistor", False)
    if not isinstance(resistor, bool):
        raise ProjectError(f"{where}.resistor must be true or false")
    return Pad("analog", _ident(a["analog"], f"{where}.analog"), resistor)


def _parse_chip(value: Any, paths: _Paths, top: str) -> ChipConfig | None:
    if value is None:
        return None
    c = _mapping(value, "chip", ("name", "pads", "macros", "pad_pitch_um", "core_um", "io"))
    name = _ident(c.get("name", f"{top}_chip"), "chip.name")
    pitch = _positive(c.get("pad_pitch_um", 100), "chip.pad_pitch_um", PAD_PITCH_MAX)
    if pitch < PAD_PITCH_MIN:
        raise ProjectError(f"chip.pad_pitch_um must be at least {PAD_PITCH_MIN:g} for bonding")
    if pitch != int(pitch):
        # The fillers come in whole micrometres; any remainder would break the ring's rails.
        raise ProjectError("chip.pad_pitch_um must be a whole number of um")
    core = None
    if c.get("core_um") is not None:
        raw = c["core_um"]
        if not isinstance(raw, list) or len(raw) != 2:
            raise ProjectError("chip.core_um must be [width, height] in um")
        w = _positive(raw[0], "chip.core_um[0]", CORE_MAX_UM)
        h = _positive(raw[1], "chip.core_um[1]", CORE_MAX_UM)
        core = (w, h)
    pads_raw = _mapping(c.get("pads"), "chip.pads", SIDES)
    pads: dict[str, tuple[Pad, ...]] = {}
    signals: set[str] = set()
    analog: set[str] = set()
    for side in SIDES:
        items = pads_raw.get(side)
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ProjectError(f"chip.pads.{side} must be a list of pads")
        if len(items) > SLOTS_MAX:
            raise ProjectError(f"chip.pads.{side}: at most {SLOTS_MAX} pads on a side")
        row = []
        for i, item in enumerate(items):
            where = f"chip.pads.{side}[{i}]"
            pad = _chip_pad(item, where)
            if pad.kind == "signal":
                if pad.net in signals:
                    raise ProjectError(f"{where}: {pad.net} is on a pad already")
                signals.add(pad.net)
            elif pad.kind == "analog":
                if pad.net in analog:
                    raise ProjectError(f"{where}: analog net {pad.net} is on a pad already")
                analog.add(pad.net)
            row.append(pad)
        pads[side] = tuple(row)
    kinds = {p.kind for row in pads.values() for p in row}
    missing = [k for k in SUPPLIES if k not in kinds]
    if missing:
        raise ProjectError(f"chip.pads: needs at least one {', '.join(missing)} pad")
    raw_macros = c.get("macros") or []
    if not isinstance(raw_macros, list):
        raise ProjectError("chip.macros must be a list of {instance, package, ...}")
    if len(raw_macros) > MACROS_MAX:
        raise ProjectError(f"chip.macros: at most {MACROS_MAX} macros")
    macros: list[ChipMacro] = []
    for i, m in enumerate(raw_macros):
        where = f"chip.macros[{i}]"
        m = _mapping(m, where, ("instance", "package", "orientation", "at_um", "connect"))
        inst = _ident(m.get("instance"), f"{where}.instance")
        if inst in {x.instance for x in macros}:
            raise ProjectError(f"{where}.instance: {inst} is listed twice")
        if inst.startswith(("pad_", "fill_", "corner_", "u_core")):
            raise ProjectError(
                f"{where}.instance: {inst} starts like ChipLeap's own instances (pad_, fill_, "
                "corner_, u_core)"
            )
        if not isinstance(m.get("package"), str):
            raise ProjectError(f"{where}.package must be the path of a .macro directory")
        package = paths.path(paths.root, m["package"], f"{where}.package", kind="dir")
        orient = _choice(m.get("orientation", "N"), ORIENTATIONS, f"{where}.orientation")
        at = None
        if m.get("at_um") is not None:
            raw = m["at_um"]
            if not isinstance(raw, list) or len(raw) != 2:
                raise ProjectError(f"{where}.at_um must be [x, y] in um from the core's corner")
            vals = []
            for k, v in enumerate(raw):
                if (
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not (0 <= v <= CORE_MAX_UM)
                ):
                    raise ProjectError(f"{where}.at_um[{k}] must be from 0 to {CORE_MAX_UM:g}")
                vals.append(float(v))
            at = (vals[0], vals[1])
        conn = m.get("connect") or {}
        if not isinstance(conn, dict):
            raise ProjectError(f"{where}.connect must map the macro's pins to nets")
        connect: dict[str, str] = {}
        for pin, net in conn.items():
            if not isinstance(pin, str) or port_bit(pin) is None:
                raise ProjectError(
                    f"{where}.connect: {str(pin)[:40]!r} is not a pin: a name, a bus or a bit "
                    "of one (q[3])"
                )
            if not isinstance(net, str) or port_bit(net) is None:
                raise ProjectError(
                    f"{where}.connect.{pin} must be a net: a port bit of the top or a name"
                )
            connect[str(pin)] = net
        macros.append(ChipMacro(inst, package, orient, at, connect))
    return ChipConfig(name, pads, tuple(macros), pitch, core, _parse_io(c.get("io")))


def _parse_io(value: Any) -> IoBudget:
    if value is None:
        return IoBudget()
    io = _mapping(
        value,
        "chip.io",
        ("input_delay_ns", "output_delay_ns", "output_load_pf", "input_transition_ns"),
    )
    out: dict[str, float] = {}
    for key, upper in (
        ("input_delay_ns", IO_DELAY_MAX_NS),
        ("output_delay_ns", IO_DELAY_MAX_NS),
        ("output_load_pf", IO_LOAD_MAX_PF),
        ("input_transition_ns", IO_TRANSITION_MAX_NS),
    ):
        if key in io:
            v = io[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= upper:
                raise ProjectError(f"chip.io.{key} must be a number from 0 to {upper:g}")
            out[key] = float(v)
    return IoBudget(**out)


def _chip_dict(chip: ChipConfig, rel: Callable[[Path], str]) -> dict[str, Any]:
    def pad(p: Pad) -> Any:
        if p.kind == "empty":
            return None
        if p.kind == "signal":
            return p.net
        if p.kind == "analog":
            return {"analog": p.net, **({"resistor": True} if p.resistor else {})}
        return p.kind

    out: dict[str, Any] = {
        "name": chip.name,
        "pad_pitch_um": chip.pad_pitch_um,
        "pads": {side: [pad(p) for p in chip.pads.get(side, ())] for side in SIDES},
    }
    if chip.core_um is not None:
        out["core_um"] = list(chip.core_um)
    if chip.io != IoBudget():
        out["io"] = {k: v for k, v in asdict(chip.io).items() if v is not None}
    if chip.macros:
        out["macros"] = [
            {
                "instance": m.instance,
                "package": rel(m.package),
                "orientation": m.orientation,
                **({"at_um": list(m.at_um)} if m.at_um is not None else {}),
                "connect": dict(m.connect),
            }
            for m in chip.macros
        ]
    return out


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
    "formal",
    "equivalence",
    "cdc",
    "gate_sim",
    "dft",
    "power",
    "ir_drop",
    "upf",
    "eco",
    "chip",
)


def parse_project(
    data: Any, root: Path, file: Path | None = None, allowed: Iterable[Path] = ()
) -> Project:
    """A project from its YAML mapping; relative paths start at ``root`` and must stay in
    it, or in one of the ``allowed`` directories (see ``allowed_roots``)."""
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
    paths = _Paths(root, allowed)
    root = paths.root
    top = _ident(data.get("top"), "top")
    name = data.get("name", top)
    if not is_safe_name(str(name), SPEC_NAME_MAX):
        raise ProjectError(
            f"name {name!r} must be 1 to {SPEC_NAME_MAX} letters, digits, '_', '.' or '-', "
            "starting with a letter or digit"
        )

    fl = _Filelist()
    if data.get("filelist") is not None:
        fl = parse_filelist(paths.path(root, str(data["filelist"]), "filelist"), roots=paths.roots)
    sources = fl.sources + paths.files(root, _str_list(data.get("sources"), "sources"), "sources")
    sources = list(dict.fromkeys(s.resolve() for s in sources))
    if not sources:
        raise ProjectError("sources: give at least one file (or a filelist)")
    for s in sources:
        if s.suffix not in HDL_SUFFIXES:
            raise ProjectError(f"sources: {s.name} is not Verilog or SystemVerilog")
    includes = fl.include_dirs + [
        paths.path(root, d, "include_dirs", kind="dir")
        for d in _str_list(data.get("include_dirs"), "include_dirs")
    ]
    defines = {**fl.defines, **_parse_defines(data.get("defines"))}

    tech = str(data.get("tech", "ihp-sg13g2"))
    if tech not in TECHNOLOGIES:
        raise ProjectError(f"tech: {tech!r} is not one of {', '.join(TECHNOLOGIES)}")
    golden = None
    if data.get("golden") is not None:
        golden = paths.path(root, str(data["golden"]), "golden")
        if golden.suffix != ".py":
            raise ProjectError("golden must be a Python file the tests import")
    cons = _mapping(data.get("constraints"), "constraints", ("sdc",))
    sdc = paths.path(root, str(cons["sdc"]), "constraints.sdc") if cons.get("sdc") else None
    clocks = _parse_clocks(data.get("clocks"))
    resets = _parse_resets(data.get("resets"))
    for r in resets:
        if r.port in {c.port for c in clocks}:
            raise ProjectError(f"resets: {r.port} is also a clock")
    tests = _parse_tests(data.get("tests"), paths, top)
    lint = _parse_lint(data.get("lint"), paths)
    formal = _parse_formal(data.get("formal"), paths)
    include_dirs = list(dict.fromkeys(d.resolve() for d in includes))
    paths.check_references(
        [*sources, *tests.sources, *lint.waivers, *(formal.sources if formal else ())],
        include_dirs,
    )
    return Project(
        root=root,
        file=(file or root / PROJECT_FILE).resolve(),
        name=str(name),
        top=top,
        sources=tuple(sources),
        include_dirs=tuple(include_dirs),
        defines=defines,
        parameters=_parse_parameters(data.get("parameters")),
        timescale=_parse_timescale(data.get("timescale")),
        clocks=clocks,
        resets=resets,
        tech=tech,
        tests=tests,
        golden=golden,
        lint=lint,
        sdc=sdc,
        pnr=_parse_pnr(data.get("pnr")),
        formal=formal,
        equivalence=_parse_equivalence(data.get("equivalence")),
        cdc=_parse_cdc(data.get("cdc"), clocks, resets),
        gate_sim=_parse_gate_sim(data.get("gate_sim")),
        chip=_parse_chip(data.get("chip"), paths, top),
        description=str(data.get("description", "")),
        filelists=tuple(fl.files),
        allowed=paths.roots[1:],
        # A section given with no keys ("dft:") turns the step on with its defaults.
        dft=_parse_dft(data["dft"]) if "dft" in data else None,
        power=_parse_power(data.get("power")),
        ir_drop=_parse_ir_drop(data.get("ir_drop")),
        upf=_parse_upf(data["upf"]) if "upf" in data else None,
        eco=_parse_eco(data.get("eco")),
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


def load_project(path: str | Path, allow: Iterable[str | Path] = ()) -> Project:
    """Read and check a project: a project file, or a directory that holds one. Its files
    may also come from ``allow`` and the directories in ``CHIPLEAP_ALLOW_PATHS``.

    The project file is held to the same rule: it must be YAML and resolve inside its own
    directory, so a project.yaml that is a symlink to a file elsewhere is refused before
    it is read. A YAML error gives the line and column but not the text there, so naming
    some other YAML file cannot show its contents either."""
    p = Path(path)
    f = p / PROJECT_FILE if p.is_dir() else p
    if f.suffix not in (".yaml", ".yml"):
        why = "not a directory or a .yaml file" if p.exists() else "No such file or directory"
        raise ProjectError(f"{p}: {why}; a project is a directory with project.yaml")
    roots = allowed_roots(allow)
    home = (f.parent.resolve(), *roots)
    if not _within(f.resolve(), home):
        raise ProjectError(f"{f} leads outside its directory")
    try:
        text = read_inside(f, home).decode(errors="replace")
    except ProjectError as exc:
        # Missing or unreadable ("project.yaml: No such file or directory"), or swapped for
        # a symlink since the check above.
        why = str(exc).split(": ", 1)[-1] if ": " in str(exc) else "leads outside its directory"
        raise ProjectError(f"{f}: {why}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None)
        raise ProjectError(
            f"{f}: not valid YAML{at}" + (f": {problem}" if problem else "")
        ) from None
    try:
        return parse_project(data, f.parent, f, roots)
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
    for p, _ in _walk(directory, (directory.resolve(),)):
        rel = p.relative_to(directory).parts
        if (
            p.suffix not in (".sv", ".v")
            or any(part.startswith(".") for part in rel[:-1])
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
        paths = parse_filelist(_Paths(root).path(root, filelist, "filelist"), roots=[root]).sources
    elif srcs:
        paths = _Paths(root).files(root, srcs, "sources")
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
        reset_list = [(p, active_low_by_name(p)) for p in ports if _RESET_RE.match(p)][:1]
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
            # Only a directory inside the project is looked into.
            d = (root / cand).resolve()
            if _within(d, (root,)) and d.is_dir() and list(d.glob("test_*.py")):
                tests_dir = cand
                notes.append(f"tests: cocotb tests in {cand}/")
                break
    if tests_dir is not None:
        data["tests"] = {"dir": tests_dir}
    project = parse_project(data, root, dest)
    # Not through a symlink: --force must not overwrite whatever project.yaml points at.
    builddir.write_text(root, PROJECT_FILE, project_yaml(project, notes))
    return dest, notes
