"""Cycle-accurate simulation of the generated accelerator with Verilator.

Each test case is a random matrix multiply. The testbench checks every output against a
golden model and reports the cycle count, which is compared with the analytical model's
prediction for the same job under the same memory behaviour.

``platform="xheep"`` simulates the core on its OBI and register interfaces;
``platform="standalone"`` simulates it behind the AXI4 wrapper, programmed over
AXI4-Lite against an AXI4 memory model that also checks the protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chipleap.gen import banks
from chipleap.gen.rtl import (
    accel_sv,
    axi_sv,
    driver_header,
    rtl_params,
    testbench_axi_cpp,
    testbench_cpp,
)
from chipleap.hw.design import Accel
from chipleap.macro.sram import Sram, model
from chipleap.model.accel import Bandwidth, k_align, plan_tiles, simulate_job
from chipleap.verify.tools import clean_env, verilator


class SimError(RuntimeError):
    pass


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
) -> Path:
    """Build the Verilator model for ``accel`` (cached by parameters) and return the binary.
    With ``srams``, the buffers are banks of those SRAM macros (``chipleap.gen.banks``),
    each simulated by ChipLeap's model of its ports."""
    tool = verilator()
    if not tool.found:
        raise SimError("Verilator is not installed; see docs/verification.md")
    p = rtl_params(accel)
    axi = platform == "standalone"
    top = "chipleap_accel_axi" if axi else "chipleap_accel"
    params = p.verilator_args()
    macro_rtl: dict[str, str] = {}
    if srams is not None:
        if axi:
            raise SimError("SRAM banks are simulated on X-HEEP's interfaces only")
        plan = banks.plan(accel, srams)
        macro_rtl[f"{banks.TOP}.v"] = banks.top_sv(plan)
        macro_rtl |= {f"{s.cell}.v": model(s) for s in plan.cells()}
        # The generated top fixes the parameters; the class keeps the test bench's name.
        top, params = banks.TOP, ["--prefix", "Vchipleap_accel"]
    tb_name, tb_text = (
        ("tb_chipleap_axi.cpp", testbench_axi_cpp())
        if axi
        else (
            "tb_chipleap.cpp",
            testbench_cpp(),
        )
    )
    header = driver_header(accel, "sim", platform=platform)
    rtl = {"chipleap_accel.sv": accel_sv()}
    if axi:
        rtl["chipleap_accel_axi.sv"] = axi_sv()
    rtl |= macro_rtl
    sources = "".join(rtl.values()) + tb_text + header + " ".join(params)
    digest = hashlib.sha256(sources.encode()).hexdigest()[:16]
    root = workdir or (cache_dir() / f"sim-{digest}")
    binary = root / "obj_dir" / ("Vchipleap_accel" if macro_rtl else f"V{top}")
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


def predicted_cycles(accel: Accel, case: Case) -> float:
    """The model's prediction for a testbench case. The memory model grants each request
    unless it stalls and answers ``latency`` cycles after the grant, so every read port
    moves one word per cycle at best."""
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
    return simulate_job(accel, case.m, case.k, case.n, case.fmt, bw, 1.0)


def run(
    accel: Accel,
    cases: list[Case],
    binary: Path | None = None,
    timeout: float = 600,
    platform: str = "xheep",
    srams: Sequence[Sram] | None = None,
) -> list[CaseResult]:
    exe = binary or build(accel, platform=platform, srams=srams)
    stdin = "\n".join(c.line() for c in cases) + "\n"
    try:
        out = subprocess.run(
            [str(exe)],
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
                predicted_cycles(accel, case),
                d["tm"],
                d["tn"],
                d["tk"],
                bool(d["x_resident"]),
                int(d["mismatches"]),
            )
        )
    return results
