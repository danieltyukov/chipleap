"""MCP server: ChipLeap's architecture tools for any MCP client.

The tools answer architecture questions (what to build and why), not tool-execution
questions. A model can run the whole loop with them: capture requirements as a spec,
ask what the target demands, explore, interrogate the bottleneck, try changes, then
generate and verify the SoC. Every tool is deterministic for the same inputs, and with
``--log-dir`` each call is appended to a JSONL run log that ``chipleap replay`` re-runs.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

from chipleap import __version__
from chipleap.explore.engine import Candidate, Evaluator, diagnose, explore
from chipleap.explore.moves import neighbours
from chipleap.explore.score import Point
from chipleap.hw.cpu import CPUS
from chipleap.hw.design import Design
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
5. generate_soc then verify_soc: X-HEEP configuration, accelerator RTL, driver, datasheet;
   Verilator simulation against a golden model, Yosys synthesis on IHP SG13G2, X-HEEP's
   own config validation.
All numbers are analytical estimates except those verify_soc measures. Say so when
reporting them."""

SPEC_REFERENCE = """# ChipLeap spec reference

```yaml
name: llama-1b-edge                 # identifier, also the design name
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
```
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
) -> dict[str, Any]:
    from chipleap.gen.bundle import write_bundle

    s = _spec(spec)
    d = _design(design)
    _, c = _candidate(s, d, weights)
    out = Path(out_dir).expanduser()
    files = write_bundle(out, d, weights=c.point.weights, spec=s, candidate=c)
    return {"out_dir": str(out), "files": [str(f.relative_to(out)) for f in files]}


def verify_soc(
    out_dir: str, steps: str = "consistency,lint,sim,synth,xheep", fetch_xheep: bool = False
) -> dict[str, Any]:
    from chipleap.verify.run import verify_bundle

    res = verify_bundle(
        Path(out_dir).expanduser(),
        steps=tuple(x.strip() for x in steps.split(",") if x.strip()),
        fetch_xheep=fetch_xheep,
    )
    return {"steps": [{"step": r.name, "status": r.status, "summary": r.summary} for r in res]}


def check_tools() -> dict[str, Any]:
    from chipleap.verify.tools import all_tools, ihp_liberty

    lib = ihp_liberty()
    return {
        "tools": [
            {"name": t.name, "found": t.found, "version": t.version, "used_for": t.purpose}
            for t in all_tools()
        ],
        "ihp_sg13g2_liberty": str(lib) if lib else None,
    }


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
}
# Tools whose results depend on the machine or write files are logged but not replayed.
SIDE_EFFECTS = {"generate_soc", "verify_soc", "check_tools"}


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
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def replay(log: Path) -> tuple[int, int]:
    """Re-run every replayable call in a run log. Returns (identical, total)."""
    ok = total = 0
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["tool"] in SIDE_EFFECTS or entry.get("error"):
            continue
        total += 1
        result = TOOLS[entry["tool"]](**entry["args"])
        ok += _digest(result) == entry["result_sha256"]
    return ok, total


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
        description="Write the SoC bundle for a design: X-HEEP configuration, "
        "accelerator RTL, X-HEEP adapter, register map, driver, testbench, datasheet."
    )
    def generate_soc(
        spec: str, design: str, out_dir: str, weights: str | None = None
    ) -> dict[str, Any]:
        return logged("generate_soc", TOOLS["generate_soc"])(
            spec=spec, design=design, out_dir=out_dir, weights=weights
        )

    @server.tool(
        description="Verify a bundle with open tools: Verilator lint, randomized "
        "simulation against a golden model with a cycle cross-check, Yosys synthesis "
        "on IHP SG13G2, X-HEEP config validation. Steps: consistency,lint,sim,synth,xheep."
    )
    def verify_soc(
        out_dir: str, steps: str = "consistency,lint,sim,synth,xheep", fetch_xheep: bool = False
    ) -> dict[str, Any]:
        return logged("verify_soc", TOOLS["verify_soc"])(
            out_dir=out_dir, steps=steps, fetch_xheep=fetch_xheep
        )

    @server.tool(
        description="Which open-source tools (Verilator, Yosys, OpenROAD, sv2v, the IHP "
        "PDK) are available on this machine."
    )
    def check_tools() -> dict[str, Any]:
        return logged("check_tools", TOOLS["check_tools"])()

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


def main(log_dir: str | None = None) -> None:
    build_server(log_dir).run("stdio")
