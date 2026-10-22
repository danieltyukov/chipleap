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

import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from chipleap.chip import merge
from chipleap.gen import banks, soc
from chipleap.gen.rtl import accel_sv
from chipleap.hw.design import Design
from chipleap.hw.memory import NONE_KEY
from chipleap.hw.tech import get_tech
from chipleap.macro import grid, layer, place, sram
from chipleap.macro.layer import MacroView
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


# The estimated setup slack the repairs keep after CTS and after global routing (``config``).
SETUP_MARGINS_NS = (6.0, 5.0)
# The hold slack the repair after global routing keeps, in ns. The worst hold endpoints
# after routing are DMA0's registers, whose clock gate hangs on the clock tree's long
# first-level net with the accelerator's macros, and routing moves them by more than the
# margin: from one 36 MHz placement they came out at -0.047 ns with a 0.1 ns margin and
# -0.060 ns with 0.2 ns. CHIPLEAP_SOC_HOLD_MARGIN sets another margin for a trial run.
HOLD_MARGIN_NS = 0.2
HOLD_MARGIN_ENV = "CHIPLEAP_SOC_HOLD_MARGIN"


def hold_margin_ns() -> float:
    """The hold margin after global routing: ``HOLD_MARGIN_ENV`` when set, a number of ns
    from 0 to 1, else ``HOLD_MARGIN_NS``."""
    raw = os.environ.get(HOLD_MARGIN_ENV, "").strip()
    if not raw:
        return HOLD_MARGIN_NS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not 0.0 <= value <= 1.0:
        raise SocError(f"{HOLD_MARGIN_ENV} must be a number of ns from 0 to 1, not {raw[:20]!r}")
    return value


# The step LibreLane runs the clock tree in, and the one inserted after it.
CTS = "OpenROAD.CTS"
ECO_BUFFERS = "Odb.InsertECOBuffers"
# IHP's SRAM macros take their clock on A_CLK.
SRAM_CLOCK_PIN = "A_CLK"
# The buffer's input is about as small as the pin it drives (3.5 fF against A_CLK's
# 3.8 fF), so the clock net it joins carries no more than it did.
CLOCK_PIN_BUFFER = "sg13g2_buf_4"
# How far beyond its halo, from the macro's edge where the pin is, the buffer is put; the
# legaliser moves it to the nearest free site.
CLOCK_PIN_GAP_UM = 5.0


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
        # IHP's DRC deck runs after LibreLane, in parallel parts (``pnr.with_ihp_drc``):
        # LibreLane's KLayout DRC of the SoC took 3 h 48 min of a 7 h run, at 1.25 CPUs
        # on average of its 8 threads.
        "RUN_KLAYOUT_DRC": False,
        "FP_MACRO_HORIZONTAL_HALO": place.HALO_UM,
        "FP_MACRO_VERTICAL_HALO": place.HALO_UM,
        # The repairs keep SETUP_MARGINS_NS of estimated setup slack, after CTS and after
        # global routing, where the accelerator keeps 0.8 and 1.6 ns. Hold repair puts its
        # delay cells wherever the estimate shows setup slack, and on the SoC's long paths
        # the estimate after global routing shows 4 to 5 ns more than routing leaves (36 MHz:
        # +4.56 ns there, -0.22 ns after routing). From one 36 MHz placement, routed:
        # 0.8 and 1.6 ns left +0.259 ns of setup and -0.060 ns of hold, 6 and 5 ns +0.349
        # and +0.047 ns.
        **pnr.signoff(*SETUP_MARGINS_NS),
        # The SoC's wires are millimetres long, and the extraction after detailed routing
        # puts 14 to 20 % more capacitance on them than the global router's estimate that
        # the repairs work against: at 50 MHz a 0.0576 pF net (LibreLane's 10 % margin
        # under an SRAM output's 0.064 pF) came out 0.0657 pF. The repair after global
        # routing keeps that much margin on slew and capacitance.
        "GRT_DESIGN_REPAIR_MAX_CAP_PCT": 25,
        "GRT_DESIGN_REPAIR_MAX_SLEW_PCT": 25,
        # Hold after global routing (``hold_margin_ns``).
        "GRT_RESIZER_HOLD_SLACK_MARGIN": hold_margin_ns(),
        # The clock tree hangs the accelerator's macros, at the top and bottom of the core,
        # and X-HEEP's clock gates on one net of its first level, millimetres of wire. IHP's
        # strongest buffer drives the tree's long nets, which alone left the macros' clock
        # pins at 0.66 to 0.76 ns after routing, against their 0.595 ns: a buffer beside
        # each macro drives its clock pin, inserted right after CTS (``clock_pin_buffers``),
        # so the repairs after CTS see it.
        "CTS_CLK_BUFFERS": [f"sg13g2_buf_{n}" for n in (16, 8, 4, 2, 1)],
        "CTS_ROOT_BUFFER": "sg13g2_buf_16",
        "meta": {
            "version": 2,
            "flow": "Classic",
            "substituting_steps": {f"+{CTS}": ECO_BUFFERS},
        },
    }


def _pin_at(p: place.Placed, view: MacroView, pin: str) -> tuple[float, float] | None:
    """The centre of ``pin``'s first shape on the placed macro ``p``, in the core's um."""
    shapes = [rect for mp in view.pins if mp.name == pin for _, rect in mp.shapes]
    if not shapes:
        return None
    x0, y0, x1, y1 = shapes[0]
    x, y = (x0 + x1) / 2, (y0 + y1) / 2
    if p.orient in ("S", "FN"):
        x = view.width - x
    if p.orient in ("S", "FS"):
        y = view.height - y
    return p.x + x, p.y + y


# IHP's integrated clock gate, which X-HEEP's clock gates are (``chipleap.gen.soc``), and
# the buffer after each.
CLOCK_GATE = "sg13g2_slgcp_1"
GATE_BUFFER = "sg13g2_buf_16"
_DEF_UNITS_RE = re.compile(r"^UNITS DISTANCE MICRONS (\d+) ;", re.M)
_DEF_GATE_RE = re.compile(
    rf"^\s*-\s+(\S+)\s+{CLOCK_GATE}\s.*?\+\s+(?:PLACED|FIXED)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)",
    re.M,
)


def clock_gates(def_text: str) -> list[tuple[str, float, float]]:
    """Each clock gate (``CLOCK_GATE``) of a placed DEF, by its instance name as written in
    the netlist (DEF's escapes removed), with its place in um."""
    units = _DEF_UNITS_RE.search(def_text)
    if units is None or int(units.group(1)) <= 0:
        raise SocError("the DEF after the clock tree gives no database units")
    dbu = int(units.group(1))
    return [
        (re.sub(r"\\(.)", r"\1", name), int(x) / dbu, int(y) / dbu)
        for name, x, y in _DEF_GATE_RE.findall(def_text)
    ]


def gate_buffers(gates: Iterable[tuple[str, float, float]]) -> list[dict[str, Any]]:
    """LibreLane's ``INSERT_ECO_BUFFERS`` for a buffer after each clock gate's output,
    placed where the gate is (the legaliser moves it beside it).

    The integrated clock gate is IHP's one, a weak cell, and drives the root of its tree
    over the wire to where CTS put that root (0.4 to 0.8 mm on the SmolLM2 SoC): the gated
    trees reach their flip-flops 0.6 to 0.7 ns after the ungated ones at the slow corner,
    and DMA0's registers were the worst hold endpoints in every layout. From one 36 MHz
    placement, routed, a buffer after each gate took setup from +0.259 to +0.490 ns at the
    slow corner and hold from -0.060 to +0.144 ns at the fast one. Placement decides where
    the gates are, so the layout stops after the clock tree to read them (``layout``)."""
    return [
        {"target": f"{name}/GCLK", "buffer": GATE_BUFFER, "placement": [round(x, 3), round(y, 3)]}
        for name, x, y in gates
    ]


def clock_pin_buffers(fp: place.Floorplan, views: Mapping[str, MacroView]) -> list[dict[str, Any]]:
    """LibreLane's ``INSERT_ECO_BUFFERS``: a buffer before the clock pin of each of the
    accelerator's macros (``views`` by instance), placed just off the macro's edge where
    the pin is, in the band of logic.

    OpenROAD's CTS builds a tree of its own for the sinks of clk_i that are not
    registers, the accelerator's macros and X-HEEP's clock gates (nine on the SmolLM2 SoC),
    and stops it at its first level, each half holding fewer sinks than it gives one
    buffer (eight). So one buffer at mid-die drove the macros at the core's corners and a
    clock gate over 4.7 mm of wire, and nothing after CTS buffers a clock net: after
    routing their clock pins saw 0.66 to 0.76 ns at the slow corner, against the macros'
    0.595 ns. No setting of LibreLane's CTS step changed that tree (the macros' cluster
    size and diameter, the distance between buffers, the maximum slew, the registers'
    cluster size). X-HEEP's banks are clocked through its memory subsystem's gate, under a
    tree of their own."""
    out: list[dict[str, Any]] = []
    for p in fp.macros:
        if not p.instance.startswith(f"{soc.ACCEL_INST}."):
            continue
        view = views[p.instance]
        at = _pin_at(p, view, SRAM_CLOCK_PIN)
        if at is None:
            raise SocError(f"{view.cell} has no {SRAM_CLOCK_PIN} pin")
        out.append(_beside(p, view, SRAM_CLOCK_PIN, at, CLOCK_PIN_BUFFER))
    return out


def _beside(
    p: place.Placed, view: MacroView, pin: str, at: tuple[float, float], buffer: str
) -> dict[str, Any]:
    """An ``INSERT_ECO_BUFFERS`` entry for ``buffer`` at ``pin`` of the placed macro ``p``
    (the pin at ``at``), just past the halo off the macro's edge where the pin is."""
    x, y = at
    off = place.HALO_UM + CLOCK_PIN_GAP_UM
    y = p.y - off if y - p.y < view.height / 2 else p.y + view.height + off
    return {
        "target": f"{p.instance}/{pin}",
        "buffer": buffer,
        "placement": [round(x, 3), round(y, 3)],
    }


# What drives a bank's output wire from beside the macro (0.6 pF limit, about ten times
# the most a routed bank output carried).
BANK_OUTPUT_BUFFER = "sg13g2_buf_2"
BANK_OUTPUT_PIN = "A_DOUT["


def bank_output_buffers(
    fp: place.Floorplan, views: Mapping[str, MacroView]
) -> list[dict[str, Any]]:
    """LibreLane's ``INSERT_ECO_BUFFERS``: a buffer after each output bit of X-HEEP's bank
    macros (``views`` by instance), placed just off the macro's edge beside the pin.

    A bank's 32 output bits run side by side on Metal3 for about 230 um, and the global
    router's estimate, which the repairs work against, has the layers' capacitance from
    the LEF without the coupling between neighbours: on the SmolLM2 SoC routed at 36 MHz,
    the 256 bank outputs carried a median 1.19 times and at most 2.39 times their
    estimate, over the same length of wire (bank_m6's A_DOUT[4]: 0.0288 pF estimated,
    0.0687 pF routed, against the macro's 0.064 pF). A design-wide limit would buffer
    every heavy net of X-HEEP, and OpenSTA does not apply a limit set on the macros' pins
    to them. With a buffer beside the pin, the macro drives a few um and the buffer the
    wire."""
    out: list[dict[str, Any]] = []
    for p in fp.macros:
        if not p.instance.startswith(f"{soc.MCU_INST}."):
            continue
        view = views[p.instance]
        for mp in view.pins:
            if not mp.name.startswith(BANK_OUTPUT_PIN):
                continue
            at = _pin_at(p, view, mp.name)
            if at is not None:
                out.append(_beside(p, view, mp.name, at, BANK_OUTPUT_BUFFER))
    if any(p.instance.startswith(f"{soc.MCU_INST}.") for p in fp.macros) and not out:
        raise SocError(f"X-HEEP's bank macros have no {BANK_OUTPUT_PIN}...] pins")
    return out


def with_floorplan(
    cfg: dict[str, Any], prep: Prepared, cell_um2: float, work: Path
) -> tuple[dict[str, Any], place.Floorplan]:
    """``cfg`` with a floorplan for ``cell_um2`` of standard cells: the die, the macros
    where ChipLeap puts them, their power grid (``pdn.tcl``) and the buffers beside the
    accelerator's macros' clock pins and X-HEEP's banks' outputs (``clock_pin_buffers``,
    ``bank_output_buffers``)."""
    fp = floorplan(prep.instances, prep.views, cell_um2)
    by_inst = dict(prep.instances)
    by_instance = {i: prep.views[c].view for i, c in prep.instances}
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
        "INSERT_ECO_BUFFERS": [
            *clock_pin_buffers(fp, by_instance),
            *bank_output_buffers(fp, by_instance),
        ],
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
    # The macros' cells go into one layout and one netlist with the rest; a clash among
    # them would stop the run only after its layout, hours from now.
    try:
        merge.check_macros(v.view for v in views.values())
    except merge.MergeError as exc:
        raise SocError(f"merged names: {exc}") from None
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
    first, _ = with_floorplan(cfg, prep, model_areas_um2(design)[0], work)
    pnr.run_librelane(work, first, to="Yosys.Synthesis", timeout_s=TIMEOUT_S)
    run = work / "runs" / "chipleap"
    if problems := netlist_problems(run):
        raise SocError(f"Yosys's check of the synthesized netlist found {problems}")
    synth = pnr.synth_area(run)
    if synth <= 0:
        raise SocError("LibreLane's synthesis reported no standard-cell area")
    full, _ = with_floorplan(cfg, prep, synth, work)
    metrics, gds = layout(work, full)
    return routed(design, work, pdk, clock, prep.instances, synth, metrics, gds)


def layout(work: Path, full: dict[str, Any]) -> tuple[dict[str, Any], Path | None]:
    """The layout, resumed after synthesis, in two parts: up to the clock tree, then the
    rest from the buffers inserted after it (``ECO_BUFFERS``), with a buffer after each
    clock gate where placement put the gate (``gate_buffers``) besides those beside the
    accelerator's macros. The final metrics and GDS."""
    # Without the second antenna repair (``late_antenna_repair``): the SoC's evidence was
    # routed without it, and it is turned on once a SoC run with it has been checked.
    pnr.run_librelane(work, full, resume_from=AFTER_SYNTHESIS, to=CTS, timeout_s=TIMEOUT_S)
    run = work / "runs" / "chipleap"
    state = pnr.step_state(run, CTS)
    if state is None:
        raise SocError("LibreLane left no state after the clock tree")
    try:
        def_path = Path(json.loads(state.read_text()).get("def") or "")
    except (OSError, ValueError, AttributeError) as exc:
        raise SocError(f"LibreLane's state after the clock tree: {exc}") from None
    if not def_path.is_file() or not def_path.resolve().is_relative_to(run.resolve()):
        raise SocError("LibreLane's state after the clock tree names no DEF in its run")
    gates = clock_gates(def_path.read_text(errors="replace"))
    full = {**full, "INSERT_ECO_BUFFERS": [*full["INSERT_ECO_BUFFERS"], *gate_buffers(gates)]}
    return pnr.run_librelane(work, full, resume_from=ECO_BUFFERS, timeout_s=TIMEOUT_S)


def routed(
    design: Design,
    work: Path,
    pdk: Path,
    clock: float,
    instances: Iterable[tuple[str, str]],
    synth_um2: float,
    metrics: dict[str, Any],
    gds: Path | None,
) -> SocResult:
    """What the SoC LibreLane laid out in ``work`` measures, from its final metrics and
    GDS: its names merged once (``chipleap.rtl.pnr.check_merged``), the PDK
    macros' own pin markers set apart, IHP's DRC deck in parallel parts
    (``chipleap.verify.pnr.with_ihp_drc``) and its antenna deck on the GDS, as every
    layout ChipLeap signs off gets it (``chipleap.verify.pnr.with_ihp_antenna``), and the
    measures against the model's."""
    from chipleap.rtl.pnr import check_merged

    run = work / "runs" / "chipleap"
    check_merged(run, work, soc.TOP)
    placed = tuple(instances)
    area = {s.cell: s.area_um2 for s in sram.catalog(pdk)}
    held = pdk_cells(
        (
            (cell, layer.pdk_macro_cells(pdk, cell, sram.LIBRARY))
            for cell in sorted({c for _, c in placed})
        ),
        (soc.TOP,),
    )
    result = replace(
        pnr.parse_run(metrics, clock, gds, run, held),
        synth_cell_um2=synth_um2,
        macros=placed,
        macro_um2=sum(area[cell] for _, cell in placed),
    )
    result = pnr.with_ihp_drc(result, pdk, soc.TOP, work, held)
    result = pnr.with_ihp_antenna(result, pdk, soc.TOP, work)
    return SocResult(result, placed, *model_areas_um2(design))
