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
import re
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
from chipleap.hw.memory import NONE_KEY
from chipleap.hw.tech import get_tech
from chipleap.macro import grid, layer, place, sram
from chipleap.rtl import builddir
from chipleap.verify import pnr
from chipleap.verify.drc import pdk_cells
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
        f"{soc.TOP}.sv": soc.top_sv(
            ports, design.accel.ports, ext_memory=design.ext_mem != NONE_KEY
        ),
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


# The utilisation the band between the macro rows is sized for, against the cells
# synthesis made. Place and route adds to them: timing and wire-length repair, and hold
# repair, which on X-HEEP inserts about 40,000 delay cells (its flip-flop FIFOs pass
# data from one flip-flop straight to the next). At the accelerator's 45 % the SoC
# ended at 61 % and global routing overflowed.
UTIL = 0.38


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
        util=UTIL,
    )


_PORT_RE = re.compile(r"^\s*(input|output)\s+(?:\[(\d+):(\d+)\]\s+)?\\?([A-Za-z_][\w$]*)\s*;", re.M)


def port_bits(netlist: str) -> tuple[int, int]:
    """The input and output bits of a synthesized netlist's top module."""
    bits = {"input": 0, "output": 0}
    for direction, hi, lo, _ in _PORT_RE.findall(netlist):
        bits[direction] += abs(int(hi) - int(lo)) + 1 if hi else 1
    return bits["input"], bits["output"]


def pin_order(inputs: int, outputs: int, fp: place.Floorplan, heights: dict[str, float]) -> str:
    """LibreLane's pin order (``IO_PIN_ORDER_CFG``): inputs on the west edge, outputs on
    the east, each spread over the band between the macro rows. A pin on the north or
    south edge would reach the logic over a row of macros, through their power grid and
    the metal they block (on the first layout, 1,431 overflows in global routing, most
    on TopMetal1 and Metal4 and Metal5). Virtual pins (``$n``) fill the edge beside the
    rows, so the real ones fall within the band."""
    _, y0, _, y1 = fp.core
    tops = [p.y for p in fp.macros if p.orient == "N"]
    bottoms = [p.y + heights[p.instance] for p in fp.macros if p.orient != "N"]
    low = (max(bottoms) if bottoms else y0) + place.HALO_UM
    high = (min(tops) if tops else y1) - place.HALO_UM
    die_h = fp.die[1]
    band = max(high - low, 1.0)
    lines = ["# Written by ChipLeap: inputs west, outputs east, within the logic band."]
    for side, n, rx in (
        ("W", inputs, r".*_n?i(\[[0-9]+\])?"),
        ("E", outputs, r".*_n?o(\[[0-9]+\])?"),
    ):
        lines.append(f"#{side}")
        below = round(n * low / band)
        above = round(n * (die_h - high) / band)
        if below:
            lines.append(f"${below}")
        lines.append(rx)
        if above:
            lines.append(f"${above}")
    return "\n".join(lines) + "\n"


# The layout of the SoC takes 6 to 10 hours on 22 cores, most of it in detailed routing.
TIMEOUT_S = 86400
SDC = "chipleap_soc.sdc"
PINS = "pins.cfg"
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
        # LibreLane counts Yosys's check of the RTL, word by word, before synthesis: X-HEEP's
        # flash controller with its cache off leaves its cache state machine's registers
        # undriven and its unused word-level muxes in loops, and its OpenTitan SPI hosts'
        # alert inputs are unconnected. ChipLeap checks the synthesized netlist instead,
        # bit by bit (``netlist_problems``), and stops on any problem there.
        "ERROR_ON_SYNTH_CHECKS": False,
        "CLOCK_PORT": "clk_i",
        "CLOCK_PERIOD": round(1000.0 / clock_mhz, 3),
        "PNR_SDC_FILE": f"dir::{SDC}",
        "SIGNOFF_SDC_FILE": f"dir::{SDC}",
        "PL_TARGET_DENSITY_PCT": 55,
        # No buffer between an output port and its net before the clock tree: X-HEEP's
        # I2S clock leaves on i2s_sck_o, and OpenROAD's CTS skips a clock net that has
        # a buffer on it already, which left the I2S's 337 flip-flops on the gate's
        # unbuffered output, 3 ns late. Repair still sizes and buffers the outputs' nets.
        "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS": False,
        # Antenna repair inserts diodes and reroutes the nets it fixed in the global router,
        # which stops on any overflow: on the SoC, with about 400 long nets to fix between
        # the edges and the logic, it stopped with "routing congestion too high" both after
        # global routing and after detailed routing. Let it go on with a little overflow;
        # the detailed router resolves it, and DRC is the judge.
        "GRT_ALLOW_CONGESTION": True,
        "PDN_CFG": "dir::pdn.tcl",
        "FP_MACRO_HORIZONTAL_HALO": place.HALO_UM,
        "FP_MACRO_VERTICAL_HALO": place.HALO_UM,
        **pnr.signoff(),
        # The SoC's wires are millimetres long, and the extraction after detailed routing
        # puts 14 to 20 % more capacitance on them than the global router's estimate that
        # the repairs work against: at 50 MHz a 0.0576 pF net (LibreLane's 10 % margin
        # under an SRAM output's 0.064 pF) came out 0.0657 pF, and hold at the fast corner
        # lost up to 0.1 ns. The repair after global routing keeps that much margin.
        "GRT_DESIGN_REPAIR_MAX_CAP_PCT": 25,
        "GRT_DESIGN_REPAIR_MAX_SLEW_PCT": 25,
        "GRT_RESIZER_HOLD_SLACK_MARGIN": 0.2,
        # The clock tree hangs the accelerator's macros, at the top and bottom of the core,
        # and X-HEEP's clock gates on one net of its first level, about 3 mm of wire on a
        # buf_8: 0.73 ns at the macros' clock pins after routing, against their 0.595 ns
        # limit. IHP's strongest buffer drives the tree's long nets.
        "CTS_CLK_BUFFERS": [f"sg13g2_buf_{n}" for n in (16, 8, 4, 2, 1)],
        "CTS_ROOT_BUFFER": "sg13g2_buf_16",
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
    out = {
        **cfg,
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, *fp.die],
        "CORE_AREA": list(fp.core),
        "MACROS": macros,
    }
    netlists = sorted((work / "runs" / "chipleap").glob("*-yosys-synthesis/*.nl.v"))
    if netlists:
        heights = {i: prep.views[c].view.height for i, c in prep.instances}
        ins, outs = port_bits(netlists[-1].read_text(errors="replace"))
        builddir.write_text(work, PINS, pin_order(ins, outs, fp, heights))
        out["IO_PIN_ORDER_CFG"] = f"dir::{PINS}"
    return out, fp


def prepare(
    design: Design,
    config_text: str,
    checkout: Path,
    work: Path,
    pdk: Path,
    clock_mhz: float | None = None,
) -> tuple[dict[str, Any], Prepared]:
    """Everything the LibreLane runs read, in ``work``: X-HEEP rendered for the design,
    its sources from FuseSoC, ChipLeap's sources and constraints, the macros' views. The
    configuration without its floorplan, and what was prepared. The clock is the design's
    unless ``clock_mhz`` says otherwise."""
    clock = clock_mhz or design.freq_mhz
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
    builddir.write_text(work, SDC, soc.sdc(ports, round(1000.0 / clock, 3)))
    instances = bank_instances(tree, plans) + [
        (f"{soc.ACCEL_INST}.{inst}", cell) for inst, cell in accel_banks.instances()
    ]
    views = {cell: sram.views(pdk, cell, work) for cell in sorted({c for _, c in instances})}
    return config(sources, clock), Prepared(sources, instances, views)


_CHECK_RE = re.compile(r"Found and reported (\d+) problems")


def netlist_problems(run: Path) -> str:
    """What Yosys's check of the synthesized netlist (``reports/chk.rpt``) found: combinational
    loops and undriven wires, bit by bit; empty when it found nothing. A missing report is
    a problem too."""
    found = sorted(run.glob("*-yosys-synthesis/reports/chk.rpt"))
    if not found:
        return "nothing: LibreLane wrote no check report"
    text = found[-1].read_text(errors="replace")
    m = _CHECK_RE.search(text)
    if m is None:
        return "nothing it reported a count of"
    if int(m.group(1)) == 0:
        return ""
    warnings = [ln.strip() for ln in text.splitlines() if ln.startswith("Warning:")][:5]
    return f"{m.group(1)} problems: " + "; ".join(warnings)


def place_and_route(
    design: Design,
    config_text: str,
    checkout: Path,
    work: Path,
    pdk: Path,
    clock_mhz: float | None = None,
) -> SocResult:
    """Synthesis first, so the floorplan is sized for the standard cells it made (X-HEEP's
    peripherals are most of them, which the model's estimate does not follow), then the
    layout, in the same LibreLane run."""
    clock = clock_mhz or design.freq_mhz
    cfg, prep = prepare(design, config_text, checkout, work, pdk, clock)
    model_cells, model_macros = model_areas_um2(design)
    first, _ = with_floorplan(cfg, prep, model_cells, work)
    pnr.run_librelane(work, first, to="Yosys.Synthesis", timeout_s=TIMEOUT_S)
    run = work / "runs" / "chipleap"
    if problems := netlist_problems(run):
        raise SocError(f"Yosys's check of the synthesized netlist found {problems}")
    synth = pnr.synth_area(run)
    if synth <= 0:
        raise SocError("LibreLane's synthesis reported no standard-cell area")
    full, _ = with_floorplan(cfg, prep, synth, work)
    metrics, gds = pnr.run_librelane(work, full, resume_from=AFTER_SYNTHESIS, timeout_s=TIMEOUT_S)
    from chipleap.rtl.pnr import check_merged

    check_merged(run, work, soc.TOP)
    area = {s.cell: s.area_um2 for s in sram.catalog(pdk)}
    held = pdk_cells(
        ((cell, layer.pdk_macro_cells(pdk, cell, sram.LIBRARY)) for cell in prep.views),
        (soc.TOP,),
    )
    result = replace(
        pnr.parse_run(metrics, clock, gds, run, held),
        synth_cell_um2=synth,
        macros=tuple(prep.instances),
        macro_um2=sum(area[cell] for _, cell in prep.instances),
    )
    return SocResult(result, tuple(prep.instances), model_cells, model_macros)
