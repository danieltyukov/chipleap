"""Logic synthesis of the generated accelerator with Yosys on the IHP SG13G2 open PDK.

The activation and weight buffers become SRAM macros in silicon, whose area ChipLeap
already takes from the PDK's liberty files. Synthesising them as flip-flop arrays would
be slow and meaningless, so the accelerator is synthesised twice with tiny buffers (8 and
16 words) and the logic area is extrapolated to zero buffer words. What remains is the
logic the model estimates separately: processing elements, accumulators, controller and
DMA. ABC's static timing of the mapped netlist gives a critical-path estimate.

Yosys 0.33 (Ubuntu 24.04) cannot read every SystemVerilog construct the RTL uses, so the
source goes through sv2v first when it is installed.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chipleap.gen.rtl import accel_sv, rtl_params
from chipleap.hw.design import Accel
from chipleap.verify.tools import ihp_liberty, yosys

SMALL_WORDS = (8, 16)
# Clock-to-Q plus setup of the SG13G2 flip-flop (sg13g2_dfrbpq_1, typical corner), added
# to ABC's combinational delay to estimate the clock period.
FLOP_OVERHEAD_NS = 0.45


class SynthError(RuntimeError):
    pass


@dataclass(frozen=True)
class SynthResult:
    logic_um2: float  # extrapolated to zero buffer words
    cells: int
    flops: int
    delay_ns: float  # ABC critical combinational path
    fmax_mhz: float
    liberty: str
    runs: tuple[tuple[int, float], ...]  # (buffer words, area um2)

    @property
    def logic_mm2(self) -> float:
        return self.logic_um2 / 1e6


def _sv2v(src: Path, out: Path) -> None:
    exe = os.environ.get("CHIPLEAP_SV2V") or shutil.which("sv2v")
    if exe is None:
        shutil.copy2(src, out)
        return
    res = subprocess.run(
        [exe, "--exclude=Always", str(src)], capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        raise SynthError(f"sv2v failed: {res.stderr.strip()[-800:]}")
    out.write_text(res.stdout)


def _run(
    yosys_path: str,
    verilog: Path,
    params: dict[str, int],
    liberty: Path,
    workdir: Path,
) -> tuple[float, int, int, float]:
    chparams = " ".join(f"-set {k} {v}" for k, v in params.items())
    # Map, buffer, size and time in one ABC call: once the logic is mapped to liberty cells,
    # a second abc pass has nothing left to extract. Buffering matters for timing: without
    # it a control signal driving a few hundred flops looks like a 5 ns gate. Yosys's
    # default mapping script spends many minutes on multiplier-heavy logic; the fast one
    # maps in seconds at a few percent more area. CHIPLEAP_ABC_FULL=1 selects the default.
    if os.environ.get("CHIPLEAP_ABC_FULL"):
        mapping = (
            "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
            "&get -n; &dch -f; &nf; &put"
        )
    else:
        mapping = "strash; dretime; map"
    # ABC's buffering needs a timed, topologically sorted network (without the first stime
    # it crashes), and the network must be re-sorted after sizing before it is written out.
    # The last stime reports the buffered critical path.
    mapping += "; topo; stime -p; buffer -p; upsize; dnsize; topo; stime -p"
    abc_script = workdir / "map.abc"
    abc_script.write_text(mapping.replace("; ", "\n") + "\n")
    script = [
        f"read_verilog -sv {verilog}",
        f"chparam {chparams} chipleap_accel",
        "synth -top chipleap_accel -flatten",
        f"dfflibmap -liberty {liberty}",
        f"abc -liberty {liberty} -script {abc_script}",
        "opt_clean",
        f"stat -liberty {liberty}",
    ]
    # Run Yosys in its own process group: it starts ABC as a child, and a timeout or an
    # interrupt must not leave ABC running on its own.
    proc = subprocess.Popen(
        [yosys_path, "-p", "; ".join(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=workdir,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=float(os.environ.get("CHIPLEAP_SYNTH_TIMEOUT", "1800")))
    except BaseException:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    log = out or ""
    (workdir / f"yosys-{params.get('ABUF_WORDS', 0)}.log").write_text(log)
    if proc.returncode != 0:
        raise SynthError("Yosys failed:\n" + "\n".join(log.splitlines()[-20:]))
    area = re.findall(r"Chip area for module '\\chipleap_accel': ([\d.]+)", log)
    cells = re.findall(r"Number of cells:\s+(\d+)", log)
    flops = sum(int(n) for n in re.findall(r"sg13g2_df\w+\s+(\d+)", log))
    delay = re.findall(r"Delay =\s+([\d.]+) ps", log)
    if not area:
        raise SynthError("Yosys did not report an area")
    return (
        float(area[-1]),
        int(cells[-1]) if cells else 0,
        flops,
        (float(delay[-1]) / 1000 if delay else 0.0),
    )


def synthesize(
    accel: Accel, liberty: Path | None = None, workdir: Path | None = None
) -> SynthResult:
    tool = yosys()
    if not tool.found or tool.path is None:
        raise SynthError("Yosys is not installed")
    lib = liberty or ihp_liberty()
    if lib is None:
        raise SynthError(
            "IHP SG13G2 liberty not found: install the open PDK (for example with ciel or "
            "volare) or point CHIPLEAP_LIBERTY at sg13g2_stdcell_typ_1p20V_25C.lib"
        )
    work = workdir or Path(tempfile.mkdtemp(prefix="chipleap-synth-"))
    work.mkdir(parents=True, exist_ok=True)
    src = work / "chipleap_accel.sv"
    src.write_text(accel_sv())
    verilog = work / "chipleap_accel.v"
    _sv2v(src, verilog)

    p = rtl_params(accel)
    base = {
        "DATAFLOW": p.dataflow,
        "ROWS": p.rows,
        "COLS": p.cols,
        "WBITS": p.wbits,
        "PORTS": p.ports,
        "DOUBLE_BUF": p.double_buf,
        "ACC_ROWS": p.acc_rows,
    }
    runs = []
    cells = flops = 0
    delay = 0.0
    for i, words in enumerate(SMALL_WORDS):
        area, c, f, d = _run(
            str(tool.path),
            verilog,
            {**base, "ABUF_WORDS": words, "WBUF_WORDS": words},
            lib,
            work,
        )
        runs.append((words, area))
        if i == 0:
            cells, flops, delay = c, f, d
    (w1, a1), (w2, a2) = runs
    logic = a1 - (a2 - a1) * w1 / (w2 - w1)
    period = delay + FLOP_OVERHEAD_NS
    return SynthResult(
        logic, cells, flops, delay, 1000 / period if period > 0 else 0.0, str(lib), tuple(runs)
    )
