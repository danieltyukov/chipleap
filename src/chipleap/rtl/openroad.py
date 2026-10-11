"""Run OpenROAD on a script ChipLeap writes, and find the files it reads.

The implementation steps (scan insertion, power, IR drop, UPF, ECO) each write a Tcl
script and run the ``openroad`` command on it in a work directory of their own. The
command is often a wrapper that starts a container with only the current directory
mounted, so every input is staged into the work directory first: hard-linked when it
can be, copied when not. OpenROAD runs through ``chipleap.verify.launch``, so a time
limit, Ctrl-C or a cancelled MCP call stops it and its container.

Two OpenROAD builds are in play, this command and the one inside LibreLane's
container, and their databases are not exchanged: steps hand each other DEF, Verilog,
SPEF, SDC, liberty and LEF.

A project's ``build`` directory may come from someone else's repository, so the files
a LibreLane run names (in ``resolved.json`` and in a step's ``state_in.json``) are held
to two places: the run's own directory, and the PDK roots this machine has
(``chipleap.verify.tools.pdk_roots``). A path anywhere else fails the step unread.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.rtl.project import CORNERS, Project
from chipleap.verify import launch
from chipleap.verify.tools import ihp_liberty, openroad, pdk_roots

# Reports and logs are read whole; anything larger is not a report of these designs.
TEXT_LIMIT = 64 << 20
ECO_TAG = "eco"
RUN_TAG = "chipleap"
ECO_MARKER = "chipleap-eco.json"


class OpenroadError(RuntimeError):
    pass


class OpenroadUnavailable(OpenroadError):
    """OpenROAD or a file it needs is not on this machine."""


def read_limited(path: Path, limit: int = TEXT_LIMIT) -> str:
    """The text of a tool's output file, refused when it is larger than ``limit``."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OpenroadError(f"{path.name}: {exc.strerror}") from None
    if size > limit:
        raise OpenroadError(f"{path.name} is larger than {limit >> 20} MB")
    with path.open("rb") as fh:
        return fh.read(limit).decode(errors="replace")


def read_json(path: Path, limit: int = TEXT_LIMIT) -> Any:
    try:
        return json.loads(read_limited(path, limit))
    except json.JSONDecodeError as exc:
        raise OpenroadError(f"{path.name} is not valid JSON: {exc.msg}") from None


def stage(src: Path, workdir: Path, name: str | None = None) -> str:
    """Put ``src`` in ``workdir/inputs`` and return its path relative to ``workdir``. A
    hard link when the file system allows one, else a copy: a container that mounts
    only the work directory then sees it, and a large SPEF or VCD costs nothing."""
    dest_dir = workdir / "inputs"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name or src.name)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)
    return dest.relative_to(workdir).as_posix()


def run_openroad(
    script: str, workdir: Path, name: str, timeout: float | None = None
) -> tuple[str, int]:
    """Run ``script`` with OpenROAD in ``workdir``; its log and exit code. The script
    should ``exit`` itself; ``-exit`` makes sure it does."""
    tool = openroad()
    if not tool.found or tool.path is None:
        raise OpenroadUnavailable("OpenROAD is not installed")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / f"{name}.tcl").write_text(script)
    log = workdir / f"{name}.log"
    limit = timeout or float(os.environ.get("CHIPLEAP_OPENROAD_TIMEOUT", "3600"))
    cmd = [tool.path, "-no_init", "-exit", f"{name}.tcl"]
    try:
        code = launch.run(cmd, workdir, log, limit)
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
    """The standard-cell views of IHP SG13G2 the implementation steps read."""

    tech_lef: Path
    cell_lefs: tuple[Path, ...]
    liberty: dict[str, Path]  # corner (typ, slow, fast) -> liberty
    models: tuple[Path, ...]  # Verilog cell models, primitives first when split out

    def lib(self, corner: str) -> Path:
        if corner not in self.liberty:
            raise OpenroadUnavailable(f"no {corner} liberty in the PDK")
        return self.liberty[corner]

    def stage_all(self, workdir: Path, corners: tuple[str, ...] = ("typ",)) -> dict[str, Any]:
        """Stage the LEFs and the liberty of each corner; their names in ``workdir``."""
        return {
            "tech_lef": stage(self.tech_lef, workdir),
            "cell_lefs": [stage(p, workdir) for p in self.cell_lefs],
            "liberty": {c: stage(self.lib(c), workdir) for c in corners},
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
    """The PDK views for a project: those a LibreLane run used when ``run`` is given (its
    ``resolved.json``), else those beside the liberty synthesis uses."""
    if project.tech != "ihp-sg13g2":
        raise OpenroadUnavailable(f"the implementation steps run on IHP SG13G2, not {project.tech}")
    if run is not None and (run / "resolved.json").is_file():
        cfg = read_json(run / "resolved.json")
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
        except (KeyError, TypeError, StopIteration, IndexError) as exc:
            raise OpenroadError(f"resolved.json does not name the PDK views ({exc})") from None
    lib = ihp_liberty()
    if lib is None:
        raise OpenroadUnavailable("no IHP SG13G2 liberty: install the open PDK or set PDK_ROOT")
    return _from_stdcell_dir(lib.resolve().parent)


def run_dir(project: Project, tag: str = RUN_TAG) -> Path:
    return project.build_dir / "pnr" / "runs" / tag


def in_run(path: str | Path, run: Path, what: str) -> Path:
    """A file a LibreLane run names, if it is inside the run's own directory."""
    real = Path(path).resolve()
    if not real.is_relative_to(run.resolve()) or not real.is_file():
        raise OpenroadError(f"{what}: {path} is not a file of the run in {run.name}")
    return real


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def base_digest(project: Project) -> str | None:
    """A digest of the routed run's final metrics: an ECO belongs to the run it has."""
    metrics = run_dir(project) / "final" / "metrics.json"
    return _digest(metrics) if metrics.is_file() else None


def routed_run(project: Project) -> Path | None:
    """The routed result later steps read: the ECO run when one was made from the current
    routed run, else the routed run itself; None when there is none."""
    base = run_dir(project)
    if not (base / "final" / "metrics.json").is_file():
        return None
    eco = run_dir(project, ECO_TAG)
    marker = eco / ECO_MARKER
    if (eco / "final" / "metrics.json").is_file() and marker.is_file():
        try:
            recorded = read_json(marker, 1 << 20).get("base_metrics_sha256")
        except (OpenroadError, AttributeError):
            recorded = None
        if recorded == base_digest(project):
            return eco
    return base


@dataclass(frozen=True)
class RoutedViews:
    """The final views of a routed run, each confined to the run."""

    run: Path
    netlist: Path
    def_: Path
    sdc: Path
    spef: Path | None
    metrics: dict[str, Any]


def routed_views(project: Project, run: Path | None = None) -> RoutedViews:
    run = run or routed_run(project)
    if run is None:
        raise OpenroadUnavailable("no routed design: run the pnr step first")
    final = run / "final"
    top = project.top
    spefs = sorted((final / "spef").glob("nom/*.spef")) if (final / "spef").is_dir() else []
    return RoutedViews(
        run=run,
        netlist=in_run(final / "nl" / f"{top}.nl.v", run, "netlist"),
        def_=in_run(final / "def" / f"{top}.def", run, "DEF"),
        sdc=in_run(final / "sdc" / f"{top}.sdc", run, "SDC"),
        spef=in_run(spefs[0], run, "SPEF") if spefs else None,
        metrics=read_json(final / "metrics.json"),
    )
