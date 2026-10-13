"""Place and route of the whole SoC: X-HEEP, rendered for the design, with the accelerator,
every memory bank and the accelerator's buffers in IHP's SRAM macros, as one block in
LibreLane on IHP SG13G2.

1. X-HEEP at the commit ChipLeap pins (``chipleap.verify.xheep``) is copied into the
   step's directory (``x-heep/``) and rendered there by its own generator for the
   configuration ChipLeap writes from ``design.yaml``, never the bundle's copy: X-HEEP's
   configuration is Python, and the generator runs it.
2. FuseSoC lists X-HEEP's sources for its ASIC synthesis target, running X-HEEP's own
   generators (register files, the boot ROM, which a RISC-V GCC compiles). X-HEEP's
   cores need its pinned FuseSoC (``scripts/install_fusesoc.sh``). The cores of the
   CPUs the design does not use are left out, their packages kept, since the MCU's
   sources import them.
3. ChipLeap writes the SoC's top, X-HEEP's banks in IHP's macros and the clock cells
   (``chipleap.gen.soc``), and the accelerator with its buffers in macros
   (``chipleap.gen.banks``).
4. LibreLane reads the sources with the slang frontend, places the macros where
   ChipLeap puts them (``chipleap.macro.place``), under their macro power grid, and
   signs the block off as the accelerator's layout is signed off.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from chipleap.gen import banks, soc
from chipleap.gen.rtl import accel_sv
from chipleap.hw.design import Design
from chipleap.hw.tech import get_tech
from chipleap.macro import grid, place, sram
from chipleap.rtl import builddir
from chipleap.verify import pnr
from chipleap.verify.tools import Tool, clean_env, fusesoc, riscv_gcc

CORE = "chipleap:soc:xheep:0"
TARGET = "asic_yosys_synthesis"
# FuseSoC's file lists and X-HEEP's generators each run in seconds; a minute is plenty.
SETUP_TIMEOUT_S = 600
EDAM_MAX = 16 << 20
# The FuseSoC cores of each CPU X-HEEP can have. Only the design's CPU is synthesized.
CPU_CORES = {
    "cv32e20": ("openhwgroup:cve2:",),
    "cv32e40p": ("openhwgroup.org:ip:cv32e40p:",),
    "cv32e40x": ("openhwgroup.org:ip:cv32e40x:",),
    "cv32e40px": ("x-heep:ip:cv32e40px:",),
}
# Files of an unused CPU's cores the MCU still reads: packages it imports and the
# eXtension Interface it declares ports of.
KEEP_ANYWAY = ("_pkg.sv", "if_xif.sv")
# X-HEEP's clock cell wrappers (``pulp_clock_*``, ``cluster_clock_*``) over the
# ``tc_clk_*`` cells ChipLeap writes for IHP; X-HEEP's own ASIC flow adds them by hand.
CLOCK_WRAPPERS = (
    "hw/vendor/pulp_platform/tech_cells_generic/src/deprecated/pulp_clk_cells.sv",
    "hw/vendor/pulp_platform/tech_cells_generic/src/deprecated/cluster_clk_cells.sv",
)
DEFINES = ("SYNTHESIS", "COMMON_CELLS_ASSERTS_OFF", "ASSERTS_OFF")
SLANG_ARGUMENTS = ("--allow-use-before-declare", "--extern-modules", "--ignore-assertions")
# X-HEEP's templates for mcu-gen: as its Makefile finds them.
_SKIP_TEMPLATES = ("hw/vendor", "util", "test")
_KEEP_TEMPLATES = ("hw/vendor/xheep", "util/profile")
# What ChipLeap copies of the X-HEEP checkout: no documentation, no history.
_SKIP_COPY = {".git", "docs"}


class SocError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sources:
    """What LibreLane reads, as paths relative to the step's directory."""

    files: tuple[str, ...]
    includes: tuple[str, ...]
    bank_plans: tuple[soc.BankPlan, ...]
    accel: banks.Banks


@dataclass(frozen=True)
class SocResult:
    pnr: pnr.PnrResult
    macros: tuple[tuple[str, str], ...]
    model_cell_um2: float
    model_macro_um2: float
    notes: tuple[str, ...] = field(default_factory=tuple)


def copy_xheep(checkout: Path, dest: Path) -> None:
    """X-HEEP's tree without its history and documentation. A symlink in it is copied as
    the file it leads to, which must be inside the checkout."""
    root = checkout.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel = here.relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not (rel == Path(".") and d in _SKIP_COPY))
        out = dest / rel
        out.mkdir(parents=True, exist_ok=True)
        for name in [*filenames, *(d for d in list(dirnames) if (here / d).is_symlink())]:
            src = here / name
            target = src.resolve()
            if not target.is_relative_to(root):
                raise SocError(f"X-HEEP's {src.relative_to(root)} leads outside the checkout")
            if src.is_symlink() and target.is_dir():
                dirnames.remove(name)
                shutil.copytree(target, out / name, symlinks=False)
            elif target.is_file():
                shutil.copyfile(target, out / name)


def templates(tree: Path) -> list[str]:
    """X-HEEP's ``.tpl`` files mcu-gen renders, as its Makefile finds them."""
    out = []
    for tpl in sorted(tree.rglob("*.tpl")):
        rel = tpl.relative_to(tree).as_posix()
        skip = any(rel.startswith(p + "/") for p in _SKIP_TEMPLATES)
        keep = any(rel.startswith(p + "/") for p in _KEEP_TEMPLATES)
        if not skip or keep:
            out.append(rel)
    return out


def _run(cmd: list[str], cwd: Path, env: dict[str, str], what: str) -> str:
    try:
        out = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=SETUP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SocError(f"{what} did not finish in {SETUP_TIMEOUT_S} s") from None
    except OSError as exc:
        raise SocError(f"{what} could not start: {exc}") from None
    log = out.stdout + out.stderr
    if out.returncode != 0:
        tail = "\n".join(log.strip().splitlines()[-15:])
        raise SocError(f"{what} failed (exit {out.returncode}):\n{tail}")
    return log


def _tools_env(fs: Tool, gcc: Tool, tree: Path) -> dict[str, str]:
    """The environment X-HEEP's generators expect: FuseSoC's Python first on PATH (they
    start ``python``), the register tools of the copy, and a RISC-V GCC for the boot ROM,
    for RV32 whatever its default."""
    assert fs.path is not None and gcc.path is not None
    env = clean_env([Path(fs.path).parent])
    prefix = gcc.path[: -len("gcc")]
    env |= {
        "REGTOOL": str(
            tree / "hw/vendor/pulp_platform/register_interface/vendor/lowrisc_opentitan"
            "/util/regtool.py"
        ),
        "PERIPH_STRUCTS_GEN": str(tree / "util/periph_structs_gen/periph_structs_gen.py"),
        "TEMPLATE_FILE": str(tree / "util/periph_structs_gen/periph_structs.tpl"),
        "GCC": f"{gcc.path} -march=rv32imc_zicsr -mabi=ilp32",
        "OBJCOPY": prefix + "objcopy",
        "OBJDUMP": prefix + "objdump",
    }
    return env


def render(tree: Path, config: Path, env: dict[str, str]) -> None:
    """X-HEEP's mcu-gen on its own copy, for ``config``."""
    _run(
        [
            str(Path(env["PATH"].split(os.pathsep)[0]) / "python"),
            "util/xheep_gen/mcu_gen.py",
            "--config",
            str(config),
            "--pads_cfg",
            "configs/pad_cfg.py",
            "--outtpl",
            " ".join(templates(tree)),
        ],
        tree,
        env,
        "X-HEEP's mcu-gen",
    )


CORE_FILE = f"""CAPI=2:
# Written by ChipLeap: X-HEEP's MCU for its ASIC synthesis target.
name: "{CORE}"
filesets:
  rtl:
    depend:
      - openhwgroup.org:systems:core-v-mini-mcu
targets:
  {TARGET}:
    filesets:
      - rtl
    toplevel: {soc.MCU}
"""


def edam(work: Path, fs: Tool, env: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    """FuseSoC's description of X-HEEP's sources (its EDAM file) and the directory its
    paths are relative to."""
    assert fs.path is not None
    builddir.write_text(work, "cores/chipleap_soc.core", CORE_FILE)
    _run(
        [
            fs.path,
            "--cores-root",
            "x-heep",
            "--cores-root",
            "cores",
            "run",
            "--setup",
            "--build-root",
            "fusesoc",
            f"--target={TARGET}",
            "--tool=verilator",
            CORE,
        ],
        work,
        env,
        "FuseSoC",
    )
    found = sorted((work / "fusesoc").glob(f"{TARGET}-verilator/*.eda.yml"))
    if len(found) != 1:
        raise SocError("FuseSoC wrote no EDAM file for X-HEEP")
    if found[0].stat().st_size > EDAM_MAX:
        raise SocError(f"FuseSoC's EDAM file is larger than {EDAM_MAX >> 20} MiB")
    data = yaml.safe_load(found[0].read_text())
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise SocError("FuseSoC's EDAM file has no list of files")
    return found[0].parent, data


def x_heep_files(
    data: dict[str, Any], base: Path, work: Path, cpu: str
) -> tuple[list[str], list[str]]:
    """X-HEEP's sources and include directories from FuseSoC's EDAM ``data`` (paths
    relative to ``base``), as paths relative to ``work``, which each must stay in."""
    drop = tuple(p for name, cores in CPU_CORES.items() if name != cpu for p in cores)
    files: list[str] = []
    includes: list[str] = []
    root = work.resolve()

    def rel(name: str) -> str:
        path = (base / name).resolve()
        if not path.is_relative_to(root):
            raise SocError(f"FuseSoC listed a file outside the step's directory: {name[:120]}")
        return path.relative_to(root).as_posix()

    for f in data["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            raise SocError("FuseSoC's EDAM file lists a file without a name")
        core = str(f.get("core", ""))
        name = f["name"]
        if core.startswith(drop) and not name.endswith(KEEP_ANYWAY):
            continue
        if f.get("file_type") not in ("systemVerilogSource", "verilogSource"):
            continue
        if f.get("is_include_file"):
            inc = f.get("include_path") or str(PurePosixPath(name).parent)
            if rel(inc) not in includes:
                includes.append(rel(inc))
        else:
            files.append(rel(name))
    return files, includes


def generate(
    design: Design, tree: Path, work: Path, srams: Iterable[sram.Sram]
) -> tuple[list[str], tuple[soc.BankPlan, ...], banks.Banks]:
    """ChipLeap's sources in ``work/src``: the clock cells, X-HEEP's banks, the
    accelerator with its buffers in macros and the top, after X-HEEP's own."""
    if design.accel is None:
        raise SocError("the design has no accelerator")
    reason = banks.unsupported(design.accel)
    if reason is not None:
        raise SocError(f"the accelerator's buffers cannot be SRAM macros: {reason}")
    srams = list(srams)
    mcu = tree / "hw/core-v-mini-mcu"
    ports = soc.mcu_ports((mcu / "core_v_mini_mcu.sv").read_text())
    depths = [w for _, w in soc.bank_words((mcu / "memory_subsystem.sv").read_text())]
    plans = tuple(soc.plan_banks(depths, srams))
    accel_banks = banks.plan(design.accel, srams)
    src = builddir.work_dir(work, "src", empty=True)
    out = {
        "chipleap_clock_cells.sv": soc.clock_cells_sv(),
        "sram_wrapper.sv": soc.sram_wrapper_sv(plans),
        "chipleap_accel.sv": accel_sv(),
        f"{banks.TOP}.v": banks.top_sv(accel_banks),
        f"{soc.TOP}.sv": soc.top_sv(ports, design.accel.ports),
    }
    for name, text in out.items():
        builddir.write_text(src, name, text)
    wrappers = [f"x-heep/{w}" for w in CLOCK_WRAPPERS]
    order = ["chipleap_clock_cells.sv"]
    tail = ["sram_wrapper.sv", "chipleap_accel.sv", f"{banks.TOP}.v", f"{soc.TOP}.sv"]
    return (
        [f"src/{order[0]}", *wrappers, *(f"src/{n}" for n in tail)],
        plans,
        accel_banks,
    )


def bank_instances(tree: Path, plans: Iterable[soc.BankPlan]) -> list[tuple[str, str]]:
    """Every macro of X-HEEP's banks as (instance below the top, cell)."""
    by_depth = {p.words: p for p in plans}
    text = (tree / "hw/core-v-mini-mcu/memory_subsystem.sv").read_text()
    out: list[tuple[str, str]] = []
    for inst, words in soc.bank_words(text):
        path = f"{soc.MCU_INST}.memory_subsystem_i.{inst}"
        out += soc.bank_instances(by_depth[words], path)
    return out


def model_areas_um2(design: Design) -> tuple[float, float]:
    """The model's standard cells and macros for what this block holds: the CPU, X-HEEP's
    logic and bus and the accelerator's logic, before the model's placement utilisation;
    X-HEEP's banks and the accelerator's buffers. The memory interface is not in it."""
    from chipleap.hw.cpu import get_cpu
    from chipleap.model.evaluate import area_breakdown

    ihp = get_tech("ihp-sg13g2")
    area = area_breakdown(design, ihp, get_cpu(design.cpu), None)
    cells = (area["cpu"] + area["soc logic"] + area.get("accelerator logic", 0.0)) * ihp.logic_util
    macros = area["sram"] + area.get("accelerator buffers", 0.0)
    return cells * 1e6, macros * 1e6


def floorplan(
    instances: list[tuple[str, str]], views: dict[str, sram.SramViews], cell_um2: float
) -> place.Floorplan:
    """The macros in a row along the top of the core and one along the bottom, the logic
    between, sized for ``cell_um2`` of standard cells (``chipleap.macro.place.rows``)."""
    return place.rows(
        [
            place.Macro(inst, views[cell].view.width, views[cell].view.height)
            for inst, cell in instances
        ],
        cell_um2,
    )


SDC = "chipleap_soc.sdc"
# The step LibreLane's Classic flow runs after synthesis: the layout run resumes there.
AFTER_SYNTHESIS = "Checker.YosysUnmappedCells"


@dataclass(frozen=True)
class Prepared:
    """What the LibreLane runs read, in the step's directory."""

    sources: Sources
    instances: list[tuple[str, str]]  # every macro, as (instance, cell)
    views: dict[str, sram.SramViews]


def config(sources: Sources, clock_mhz: float) -> dict[str, Any]:
    """LibreLane's configuration for the SoC without its floorplan: slang reads the
    SystemVerilog, with the macros' and IHP's clock gate's black boxes that Yosys holds
    (``--extern-modules``) and without X-HEEP's assertions; LibreLane's Verilator lint is
    off, since X-HEEP is linted with its own waivers; the constraints are ChipLeap's
    (``chipleap.gen.soc.sdc``), and the signoff is the accelerator's."""
    return {
        **pnr.MACRO_REPAIR,
        "DESIGN_NAME": soc.TOP,
        "VERILOG_FILES": [f"dir::{f}" for f in sources.files],
        "VERILOG_INCLUDE_DIRS": [f"dir::{d}" for d in sources.includes],
        "VERILOG_DEFINES": list(DEFINES),
        "USE_SLANG": True,
        "SLANG_ARGUMENTS": list(SLANG_ARGUMENTS),
        "RUN_LINTER": False,
        "CLOCK_PORT": "clk_i",
        "CLOCK_PERIOD": round(1000.0 / clock_mhz, 3),
        "PNR_SDC_FILE": f"dir::{SDC}",
        "SIGNOFF_SDC_FILE": f"dir::{SDC}",
        "PL_TARGET_DENSITY_PCT": 55,
        "PDN_CFG": "dir::pdn.tcl",
        "FP_MACRO_HORIZONTAL_HALO": place.HALO_UM,
        "FP_MACRO_VERTICAL_HALO": place.HALO_UM,
        **pnr.signoff(),
    }


def with_floorplan(
    cfg: dict[str, Any], prep: Prepared, cell_um2: float, work: Path
) -> tuple[dict[str, Any], place.Floorplan]:
    """``cfg`` with a floorplan for ``cell_um2`` of standard cells: the die, the macros
    where ChipLeap puts them and their power grid (``pdn.tcl``)."""
    fp = floorplan(prep.instances, prep.views, cell_um2)
    by_inst = dict(prep.instances)
    placements = [
        grid.Placement(p.instance, prep.views[by_inst[p.instance]].view, p.x, p.y, p.orient)
        for p in fp.macros
    ]
    builddir.write_text(work, "pdn.tcl", grid.core_script(placements, fp.core[1]))
    macros, _ = sram.librelane_macros(
        prep.views,
        [(p.instance, by_inst[p.instance], p.x, p.y, p.orient) for p in fp.macros],
        work,
    )
    return {
        **cfg,
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, *fp.die],
        "CORE_AREA": list(fp.core),
        "MACROS": macros,
    }, fp


def prepare(
    design: Design, config_text: str, checkout: Path, work: Path, pdk: Path
) -> tuple[dict[str, Any], Prepared]:
    """Everything the LibreLane runs read, in ``work``: X-HEEP rendered for the design,
    its sources from FuseSoC, ChipLeap's sources and constraints, the macros' views. The
    configuration without its floorplan, and what was prepared."""
    if design.platform != "xheep":
        raise SocError("not an X-HEEP design (standalone AXI4 IP)")
    fs, gcc = fusesoc(), riscv_gcc()
    if fs.path is None:
        raise SocError(fs.missing("FuseSoC (scripts/install_fusesoc.sh)"))
    if gcc.path is None or not gcc.path.endswith("gcc"):
        raise SocError(gcc.missing("a RISC-V GCC (for X-HEEP's boot ROM)"))
    srams = sram.catalog(pdk)
    if not srams:
        raise SocError(f"the PDK at {pdk} has no single-port SRAM macros")
    tree = builddir.work_dir(work, "x-heep", empty=True)
    copy_xheep(checkout, tree)
    cfg = builddir.write_text(work, f"xheep_cfg/{design.name}.py", config_text)
    env = _tools_env(fs, gcc, tree)
    render(tree, cfg, env)
    builddir.work_dir(work, "fusesoc", empty=True)
    base, data = edam(work, fs, env)
    files, includes = x_heep_files(data, base, work, design.cpu)
    ours, plans, accel_banks = generate(design, tree, work, srams)
    sources = Sources(tuple(files + ours), tuple(includes), plans, accel_banks)
    ports = soc.mcu_ports((tree / "hw/core-v-mini-mcu/core_v_mini_mcu.sv").read_text())
    builddir.write_text(work, SDC, soc.sdc(ports, round(1000.0 / design.freq_mhz, 3)))
    instances = bank_instances(tree, plans) + [
        (f"{soc.ACCEL_INST}.{inst}", cell) for inst, cell in accel_banks.instances()
    ]
    views = {cell: sram.views(pdk, cell, work) for cell in sorted({c for _, c in instances})}
    return config(sources, design.freq_mhz), Prepared(sources, instances, views)


def place_and_route(
    design: Design, config_text: str, checkout: Path, work: Path, pdk: Path
) -> SocResult:
    """Synthesis first, so the floorplan is sized for the standard cells it made (X-HEEP's
    peripherals are most of them, which the model's estimate does not follow), then the
    layout, in the same LibreLane run."""
    cfg, prep = prepare(design, config_text, checkout, work, pdk)
    model_cells, model_macros = model_areas_um2(design)
    first, _ = with_floorplan(cfg, prep, model_cells, work)
    pnr.run_librelane(work, first, to="Yosys.Synthesis")
    run = work / "runs" / "chipleap"
    synth = pnr.synth_area(run)
    if synth <= 0:
        raise SocError("LibreLane's synthesis reported no standard-cell area")
    full, _ = with_floorplan(cfg, prep, synth, work)
    metrics, gds = pnr.run_librelane(work, full, resume_from=AFTER_SYNTHESIS)
    from chipleap.rtl.pnr import check_merged

    check_merged(run, work, soc.TOP)
    area = {s.cell: s.area_um2 for s in sram.catalog(pdk)}
    result = replace(
        pnr.parse(metrics, design.freq_mhz, gds),
        synth_cell_um2=synth,
        macros=tuple(prep.instances),
        macro_um2=sum(area[cell] for _, cell in prep.instances),
    )
    return SocResult(result, tuple(prep.instances), model_cells, model_macros)
