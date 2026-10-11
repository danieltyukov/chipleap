"""MCP server: ChipLeap's architecture tools for any MCP client.

The tools answer architecture questions (what to build and why), not tool-execution
questions. A model can run the whole loop with them: capture requirements as a spec,
ask what the target demands, explore, interrogate the bottleneck, try changes, then
generate and verify the SoC. Every tool is deterministic for the same inputs, and with
``--log-dir`` each call is appended to a JSONL run log that ``chipleap replay`` re-runs.
"""

from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import yaml

from chipleap import __version__
from chipleap.explore.engine import Candidate, Evaluator, diagnose, explore
from chipleap.explore.moves import neighbours
from chipleap.explore.score import Point
from chipleap.hw.cpu import CPUS
from chipleap.hw.design import Design, DesignError
from chipleap.hw.memory import MEMORIES
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.model.bounds import requirements
from chipleap.report.data import (
    candidate_dict,
    explore_dict,
    requirements_dict,
)
from chipleap.spec import Spec, parse_spec
from chipleap.workload.llm import PRESETS

INSTRUCTIONS = """ChipLeap explores hardware architectures for a workload and generates an X-HEEP
RISC-V SoC with an accelerator. Typical flow:
1. Write the requirements as a spec (read the chipleap://spec-reference resource). A spec
   names the workload (an LLM preset or a custom graph), targets (throughput, latency) and
   constraints (area, power, energy, allowed process nodes, memories, CPUs).
2. required_resources: the bandwidth, compute and capacity the target implies, and which
   external memories can deliver it. Use it to rule regions in or out before searching.
3. explore_designs: the explainable agent plus a global search; returns the recommended
   design, the decision log and the Pareto front.
4. explain_bottleneck and evaluate_design: interrogate a design and try your own changes.
   explain_bottleneck lists every single change with its effect, so you can reason about
   trade-offs the objective does not capture.
5. generate_soc then verify_soc: accelerator RTL, driver, datasheet, and either an X-HEEP
   configuration (platform xheep, a RISC-V SoC) or an AXI4 wrapper for any host (platform
   standalone: Arm, other RISC-V, FPGA); Verilator simulation against a golden model,
   Yosys synthesis on IHP SG13G2, X-HEEP's own config validation for xheep designs.
All numbers are analytical estimates except those verify_soc measures. Say so when
reporting them.

For a design the user wrote (an RTL project with a project.yaml, see the
chipleap://project-reference resource): describe_project checks the project file;
lint_rtl, simulate_rtl, coverage_rtl and synthesize_rtl return structured diagnostics
(file, line, severity, message, failing test) to fix the RTL in a loop; verify_rtl runs
the steps and writes the report, place and route included when asked for.
For verification depth: prove_rtl proves the project's properties and check_assertions
proposed ones (counterexamples come back as values per step); equivalence_rtl checks the
synthesized and routed netlists against the RTL; cdc_rtl checks clock and reset domain
crossings; gate_sim_rtl runs the tests on the routed netlist with SDF; coverage_gaps shows
what the tests never reached, with source lines, to write tests that reach it.
simulate_rtl, coverage_rtl, gate_sim_rtl and verify_rtl run the project's cocotb tests and
golden model, and prove_rtl a .sby file the project names, which are code: use them only
on projects the user trusts.

check_macro validates a macro package (an analog block from aispice, say) before a chip
places it."""

SPEC_REFERENCE = """# ChipLeap spec reference

```yaml
name: llama-1b-edge                 # letters, digits, _ . -; also the design and file name
description: optional sentence
workload:
  llm:                              # or graph: (see below)
    model: llama-3.2-1b             # preset; list_models shows all
    phase: decode                   # decode | prefill
    context: 1024                   # KV length a decode step attends to
    prompt: 256                     # prompt length for prefill
    batch: 1
    weights: int4                   # int8 | int4 | int2 (int4/int2 carry fp16 group scales)
    kv: int8                        # int8 | int4
    group_size: 64
targets:
  throughput: 5                     # minimum tokens (or inferences) per second
  latency_ms: 300                   # maximum time per step
constraints:
  area_mm2: 8                       # core area budget, pads excluded
  power_mw: 400
  energy_mj: 50                     # per token or inference
  tech: [generic-28nm, generic-16nm]  # allowed: ihp-sg13g2 generic-65nm generic-28nm generic-16nm
  ext_mem: [none, hyperram, lpddr4-x16] # allowed external memories (catalog lists them)
  cpu: [cv32e20, cv32e40p]          # allowed X-HEEP cores
  max_freq_mhz: 500
  firmware_kib: 32                  # SRAM reserved for code, data and stack
allow:
  weights: [int8, int4]             # precisions the explorer may choose between
objective: energy                   # energy | area | throughput | edp
platform: xheep                     # xheep: an X-HEEP SoC (RISC-V) is generated
                                    # standalone: the accelerator alone, AXI4 IP for any
                                    # host (Arm, other RISC-V, FPGA); cpu, bus, banks and
                                    # ext_mem then describe the host the model assumes
start:                              # optional design the agent starts from
  tech: ihp-sg13g2
```

Custom workloads use `graph` instead of `llm`:

```yaml
workload:
  graph:
    name: kws
    unit: inference
    weights: int8
    ops:
      - conv2d: {name: conv1, h: 49, w: 10, cin: 1, cout: 64, kh: 10, kw: 4, stride: 2, act: true}
      - dwconv2d: {name: dw1, h: 25, w: 5, c: 64, kh: 3, kw: 3, act: true}
      - matmul: {name: fc, m: 1, k: 64, n: 12}
      - vector: {name: softmax, kind: softmax, elements: 12}
```

A design (for evaluate_design, explain_bottleneck, generate_soc) is a mapping:

```yaml
name: my-design
tech: generic-28nm
freq_mhz: 400
cpu: cv32e40p                       # cv32e20 | cv32e40x | cv32e40p | cv32e40px
bus: NtoM                           # onetoM | NtoM (NtoM needed for interleaving and multi-port)
banks: 4
bank_kib: 32
il_banks: 4                         # 0, 2, 4 or 8 interleaved banks
il_bank_kib: 16
ext_mem: lpddr4-x16
ext_channels: 1                     # 1, 2 or 4
accel:                              # or null
  dataflow: gemv                    # gemv | ws | os
  rows: 8
  cols: 8
  w_bits: 8                         # widest weight format the PEs take
  ports: 4                          # 32-bit OBI read ports
  abuf_kib: 16
  wbuf_kib: 8
  double_buffer: true
platform: xheep                     # xheep | standalone
```
"""


PROJECT_REFERENCE = """# ChipLeap project reference (project.yaml)

A project describes RTL someone wrote. Paths are relative to the project file and must
stay inside its directory (the user can allow others; a project file cannot). Names are
identifiers without $, parameter values integers, define values short literals. An
`include names a quoted relative file without "..", and a $readmemh file given as a
string must be inside the project. On Linux, lint and Yosys synthesis read only the
project and their own files (Landlock). Simulation runs the project's cocotb tests and
golden model, which are code: only for projects the user trusts.

```yaml
schema: project/v1
name: uart                    # letters, digits, _ . -
top: uart_top
sources: [rtl/uart_tx.sv, rtl/uart_rx.sv, rtl/uart_top.sv]   # globs allowed
filelist: rtl/files.f         # optional: -f, +incdir+, +define+
include_dirs: [rtl/include]
defines: {SYNTHESIS: null, WIDTH: 8}
parameters: {CLKS_PER_BIT: 16}  # integers, for the top module
timescale: 1ns/1ps
clocks: [{port: clk, period_ns: 20}]
resets: [{port: rst_n, active: low, sync: false}]
tech: ihp-sg13g2
tests:                        # cocotb
  dir: test
  modules: [test_uart]        # default: every test_*.py
  simulator: auto             # auto (Icarus, else Verilator) | icarus | verilator
  seed: 1
golden: model/uart_model.py   # Python reference model the tests import
lint: {waivers: [rtl/waivers.vlt], slang: true}
constraints: {sdc: constraints/uart.sdc}   # optional; else generated from clocks
pnr: {utilisation: 45, density: 55, die_um: [300, 300], librelane: {}}
formal:                       # SymbiYosys on the RTL's own assertions (immediate ones)
  sources: [formal/props.sv]  # optional: property files or a wrapper top
  checks: [{name: prove, mode: prove, depth: 20, engine: smtbmc z3}]   # mode: prove | bmc | cover
  sby: [formal/extra.sby]     # optional: run as they are (code)
equivalence: {checks: [rtl-synth, synth-pnr], engine: yosys, depth: 40}
cdc:
  async_inputs: [rx]          # inputs no clock samples safely
  input_domains: {cfg_i: clk_b}   # inputs of a clock other than the first
  reset_synchronized: [rst_n] # resets synchronized outside the design
  waive: [{from: "dbg_*", to: "*", reason: set before the clock starts}]
gate_sim: {netlist: routed, corner: nom_typ_1p20V_25C, delays: typ}   # netlist: routed | synth
```

`chipleap project init DIR` writes one for existing RTL, guessing the top, clock and
reset. Steps: lint (Verilator -Wall, slang -Wextra), sim (cocotb), coverage (Verilator
line, branch and toggle), formal (SymbiYosys), cdc (clock and reset domain crossings),
synth (Yosys on the technology's cells), pnr (LibreLane on IHP SG13G2: timing at the slow
corner, DRC, LVS, antenna), equiv (RTL, synthesized and routed netlists), gls (the tests
on the routed netlist with SDF, Icarus 13).
"""


class ToolError(ValueError):
    pass


def _spec(spec: str | dict[str, Any]) -> Spec:
    data = yaml.safe_load(spec) if isinstance(spec, str) else spec
    return parse_spec(data, name="spec")


def _design(design: str | dict[str, Any]) -> Design:
    data = yaml.safe_load(design) if isinstance(design, str) else design
    if not isinstance(data, dict):
        raise ToolError("design must be a mapping")
    body = data.get("design", data)
    base = Design().to_dict()
    base.update(body)
    return Design.from_dict(base)


def _candidate(spec: Spec, design: Design, weights: str | None) -> tuple[Evaluator, Candidate]:
    ev = Evaluator(spec)
    wf = weights or spec.default_weight_format()
    return ev, ev(Point(design, wf), origin="user")


# Tool implementations: plain functions so tests and the replay can call them directly.


def list_models() -> list[dict[str, Any]]:
    return [
        {
            "key": k,
            "name": a.name,
            "params_b": round(a.params / 1e9, 4),
            "layers": a.layers,
            "hidden": a.hidden,
            "heads": a.heads,
            "kv_heads": a.kv_heads,
            "ffn": a.ffn,
            "vocab": a.vocab,
            "source": a.source,
        }
        for k, a in PRESETS.items()
    ]


def catalog() -> dict[str, Any]:
    return {
        "technologies": [
            {
                "key": k,
                "name": t.name,
                "node_nm": t.node_nm,
                "max_freq_mhz": t.max_freq_mhz,
                "max_io_mts": t.max_io_mts,
                "int8_mac_pj": round(t.mac_pj_8x8, 4),
                "sram_pj_per_byte_16kib": round(t.sram_pj_per_byte, 3),
                "basis": "measured from PDK data" if t.measured else "scaled estimate",
                "note": t.note,
            }
            for k, t in TECHNOLOGIES.items()
        ],
        "memories": [
            {
                "key": k,
                "name": m.name,
                "peak_gbps": round(m.peak_gbps, 3),
                "capacity_mib": m.capacity_mib,
                "pj_per_bit": m.pj_per_bit,
                "pins": m.pins,
                "read_only": m.read_only,
                "min_mts": m.min_mts,
            }
            for k, m in MEMORIES.items()
        ],
        "cpus": [
            {
                "key": k,
                "name": c.name,
                "kge": c.kge,
                "cycles_per_int8_mac": c.cycles_per_mac[8],
                "note": c.note,
            }
            for k, c in CPUS.items()
        ],
    }


def describe_workload(spec: str | dict[str, Any]) -> dict[str, Any]:
    s = _spec(spec)
    w = s.workload()
    total = w.macs or 1
    return {
        "description": w.description,
        "params": w.params,
        "gmac_per_step": round(w.macs / 1e9, 5),
        "weights_mb_read_per_step": round(w.weight_bytes_read / 1e6, 4),
        "kv_mb_read_per_step": round(w.kv_bytes_read / 1e6, 4),
        "resident_mib": {
            "weights": round(w.storage.weights / 2**20, 3),
            "kv": round(w.storage.kv / 2**20, 3),
            "activations": round(w.storage.activations / 2**20, 4),
        },
        "matmuls": [
            {
                "name": op.name,
                "m": op.m,
                "k": op.k,
                "n": op.n,
                "count": op.count,
                "share_of_macs": round(op.macs / total, 4),
                "w_operand": op.w_kind.value,
            }
            for op in sorted(w.matmuls, key=lambda o: -o.macs)
        ],
    }


def required_resources(spec: str | dict[str, Any], throughput: float = 0.0) -> dict[str, Any]:
    s = _spec(spec)
    target = throughput or s.throughput
    if not target:
        raise ToolError("give throughput or a spec with targets.throughput")
    return requirements_dict(requirements(s.workload(), target))


def evaluate_design(
    spec: str | dict[str, Any], design: str | dict[str, Any], weights: str | None = None
) -> dict[str, Any]:
    s = _spec(spec)
    _, c = _candidate(s, _design(design), weights)
    out = candidate_dict(c, ops=True)
    out["operators"] = out["operators"][:15]
    return out


def explain_bottleneck(
    spec: str | dict[str, Any],
    design: str | dict[str, Any],
    weights: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Diagnosis plus the effect of every single change, best first."""
    s = _spec(spec)
    ev, c = _candidate(s, _design(design), weights)
    rows = []
    for mv in neighbours(s, c.point, ev.workload(c.point.weights)):
        n = ev(mv.point)
        e = n.evaluation
        rows.append(
            {
                "change": mv.label,
                "kind": mv.kind,
                "hypothesis": mv.why,
                "throughput": round(e.throughput, 5),
                "energy_mj": round(e.energy_per_unit_mj, 5),
                "area_mm2": round(e.total_area, 4),
                "power_mw": round(e.power_mw, 3),
                "bottleneck": e.bound,
                "feasible": n.assessment.feasible,
                "meets_spec": n.assessment.meets,
                "_key": n.key,
            }
        )
    rows.sort(key=lambda r: cast(tuple[int, float, float], r["_key"]))
    for r in rows:
        r.pop("_key")
    return {
        "design": c.design.summary(),
        "diagnosis": diagnose(c, s),
        "meets_spec": c.assessment.meets,
        "changes": rows[:limit],
        "changes_considered": len(rows),
    }


def explore_designs(
    spec: str | dict[str, Any], samples: int = 1500, seed: int = 0, max_steps: int = 40
) -> dict[str, Any]:
    r = explore(_spec(spec), samples=samples, seed=seed, max_steps=max_steps)
    d = explore_dict(r)
    d["pareto"] = d["pareto"][-12:]
    d["recommended"]["operators"] = d["recommended"]["operators"][:10]
    d["recommended_design_yaml"] = yaml.safe_dump(r.best.design.to_dict(), sort_keys=False)
    return d


def generate_soc(
    spec: str | dict[str, Any],
    design: str | dict[str, Any],
    out_dir: str,
    weights: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    from chipleap.gen.bundle import write_bundle

    s = _spec(spec)
    d = _design(design)
    if platform:
        try:
            d = Design.from_dict({**d.to_dict(), "platform": platform})
        except DesignError as exc:
            raise ToolError(str(exc)) from exc
    _, c = _candidate(s, d, weights)
    out = Path(out_dir).expanduser()
    files = write_bundle(out, d, weights=c.point.weights, spec=s, candidate=c)
    return {"out_dir": str(out), "files": [str(f.relative_to(out)) for f in files]}


def verify_soc(
    out_dir: str,
    steps: str = "consistency,lint,sim,synth,xheep",
    fetch_xheep: bool = False,
    sim_tool: str = "verilator",
    synth_tool: str = "yosys",
    synth_tech: str = "ihp-sg13g2",
    pnr_tool: str = "librelane",
) -> dict[str, Any]:
    """Verify a bundle. The client picks tools from a fixed list; whether they run on a
    server, and the setup that runs there, comes only from the user's own environment
    (CHIPLEAP_REMOTE, CHIPLEAP_REMOTE_SETUP), never from the client."""
    from chipleap.verify.backends import PNR_TOOLS, SIM_TOOLS, SYNTH_TOOLS
    from chipleap.verify.remote import Remote
    from chipleap.verify.run import verify_bundle

    if sim_tool not in SIM_TOOLS:
        raise ToolError(f"sim_tool must be one of {', '.join(SIM_TOOLS)}")
    if synth_tool not in SYNTH_TOOLS:
        raise ToolError(f"synth_tool must be one of {', '.join(SYNTH_TOOLS)}")
    if synth_tech not in TECHNOLOGIES:
        raise ToolError(f"synth_tech must be one of {', '.join(TECHNOLOGIES)}")
    if pnr_tool not in PNR_TOOLS:
        raise ToolError(f"pnr_tool must be one of {', '.join(PNR_TOOLS)}")
    res = verify_bundle(
        Path(out_dir).expanduser(),
        steps=tuple(x.strip() for x in steps.split(",") if x.strip()),
        fetch_xheep=fetch_xheep,
        sim_tool=sim_tool,
        synth_tool=synth_tool,
        synth_tech=synth_tech,
        remote=Remote.from_env(),
        pnr_tool=pnr_tool,
    )
    return {"steps": [{"step": r.name, "status": r.status, "summary": r.summary} for r in res]}


def _project(project: str) -> Any:
    from chipleap.rtl.project import ProjectError, load_project

    try:
        return load_project(Path(project).expanduser())
    except ProjectError as exc:
        raise ToolError(str(exc)) from exc


def _step(result: Any) -> dict[str, Any]:
    return {"status": result.status, "summary": result.summary, **result.data}


def describe_project(project: str) -> dict[str, Any]:
    """The project with every default filled in, paths relative to it."""
    p = _project(project)
    return {**p.to_dict(), "input_files": len(p.input_files())}


def lint_rtl(project: str) -> dict[str, Any]:
    from chipleap.rtl.verify import run_step

    return _step(run_step(_project(project), "lint"))


def simulate_rtl(project: str, tests: str = "", simulator: str = "") -> dict[str, Any]:
    """The project's cocotb tests; ``tests`` is a regular expression on test names."""
    from chipleap.rtl.sim import NAMES
    from chipleap.rtl.verify import run_step
    from chipleap.verify.remote import Remote

    if simulator and simulator not in (*NAMES, "auto"):
        raise ToolError(f"simulator must be one of auto, {', '.join(NAMES)}")
    p = _project(project)
    # run_step sends only a commercial simulator to the user's server.
    return _step(
        run_step(p, "sim", sim_tool=simulator, test_filter=tests, remote=Remote.from_env())
    )


def coverage_rtl(project: str, limit: int = 50) -> dict[str, Any]:
    """Line, branch and toggle coverage, and up to ``limit`` points never reached."""
    from chipleap.rtl.verify import run_step

    out = _step(run_step(_project(project), "coverage"))
    if "uncovered" in out:
        out["uncovered"] = out["uncovered"][: max(0, limit)]
    return out


def synthesize_rtl(project: str, tool: str = "yosys") -> dict[str, Any]:
    from chipleap.rtl.verify import SYNTH_TOOLS, run_step
    from chipleap.verify.remote import Remote

    if tool not in SYNTH_TOOLS:
        raise ToolError(f"tool must be one of {', '.join(SYNTH_TOOLS)}")
    p = _project(project)
    remote = Remote.from_env() if tool != "yosys" else None
    return _step(run_step(p, "synth", synth_tool=tool, remote=remote))


def verify_rtl(
    project: str,
    steps: str = "lint,sim,coverage,formal,cdc,synth,equiv",
    sim_tool: str = "",
    synth_tool: str = "yosys",
    pnr_tool: str = "librelane",
) -> dict[str, Any]:
    """Run the project's steps and write build/verify/report.html. As for verify_soc, a
    server and its setup come only from the user's environment."""
    from chipleap.rtl.verify import PNR_TOOLS, SIM_TOOLS, SYNTH_TOOLS, verify_project
    from chipleap.verify.remote import Remote

    if sim_tool and sim_tool not in SIM_TOOLS:
        raise ToolError(f"sim_tool must be one of {', '.join(SIM_TOOLS)}")
    if synth_tool not in SYNTH_TOOLS:
        raise ToolError(f"synth_tool must be one of {', '.join(SYNTH_TOOLS)}")
    if pnr_tool not in PNR_TOOLS:
        raise ToolError(f"pnr_tool must be one of {', '.join(PNR_TOOLS)}")
    p = _project(project)
    try:
        res = verify_project(
            p.file,
            steps=steps,
            sim_tool=sim_tool,
            synth_tool=synth_tool,
            pnr_tool=pnr_tool,
            remote=Remote.from_env(),
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "steps": [{"step": r.name, "status": r.status, "summary": r.summary} for r in res],
        "report": p.rel(p.build_dir / "verify" / "report.html"),
    }


def _guarded(step: str, fn: Callable[[], Any]) -> dict[str, Any]:
    """A step's result; a tool that fails in an unexpected way fails the step, as in
    ``chipleap.rtl.verify.run_step``."""
    from chipleap.rtl.verify import STEP_ERRORS
    from chipleap.verify.result import StepResult

    try:
        result = fn()
    except STEP_ERRORS as exc:
        first = (str(exc).strip().splitlines() or [""])[0]
        result = StepResult(step, "fail", f"{type(exc).__name__}: {first}", {"error": str(exc)})
    return _step(result)


def prove_rtl(project: str, check: str = "") -> dict[str, Any]:
    """The project's formal checks (or the one named), with counterexamples as data."""
    from chipleap.rtl.formal import formal

    p = _project(project)
    if check and p.formal is not None:
        known = [c.name for c in p.formal.checks] + [f"sby:{x.stem}" for x in p.formal.sby]
        if check not in known:
            raise ToolError(f"no formal check named {check!r}; checks: {', '.join(known)}")
    return _guarded("formal", lambda: formal(p, check))


def check_assertions(
    project: str,
    module: str,
    assertions: list[dict[str, Any]],
    mode: str = "prove",
    depth: int = 20,
    clock: str = "",
    reset: str = "",
) -> dict[str, Any]:
    """Proposed properties of a module, proved in a copy of its file under build/."""
    from chipleap.rtl import formal

    p = _project(project)
    try:
        return formal.check_assertions(
            p, module, assertions, mode=mode, depth=depth, clock=clock, reset=reset
        )
    except (ValueError, RuntimeError) as exc:
        raise ToolError(str(exc)) from exc


def equivalence_rtl(project: str, check: str = "") -> dict[str, Any]:
    from chipleap.rtl.equiv import PAIRS, equiv

    if check and check not in PAIRS:
        raise ToolError(f"check must be one of {', '.join(PAIRS)}")
    p = _project(project)
    return _guarded("equiv", lambda: equiv(p, check))


def cdc_rtl(project: str) -> dict[str, Any]:
    from chipleap.rtl.cdc import cdc

    p = _project(project)
    return _guarded("cdc", lambda: cdc(p))


def gate_sim_rtl(project: str, tests: str = "") -> dict[str, Any]:
    """The project's cocotb tests on its routed (or synthesized) netlist."""
    from chipleap.rtl.gls import gls

    p = _project(project)
    return _guarded("gls", lambda: gls(p, tests))


def coverage_gaps(
    project: str, file: str = "", kinds: str = "", limit: int = 50, context: int = 2
) -> dict[str, Any]:
    """The points the last coverage run never reached, with their source lines."""
    from chipleap.rtl.coverage import gaps

    p = _project(project)
    try:
        return gaps(p, file=file, kinds=kinds, limit=limit, context=context)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def check_tools() -> dict[str, Any]:
    from chipleap.verify.tools import all_tools, commercial, ihp_liberty

    lib = ihp_liberty()
    return {
        "tools": [
            {"name": t.name, "found": t.found, "version": t.version, "used_for": t.purpose}
            for t in all_tools() + commercial()
        ],
        "ihp_sg13g2_liberty": str(lib) if lib else None,
    }


def check_macro(path: str, pdk: str | None = None) -> dict[str, Any]:
    """Check a macro package (macro/v1) before placing it in a chip."""
    from chipleap.macro import check_package

    try:
        return check_package(Path(path).expanduser(), pdk=pdk).to_dict()
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


TOOLS: dict[str, Callable[..., Any]] = {
    "list_models": list_models,
    "catalog": catalog,
    "describe_workload": describe_workload,
    "required_resources": required_resources,
    "evaluate_design": evaluate_design,
    "explain_bottleneck": explain_bottleneck,
    "explore_designs": explore_designs,
    "generate_soc": generate_soc,
    "verify_soc": verify_soc,
    "check_tools": check_tools,
    "describe_project": describe_project,
    "lint_rtl": lint_rtl,
    "simulate_rtl": simulate_rtl,
    "coverage_rtl": coverage_rtl,
    "synthesize_rtl": synthesize_rtl,
    "verify_rtl": verify_rtl,
    "check_macro": check_macro,
    "prove_rtl": prove_rtl,
    "check_assertions": check_assertions,
    "equivalence_rtl": equivalence_rtl,
    "cdc_rtl": cdc_rtl,
    "gate_sim_rtl": gate_sim_rtl,
    "coverage_gaps": coverage_gaps,
}
# Tools whose results depend on the machine, on files or write files are logged but not
# replayed. coverage_gaps reads the last coverage run, so replaying it says nothing about
# the call.
SIDE_EFFECTS = {
    "generate_soc",
    "verify_soc",
    "check_tools",
    "verify_rtl",
    "check_macro",
    "coverage_gaps",
}
# Tools that read a project. Their results depend on its files and on the installed tools,
# so their log entries also carry a digest of every input file and the tool versions, and
# a replay that differs says which of the two changed.
PROJECT_TOOLS = {
    "describe_project",
    "lint_rtl",
    "simulate_rtl",
    "coverage_rtl",
    "synthesize_rtl",
    "prove_rtl",
    "check_assertions",
    "equivalence_rtl",
    "cdc_rtl",
    "gate_sim_rtl",
}


def project_context(project: str) -> dict[str, Any]:
    """The inputs digest and tool versions a project tool's result depends on."""
    from chipleap.rtl.sim import cocotb_python, cocotb_version
    from chipleap.verify.tools import iverilog, slang, sv2v, verilator, yosys

    try:
        digest = _project(project).inputs_digest()
    except ToolError:
        digest = None
    py = cocotb_python()
    tools: dict[str, str | None] = {
        t.name: t.version if t.found else None
        for t in (verilator(), slang(), iverilog(), yosys(), sv2v())
    }
    tools["cocotb"] = cocotb_version(py) if py else None
    return {"inputs_sha256": digest, "tools": tools}


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class RunLog:
    def __init__(self, directory: str | None) -> None:
        self.path: Path | None = None
        if directory:
            d = Path(directory).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / f"chipleap-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    def record(self, tool: str, args: dict[str, Any], result: Any, error: str | None) -> None:
        if self.path is None:
            return
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "chipleap": __version__,
            "tool": tool,
            "args": args,
            "result_sha256": _digest(result) if error is None else None,
            "error": error,
        }
        if tool in PROJECT_TOOLS and isinstance(args.get("project"), str):
            entry.update(project_context(args["project"]))
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def replay_calls(log: Path) -> list[dict[str, Any]]:
    """Re-run every replayable call in a run log. Each call's outcome is ``same``, or why
    it differs: ``inputs changed`` or ``tools changed`` for a project tool whose files or
    tool versions are not what they were, else ``differs``."""
    out = []
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["tool"] in SIDE_EFFECTS or entry.get("error"):
            continue
        try:
            result = TOOLS[entry["tool"]](**entry["args"])
            outcome = "same" if _digest(result) == entry["result_sha256"] else "differs"
        except Exception as exc:  # a project that moved or broke since the call
            outcome = f"fails now: {exc}"
        if outcome != "same" and "inputs_sha256" in entry:
            now = project_context(entry["args"]["project"])
            if now["inputs_sha256"] != entry["inputs_sha256"]:
                outcome = "inputs changed"
            elif now["tools"] != entry.get("tools"):
                outcome = "tools changed"
        out.append({"tool": entry["tool"], "args": entry["args"], "outcome": outcome})
    return out


def replay(log: Path) -> tuple[int, int]:
    """Re-run every replayable call in a run log. Returns (identical, total)."""
    calls = replay_calls(log)
    return sum(c["outcome"] == "same" for c in calls), len(calls)


def build_server(log_dir: str | None = None) -> Any:
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server  # type: ignore[attr-defined,no-redef]

    server = Server("chipleap", instructions=INSTRUCTIONS)
    runlog = RunLog(log_dir)

    def logged(name: str, fn: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
        def call(**kwargs: Any) -> dict[str, Any]:
            try:
                result = fn(**kwargs)
            except Exception as exc:
                runlog.record(name, kwargs, None, str(exc))
                raise
            runlog.record(name, kwargs, result, None)
            return cast(dict[str, Any], result)

        return call

    @server.tool(description="List the LLM presets (shapes from each model's config.json).")
    def list_models() -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], logged("list_models", TOOLS["list_models"])())

    @server.tool(
        description="Process technologies, external memories and X-HEEP CPUs with the "
        "numbers the model uses and where they come from."
    )
    def catalog() -> dict[str, Any]:
        return logged("catalog", TOOLS["catalog"])()

    @server.tool(
        description="What a spec's workload needs per step: MACs, bytes of weights and "
        "KV cache read, resident storage, and its matrix multiplies."
    )
    def describe_workload(spec: str) -> dict[str, Any]:
        return logged("describe_workload", TOOLS["describe_workload"])(spec=spec)

    @server.tool(
        description="Inverse sizing: the external bandwidth, MAC rate, capacity and CPU "
        "work a throughput target demands, and which external memories and "
        "nodes can deliver it. Call this before searching."
    )
    def required_resources(spec: str, throughput: float = 0.0) -> dict[str, Any]:
        return logged("required_resources", TOOLS["required_resources"])(
            spec=spec, throughput=throughput
        )

    @server.tool(
        description="Evaluate one design (YAML mapping) against a spec: throughput, "
        "latency, energy, power, area, bottleneck, per-operator breakdown, violations."
    )
    def evaluate_design(spec: str, design: str, weights: str | None = None) -> dict[str, Any]:
        return logged("evaluate_design", TOOLS["evaluate_design"])(
            spec=spec, design=design, weights=weights
        )

    @server.tool(
        description="Why a design performs as it does, and the measured effect of every "
        "single change (memory, dataflow, array size, ports, buffers, clock, node, "
        "CPU, precision), best first."
    )
    def explain_bottleneck(
        spec: str, design: str, weights: str | None = None, limit: int = 25
    ) -> dict[str, Any]:
        return logged("explain_bottleneck", TOOLS["explain_bottleneck"])(
            spec=spec, design=design, weights=weights, limit=limit
        )

    @server.tool(
        description="Search the design space for a spec: explainable agent loop plus "
        "seeded global sampling. Returns the recommended design, the decision log "
        "and the Pareto front. Deterministic for a given seed."
    )
    def explore_designs(
        spec: str, samples: int = 1500, seed: int = 0, max_steps: int = 40
    ) -> dict[str, Any]:
        return logged("explore_designs", TOOLS["explore_designs"])(
            spec=spec, samples=samples, seed=seed, max_steps=max_steps
        )

    @server.tool(
        description="Write the bundle for a design: accelerator RTL, register map, driver, "
        "testbench, datasheet, plus either an X-HEEP configuration and adapter "
        '(platform="xheep") or an AXI4 wrapper for any host (platform="standalone"). '
        "platform overrides the design's own."
    )
    def generate_soc(
        spec: str,
        design: str,
        out_dir: str,
        weights: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        return logged("generate_soc", TOOLS["generate_soc"])(
            spec=spec, design=design, out_dir=out_dir, weights=weights, platform=platform
        )

    @server.tool(
        description="Verify a bundle: Verilator lint, randomized simulation against a "
        "golden model with a cycle cross-check, synthesis, X-HEEP config validation. Steps: "
        "consistency,lint,sim,synth,xheep. sim_tool: verilator (default), xcelium, vcs, "
        "questa or verilator-sv; synth_tool: yosys (default), genus or dc; synth_tech: a "
        "technology key to map to. Commercial tools run where the user configured them "
        "(here, or on the server in the user's CHIPLEAP_REMOTE)."
    )
    @_stops_its_runs
    def verify_soc(
        out_dir: str,
        steps: str = "consistency,lint,sim,synth,xheep",
        fetch_xheep: bool = False,
        sim_tool: str = "verilator",
        synth_tool: str = "yosys",
        synth_tech: str = "ihp-sg13g2",
        pnr_tool: str = "librelane",
    ) -> dict[str, Any]:
        return logged("verify_soc", TOOLS["verify_soc"])(
            out_dir=out_dir,
            steps=steps,
            fetch_xheep=fetch_xheep,
            sim_tool=sim_tool,
            synth_tool=synth_tool,
            synth_tech=synth_tech,
            pnr_tool=pnr_tool,
        )

    @server.tool(
        description="Which tools are available on this machine: the open ones (Verilator, "
        "Yosys, OpenROAD, LibreLane, sv2v, the IHP PDK) and commercial simulators and "
        "synthesis tools on the path (Xcelium, VCS, Questa, Genus, Design Compiler, Innovus)."
    )
    def check_tools() -> dict[str, Any]:
        return logged("check_tools", TOOLS["check_tools"])()

    @server.tool(
        description="Read an RTL project (project.yaml, or the directory holding it) and "
        "return it with every default filled in: top, sources, clocks, resets, tests. "
        "Errors name the key that is wrong."
    )
    def describe_project(project: str) -> dict[str, Any]:
        return logged("describe_project", TOOLS["describe_project"])(project=project)

    @server.tool(
        description="Lint an RTL project with Verilator -Wall and slang -Wextra (when "
        "installed). Returns diagnostics with tool, severity, file, line, column, code and "
        "message, paths relative to the project. Fix them and call again."
    )
    def lint_rtl(project: str) -> dict[str, Any]:
        return logged("lint_rtl", TOOLS["lint_rtl"])(project=project)

    @server.tool(
        description="Run an RTL project's cocotb tests (Icarus or Verilator; Xcelium, VCS "
        "or Questa where the user configured them). Returns every test's status and, for "
        "failures, the message and the file and line that failed, plus compile errors as "
        "diagnostics. tests: a regular expression selecting test names. Runs the project's "
        "tests and golden model, which are code: only for projects the user trusts."
    )
    def simulate_rtl(project: str, tests: str = "", simulator: str = "") -> dict[str, Any]:
        return logged("simulate_rtl", TOOLS["simulate_rtl"])(
            project=project, tests=tests, simulator=simulator
        )

    @server.tool(
        description="Code coverage of an RTL project's design from its cocotb tests on "
        "Verilator: line, branch and toggle totals per file, and up to limit points the "
        "tests never reached (file, line, kind, name), to write the tests that reach them. "
        "Runs the project's tests, which are code: only for projects the user trusts."
    )
    def coverage_rtl(project: str, limit: int = 50) -> dict[str, Any]:
        return logged("coverage_rtl", TOOLS["coverage_rtl"])(project=project, limit=limit)

    @server.tool(
        description="Synthesize an RTL project on its technology's standard cells (Yosys; "
        "genus or dc where configured): cell area, cells, flip-flops, critical path and an "
        "Fmax estimate at the typical corner, with Yosys warnings as diagnostics."
    )
    def synthesize_rtl(project: str, tool: str = "yosys") -> dict[str, Any]:
        return logged("synthesize_rtl", TOOLS["synthesize_rtl"])(project=project, tool=tool)

    @server.tool(
        description="Verify an RTL project and write build/verify/report.html. Steps: "
        "lint,sim,coverage,formal,cdc,synth,equiv by default; add pnr (LibreLane place and "
        "route on IHP SG13G2, minutes) and gls (gate-level simulation of the routed netlist), "
        "or use all. Tools run where the user configured them. Runs the project's tests and "
        "golden model, which are code: only for projects the user trusts."
    )
    def verify_rtl(
        project: str,
        steps: str = "lint,sim,coverage,formal,cdc,synth,equiv",
        sim_tool: str = "",
        synth_tool: str = "yosys",
        pnr_tool: str = "librelane",
    ) -> dict[str, Any]:
        return logged("verify_rtl", TOOLS["verify_rtl"])(
            project=project,
            steps=steps,
            sim_tool=sim_tool,
            synth_tool=synth_tool,
            pnr_tool=pnr_tool,
        )

    @server.tool(
        description="Prove an RTL project's formal properties with SymbiYosys (the checks "
        "of its formal section, or the one named). Each check is pass, fail, unknown or error; "
        "a failure names the property, its file, line and step, with the counterexample as "
        "values per step of the top module's signals and a VCD path. A .sby file the project "
        "names runs as it is: it is code, only for projects the user trusts."
    )
    def prove_rtl(project: str, check: str = "") -> dict[str, Any]:
        return logged("prove_rtl", TOOLS["prove_rtl"])(project=project, check=check)

    @server.tool(
        description="Check properties you propose for a module of an RTL project, without "
        "editing it: each assertion is a boolean SystemVerilog expression over the module's "
        "signals ($past, $rose, $fell, $stable, $changed, $countones, $onehot, $onehot0, "
        "$isunknown, $signed and $unsigned allowed; no statements), kind assert, assume or "
        "cover, and an optional name. They are sampled at the "
        "project's clock outside reset. Each comes back holds, fails (with step and the "
        "counterexample values), unknown, reached or unreachable. The design's own "
        "assertions are left out of this run, so each answer is the proposal's own."
    )
    def check_assertions(
        project: str,
        module: str,
        assertions: list[dict[str, Any]],
        mode: str = "prove",
        depth: int = 20,
        clock: str = "",
        reset: str = "",
    ) -> dict[str, Any]:
        return logged("check_assertions", TOOLS["check_assertions"])(
            project=project,
            module=module,
            assertions=assertions,
            mode=mode,
            depth=depth,
            clock=clock,
            reset=reset,
        )

    @server.tool(
        description="Check that an RTL project's netlists implement it: rtl-synth (the RTL "
        "against ChipLeap's synthesized netlist) and synth-pnr (LibreLane's synthesized "
        "netlist against its routed one, after place and route). Each pair is proved, "
        "bounded (some points equal only for N cycles from reset), differs (with a "
        "counterexample from reset) or skipped."
    )
    def equivalence_rtl(project: str, check: str = "") -> dict[str, Any]:
        return logged("equivalence_rtl", TOOLS["equivalence_rtl"])(project=project, check=check)

    @server.tool(
        description="Check an RTL project's clock and reset domain crossings on its netlist: "
        "each finding has a rule (unsynchronized, logic_before_synchronizer, "
        "multibit_synchronizer, reset_crossing, reset_release, generated_clock ...), the "
        "source and destination registers and domains, and the path between them; accepted "
        "structures (synchronizers, gray-coded pointers, handshakes, FIFO memories) are listed."
    )
    def cdc_rtl(project: str) -> dict[str, Any]:
        return logged("cdc_rtl", TOOLS["cdc_rtl"])(project=project)

    @server.tool(
        description="Run an RTL project's cocotb tests on its netlist: the routed netlist with "
        "SDF delays from the last place and route (gate_sim.netlist: synth for the "
        "synthesized netlist with zero delay). Needs Icarus 13. Runs the project's tests and "
        "golden model, which are code: only for projects the user trusts."
    )
    def gate_sim_rtl(project: str, tests: str = "") -> dict[str, Any]:
        return logged("gate_sim_rtl", TOOLS["gate_sim_rtl"])(project=project, tests=tests)

    @server.tool(
        description="The coverage points an RTL project's tests never reached in the last "
        "coverage_rtl run, grouped by source line with the lines around each, and the "
        "project's test functions, to write tests that reach them. stale is true when the "
        "project's files changed since that run. kinds: line, branch, toggle (comma-separated)."
    )
    def coverage_gaps(
        project: str, file: str = "", kinds: str = "", limit: int = 50, context: int = 2
    ) -> dict[str, Any]:
        return logged("coverage_gaps", TOOLS["coverage_gaps"])(
            project=project, file=file, kinds=kinds, limit=limit, context=context
        )

    @server.prompt(description="Propose and prove properties of a module of an RTL project.")
    def propose_assertions(project: str, module: str) -> str:
        return (
            f"Find properties of module {module} in the RTL project at {project}. Call "
            "describe_project and read the module's source. Propose invariants a correct design "
            "must keep (counters within range, one-hot states, handshake rules, pointers that "
            "agree), and covers for the states the tests should reach; call check_assertions "
            "with them. For each assertion that fails, read the counterexample's values step "
            "by step and decide whether the property is wrong or the design is, and say which "
            "and why; fix the property or report the bug, never weaken a property only to make "
            "it pass. Suggest adding the ones that hold to the project (under `ifdef FORMAL or "
            "in formal.sources) and finish with prove_rtl."
        )

    @server.prompt(description="Raise an RTL project's coverage with new tests.")
    def close_coverage(project: str, goal: str = "90") -> str:
        return (
            f"Raise the coverage of the RTL project at {project} to {goal}% of lines and "
            "branches. Call coverage_rtl, then coverage_gaps. For each gap, read its source "
            "lines and write a cocotb test in the project's test directory (coverage_gaps names "
            "it and the existing tests) that drives the inputs to reach it, checking results "
            "against the golden model where there is one. Call coverage_rtl again and report "
            "newly_covered. Repeat until the goal is met or the points left cannot be reached "
            "(say why for each, for example a default branch of a full case). Never change the "
            "RTL or weaken a test to raise coverage."
        )

    @server.resource(
        "chipleap://project-reference",
        name="project-reference",
        description="RTL project file format (project.yaml)",
        mime_type="text/markdown",
    )
    def project_reference() -> str:
        return PROJECT_REFERENCE

    @server.prompt(description="Make an RTL project lint clean and pass its tests.")
    def fix_rtl(project: str) -> str:
        return (
            f"Work on the RTL project at {project} with ChipLeap. Call describe_project, then "
            "lint_rtl and simulate_rtl. For each diagnostic, open the file at the line it names "
            "and fix the RTL (or the test, if the test is wrong; say which and why). Call the "
            "tools again until lint is clean and every test passes, then call coverage_rtl and "
            "say which uncovered points matter, and synthesize_rtl for area and the Fmax "
            "estimate. Never edit a test only to make it pass."
        )

    @server.tool(
        description="Check a macro package (macro/v1, the format ChipLeap and aispice share "
        "for hardened blocks) before placing it: macro.toml against its schema, required "
        "files, pins across GDS, LEF, liberty, Verilog and SPICE, size, measured specs, and "
        "DRC and LVS evidence. pdk is the chip's PDK, NAME or NAME@COMMIT. Returns every "
        "finding with its rule id, severity, file and message."
    )
    def check_macro(path: str, pdk: str | None = None) -> dict[str, Any]:
        return logged("check_macro", TOOLS["check_macro"])(path=path, pdk=pdk)

    @server.resource(
        "chipleap://spec-reference",
        name="spec-reference",
        description="Spec and design file format",
        mime_type="text/markdown",
    )
    def spec_reference() -> str:
        return SPEC_REFERENCE

    @server.prompt(description="Design a chip for a workload, end to end, with ChipLeap.")
    def design_chip(requirements: str) -> str:
        return (
            "Design a RISC-V SoC with ChipLeap for these requirements:\n\n"
            f"{requirements}\n\n"
            "Steps: read chipleap://spec-reference and write a spec; call required_resources and "
            "say which memories and nodes can meet the target at all; call explore_designs; "
            "summarise the decision log in a few sentences, naming the bottleneck at each step; "
            "use explain_bottleneck on the recommended design and point out any trade-off the "
            "objective ignores; if the user agrees, call generate_soc and verify_soc and report "
            "what was measured versus estimated. Keep estimates and measurements separate."
        )

    return server


def _stops_its_runs(fn: Callable[..., dict[str, Any]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """A blocking tool that runs in a worker thread, and whose LibreLane runs stop when the
    call is cancelled or the server shuts down. A plain blocking tool would keep its
    thread, and LibreLane, going for hours after either."""
    from chipleap.verify import launch

    @functools.wraps(fn)
    async def call(**kwargs: Any) -> dict[str, Any]:
        import anyio

        stop = threading.Event()

        def work() -> dict[str, Any]:
            with launch.stop_when(stop):
                return fn(**kwargs)

        try:
            return await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
        finally:
            stop.set()

    return call


def main(log_dir: str | None = None) -> None:
    from chipleap.verify import launch

    # SIGTERM, SIGHUP and the end of the session stop every LibreLane run in progress.
    with launch.stop_on_exit():
        build_server(log_dir).run("stdio")
