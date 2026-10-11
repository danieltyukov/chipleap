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
simulate_rtl, coverage_rtl and verify_rtl run the project's cocotb tests and golden
model, which are code: use them only on projects the user trusts.

After place and route: dft_rtl (scan chains, ATPG coverage, patterns simulated),
power_rtl (gate-level power from the tests, at corners), ir_drop_rtl (worst supply drop),
upf_rtl (power domains in OpenROAD), timing_paths_rtl (the worst paths, stage by stage)
and eco_rtl (resize gates, add buffers or repair timing on the routed design, then sign
off again). power_rtl runs the tests too.

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
dft: {max_chain_length: 100, insert: true, min_coverage: 90, patterns: 8}
power: {corners: [typ, slow, fast], netlist: auto}   # typ 1.2 V 25 C, slow 1.08 V 125 C
ir_drop: {corner: typ, max_drop_pct: 5}
upf:
  domains:
    - {name: PD_TOP}                               # the top: everything not listed below
    - {name: PD_RX, elements: [u_rx], voltage: 1.2}  # area_um: [x0, y0, x1, y1] optional
eco:                                               # changes to the routed design
  - {resize: _174_, cell: sg13g2_a21oi_2}
  - {buffer: _212_/Y, cell: sg13g2_buf_2}
  - {repair: setup, margin_ns: 0.5}
```

`chipleap project init DIR` writes one for existing RTL, guessing the top, clock and
reset. Steps: lint (Verilator -Wall, slang -Wextra), sim (cocotb), coverage (Verilator
line, branch and toggle), synth (Yosys on the technology's cells), pnr (LibreLane on
IHP SG13G2: timing at the slow corner, DRC, LVS, antenna; the scan netlist when dft
inserts one), eco (the eco changes on the routed design, signed off again), dft
(OpenROAD scan chains, Fault ATPG coverage, the patterns simulated through the chains),
power (OpenROAD power from the cocotb tests' VCD on the gate-level netlist, beside the
vectorless figure), ir_drop (PDNSim on the routed grid), upf (OpenROAD reads the power
domains and places them; no open tool simulates power intent).
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
    steps: str = "lint,sim,coverage,synth",
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


def dft_rtl(project: str) -> dict[str, Any]:
    """Scan chains, ATPG coverage and the chain simulation (the project's dft section)."""
    from chipleap.rtl.verify import run_step

    return _step(run_step(_project(project), "dft"))


def _corners(text: str) -> tuple[str, ...]:
    from chipleap.rtl.project import CORNERS

    items = tuple(c.strip() for c in text.split(",") if c.strip())
    bad = [c for c in items if c not in CORNERS]
    if bad:
        raise ToolError(f"corners are {', '.join(CORNERS)}, not {bad[0]}")
    return items


def power_rtl(project: str, corners: str = "") -> dict[str, Any]:
    """Gate-level power from the tests' activity; ``corners`` overrides the project's."""
    import dataclasses

    from chipleap.rtl.verify import run_step

    p = _project(project)
    if corners:
        p = dataclasses.replace(p, power=dataclasses.replace(p.power, corners=_corners(corners)))
    return _step(run_step(p, "power"))


def ir_drop_rtl(project: str, corner: str = "") -> dict[str, Any]:
    import dataclasses

    from chipleap.rtl.verify import run_step

    p = _project(project)
    picked = _corners(corner)
    if picked:
        p = dataclasses.replace(p, ir_drop=dataclasses.replace(p.ir_drop, corner=picked[0]))
    return _step(run_step(p, "ir_drop"))


def upf_rtl(project: str) -> dict[str, Any]:
    from chipleap.rtl.verify import run_step

    return _step(run_step(_project(project), "upf"))


def timing_paths_rtl(
    project: str, corner: str = "slow", check: str = "setup", count: int = 5
) -> dict[str, Any]:
    """The routed design's worst paths, each stage's pin, cell, instance and delay."""
    from chipleap.rtl.eco import timing_paths
    from chipleap.rtl.openroad import OpenroadError

    p = _project(project)
    try:
        paths, run = timing_paths(p, corner, check, count)
    except (OpenroadError, ValueError, RuntimeError) as exc:
        raise ToolError(str(exc).splitlines()[0]) from exc
    return {"run": run, "corner": corner, "check": check, "paths": [x.to_dict() for x in paths]}


def eco_rtl(project: str, changes: list[str]) -> dict[str, Any]:
    """Apply ``changes`` (resize:INST=CELL, buffer:INST/PIN=CELL, repair:setup|hold[@NS])
    to the routed design and sign it off again."""
    from chipleap.rtl.eco import eco
    from chipleap.rtl.project import EcoChange, ProjectError

    p = _project(project)
    try:
        parsed = [EcoChange.parse(c) for c in changes]
    except ProjectError as exc:
        raise ToolError(str(exc)) from exc
    if not parsed:
        raise ToolError("give at least one change")
    out = _step(eco(p, parsed))
    out["keep"] = "add the changes to the project's eco section to make them part of its flow"
    return out


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
    "dft_rtl": dft_rtl,
    "power_rtl": power_rtl,
    "ir_drop_rtl": ir_drop_rtl,
    "upf_rtl": upf_rtl,
    "timing_paths_rtl": timing_paths_rtl,
    "eco_rtl": eco_rtl,
}
# Tools whose results depend on the machine, on files or write files are logged but not
# replayed. Power, IR drop, timing paths and ECOs read the routed design in build/, which
# the inputs digest does not cover.
SIDE_EFFECTS = {
    "generate_soc",
    "verify_soc",
    "check_tools",
    "verify_rtl",
    "check_macro",
    "power_rtl",
    "ir_drop_rtl",
    "timing_paths_rtl",
    "eco_rtl",
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
    "dft_rtl",
    "upf_rtl",
}


def project_context(project: str, tool: str = "") -> dict[str, Any]:
    """The inputs digest and tool versions a project tool's result depends on."""
    from chipleap.rtl.sim import cocotb_python, cocotb_version
    from chipleap.verify.tools import (
        fault,
        gl_iverilog,
        iverilog,
        openroad,
        quaigh,
        slang,
        sv2v,
        verilator,
        yosys,
    )

    try:
        digest = _project(project).inputs_digest()
    except ToolError:
        digest = None
    py = cocotb_python()
    tools: dict[str, str | None] = {
        t.name: t.version if t.found else None
        for t in (verilator(), slang(), iverilog(), yosys(), sv2v())
    }
    if tool in ("dft_rtl", "upf_rtl"):
        for t in (openroad(), fault(), quaigh(), gl_iverilog()):
            tools[t.name] = t.version if t.found else None
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
            entry.update(project_context(args["project"], tool))
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
            now = project_context(entry["args"]["project"], entry["tool"])
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
        "lint,sim,coverage,synth by default; add pnr (LibreLane place and route on IHP "
        "SG13G2, minutes) or use all. Tools run where the user configured them. Runs the "
        "project's tests and golden model, which are code: only for projects the user trusts."
    )
    def verify_rtl(
        project: str,
        steps: str = "lint,sim,coverage,synth",
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
        description="Insert scan chains with OpenROAD into an RTL project with a dft section, "
        "generate stuck-at patterns with Fault and Quaigh, and simulate a flush pattern and "
        "the first patterns through the chains on Icarus 13 (also on the routed netlist when "
        "it has the chains). Returns chains, coverage, pattern files and mismatches."
    )
    @_stops_its_runs
    def dft_rtl(project: str) -> dict[str, Any]:
        return logged("dft_rtl", TOOLS["dft_rtl"])(project=project)

    @server.tool(
        description="Gate-level power of an RTL project: its cocotb tests run on the routed "
        "netlist (else the synthesized one) with the cell models, and OpenROAD reports power "
        "from that activity and vectorless, per group, at each corner (corners: comma "
        "separated typ, slow, fast). Runs the project's tests, which are code: only for "
        "projects the user trusts."
    )
    @_stops_its_runs
    def power_rtl(project: str, corners: str = "") -> dict[str, Any]:
        return logged("power_rtl", TOOLS["power_rtl"])(project=project, corners=corners)

    @server.tool(
        description="Static IR drop of a routed RTL project with OpenROAD PDNSim, with "
        "currents from the tests' activity: worst and average drop per supply net, the worst "
        "cells, and a map. corner: typ, slow or fast."
    )
    @_stops_its_runs
    def ir_drop_rtl(project: str, corner: str = "") -> dict[str, Any]:
        return logged("ir_drop_rtl", TOOLS["ir_drop_rtl"])(project=project, corner=corner)

    @server.tool(
        description="Read an RTL project's upf section into OpenROAD: power domains, their "
        "cells placed in their areas, and the signals between domains, with level shifters "
        "flagged where voltages differ. No open tool simulates power intent."
    )
    @_stops_its_runs
    def upf_rtl(project: str) -> dict[str, Any]:
        return logged("upf_rtl", TOOLS["upf_rtl"])(project=project)

    @server.tool(
        description="The worst setup or hold paths of a routed RTL project at a corner "
        "(check: setup or hold; corner: typ, slow or fast), each stage with its pin, "
        "instance, cell and delay: what eco_rtl changes are chosen from."
    )
    @_stops_its_runs
    def timing_paths_rtl(
        project: str, corner: str = "slow", check: str = "setup", count: int = 5
    ) -> dict[str, Any]:
        return logged("timing_paths_rtl", TOOLS["timing_paths_rtl"])(
            project=project, corner=corner, check=check, count=count
        )

    @server.tool(
        description="Apply an engineering change order to a routed RTL project and sign it "
        "off again (re-route, timing at every corner, DRC, LVS, antenna), without placing "
        "again. changes: resize:INSTANCE=CELL (same pins, another size), "
        "buffer:INSTANCE/PIN=CELL (after a driver or before a sink), repair:setup or "
        "repair:hold, optionally @MARGIN_NS. Changes always apply to the original routed "
        "design, so pass the whole list each time. Takes minutes."
    )
    @_stops_its_runs
    def eco_rtl(project: str, changes: list[str]) -> dict[str, Any]:
        return logged("eco_rtl", TOOLS["eco_rtl"])(project=project, changes=changes)

    @server.prompt(description="Close timing on a routed RTL project with ECO changes.")
    def fix_timing(project: str) -> str:
        return (
            f"The RTL project at {project} is placed and routed. Call timing_paths_rtl for "
            "the worst setup paths at the slow corner (and hold paths at the fast corner if "
            "hold fails). Pick one change at a time: a larger cell for a slow gate on the "
            "worst path (same pins: sg13g2_nor2_1 to sg13g2_nor2_2), a buffer where one "
            "driver has many loads, or repair:setup with a margin. Call eco_rtl with every "
            "change so far, read the new slack and checks, and keep a change only if it "
            "helps and DRC, LVS and antenna stay clean. Then tell the user which changes to "
            "put in the project's eco section."
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
