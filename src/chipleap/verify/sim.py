"""Cycle-accurate simulation of the generated accelerator with Verilator.

Each test case is a random matrix multiply. The testbench checks every output against a
golden model and reports the cycle count, which is compared with the analytical model's
prediction for the same job under the same memory behaviour.

``platform="xheep"`` simulates the core on its OBI and register interfaces;
``platform="standalone"`` simulates it behind the AXI4 wrapper, programmed over
AXI4-Lite against an AXI4 memory model that also checks the protocol. With ``clocks``
(``Clocks``), the X-HEEP interfaces are on one clock and the accelerator on another,
through its crossings (``chipleap_accel_cdc``).

``build_direct`` and ``run_direct`` simulate the accelerator's direct read path to external
memory: the accelerator with ``EXT_DIRECT`` and ``chipleap_mem``, the arbiter that shares
the memory ports with X-HEEP's external slave ports, driven by traffic generators
(``tb_chipleap_mem.cpp``).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from chipleap.gen import banks
from chipleap.gen.rtl import (
    EXT_WINDOW,
    accel_sv,
    axi_sv,
    cdc_sv,
    driver_header,
    mem_sv,
    rtl_params,
    testbench_axi_cpp,
    testbench_cdc_cpp,
    testbench_cpp,
    testbench_mem_cpp,
)
from chipleap.hw.design import Accel
from chipleap.macro.sram import Sram, model
from chipleap.model.accel import Bandwidth, crossed, k_align, plan_tiles, simulate_job
from chipleap.verify.tools import clean_env, verilator


class SimError(RuntimeError):
    pass


CDC_DEPTH = 8  # words of each crossing FIFO in chipleap_accel_cdc (CDC_AW = 3)


@dataclass(frozen=True)
class Clocks:
    """The bus clock and the accelerator's, in picoseconds: their periods and the time of
    the accelerator clock's first rising edge (the bus clock's is at 0)."""

    bus_ps: int
    accel_ps: int
    phase_ps: int = 0

    def args(self) -> list[str]:
        return [str(self.bus_ps), str(self.accel_ps), str(self.phase_ps)]


@dataclass(frozen=True)
class Case:
    m: int
    k: int
    n: int
    fmt: int
    seed: int = 1
    stall_pct: int = 0
    latency: int = 1

    def line(self) -> str:
        return f"{self.m} {self.k} {self.n} {self.fmt} {self.seed} {self.stall_pct} {self.latency}"


@dataclass(frozen=True)
class CaseResult:
    case: Case
    ok: bool
    cycles: int
    predicted: float  # simulate_job, exact schedule
    tm: int
    tn: int
    tk: int
    x_resident: bool
    mismatches: int

    @property
    def error(self) -> float:
        return (self.predicted - self.cycles) / self.cycles if self.cycles else 0.0


def cache_dir() -> Path:
    base = (
        os.environ.get("CHIPLEAP_CACHE")
        or os.environ.get("XDG_CACHE_HOME")
        or str(Path.home() / ".cache")
    )
    return Path(base) / "chipleap"


def build(
    accel: Accel,
    workdir: Path | None = None,
    jobs: int = 0,
    platform: str = "xheep",
    srams: Sequence[Sram] | None = None,
    cdc: bool = False,
) -> Path:
    """Build the Verilator model for ``accel`` (cached by parameters) and return the binary.
    With ``srams``, the buffers are banks of those SRAM macros (``chipleap.gen.banks``),
    each simulated by ChipLeap's model of its ports. With ``cdc``, the accelerator runs on
    a clock of its own behind its crossings (``chipleap_accel_cdc``)."""
    tool = verilator()
    if not tool.found:
        raise SimError("Verilator is not installed; see docs/verification.md")
    p = rtl_params(accel)
    axi = platform == "standalone"
    if cdc and axi:
        raise SimError("the accelerator's own clock is simulated on X-HEEP's interfaces")
    top = "chipleap_accel_axi" if axi else "chipleap_accel_cdc" if cdc else "chipleap_accel"
    params = p.verilator_args()
    macro_rtl: dict[str, str] = {}
    if srams is not None:
        if axi:
            raise SimError("SRAM banks are simulated on X-HEEP's interfaces only")
        plan = banks.plan(accel, srams)
        # Behind the crossings the core keeps as many reads in flight as they hold.
        macro_rtl[f"{banks.TOP}.v"] = banks.top_sv(plan, CDC_DEPTH if cdc else None)
        macro_rtl |= {f"{s.cell}.v": model(s) for s in plan.cells()}
        if not cdc:
            # The generated top fixes the parameters; the class keeps the test bench's name.
            top, params = banks.TOP, ["--prefix", "Vchipleap_accel"]
    tb_name, tb_text = (
        ("tb_chipleap_axi.cpp", testbench_axi_cpp())
        if axi
        else ("tb_chipleap_cdc.cpp", testbench_cdc_cpp())
        if cdc
        else ("tb_chipleap.cpp", testbench_cpp())
    )
    header = driver_header(accel, "sim", platform=platform)
    rtl = {"chipleap_accel.sv": accel_sv()}
    if axi:
        rtl["chipleap_accel_axi.sv"] = axi_sv()
    if cdc:
        rtl["chipleap_accel_cdc.sv"] = cdc_sv(banks.TOP if srams is not None else "chipleap_accel")
    rtl |= macro_rtl
    sources = "".join(rtl.values()) + tb_text + header + " ".join(params)
    digest = hashlib.sha256(sources.encode()).hexdigest()[:16]
    root = workdir or (cache_dir() / f"sim-{digest}")
    binary = root / "obj_dir" / ("Vchipleap_accel" if macro_rtl and not cdc else f"V{top}")
    if binary.exists():
        return binary
    root.mkdir(parents=True, exist_ok=True)
    for fname, text in rtl.items():
        (root / fname).write_text(text)
    (root / tb_name).write_text(tb_text)
    (root / "chipleap_driver.h").write_text(header)
    cmd = [
        str(tool.path),
        "--cc",
        "--exe",
        "--build",
        "-O3",
        "--x-assign",
        "fast",
        "--x-initial",
        "fast",
        "--noassert",
        "-Wno-fatal",
        "-Wno-lint",
        "-Wno-style",
        "--top-module",
        top,
        *params,
        "-CFLAGS",
        f"-O2 -I{root}",
        *rtl,
        tb_name,
        "-j",
        str(jobs or max(1, (os.cpu_count() or 2) // 2)),
    ]
    out = subprocess.run(
        cmd, cwd=root, env=clean_env(exclude=[root]), capture_output=True, text=True, check=False
    )
    if out.returncode != 0 or not binary.exists():
        tail = (out.stdout + out.stderr)[-3000:]
        raise SimError(f"Verilator build failed:\n{tail}")
    return binary


def default_cases(accel: Accel, fmts: tuple[int, ...] | None = None, seed: int = 0) -> list[Case]:
    """A spread of shapes: decode-like GEMV, small GEMMs, K tiling, edge tiles, stalls."""
    rng = random.Random(seed)
    fmts = fmts or tuple(f for f in (8, 4, 2) if f <= accel.w_bits)
    cases = []
    for fmt in fmts:
        al = k_align(accel, fmt)
        shapes = [
            (1, al * 8, accel.rows * 3),
            (3, al * 4, 7),
            (accel.rows + 1, al * 2, accel.cols + 3),
            (2, al * 64, 5),  # forces K tiling for small buffers
            (9, al * 3, 2 * max(accel.rows, accel.cols) + 1),
        ]
        for i, (m, k, n) in enumerate(shapes):
            cases.append(Case(m, k, n, fmt, seed=rng.randrange(1 << 30)))
            if i in (0, 3):
                cases.append(
                    Case(m, k, n, fmt, seed=rng.randrange(1 << 30), stall_pct=30, latency=3)
                )
    return cases


def predicted_cycles(accel: Accel, case: Case, clocks: Clocks | None = None) -> float:
    """The model's prediction for a testbench case. The memory model grants each request
    unless it stalls and answers ``latency`` cycles after the grant, so every read port
    moves one word per cycle at best. With ``clocks``, those are bus cycles, and the count
    is the accelerator's, through its crossings (``chipleap.model.accel.crossed``)."""
    g = 1 - case.stall_pct / 100
    words = accel.ports * g
    bw = Bandwidth(
        x=4 * words,
        w_on=4 * words,
        w_ext=4 * words,
        y=4 * g,
        lat_on=case.latency,
        lat_ext=case.latency,
        grant=g,
        ports=accel.ports,
    )
    if clocks is not None:
        bw = crossed(bw, clocks.bus_ps / clocks.accel_ps, accel.ports)
    return simulate_job(accel, case.m, case.k, case.n, case.fmt, bw, 1.0)


def run(
    accel: Accel,
    cases: list[Case],
    binary: Path | None = None,
    timeout: float = 600,
    platform: str = "xheep",
    srams: Sequence[Sram] | None = None,
    clocks: Clocks | None = None,
) -> list[CaseResult]:
    exe = binary or build(accel, platform=platform, srams=srams, cdc=clocks is not None)
    stdin = "\n".join(c.line() for c in cases) + "\n"
    try:
        out = subprocess.run(
            [str(exe), *(clocks.args() if clocks else [])],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SimError(f"simulation timed out after {timeout:.0f} s") from exc
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    if len(lines) != len(cases):
        raise SimError(
            f"testbench returned {len(lines)} results for {len(cases)} cases:\n"
            + (out.stdout + out.stderr)[-2000:]
        )
    results = []
    for case, ln in zip(cases, lines, strict=True):
        d = json.loads(ln)
        if "error" in d:
            raise SimError(f"case {case}: testbench error {d['error']}")
        plan = plan_tiles(accel, case.m, case.k, case.n, case.fmt)
        if (plan.tm, plan.tn, plan.tk, plan.x_resident) != (
            d["tm"],
            d["tn"],
            d["tk"],
            bool(d["x_resident"]),
        ):
            raise SimError(
                f"driver and model disagree on tiles for {case}: driver "
                f"{d['tm']}x{d['tn']}x{d['tk']}, model {plan.tm}x{plan.tn}x{plan.tk}"
            )
        results.append(
            CaseResult(
                case,
                bool(d["ok"]),
                int(d["cycles"]),
                predicted_cycles(accel, case, clocks),
                d["tm"],
                d["tn"],
                d["tk"],
                bool(d["x_resident"]),
                int(d["mismatches"]),
            )
        )
    return results


# ---------------------------------------------------------------------------------------
# The direct read path to external memory
# ---------------------------------------------------------------------------------------

DIRECT_TOP = "chipleap_accel_mem"
XHEEP_PORTS = 3  # X-HEEP's external slave ports the SoC uses: CPU data, DMA read, DMA write


@dataclass(frozen=True)
class DirectCase:
    """A job on the direct path's testbench: the memory models' stalls and latencies
    (``latency`` on chip in bus cycles, ``ext_latency`` at the memory ports in their clock's
    cycles), how busy X-HEEP's ports keep the memory (``x_pct``, the chance per bus cycle
    that an idle port starts a request) and where the operands live (``place``: bit 0 the
    weights in external memory, bit 1 the activations)."""

    m: int
    k: int
    n: int
    fmt: int
    seed: int = 1
    stall_pct: int = 0
    latency: int = 2
    ext_latency: int = 9
    x_pct: int = 0
    place: int = 1

    def line(self) -> str:
        return (
            f"{self.m} {self.k} {self.n} {self.fmt} {self.seed} {self.stall_pct} "
            f"{self.latency} {self.ext_latency} {self.x_pct} {self.place}"
        )


@dataclass(frozen=True)
class DirectResult:
    case: DirectCase
    ok: bool
    cycles: int
    predicted: float  # simulate_job with the direct path's rates; X-HEEP idle
    mismatches: int
    max_inflight: int  # requests in flight at memory port 0, at most
    x_requests: int
    x_reads: int
    x_bad: int  # X-HEEP reads with wrong data, or its writes lost
    x_max_wait: int  # bus cycles from an X-HEEP request to its grant, at most
    x_max_latency: int  # bus cycles from an X-HEEP request to its response, at most
    obi_bad: int  # memory-port requests that changed or dropped before their grant
    accel_run: int  # grants in a row to the accelerator while X-HEEP waited, at most

    @property
    def error(self) -> float:
        return (self.predicted - self.cycles) / self.cycles if self.cycles else 0.0


def direct_top_sv(accel: Accel, inner: str, cdc: bool, nx: int = XHEEP_PORTS) -> str:
    """``chipleap_accel_mem``: ``inner`` (``chipleap_accel``, ``chipleap_accel_cdc`` or the
    macro build ``chipleap.gen.banks.TOP``) with its direct ports on ``chipleap_mem``, and
    the ports of both, for the testbench."""
    p = rtl_params(accel, direct=True)
    n = p.ports
    params = (
        f" #(\n{p.sv_overrides()}\n  )" if inner != banks.TOP else " #(\n    .EXT_DIRECT(1)\n  )"
    )
    clock = "      .accel_clk_i,\n" if inner == "chipleap_accel_cdc" else ""
    return f"""// Written by ChipLeap's simulation: the accelerator and its memory path, for a test bench.
/* verilator lint_off DECLFILENAME */
module {DIRECT_TOP} (
    input logic clk_i,
    input logic rst_ni,
    input logic accel_clk_i,
    input logic reg_valid_i,
    input logic reg_write_i,
    input logic [31:0] reg_addr_i,
    input logic [31:0] reg_wdata_i,
    input logic [3:0] reg_wstrb_i,
    output logic [31:0] reg_rdata_o,
    output logic reg_error_o,
    output logic reg_ready_o,
    output logic [{n}-1:0] rd_req_o,
    input logic [{n}-1:0] rd_gnt_i,
    output logic [{n}-1:0][31:0] rd_addr_o,
    input logic [{n}-1:0] rd_rvalid_i,
    input logic [{n}-1:0][31:0] rd_rdata_i,
    output logic wr_req_o,
    input logic wr_gnt_i,
    output logic [31:0] wr_addr_o,
    output logic [31:0] wr_wdata_o,
    output logic [3:0] wr_be_o,
    input logic wr_rvalid_i,
    input logic [{nx}-1:0] x_req_i,
    output logic [{nx}-1:0] x_gnt_o,
    input logic [{nx}-1:0] x_we_i,
    input logic [{nx}-1:0][3:0] x_be_i,
    input logic [{nx}-1:0][31:0] x_addr_i,
    input logic [{nx}-1:0][31:0] x_wdata_i,
    output logic [{nx}-1:0] x_rvalid_o,
    output logic [{nx}-1:0][31:0] x_rdata_o,
    output logic [{n}-1:0] mem_req_o,
    input logic [{n}-1:0] mem_gnt_i,
    output logic [{n}-1:0] mem_we_o,
    output logic [{n}-1:0][3:0] mem_be_o,
    output logic [{n}-1:0][31:0] mem_addr_o,
    output logic [{n}-1:0][31:0] mem_wdata_o,
    input logic [{n}-1:0] mem_rvalid_i,
    input logic [{n}-1:0][31:0] mem_rdata_i,
    output logic irq_o
);
  logic [{n}-1:0] a_req, a_gnt, a_rvalid;
  logic [{n}-1:0][31:0] a_addr, a_rdata;
  {inner}{params} u_accel (
      .clk_i,
      .rst_ni,
{clock}      .reg_valid_i,
      .reg_write_i,
      .reg_addr_i,
      .reg_wdata_i,
      .reg_wstrb_i,
      .reg_rdata_o,
      .reg_error_o,
      .reg_ready_o,
      .rd_req_o,
      .rd_gnt_i,
      .rd_addr_o,
      .rd_rvalid_i,
      .rd_rdata_i,
      .wr_req_o,
      .wr_gnt_i,
      .wr_addr_o,
      .wr_wdata_o,
      .wr_be_o,
      .wr_rvalid_i,
      .ext_req_o(a_req),
      .ext_gnt_i(a_gnt),
      .ext_addr_o(a_addr),
      .ext_rvalid_i(a_rvalid),
      .ext_rdata_i(a_rdata),
      .irq_o
  );
  chipleap_mem #(
      .PORTS({n}),
      .NX({nx}),
      .CDC({int(cdc)})
  ) u_mem (
      .clk_i,
      .rst_ni,
      .accel_clk_i,
      .x_req_i,
      .x_gnt_o,
      .x_we_i,
      .x_be_i,
      .x_addr_i,
      .x_wdata_i,
      .x_rvalid_o,
      .x_rdata_o,
      .a_req_i(a_req),
      .a_gnt_o(a_gnt),
      .a_addr_i(a_addr),
      .a_rvalid_o(a_rvalid),
      .a_rdata_o(a_rdata),
      .mem_req_o,
      .mem_gnt_i,
      .mem_we_o,
      .mem_be_o,
      .mem_addr_o,
      .mem_wdata_o,
      .mem_rvalid_i,
      .mem_rdata_i
  );
endmodule
"""


def build_direct(
    accel: Accel,
    workdir: Path | None = None,
    jobs: int = 0,
    srams: Sequence[Sram] | None = None,
    cdc: bool = False,
    nx: int = XHEEP_PORTS,
) -> Path:
    """Build the Verilator model of the direct path's testbench for ``accel`` (cached by
    its sources) and return the binary. ``srams`` and ``cdc`` as for ``build``: the
    buffers in SRAM macros, the accelerator on its own clock (and the memory ports on it,
    X-HEEP's side crossing)."""
    tool = verilator()
    if not tool.found:
        raise SimError("Verilator is not installed; see docs/verification.md")
    rtl: dict[str, str] = {"chipleap_accel.sv": accel_sv()}
    inner = "chipleap_accel"
    if srams is not None:
        plan = banks.plan(accel, srams)
        rtl[f"{banks.TOP}.v"] = banks.top_sv(plan, CDC_DEPTH if cdc else None, direct=True)
        rtl |= {f"{s.cell}.v": model(s) for s in plan.cells()}
        inner = banks.TOP
    if cdc:
        rtl["chipleap_accel_cdc.sv"] = cdc_sv(inner)
        inner = "chipleap_accel_cdc"
    rtl["chipleap_mem.sv"] = mem_sv()
    rtl[f"{DIRECT_TOP}.sv"] = direct_top_sv(accel, inner, cdc, nx)
    tb_name, tb_text = "tb_chipleap_mem.cpp", testbench_mem_cpp()
    header = driver_header(accel, "sim")
    defines = [f"-DCHIPLEAP_MEM_NX={nx}", f"-DCHIPLEAP_EXT_BASE={EXT_WINDOW[0]:#x}u"]
    defines += ["-DCHIPLEAP_MEM_CDC"] if cdc else []
    sources = "".join(rtl.values()) + tb_text + header + " ".join(defines)
    digest = hashlib.sha256(sources.encode()).hexdigest()[:16]
    root = workdir or (cache_dir() / f"sim-direct-{digest}")
    binary = root / "obj_dir" / f"V{DIRECT_TOP}"
    if binary.exists():
        return binary
    root.mkdir(parents=True, exist_ok=True)
    for fname, text in rtl.items():
        (root / fname).write_text(text)
    (root / tb_name).write_text(tb_text)
    (root / "chipleap_driver.h").write_text(header)
    cmd = [
        str(tool.path),
        "--cc",
        "--exe",
        "--build",
        "-O3",
        "--x-assign",
        "fast",
        "--x-initial",
        "fast",
        "--noassert",
        "-Wno-fatal",
        "-Wno-lint",
        "-Wno-style",
        "--top-module",
        DIRECT_TOP,
        "-CFLAGS",
        f"-O2 -I{root} {' '.join(defines)}",
        *rtl,
        tb_name,
        "-j",
        str(jobs or max(1, (os.cpu_count() or 2) // 2)),
    ]
    out = subprocess.run(
        cmd, cwd=root, env=clean_env(exclude=[root]), capture_output=True, text=True, check=False
    )
    if out.returncode != 0 or not binary.exists():
        tail = (out.stdout + out.stderr)[-3000:]
        raise SimError(f"Verilator build failed:\n{tail}")
    return binary


def predicted_direct_cycles(accel: Accel, case: DirectCase, clocks: Clocks | None = None) -> float:
    """The model's count for a direct-path case with X-HEEP's ports idle: weights from
    the direct ports at a word per port per cycle of the memory ports' clock, less the
    stalls, ``ext_latency`` after each grant. With ``clocks`` the memory ports run on the
    accelerator's clock, so those rates and latencies are its own, while the on-chip
    ones cross (``chipleap.model.accel.crossed``). Activations in external memory are
    not modelled (the model keeps them on chip)."""
    g = 1 - case.stall_pct / 100
    words = accel.ports * g
    bw = Bandwidth(
        x=4 * words,
        w_on=4 * words,
        w_ext=4 * words,
        y=4 * g,
        lat_on=case.latency,
        lat_ext=case.ext_latency,
        grant=g,
        ports=accel.ports,
    )
    if clocks is not None:
        bw = replace(
            crossed(bw, clocks.bus_ps / clocks.accel_ps, accel.ports),
            w_ext=bw.w_ext,
            lat_ext=bw.lat_ext,
        )
    w_on = 0.0 if case.place & 1 else 1.0
    return simulate_job(accel, case.m, case.k, case.n, case.fmt, bw, w_on)


def run_direct(
    accel: Accel,
    cases: list[DirectCase],
    binary: Path | None = None,
    timeout: float = 600,
    srams: Sequence[Sram] | None = None,
    clocks: Clocks | None = None,
    nx: int = XHEEP_PORTS,
) -> list[DirectResult]:
    """Run ``cases`` on the direct path's testbench. Without ``clocks`` everything runs on
    one clock of 10 ns."""
    exe = binary or build_direct(accel, srams=srams, cdc=clocks is not None, nx=nx)
    clk = clocks or Clocks(10000, 10000, 0)
    try:
        out = subprocess.run(
            [str(exe), *clk.args()],
            input="\n".join(c.line() for c in cases) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SimError(f"simulation timed out after {timeout:.0f} s") from exc
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    if len(lines) != len(cases):
        raise SimError(
            f"testbench returned {len(lines)} results for {len(cases)} cases:\n"
            + (out.stdout + out.stderr)[-2000:]
        )
    results = []
    for case, ln in zip(cases, lines, strict=True):
        d = json.loads(ln)
        if "error" in d:
            raise SimError(f"case {case}: testbench error {d['error']}")
        results.append(
            DirectResult(
                case,
                bool(d["ok"]),
                int(d["cycles"]),
                predicted_direct_cycles(accel, case, clocks),
                int(d["mismatches"]),
                int(d["max_inflight"]),
                int(d["x_requests"]),
                int(d["x_reads"]),
                int(d["x_bad"]),
                int(d["x_max_wait"]),
                int(d["x_max_latency"]),
                int(d["obi_bad"]),
                int(d["accel_run"]),
            )
        )
    return results
