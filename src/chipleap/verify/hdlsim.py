"""Simulation of the generated accelerator with commercial simulators.

Cadence Xcelium, Synopsys VCS and Siemens Questa run the simulator-neutral SystemVerilog
testbench ``tb_chipleap.sv``. Its stimulus comes from ``stim_gen.cpp``, compiled on this
machine: it plans and starts every job with the generated C driver, recording the
register writes, and computes the golden result with the same data generator as the
Verilator testbench. Each case therefore checks the same things whichever simulator runs
it: every output word against the golden model, and the CYCLES register against the
model's prediction. Verilator 5 runs the same testbench with ``--timing``, which keeps
the flow testable without a commercial licence.

The simulator can run here or on a server (see ``chipleap.verify.remote``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from chipleap.gen.rtl import accel_sv, driver_header, rtl_params
from chipleap.hw.design import Accel
from chipleap.model.accel import plan_tiles
from chipleap.verify.remote import Remote, RemoteError, Step, run_steps
from chipleap.verify.sim import Case, CaseResult, SimError, cache_dir, predicted_cycles


@dataclass(frozen=True)
class Simulator:
    key: str
    name: str
    vendor: str
    binary: str  # what must be on the path
    steps: tuple[Step, ...]  # run in the work directory, in order

    @property
    def commercial(self) -> bool:
        return self.vendor != "open source"


_SOURCES = ("chipleap_accel.sv", "tb_chipleap.sv")
_STIM = "+stim=stim.txt"
SIMULATORS: dict[str, Simulator] = {
    s.key: s
    for s in (
        Simulator(
            "xcelium",
            "Xcelium",
            "Cadence",
            "xrun",
            (
                Step(
                    (
                        "xrun", "-64bit", "-sv", "-q", "-nocopyright", "-timescale", "1ns/1ps",
                        "-top", "tb_chipleap", *_SOURCES, _STIM, "-l", "xrun.log",
                    )
                ),
            ),
        ),
        Simulator(
            "vcs",
            "VCS",
            "Synopsys",
            "vcs",
            (
                Step(
                    (
                        "vcs", "-full64", "-sverilog", "-q", "-timescale=1ns/1ps",
                        "-top", "tb_chipleap", *_SOURCES, "-o", "simv",
                    ),
                    "vcs_compile.log",
                ),
                Step(("./simv", "-q", _STIM, "-l", "simv.log")),
            ),
        ),
        Simulator(
            "questa",
            "Questa",
            "Siemens",
            "vsim",
            (
                Step(("vlib", "work"), "vlib.log"),
                Step(("vlog", "-sv", "-quiet", "-timescale", "1ns/1ps", *_SOURCES), "vlog.log"),
                Step(("vsim", "-c", "-quiet", "tb_chipleap", _STIM, "-do", "run -all; quit -f")),
            ),
        ),
        Simulator(
            "verilator-sv",
            "Verilator (SystemVerilog testbench)",
            "open source",
            "verilator",
            (
                Step(
                    (
                        "verilator", "--binary", "--timing", "-Wno-fatal", "-Wno-lint",
                        "-Wno-style", "-Wno-TIMESCALEMOD", "--top-module", "tb_chipleap",
                        *_SOURCES, "-o", "tb",
                    ),
                    "verilator.log",
                ),
                Step(("obj_dir/tb", _STIM)),
            ),
        ),
    )
}  # fmt: skip


def get_simulator(key: str) -> Simulator:
    try:
        return SIMULATORS[key]
    except KeyError:
        known = ", ".join(["verilator", *SIMULATORS])
        raise SimError(f"unknown simulator {key!r}. Known: {known}") from None


def _source(name: str) -> str:
    return resources.files("chipleap.gen").joinpath("tb").joinpath(name).read_text()


def testbench_sv() -> str:
    return _source("tb_chipleap.sv")


def stim_gen_cpp() -> str:
    return _source("stim_gen.cpp")


def tb_config(accel: Accel) -> str:
    """Parameters the SystemVerilog testbench includes, from the design point."""
    p = rtl_params(accel)
    values = {
        "DATAFLOW": p.dataflow,
        "ROWS": p.rows,
        "COLS": p.cols,
        "WBITS": p.wbits,
        "PORTS": p.ports,
        "ABUF_WORDS": p.abuf_words,
        "WBUF_WORDS": p.wbuf_words,
        "DOUBLE_BUF": p.double_buf,
        "ACC_ROWS": p.acc_rows,
    }
    lines = ["// Written by ChipLeap for the design under test."]
    lines += [f"localparam int unsigned {k} = {v};" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def stim_generator(accel: Accel) -> Path:
    """Build stim_gen for ``accel`` with this machine's C++ compiler (cached by source)."""
    cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if cxx is None:
        raise SimError("a C++ compiler is needed to write the stimulus (c++, g++ or clang++)")
    header = driver_header(accel, "sim")
    digest = hashlib.sha256((stim_gen_cpp() + header).encode()).hexdigest()[:16]
    root = cache_dir() / f"stim-{digest}"
    exe = root / "stim_gen"
    if exe.exists():
        return exe
    root.mkdir(parents=True, exist_ok=True)
    (root / "stim_gen.cpp").write_text(stim_gen_cpp())
    (root / "chipleap_driver.h").write_text(header)
    out = subprocess.run(
        [cxx, "-O2", "-std=c++14", f"-I{root}", "stim_gen.cpp", "-o", "stim_gen"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not exe.exists():
        raise SimError(f"stimulus generator did not build:\n{out.stderr[-2000:]}")
    return exe


def prepare(accel: Accel, cases: list[Case], workdir: Path) -> None:
    """Write the RTL, the testbench, its parameters and the stimulus into ``workdir``."""
    workdir.mkdir(parents=True, exist_ok=True)
    stdin = "\n".join(c.line() for c in cases) + "\n"
    out = subprocess.run(
        [str(stim_generator(accel))], input=stdin, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise SimError(f"stimulus generator failed: {out.stderr[-1000:]}")
    (workdir / "stim.txt").write_text(out.stdout)
    (workdir / "chipleap_accel.sv").write_text(accel_sv())
    (workdir / "tb_chipleap.sv").write_text(testbench_sv())
    (workdir / "tb_config.svh").write_text(tb_config(accel))


def parse(accel: Accel, cases: list[Case], stdout: str) -> list[CaseResult]:
    # Simulators may prefix output lines (Questa's transcript starts each with "# "), so
    # take the JSON object wherever it starts.
    rows = []
    for ln in stdout.splitlines():
        start = ln.find('{"')
        if start >= 0:
            rows.append(json.loads(ln[start:].strip()))
    if any("error" in r for r in rows):
        raise SimError(f"testbench error: {next(r for r in rows if 'error' in r)['error']}")
    if len(rows) != len(cases):
        raise SimError(f"testbench returned {len(rows)} results for {len(cases)} cases")
    results = []
    for case, d in zip(cases, rows, strict=True):
        plan = plan_tiles(accel, case.m, case.k, case.n, case.fmt)
        if (plan.tm, plan.tn, plan.tk, plan.x_resident) != (
            d["tm"],
            d["tn"],
            d["tk"],
            bool(d["x_resident"]),
        ):
            raise SimError(f"driver and model disagree on tiles for {case}")
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


def run(
    accel: Accel,
    cases: list[Case],
    simulator: str,
    workdir: Path,
    remote: Remote | None = None,
    timeout: float = 3600,
) -> list[CaseResult]:
    """Simulate ``cases`` with ``simulator``, here or on ``remote``."""
    sim = get_simulator(simulator)
    if remote is None and shutil.which(sim.binary) is None:
        raise SimError(
            f"{sim.vendor} {sim.name} ({sim.binary}) is not on the path; run it on a server "
            "with --remote HOST"
        )
    prepare(accel, cases, workdir)
    try:
        out = run_steps(list(sim.steps), workdir, remote, timeout)
    except RemoteError as exc:
        raise SimError(str(exc)) from exc
    log = out.stdout + "\n" + out.stderr
    (workdir / f"{sim.key}.out").write_text(log)
    try:
        return parse(accel, cases, out.stdout)
    except SimError as exc:
        tail = "\n".join(log.strip().splitlines()[-25:])
        raise SimError(f"{sim.name}: {exc}\n{tail}") from exc
