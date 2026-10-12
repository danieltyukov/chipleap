"""A project's SRAM macros (``macros``) in its steps.

The RTL instantiates IHP's single-port SRAMs by their cell names. The steps see them so:

* lint and synthesis: a black-box module with the macro's ports, written from its name
  (``chipleap.macro.sram.ports``), so neither needs the PDK. Synthesis keeps the macro as
  an instance and reports its area beside the cells' when the PDK is installed.
* simulation: the PDK's own model of each macro, behavioural under ``FUNCTIONAL``, copied
  from the PDK ChipLeap uses into the step's directory.
* place and route: the PDK's views (``chipleap.macro.sram.views``), the macro grid and
  the macros placed where the project says, or in rows above and below the logic.

The steps that read the design or its gate netlist in ways that do not model a macro
yet (formal, CDC, equivalence, gate-level simulation, ECO, scan insertion, power, IR
drop, UPF, the chip, and the commercial tools) do not run on a project with macros; each
says so, rather than check a design it cannot read.
"""

from __future__ import annotations

from pathlib import Path

from chipleap.chip import io
from chipleap.macro import sram
from chipleap.macro.sram import Sram, SramViews
from chipleap.rtl import builddir
from chipleap.rtl.project import Project

# Steps that do not model a macro yet.
UNSUPPORTED = (
    "formal",
    "cdc",
    "equiv",
    "gls",
    "eco",
    "dft",
    "power",
    "ir_drop",
    "upf",
    "chip",
)
OPEN_TOOLS = ("", "auto", "icarus", "verilator", "yosys", "librelane")


class MacroStepError(RuntimeError):
    pass


def unsupported(project: Project, step: str, tool: str = "") -> str | None:
    """Why ``step`` (with ``tool``) does not run on ``project``, or None."""
    if not project.macros:
        return None
    what = step if tool in OPEN_TOOLS else f"{step} with {tool}"
    if step in UNSUPPORTED or tool not in OPEN_TOOLS:
        return (
            f"{what} does not model SRAM macros yet; the project instantiates "
            f"{len(project.macros)} ({', '.join(m.instance for m in project.macros)})"
        )
    return None


def cells(project: Project) -> list[Sram]:
    """Each distinct cell the project's macros use, as its name describes it; the outline
    is unknown (0) without the PDK."""
    out: dict[str, Sram] = {}
    for m in project.macros:
        if m.cell not in out:
            match = sram.NAME_RE.fullmatch(m.cell)
            if match is None:
                raise MacroStepError(f"{m.cell} is not a single-port SRAM of {sram.LIBRARY}")
            out[m.cell] = Sram(m.cell, int(match[1]), int(match[2]), 0.0, 0.0, bool(match[4]))
    return list(out.values())


def stubs(project: Project) -> list[Path]:
    """The macros' black-box modules, in ``build/macros/``, for lint and synthesis."""
    if not project.macros:
        return []
    work = project.work_dir("macros")
    return [builddir.write_text(work, f"{s.cell}.v", sram.stub(s)) for s in cells(project)]


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
    """The project's cells as the PDK at ``root`` has them, with their outlines; refuses
    a cell the PDK does not have."""
    known = {s.cell: s for s in sram.catalog(root)}
    missing = sorted({m.cell for m in project.macros} - set(known))
    if missing:
        raise MacroStepError(
            f"the PDK at {root} has no single-port SRAM {', '.join(missing)} (with LEF, GDS, "
            "CDL and liberty at every corner)"
        )
    return {m.cell: known[m.cell] for m in project.macros}


def area_um2(project: Project) -> float | None:
    """The macros' area from the PDK's LEF, or None without the PDK."""
    root = io.pdk_dir()
    if root is None or not project.macros:
        return None
    by_cell = available(project, root)
    return sum(by_cell[m.cell].area_um2 for m in project.macros)


def models(project: Project, work: Path) -> list[Path]:
    """The PDK's simulation models of the project's macros, copied into ``work/macros``:
    each cell's and the behavioural core it instantiates under ``FUNCTIONAL``."""
    if not project.macros:
        return []
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
    dest = builddir.work_dir(work, "macros", empty=True)
    out = []
    for name in [*sorted(cores), *names]:
        src = ref / name
        if not src.is_file():
            raise MacroStepError(f"the PDK at {root} has no simulation model {name}")
        out.append(builddir.write_bytes(dest, name, src.read_bytes()))
    return out


def views(project: Project, work: Path) -> tuple[Path, dict[str, SramViews]]:
    """The PDK and each cell's views in ``work/macros/<cell>/``, for place and route."""
    root = pdk()
    available(project, root)
    return root, {s.cell: sram.views(root, s.cell, work) for s in cells(project)}
