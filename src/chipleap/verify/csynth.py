"""Synthesis of the generated accelerator with commercial tools.

Cadence Genus and Synopsys Design Compiler read the SystemVerilog directly. The method is
the same as with Yosys (``chipleap.verify.synth``): the buffers are SRAM macros in
silicon, so the accelerator is synthesized twice with 8- and 16-word buffers and the
logic area is extrapolated to zero buffer words; the worst setup slack at the design's
clock gives the critical path. Each run's parameters are written into a copy of the RTL
as defaults, so no tool-specific parameter syntax is involved.

The liberty file comes from the chosen technology (``chipleap tech add`` for a PDK of
your own). A liberty on this machine is copied next to the scripts; a path that only
exists on the server (``--remote``) is used where it is, so a foundry library never has
to leave the machine it is licensed on. Design Compiler needs a compiled ``.db``: give
one, or the run compiles the liberty with Library Compiler first.

Besides the synthesis results, the work directory gets an Innovus script for the mapped
netlist (``chipleap.verify.innovus``) when the technology names its LEF files.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from chipleap.gen.rtl import accel_sv, rtl_params
from chipleap.hw.design import Accel
from chipleap.verify.remote import Remote, RemoteError, Step, run_steps
from chipleap.verify.synth import SMALL_WORDS, SynthError, SynthResult
from chipleap.verify.tools import which

TOP = "chipleap_accel"


@dataclass(frozen=True)
class SynthTool:
    key: str
    name: str
    vendor: str
    binary: str


SYNTH_TOOLS: dict[str, SynthTool] = {
    t.key: t
    for t in (
        SynthTool("genus", "Genus", "Cadence", "genus"),
        SynthTool("dc", "Design Compiler", "Synopsys", "dc_shell"),
    )
}


def get_synth_tool(key: str) -> SynthTool:
    try:
        return SYNTH_TOOLS[key]
    except KeyError:
        known = ", ".join(["yosys", *SYNTH_TOOLS])
        raise SynthError(f"unknown synthesis tool {key!r}. Known: {known}") from None


def specialize(sv: str, params: dict[str, int]) -> str:
    """The RTL with each parameter's default replaced, so the top needs no overrides. Every
    module that declares it gets the value: ``chipleap_accel`` passes its own parameters
    to ``chipleap_accel_core``, so both must carry it."""
    out = sv
    for name, value in params.items():
        pattern = re.compile(rf"(parameter\s+int\s+unsigned\s+{name}\s*=\s*)[^,\n)]+")
        out, n = pattern.subn(rf"\g<1>{value}", out)
        if n < 1:
            raise SynthError(f"parameter {name} not found in the RTL")
    return out


def run_params(accel: Accel, words: int) -> dict[str, int]:
    p = rtl_params(accel)
    return {
        "DATAFLOW": p.dataflow,
        "ROWS": p.rows,
        "COLS": p.cols,
        "WBITS": p.wbits,
        "PORTS": p.ports,
        "ABUF_WORDS": words,
        "WBUF_WORDS": words,
        "DOUBLE_BUF": p.double_buf,
        "ACC_ROWS": p.acc_rows,
    }


def genus_script(
    rtl: str, liberty: str, period_ns: float, tag: str, lefs: tuple[str, ...] = ()
) -> str:
    # With LEF files Genus estimates wires from the placement it would make (PLE);
    # without them it falls back to the library's wireload models, which for a block
    # this small are pessimistic.
    physical = (
        f"read_physical -lef {{{' '.join(lefs)}}}\nset_db interconnect_mode ple\n" if lefs else ""
    )
    return f"""# ChipLeap: Genus synthesis of the accelerator, run {tag}.
set_db information_level 1
# Registers that reset to 1 need an async-preset flop; libraries with only async-reset
# flops (IHP SG13G2) get one by inverting around a reset flop, as Yosys does.
set_db lbr_seq_in_out_phase_opto true
read_libs {{{liberty}}}
{physical}read_hdl -sv {rtl}
elaborate {TOP}
create_clock -name clk -period {period_ns:g} [get_ports clk_i]
set_input_delay 0 -clock clk [remove_from_collection [all_inputs] [get_ports clk_i]]
set_output_delay 0 -clock clk [all_outputs]
syn_generic
syn_map
syn_opt
report_area > area_{tag}.rpt
report_timing > timing_{tag}.rpt
write_hdl > {TOP}_{tag}.v
write_sdc > {TOP}_{tag}.sdc
puts "CHIPLEAP_DONE {tag}"
exit
"""


def dc_script(rtl: str, db: str, period_ns: float, tag: str) -> str:
    return f"""# ChipLeap: Design Compiler synthesis of the accelerator, run {tag}.
set_app_var target_library {{{db}}}
set_app_var link_library {{* {db}}}
analyze -format sverilog {rtl}
elaborate {TOP}
current_design {TOP}
link
create_clock -name clk -period {period_ns:g} [get_ports clk_i]
set_input_delay 0 -clock clk [remove_from_collection [all_inputs] [get_ports clk_i]]
set_output_delay 0 -clock clk [all_outputs]
compile
report_area > area_{tag}.rpt
report_timing > timing_{tag}.rpt
write -format verilog -hierarchy -output {TOP}_{tag}.v
write_sdc {TOP}_{tag}.sdc
puts "CHIPLEAP_DONE {tag}"
exit
"""


def lc_script(liberty: str, db: str) -> str:
    """Library Compiler: compile a liberty file to the .db Design Compiler reads."""
    return f"""read_lib {liberty}
write_lib [get_object_name [index_collection [get_libs] 0]] -format db -output {db}
exit
"""


_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_area(tool: str, text: str, top: str = TOP) -> tuple[float, int]:
    """Cell area and cell count from a report_area."""
    if tool == "dc":
        m = re.search(r"Total cell area:\s*" + _NUM, text)
        cells = re.search(r"Number of cells:\s*(\d+)", text)
        if not m:
            raise SynthError("no 'Total cell area' in the Design Compiler area report")
        return float(m.group(1)), int(cells.group(1)) if cells else 0
    for line in text.splitlines():
        tok = line.split()
        if tok and tok[0] == top and len(tok) >= 4:
            nums = [t for t in tok[1:] if re.fullmatch(r"-?\d+(\.\d+)?", t)]
            if len(nums) >= 2:
                return float(nums[1]), int(float(nums[0]))
    raise SynthError("no area line for the design in the Genus area report")


def parse_slack(tool: str, text: str) -> float:
    """Worst setup slack in ns from a report_timing (Genus reports picoseconds)."""
    if tool == "dc":
        m = re.search(r"slack \((?:MET|VIOLATED)[^)]*\)\s+" + _NUM, text)
        if not m:
            raise SynthError("no slack in the Design Compiler timing report")
        return float(m.group(1))
    m = re.search(r"Path 1:\s*(?:MET|VIOLATED)\s*\(\s*" + _NUM + r"\s*ps\s*\)", text)
    if m:
        return float(m.group(1)) / 1000
    m = re.search(r"Slack:?=?\s*" + _NUM, text)
    if m:
        return float(m.group(1)) / 1000
    raise SynthError("no slack in the Genus timing report")


def local_name(path: str) -> str:
    """The plain file name a local input gets in the work directory."""
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", Path(path).name).lstrip("-") or "input"


def stage(path: str, workdir: Path, remote: Remote | None) -> str:
    """How a script names an input file: a local file is copied into the work directory
    (and travels with it to a server); a path that only exists on the server is used
    where it is."""
    # Names end up in Tcl braces: a local file is copied under a plain name, and a path on
    # the server must be plain already.
    if Path(path).is_file():
        name = local_name(path)
        shutil.copy2(path, workdir / name)
        return name
    if remote is None:
        raise SynthError(f"{path} not found")
    if not re.fullmatch(r"[A-Za-z0-9_./+~-]+", path) or path.startswith("-"):
        raise SynthError(f"{path!r}: a path on the server must use letters, digits and _ . / + ~ -")
    return path


def synthesize(
    accel: Accel,
    tool: str,
    liberty: str,
    workdir: Path,
    clock_mhz: float = 100.0,
    remote: Remote | None = None,
    db: str = "",
    timeout: float = 7200,
    lefs: tuple[str, ...] = (),
) -> SynthResult:
    """Synthesize ``accel`` at 8 and 16 buffer words with Genus or Design Compiler."""
    st = get_synth_tool(tool)
    if remote is None and which(st.binary) is None:
        raise SynthError(
            f"{st.vendor} {st.name} ({st.binary}) is not on the path; run it on a server "
            "with --remote HOST"
        )
    workdir.mkdir(parents=True, exist_ok=True)
    for old in [*workdir.glob("area_*.rpt"), *workdir.glob("timing_*.rpt")]:
        old.unlink()
    lib = stage(liberty, workdir, remote)
    phys = tuple(stage(p, workdir, remote) for p in lefs) if tool == "genus" else ()
    period = 1000.0 / clock_mhz
    steps: list[Step] = []
    if tool == "dc" and not db:
        db = Path(lib).with_suffix(".db").name
        (workdir / "compile_lib.tcl").write_text(lc_script(lib, db))
        steps.append(Step(("lc_shell", "-f", "compile_lib.tcl"), "lc.log"))
    elif db:
        db = stage(db, workdir, remote)
    for words in SMALL_WORDS:
        tag = f"w{words}"
        rtl = f"{TOP}_{tag}.sv"
        (workdir / rtl).write_text(specialize(accel_sv(), run_params(accel, words)))
        if tool == "genus":
            (workdir / f"run_{tag}.tcl").write_text(genus_script(rtl, lib, period, tag, phys))
            steps.append(
                Step(
                    (
                        "genus",
                        "-batch",
                        "-no_gui",
                        "-files",
                        f"run_{tag}.tcl",
                        "-log",
                        f"genus_{tag}",
                    ),
                    f"genus_{tag}.out",
                )
            )
        else:
            (workdir / f"run_{tag}.tcl").write_text(dc_script(rtl, db, period, tag))
            steps.append(Step(("dc_shell", "-f", f"run_{tag}.tcl"), f"dc_{tag}.out"))
    try:
        out = run_steps(steps, workdir, remote, timeout)
    except RemoteError as exc:
        raise SynthError(str(exc)) from exc
    runs = []
    cells = 0
    slack = 0.0
    for i, words in enumerate(SMALL_WORDS):
        tag = f"w{words}"
        area_rpt = workdir / f"area_{tag}.rpt"
        if not area_rpt.is_file():
            logs = sorted(workdir.glob(f"*{tag}.out"))
            tail = logs[0].read_text(errors="replace")[-1500:] if logs else out.stderr[-1500:]
            raise SynthError(f"{st.name} produced no area report for {tag}:\n{tail}")
        area, n = parse_area(tool, area_rpt.read_text(errors="replace"))
        runs.append((words, area))
        if i == 0:
            cells = n
            slack = parse_slack(tool, (workdir / f"timing_{tag}.rpt").read_text(errors="replace"))
    (w1, a1), (w2, a2) = runs
    logic = a1 - (a2 - a1) * w1 / (w2 - w1)
    # The tools' slack is register to register, clock-to-Q and setup included.
    delay = period - slack
    fmax = 1000 / delay if delay > 0 else 0.0
    return SynthResult(logic, cells, 0, delay, fmax, liberty, tuple(runs))
