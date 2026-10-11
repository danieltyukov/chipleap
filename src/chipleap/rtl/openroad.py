"""Run OpenROAD on a script ChipLeap writes, and find the files it reads.

The implementation steps (scan insertion, power, IR drop, UPF, ECO) each write a Tcl
script from ChipLeap's own templates, with every value from the project quoted
(``chipleap.rtl.quote``), and run the ``openroad`` command on it. Each run gets a private
directory (``Job``) that ChipLeap empties first: copies of its inputs go into ``inputs/``,
never links, and OpenROAD writes its results beside them. The command is often a wrapper
that starts a container with only its working directory mounted (and the PDK, read
only), so the container sees those copies and nothing else of the project. OpenROAD runs
through ``chipleap.verify.launch``, so a time limit, Ctrl-C or a cancelled MCP call stops
it and its container.

Two OpenROAD builds are in play, this command and the one inside LibreLane's container,
and their databases are not exchanged: steps hand each other DEF, Verilog, SPEF, SDC,
liberty and LEF.

A project's ``build`` directory may come from someone else's repository, so a step reads
a routed result only when ChipLeap recorded it (``chipleap.rtl.provenance``): the run's
manifest must verify and every file read must have the hash it recorded. The PDK files a
run names (in ``resolved.json``) must also lie under the PDK roots this machine has
(``chipleap.verify.tools.pdk_roots``). Anything else fails the step unread.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir
from chipleap.rtl.project import CORNERS, Project
from chipleap.rtl.provenance import Sealed, SealError, copy_file, open_sealed
from chipleap.verify import launch
from chipleap.verify.tools import ihp_liberty, openroad, pdk_roots

# Reports and logs are read whole; anything larger is not a report of these designs.
TEXT_LIMIT = 64 << 20
ECO_TAG = "eco"
RUN_TAG = "chipleap"
# The manifests of the routed run and of an ECO, both in build/pnr.
PNR_MANIFEST = "chipleap-pnr.manifest.json"
ECO_MANIFEST = "chipleap-eco.manifest.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class OpenroadError(RuntimeError):
    pass


class OpenroadUnavailable(OpenroadError):
    """OpenROAD or a file it needs is not on this machine."""


def read_limited(path: Path, limit: int = TEXT_LIMIT) -> str:
    """The text of a tool's output file, refused when it is larger than ``limit`` or is
    not a regular file (a symlink is never followed)."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise OpenroadError(f"{path.name}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OpenroadError(f"{path.name} is not a regular file")
        if st.st_size > limit:
            raise OpenroadError(f"{path.name} is larger than {limit >> 20} MB")
        return fh.read(limit).decode(errors="replace")


def read_json(path: Path, limit: int = TEXT_LIMIT) -> Any:
    try:
        return json.loads(read_limited(path, limit))
    except json.JSONDecodeError as exc:
        raise OpenroadError(f"{path.name} is not valid JSON: {exc.msg}") from None


class Job:
    """A private directory for one OpenROAD run, ``workdir/name``, emptied first: copies of
    the inputs in ``inputs/`` and whatever OpenROAD writes. ``workdir`` is a step's work
    directory (``Project.work_dir``). The inputs must already be confined by the caller:
    PDK views from ``PdkViews``, recorded files through their ``Sealed`` manifest, and
    files the step itself just wrote."""

    def __init__(self, workdir: Path, name: str) -> None:
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"{name!r} is not a plain job name")
        self.name = name
        self.dir = builddir.work_dir(workdir, name, empty=True)
        self.inputs = builddir.work_dir(self.dir, "inputs")

    def stage(self, src: Path, name: str | None = None, *, sealed: Sealed | None = None) -> str:
        """Copy ``src`` into ``inputs/`` and return its path relative to the job; with
        ``sealed``, ``src`` must be a file it recorded, with the hash it recorded. A copy,
        not a link: what OpenROAD reads cannot change the original, nor lead out of the
        job directory."""
        dest_name = name or src.name
        if not _NAME_RE.fullmatch(dest_name):
            raise OpenroadError(f"{dest_name!r} is not a plain file name")
        dest = self.inputs / dest_name
        if os.path.lexists(dest):
            raise OpenroadError(f"{dest_name} is staged twice")
        try:
            if sealed is not None:
                sealed.copy(src, dest)
            else:
                copy_file(src.resolve(), dest)
        except SealError as exc:
            raise OpenroadError(str(exc)) from None
        except OSError as exc:
            raise OpenroadError(f"{src.name} cannot be staged: {exc.strerror}") from None
        return dest.relative_to(self.dir).as_posix()

    def output(self, name: str) -> Path:
        """A file OpenROAD wrote in the job directory (it may not exist)."""
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"{name!r} is not a plain file name")
        return self.dir / name

    def run(self, script: str, timeout: float | None = None) -> tuple[str, int]:
        """Run ``script`` with OpenROAD in the job directory; its log and exit code. The
        script should ``exit`` itself; ``-exit`` makes sure it does."""
        tool = openroad()
        if not tool.found or tool.path is None:
            raise OpenroadUnavailable("OpenROAD is not installed")
        builddir.write_text(self.dir, f"{self.name}.tcl", script)
        log = self.dir / f"{self.name}.log"
        limit = timeout or float(os.environ.get("CHIPLEAP_OPENROAD_TIMEOUT", "3600"))
        cmd = [tool.path, "-no_init", "-exit", f"{self.name}.tcl"]
        try:
            code = launch.run(cmd, self.dir, log, limit)
        except launch.Busy as exc:
            raise OpenroadError(f"OpenROAD did not start: {exc}") from None
        except launch.TimedOut as exc:
            raise OpenroadError(f"OpenROAD timed out after {exc.seconds:g} s") from None
        return read_limited(log), code


def errors(log: str) -> list[str]:
    """OpenROAD's error lines, as ``[ERROR XXX-0000] message``, and Tcl errors."""
    out = [line.strip() for line in log.splitlines() if line.lstrip().startswith("[ERROR")]
    if not out:
        out = [
            line.strip()
            for line in log.splitlines()
            if line.startswith(("Error:", "invalid command name", "can't read"))
        ]
    return out


def failure(what: str, log: str, code: int) -> OpenroadError:
    found = errors(log)
    tail = "\n".join(log.strip().splitlines()[-5:])
    return OpenroadError(f"{what} failed (exit {code}): {found[0] if found else tail}")


@dataclass(frozen=True)
class PdkViews:
    """The standard-cell views of IHP SG13G2 the implementation steps read, each a file
    under a PDK root of this machine."""

    tech_lef: Path
    cell_lefs: tuple[Path, ...]
    liberty: dict[str, Path]  # corner (typ, slow, fast) -> liberty
    models: tuple[Path, ...]  # Verilog cell models, primitives first when split out

    def lib(self, corner: str) -> Path:
        if corner not in self.liberty:
            raise OpenroadUnavailable(f"no {corner} liberty in the PDK")
        return self.liberty[corner]

    def stage_all(self, job: Job, corners: tuple[str, ...] = ("typ",)) -> dict[str, Any]:
        """Stage the LEFs and the liberty of each corner; their names in the job."""
        return {
            "tech_lef": job.stage(self.tech_lef),
            "cell_lefs": [job.stage(p) for p in self.cell_lefs],
            "liberty": {c: job.stage(self.lib(c)) for c in corners},
        }


def _in_pdk(path: Path, suffixes: tuple[str, ...], what: str) -> Path:
    real = path.resolve()
    if not any(real.is_relative_to(r) for r in pdk_roots()):
        raise OpenroadError(f"{what}: {path} is not inside a PDK directory this machine knows")
    if real.suffix not in suffixes or not real.is_file():
        raise OpenroadError(f"{what}: {path} is not a {' or '.join(suffixes)} file")
    return real


def _from_stdcell_dir(lib_dir: Path) -> PdkViews:
    """The views beside a liberty in an IHP checkout: libs.ref/sg13g2_stdcell/{lef,lib,verilog}."""
    root = lib_dir.parent
    liberty = {}
    for corner, suffix in CORNERS.items():
        p = lib_dir / f"sg13g2_stdcell_{suffix}.lib"
        if p.is_file():
            liberty[corner] = _in_pdk(p, (".lib",), "liberty")
    if "typ" not in liberty:
        raise OpenroadUnavailable(f"no typical-corner liberty in {lib_dir}")
    lef = root / "lef"
    if not (lef / "sg13g2_tech.lef").is_file():
        raise OpenroadUnavailable(
            f"no LEF beside the liberty ({lef}); point PDK_ROOT at a full IHP SG13G2 PDK"
        )
    verilog = root / "verilog"
    models = [verilog / "sg13g2_udp.v", verilog / "sg13g2_stdcell.v"]
    return PdkViews(
        tech_lef=_in_pdk(lef / "sg13g2_tech.lef", (".lef",), "tech LEF"),
        cell_lefs=(_in_pdk(lef / "sg13g2_stdcell.lef", (".lef",), "cell LEF"),),
        liberty=liberty,
        models=tuple(_in_pdk(m, (".v",), "cell models") for m in models if m.is_file()),
    )


def pdk_views(project: Project, run: Path | None = None) -> PdkViews:
    """The PDK views for a project: those a routed run used when ``run`` is given (its
    recorded ``resolved.json``), else those beside the liberty synthesis uses."""
    if project.tech != "ihp-sg13g2":
        raise OpenroadUnavailable(f"the implementation steps run on IHP SG13G2, not {project.tech}")
    if run is not None:
        sealed = sealed_run(project, run)
        resolved = run / "resolved.json"
        if resolved.relative_to(sealed.root).as_posix() in sealed.files:
            return _views_of(_sealed_json(sealed, resolved))
    lib = ihp_liberty()
    if lib is None:
        raise OpenroadUnavailable("no IHP SG13G2 liberty: install the open PDK or set PDK_ROOT")
    return _from_stdcell_dir(lib.resolve().parent)


def _views_of(cfg: Any) -> PdkViews:
    try:
        techs = cfg["TECH_LEFS"]
        tech = next(iter(techs.values())) if isinstance(techs, dict) else techs
        libs = cfg["LIB"]
        liberty: dict[str, Path] = {}
        for corner, suffix in CORNERS.items():
            for key, files in libs.items():
                if suffix in str(key) and files:
                    liberty[corner] = _in_pdk(Path(files[0]), (".lib",), "liberty")
        models = [Path(m) for m in cfg.get("CELL_VERILOG_MODELS") or []]
        udp = [m.parent / "sg13g2_udp.v" for m in models[:1]]
        return PdkViews(
            tech_lef=_in_pdk(Path(tech), (".lef",), "tech LEF"),
            cell_lefs=tuple(_in_pdk(Path(p), (".lef",), "cell LEF") for p in cfg["CELL_LEFS"]),
            liberty=liberty,
            models=tuple(
                _in_pdk(m, (".v",), "cell models") for m in [*udp, *models] if m.is_file()
            ),
        )
    except (KeyError, TypeError, AttributeError, StopIteration, IndexError) as exc:
        raise OpenroadError(f"resolved.json does not name the PDK views ({exc})") from None


def _sealed_json(sealed: Sealed, path: Path) -> Any:
    try:
        return sealed.read_json(path)
    except SealError as exc:
        raise OpenroadError(str(exc)) from None


def run_dir(project: Project, tag: str = RUN_TAG) -> Path:
    return project.build_dir / "pnr" / "runs" / tag


def pnr_sealed(project: Project) -> Sealed:
    """The routed run's manifest, if ChipLeap on this machine wrote it."""
    try:
        return open_sealed(project.build_dir / "pnr", PNR_MANIFEST, "pnr")
    except SealError as exc:
        raise OpenroadError(f"the routed design in build/pnr cannot be used: {exc}") from None


def eco_sealed(project: Project, base: Sealed) -> Sealed | None:
    """The ECO's manifest when ChipLeap wrote it from the routed run ``base``, else None."""
    try:
        sealed = open_sealed(project.build_dir / "pnr", ECO_MANIFEST, "eco")
    except SealError:
        return None
    return sealed if sealed.extra.get("base") == base.mac else None


def sealed_run(project: Project, run: Path) -> Sealed:
    """The manifest that vouches for ``run``: the routed run's, or the ECO's made from it."""
    base = pnr_sealed(project)
    if run == run_dir(project, ECO_TAG):
        eco = eco_sealed(project, base)
        if eco is None:
            raise OpenroadError("the ECO in build/pnr/runs/eco was not made from this routed run")
        return eco
    if run != run_dir(project):
        raise OpenroadError(f"{run} is not a routed run of the project")
    return base


def routed_run(project: Project) -> Path | None:
    """The routed result later steps read: the ECO run when ChipLeap made one from the
    current routed run, else the routed run itself; None when there is none. A routed run
    without a manifest from this machine fails the step."""
    if not (run_dir(project) / "final" / "metrics.json").is_file():
        return None
    base = pnr_sealed(project)
    return run_dir(project, ECO_TAG) if eco_sealed(project, base) else run_dir(project)


@dataclass(frozen=True)
class RoutedViews:
    """The final views of a routed run, each a file its manifest recorded. Stage them with
    ``Job.stage(..., sealed=views.sealed)``, which checks the hash of what it copies."""

    run: Path
    sealed: Sealed
    netlist: Path
    def_: Path
    sdc: Path
    spef: Path | None
    metrics: dict[str, Any]

    def read_text(self, path: Path, limit: int = TEXT_LIMIT) -> str:
        try:
            return self.sealed.read_text(path, limit)
        except SealError as exc:
            raise OpenroadError(str(exc)) from None


def routed_views(project: Project, run: Path | None = None) -> RoutedViews:
    run = run or routed_run(project)
    if run is None:
        raise OpenroadUnavailable("no routed design: run the pnr step first")
    sealed = sealed_run(project, run)
    final = run / "final"
    top = project.top
    prefix = (final / "spef" / "nom").relative_to(sealed.root).as_posix() + "/"
    spefs = sorted(f for f in sealed.files if f.startswith(prefix) and f.endswith(".spef"))

    def recorded(path: Path, what: str) -> Path:
        try:
            sealed.locate(path)
        except SealError as exc:
            raise OpenroadError(f"{what}: {exc}") from None
        return path

    metrics = _sealed_json(sealed, final / "metrics.json")
    if not isinstance(metrics, dict):
        raise OpenroadError("the routed run's metrics.json is not a mapping")
    return RoutedViews(
        run=run,
        sealed=sealed,
        netlist=recorded(final / "nl" / f"{top}.nl.v", "netlist"),
        def_=recorded(final / "def" / f"{top}.def", "DEF"),
        sdc=recorded(final / "sdc" / f"{top}.sdc", "SDC"),
        spef=sealed.root / spefs[0] if spefs else None,
        metrics=metrics,
    )
