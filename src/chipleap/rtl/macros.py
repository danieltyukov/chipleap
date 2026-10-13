"""A project's hard macros (``macros``) in its steps.

The RTL instantiates each macro by its cell name: an SRAM of IHP's PDK (``cell``) or the
cell of a ``macro/v1`` package (``package``), as a chip's ``macros`` name them. The steps
see them so:

* lint and synthesis: a black-box module with the macro's ports, written from the
  SRAM's name (``chipleap.macro.sram.ports``) or the package's manifest, so neither
  needs the PDK. Synthesis keeps each macro as an instance.
* simulation: the PDK's own model of each SRAM, behavioural under ``FUNCTIONAL``, and
  each package's Verilog model, copied into the step's directory.
* place and route: the views of the macro layer (``chipleap.macro.layer``: an SRAM's
  from the PDK, a package's copied, checked and converted with the same name rules as a
  chip's) and the macros placed where the project says, or in rows above and below the
  logic.

The steps after them treat a macro as a box with its ports: formal makes it a cut point,
CDC a state element of the clock its port samples on, equivalence turns its pins into
compared outputs and shared inputs, gate-level simulation runs the PDK's model, scan
insertion and ATPG leave it off the chains and out of the test, power and IR drop read
its liberty and LEF, UPF places it in its domain, and an ECO passes its views on. The
chip and the commercial tools do not model a macro yet; they say so, rather than check
a design they cannot read.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterable
from pathlib import Path

from chipleap.chip import io
from chipleap.macro import sram
from chipleap.macro.layer import (
    Imported,
    MacroImportError,
    MacroView,
    Reserved,
    import_packages,
    reserved_names,
)
from chipleap.macro.manifest import MANIFEST, Manifest, parse_toml
from chipleap.macro.sram import Sram, SramViews
from chipleap.rtl import builddir, confine
from chipleap.rtl.project import Project, RtlMacro
from chipleap.verify.tools import yosys

# Steps that do not model a macro yet.
UNSUPPORTED = ("chip",)
OPEN_TOOLS = ("", "auto", "icarus", "verilator", "yosys", "librelane")
MANIFEST_MAX = 1 << 20
# Yosys lists every wire and cell too, so a large design's list is long.
MODULE_LIST_MAX = 64 << 20


class MacroStepError(RuntimeError):
    pass


def unsupported(project: Project, step: str, tool: str = "") -> str | None:
    """Why ``step`` (with ``tool``) does not run on ``project``, or None."""
    if not project.macros:
        return None
    what = step if tool in OPEN_TOOLS else f"{step} with {tool}"
    if step in UNSUPPORTED or tool not in OPEN_TOOLS:
        return (
            f"{what} does not model hard macros yet; the project instantiates "
            f"{len(project.macros)} ({', '.join(m.instance for m in project.macros)})"
        )
    return None


def srams(project: Project) -> list[RtlMacro]:
    return [m for m in project.macros if m.package is None]


def packages(project: Project) -> list[RtlMacro]:
    return [m for m in project.macros if m.package is not None]


def cells(project: Project) -> list[Sram]:
    """Each distinct SRAM cell the project's macros use, as its name describes it; the
    outline is unknown (0) without the PDK."""
    out: dict[str, Sram] = {}
    for m in srams(project):
        if m.cell not in out:
            match = sram.NAME_RE.fullmatch(m.cell)
            if match is None:
                raise MacroStepError(f"{m.cell} is not a single-port SRAM of {sram.LIBRARY}")
            out[m.cell] = Sram(m.cell, int(match[1]), int(match[2]), 0.0, 0.0, bool(match[4]))
    return list(out.values())


def manifest(project: Project, package: Path) -> Manifest:
    """A package's manifest, read as a project file."""
    data = project.read(package / MANIFEST)
    if len(data) > MANIFEST_MAX:
        raise MacroStepError(f"{package.name}/{MANIFEST} is larger than ChipLeap reads")
    try:
        m = Manifest.from_dict(parse_toml(data.decode("utf-8", errors="replace")))
    except (KeyError, TypeError, ValueError) as exc:
        raise MacroStepError(f"{package.name}/{MANIFEST}: {exc}") from exc
    # The name rules a chip's packages follow, as far as they need no PDK: the PDK's and
    # the flow's name spaces, and the top's.
    try:
        Reserved(
            frozenset({project.top.casefold()}), spaces=(project.top.casefold(),)
        ).check_package(m.name)
    except MacroImportError as exc:
        raise MacroStepError(f"{package.name}: {exc}") from None
    return m


def _package_ports(m: Manifest) -> list[tuple[str, str, int]]:
    return [(p.name, p.direction, p.width or 0) for p in m.pins if not p.is_supply]


def stubs(project: Project) -> list[Path]:
    """The macros' black-box modules, in ``build/macros/``, for lint and synthesis."""
    if not project.macros:
        return []
    work = project.work_dir("macros")
    out = [builddir.write_text(work, f"{s.cell}.v", sram.stub(s)) for s in cells(project)]
    seen: set[str] = set()
    for m in packages(project):
        assert m.package is not None
        man = manifest(project, m.package)
        if man.name in seen:
            continue
        seen.add(man.name)
        text = sram.module(man.name, _package_ports(man), "the package's manifest")
        out.append(builddir.write_text(work, f"{man.name}.v", text))
    return out


def pdk() -> Path:
    """The PDK ChipLeap uses, which a step that needs the macros' own views requires."""
    root = io.pdk_dir()
    if root is None:
        raise MacroStepError(
            f"the SRAM macros need the IHP PDK with {sram.LIBRARY}; set {io.PDK_ENV} to its "
            "ihp-sg13g2 directory"
        )
    return root


def available(project: Project, root: Path) -> dict[str, Sram]:
    """The project's SRAM cells as the PDK at ``root`` has them, with their outlines;
    refuses a cell the PDK does not have."""
    known = {s.cell: s for s in sram.catalog(root)}
    missing = sorted({m.cell for m in srams(project)} - set(known))
    if missing:
        raise MacroStepError(
            f"the PDK at {root} has no single-port SRAM {', '.join(missing)} (with LEF, GDS, "
            "CDL and liberty at every corner)"
        )
    return {m.cell: known[m.cell] for m in srams(project)}


def models(project: Project, work: Path) -> list[Path]:
    """The macros' simulation models, copied into ``work/macros``: the PDK's model of
    each SRAM and the behavioural core it instantiates under ``FUNCTIONAL``, and each
    package's Verilog model."""
    if not project.macros:
        return []
    dest = builddir.work_dir(work, "macros", empty=True)
    out = []
    if srams(project):
        root = pdk()
        available(project, root)
        ref = root / "libs.ref" / sram.LIBRARY / "verilog"
        names = [f"{s.cell}.v" for s in cells(project)]
        cores = {
            "RM_IHPSG13_1P_core_behavioral_bm_bist.v"
            if s.bit_mask
            else "RM_IHPSG13_1P_core_behavioral.v"
            for s in cells(project)
        }
        for name in [*sorted(cores), *names]:
            src = ref / name
            if not src.is_file():
                raise MacroStepError(f"the PDK at {root} has no simulation model {name}")
            out.append(builddir.write_bytes(dest, name, src.read_bytes()))
    seen: set[str] = set()
    for m in packages(project):
        assert m.package is not None
        man = manifest(project, m.package)
        if man.name not in seen:
            seen.add(man.name)
            model = m.package / man.file("v")
            out.append(builddir.write_bytes(dest, model.name, project.read(model)))
    return out


def area_um2(project: Project) -> float | None:
    """The macros' area: an SRAM's from the PDK's LEF, a package's from its manifest;
    None when an SRAM's needs the PDK and it is not installed."""
    if not project.macros:
        return None
    total = 0.0
    if srams(project):
        root = io.pdk_dir()
        if root is None:
            return None
        by_cell = available(project, root)
        total += sum(by_cell[m.cell].area_um2 for m in srams(project))
    for m in packages(project):
        assert m.package is not None
        man = manifest(project, m.package)
        total += man.width_um * man.height_um
    return total


def views(
    project: Project, work: Path
) -> tuple[dict[str, MacroView], dict[str, SramViews], dict[str, str]]:
    """Every macro's views in ``work/macros/``, for place and route: the macro layer's
    views by cell, an SRAM's added views (liberty at every corner, its black box) by
    cell, and each flattened instance's cell. A package is imported as a chip's is:
    copied, checked against the PDK and converted, its names held to the PDK's, the
    flow's and the top's."""
    root = pdk()
    available(project, root)
    by_sram = {s.cell: sram.views(root, s.cell, work) for s in cells(project)}
    all_views: dict[str, MacroView] = {c: v.view for c, v in by_sram.items()}
    inst_cell = {m.flat: m.cell for m in srams(project)}
    if packages(project):
        commit = io.pdk_commit(root)
        spec = f"ihp-sg13g2@{commit}" if commit else "ihp-sg13g2"
        reserved = reserved_names(root, (project.top,))
        imported: Imported = import_packages(
            [(m.flat, m.package) for m in packages(project) if m.package is not None],
            project.files_in,
            project.read,
            work,
            spec,
            reserved,
        )
        for cell, view in imported.views.items():
            if cell in all_views:
                raise MacroStepError(f"package {cell} has the name of an SRAM of the PDK")
            all_views[cell] = view
        inst_cell |= imported.cells
    return all_views, by_sram, inst_cell


def rtl_modules(project: Project, work: Path) -> list[str]:
    """Every module the project's sources define, black boxes and the modules its top
    does not instantiate included, as Yosys reads the sources alone in ``work``
    (``chipleap.chip.merge.LIST_MODULES``)."""
    from chipleap.chip.merge import LIST_MODULES, MergeError, listed_modules
    from chipleap.rtl.synth import SynthError, prepare_verilog, source_reads

    tool = yosys()
    if tool.path is None:
        raise MacroStepError(tool.missing("Yosys"))
    try:
        files, raw = prepare_verilog(project, work)
    except SynthError as exc:
        raise MacroStepError(str(exc)) from exc
    script = [*source_reads(project, files, raw), f"tee -q -o rtl_modules.txt {LIST_MODULES}"]
    ys = builddir.write_text(work, "rtl_modules.ys", "\n".join(script) + "\n")
    cmd = [tool.path, "-q", "-l", "rtl_modules.log", ys.name]
    with confine.confined(cmd, [*project.roots, work]) as (argv, env):
        proc = subprocess.Popen(
            argv,
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            log, _ = proc.communicate(
                timeout=float(os.environ.get("CHIPLEAP_SYNTH_TIMEOUT", "1800"))
            )
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    listed = work / "rtl_modules.txt"
    if proc.returncode != 0 or not listed.is_file():
        errors = [ln for ln in (log or "").splitlines() if "ERROR" in ln]
        raise MacroStepError(f"Yosys could not read the RTL: {(errors or ['no output'])[0]}")
    if listed.stat().st_size > MODULE_LIST_MAX:
        raise MacroStepError("Yosys's list of the RTL's modules is larger than ChipLeap reads")
    try:
        return listed_modules(listed.read_text(errors="replace"))
    except MergeError as exc:
        raise MacroStepError(str(exc)) from None


def check_modules(project: Project, work: Path, views: Iterable[str]) -> None:
    """Refuse a macro cell (``views``) the project's RTL defines as a module of its own:
    LibreLane would bind the instance to that RTL instead of the macro, silently. The
    check is a chip's (``chipleap.chip.merge.check_modules``), without a chip top or IO
    cells."""
    from chipleap.chip.merge import MergeError
    from chipleap.chip.merge import check_modules as check

    try:
        check(rtl_modules(project, work), views)
    except MergeError as exc:
        raise MacroStepError(str(exc)) from None
