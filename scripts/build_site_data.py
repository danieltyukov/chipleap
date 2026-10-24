"""Build the website's data and generated content from real ChipLeap runs.

    python scripts/build_site_data.py [--verify BUNDLE [--verify-extra VERIFY.json]]
                                      [--hero RENDER.png --callouts C.json
                                       --hero-floorplan FP.json] [--og]
                                      [--rtl-project PROJECT [--rtl-verify VERIFY.json]]

The RTL flow and chip sections come from one RTL project with a chip section, run through
every step first (KLayout and the IHP PDK draw the chip):

    chipleap verify examples/uart --steps all
    python scripts/build_site_data.py --rtl-project examples/uart

A step run again on its own (gate-level simulation with another Icarus, say) is passed
with --rtl-verify and replaces that step's row; --verify-extra does the same for the
bundle's steps (lint and simulation run where Verilator is, say). Without --verify, the
accelerator's published results are kept as they are.

The hero render is made in three steps, and --hero-floorplan refuses a render whose
floorplan no longer matches what the model gives for the design:

    python scripts/build_site_data.py --write-hero-floorplan fp.json
    blender -b -P scripts/render_die.py -- fp.json callouts.json 2400 1600 project
    blender -b -P scripts/render_die.py -- fp.json die.png 2400 1600 128

Everything the site shows comes from here, so the page cannot drift from the tool:

* the story: the explorer's decisions for examples/llama-3.2-1b-decode.yaml (seed 0), with
  a to-scale floorplan for every accepted step
* the console transcript: real MCP tool calls on examples/smollm2-135m-open-pdk.yaml
* the calculator: bytes per token per preset and precision, memory bandwidths and the
  nodes that can host each interface
* the verification band: `chipleap verify` results for a bundle
* the hero images and callouts: a Blender render from scripts/render_die.py
* the RTL flow and the chip: `chipleap verify PROJECT --steps all` results for an RTL
  project with a chip section (--rtl-project examples/uart), the chip's layout drawn from
  its GDS with scripts/render_gds.py and callouts placed from its floorplan
* content hashes on the styles.css and app.js links, so run it after editing either

Only ChipLeap's built-in technologies are used: technologies added on this machine with
`chipleap tech add` (a licensed PDK, say) never reach the page.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

# Before chipleap is imported: its technology table loads the user's own technologies.
os.environ["CHIPLEAP_TECH_DIR"] = ""

from chipleap import __version__, mcp_server
from chipleap.explore import explore
from chipleap.explore.engine import Candidate
from chipleap.hw.memory import MEMORIES
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.report.floorplan import floorplan
from chipleap.spec import load_spec
from chipleap.workload.llm import PRESETS, LLMWorkloadSpec, build_llm

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"

NODE = {
    "ihp-sg13g2": "IHP 130 nm",
    "generic-65nm": "65 nm",
    "generic-28nm": "28 nm",
    "generic-16nm": "16 nm",
}
MEM = {
    "none": "on chip only",
    "qspi-flash": "QSPI flash",
    "octal-psram": "octal PSRAM",
    "hyperram": "HyperRAM",
    "lpddr2-x32": "LPDDR2",
    "lpddr4-x16": "LPDDR4",
    "lpddr4x-x32": "LPDDR4X",
    "lpddr5-x16": "LPDDR5",
}
DF = {"gemv": "GEMV", "ws": "weight-stationary", "os": "output-stationary"}


def fmt_tps(v: float) -> str:
    return f"{v:.1f}" if v >= 10 else f"{v:.2f}" if v >= 1 else f"{v:.2g}"


def cap(s: str) -> str:
    return s[0].upper() + s[1:] if s else s


def title(kind: str, label: str, c: Candidate, prev: Candidate) -> str:
    """A plain-English title for one decision."""
    d, b = c.design, prev.design
    a = d.accel
    if kind == "add_accel" and a:
        return f"Add a {DF[a.dataflow]} engine"
    if kind == "node":
        extra = []
        if d.freq_mhz != b.freq_mhz:
            extra.append(f"{d.freq_mhz:g} MHz")
        if d.ext_mem != b.ext_mem:
            extra.append(MEM[d.ext_mem])
        return f"Move to {NODE[d.tech]}" + (f", {' and '.join(extra)}" if extra else "")
    if kind in ("faster_mem", "slower_mem", "add_ext_mem"):
        return f"Switch to {MEM[d.ext_mem]}"
    if kind in ("more_channels", "fewer_channels"):
        return f"Use {d.ext_channels} memory channel{'s' if d.ext_channels > 1 else ''}"
    if kind in ("more_ports", "fewer_ports") and a:
        return f"Give the accelerator {a.ports} port{'s' if a.ports > 1 else ''}"
    if kind in ("grow_array", "shrink_array") and a:
        return f"Resize the array to {a.rows} x {a.cols}"
    if kind in ("grow_buffer", "shrink_buffer") and a:
        return f"Resize the buffers to {a.abuf_kib} and {a.wbuf_kib} KiB"
    if kind in ("more_sram", "less_sram"):
        return f"Use {d.banks} SRAM bank{'s' if d.banks > 1 else ''}"
    if kind in ("bigger_banks", "smaller_banks"):
        return f"Make the banks {d.bank_kib} KiB"
    if kind in ("interleave", "deinterleave"):
        return f"Use {d.il_banks} interleaved banks"
    if kind in ("raise_freq", "lower_freq"):
        return f"Clock at {d.freq_mhz:g} MHz"
    if kind in ("faster_cpu", "smaller_cpu"):
        return f"Switch to the {d.cpu.upper()} core"
    if kind == "dataflow" and a:
        return f"Switch to a {DF[a.dataflow]} array"
    if kind == "double_buffer" and a:
        return f"Turn double buffering {'on' if a.double_buffer else 'off'}"
    if kind in ("lower_precision", "higher_precision"):
        return f"Store weights as {c.point.weights}"
    if kind == "bus":
        return "Use the many-to-many bus" if d.bus == "NtoM" else "Use the one-to-many bus"
    if kind == "remove_accel":
        return "Remove the accelerator"
    return cap(label)


SHORT = {"sram": "SRAM", "accel": "PE", "buffer": "buf", "cpu": "CPU", "soc": "SoC", "phy": "PHY"}


def labelled(fp: dict[str, Any], c: Candidate) -> dict[str, Any]:
    """Readable block labels for the site, with a short form for small blocks."""
    d = c.design
    for b in fp["blocks"]:
        if b["kind"] == "phy":
            b["label"] = f"{MEM[d.ext_mem]} interface"
        elif b["kind"] == "accel" and d.accel:
            b["label"] = f"{DF[d.accel.dataflow]} {d.accel.rows} x {d.accel.cols}"
        elif b["kind"] == "soc":
            b["label"] = "Bus and peripherals"
        elif b["kind"] == "buffer":
            b["label"] = "Buffers"
        b["short"] = SHORT[b["kind"]]
    return fp


def point(c: Candidate) -> dict[str, Any]:
    e = c.evaluation
    return {
        "tech": c.design.tech,
        "mem": c.design.ext_mem,
        "node_label": NODE[c.design.tech],
        "mem_label": cap(MEM[c.design.ext_mem]),
        "tps": e.throughput,
        "mj": e.energy_per_unit_mj,
        "mm2": e.total_area,
        "mw": e.power_mw,
        "bound": e.bound,
        "feasible": c.assessment.feasible,
        "design": c.design.summary(),
        "floorplan": labelled(floorplan(e).to_dict(), c),
    }


BOUND_PHRASE = {"accelerator ports": "the accelerator's ports"}

# What the final design is limited by, and what would make it faster.
NEXT_GAIN = {
    "external memory": "It is still limited by external memory, so the next gain needs a "
    "faster interface rather than more compute.",
    "accelerator ports": "Its weights now arrive as fast as the accelerator's ports take them "
    "in, so more ports would raise the token rate but not lower the energy per token.",
    "compute": "It is limited by compute, so the next gain needs a larger array.",
    "on-chip memory": "It is limited by on-chip bandwidth, so the next gain needs more "
    "interleaved banks or ports.",
}


def trace() -> dict[str, Any]:
    spec = load_spec(ROOT / "examples" / "llama-3.2-1b-decode.yaml")
    r = explore(spec, samples=2000, seed=0)
    base = r.steps[0].before
    pts = [point(base)]
    w = base.evaluation.workload
    mem = MEMORIES.get(base.design.ext_mem)
    need = (w.storage.weights + w.storage.kv) / 2**20
    pts[0]["title"] = "Start from a bare microcontroller"
    pts[0]["body"] = (
        f"A {base.design.cpu.upper()} at {base.design.freq_mhz:g} MHz with two 32 KiB banks on "
        f"{NODE[base.design.tech]}. The CPU does every multiply itself, and the {need:.0f} MiB "
        "of weights and KV cache do not fit in "
        + (f"a {mem.capacity_mib} MiB {MEM[base.design.ext_mem]}." if mem else "on-chip SRAM.")
    )
    pts[0]["delta"] = f"<b>{fmt_tps(base.evaluation.throughput)}</b> tokens per second"
    for s in r.steps:
        if s.chosen is None or s.after is None:
            continue
        p = point(s.after)
        before, after = s.before.evaluation, s.after.evaluation
        p["title"] = title(s.chosen.kind, s.chosen.label, s.after, s.before)
        share = max(before.bound_share().items(), key=lambda kv: kv[1])
        p["body"] = (
            f"{cap(s.chosen.why)}. Before this step {share[1] * 100:.0f}% of the time went "
            f"to {BOUND_PHRASE.get(share[0], share[0])}."
        )
        if abs(after.throughput - before.throughput) / before.throughput < 0.01:
            bits = [f"throughput unchanged at <b>{fmt_tps(after.throughput)}</b> tokens per second"]
        else:
            bits = [
                f"<b>{fmt_tps(before.throughput)} to {fmt_tps(after.throughput)}</b> tokens per "
                "second"
            ]
        if (
            abs(after.energy_per_unit_mj - before.energy_per_unit_mj)
            > 0.01 * before.energy_per_unit_mj
        ):
            bits.append(
                f"{before.energy_per_unit_mj:.3g} to {after.energy_per_unit_mj:.3g} mJ per token"
            )
        if abs(after.total_area - before.total_area) > 0.02 * before.total_area:
            bits.append(f"{before.total_area:.2f} to {after.total_area:.2f} mm²")
        if s.after.assessment.feasible and not s.before.assessment.feasible:
            bits.append("now buildable")
        if s.after.assessment.meets and not s.before.assessment.meets:
            bits.append("meets every target")
        p["delta"] = ", ".join(bits)
        pts.append(p)
    b = r.best
    d = b.design
    plain = (
        f"a {d.accel.rows} x {d.accel.cols} {DF[d.accel.dataflow]} engine"
        if d.accel
        else "no accelerator"
    ) + (
        f" next to a {d.cpu.upper()} at {d.freq_mhz:g} MHz in {NODE[d.tech]}, fed by {MEM[d.ext_mem]}"
    )
    if r.steps[-1].chosen is None:
        pts[-1]["final"] = (
            f"No single change lowers the energy further, so the agent stops with {plain}: "
            f"{fmt_tps(b.evaluation.throughput)} tokens per second at "
            f"{b.evaluation.energy_per_unit_mj:.3g} mJ per token in {b.evaluation.total_area:.2f} "
            f"mm². {NEXT_GAIN.get(b.evaluation.bound, '')}"
        )
    return {
        "spec": spec.name,
        "workload": spec.workload().description,
        "target": spec.throughput,
        "points": pts,
        "evaluated": r.evaluated,
        "seconds": round(r.seconds, 2),
    }


def calculator() -> dict[str, Any]:
    models = []
    for key, a in PRESETS.items():
        nbytes: dict[str, float] = {}
        capacity: dict[str, float] = {}
        for fmt in ("int8", "int4"):
            w = build_llm(LLMWorkloadSpec(key, context=1024, weights=fmt, kv="int8"))
            nbytes[fmt] = w.weight_bytes_read + w.kv_bytes_read
            capacity[fmt] = w.storage.weights + w.storage.kv
        models.append(
            {"key": key, "name": a.name, "params": a.params, "bytes": nbytes, "capacity": capacity}
        )
    mems = []
    for key, m in MEMORIES.items():
        nodes = [t.key for t in TECHNOLOGIES.values() if m.available(t.node_nm, t.max_io_mts)]
        mems.append(
            {
                "key": key,
                "name": m.name,
                "sustained": m.sustained_bps,
                "capacity": m.capacity_mib * 2**20,
                "read_only": m.read_only,
                "nodes": nodes,
            }
        )
    return {
        "models": models,
        "memories": mems,
        "techs": {k: t.name for k, t in TECHNOLOGIES.items()},
    }


def hero_floorplan(hero_design: dict[str, Any]) -> dict[str, Any]:
    """The floorplan render_die.py draws for the hero: the open-PDK example's recommendation."""
    spec = load_spec(ROOT / "examples" / "smollm2-135m-open-pdk.yaml")
    design = mcp_server._design(hero_design)
    _, c = mcp_server._candidate(spec, design, None)
    fp: dict[str, Any] = floorplan(c.evaluation).to_dict()
    a = design.accel
    fp.update(
        rows=a.rows if a else 0,
        cols=a.cols if a else 0,
        design=design.summary(),
        mark="",
        submark="",
    )
    return fp


def same_floorplan(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when two floorplans have the same blocks in the same places, to 0.5% of the die."""
    tol = 0.005 * float(b["side"])
    if abs(float(a["side"]) - float(b["side"])) > tol:
        return False
    blocks = {x["id"]: x for x in b["blocks"]}
    if {x["id"] for x in a["blocks"]} != set(blocks):
        return False
    return all(
        abs(float(x[k]) - float(blocks[x["id"]][k])) <= tol
        for x in a["blocks"]
        for k in ("x", "y", "w", "h")
    )


def console() -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """A transcript made of real tool results for the open-PDK example."""
    spec_text = (ROOT / "examples" / "smollm2-135m-open-pdk.yaml").read_text()
    req = mcp_server.required_resources(spec_text)
    ex = mcp_server.explore_designs(spec_text, samples=1500, seed=0)
    rec = ex["recommended"]
    m = rec["metrics"]
    d = rec["design"]
    acc = d["accel"] or {}
    ihp = TECHNOLOGIES["ihp-sg13g2"]
    serial = [
        MEM[k]
        for k in ("hyperram", "octal-psram")
        if MEMORIES[k].available(ihp.node_nm, ihp.max_io_mts)
    ]
    lines = [
        (
            "c-user",
            "Design the smallest chip on the open IHP PDK that runs SmolLM2 135M at two "
            "tokens per second.",
        ),
        ("c-tool", "required_resources(spec)"),
        (
            "c-out",
            f"{req['stream_mb_per_step']:.0f} MB read per token: "
            f"{req['external_gbps_if_nothing_on_chip'] * 1000:.0f} MB/s sustained and "
            f"{req['capacity_mib']:.0f} MiB resident. At 130 nm only serial memories work: "
            f"{' or '.join(serial)}, several channels for capacity.",
        ),
        ("c-tool", 'explore_designs(spec, objective="area")'),
        ("c-out", summarize(ex)),
        (
            "c-ai",
            f"Recommended: {str(d['cpu']).upper()} at {d['freq_mhz']:g} MHz, "
            f"{sram_kib(d)} KiB of SRAM, a {acc.get('rows')} x {acc.get('cols')} "
            f"{DF.get(str(acc.get('dataflow')), '')} engine and {d['ext_channels']} "
            f"{MEM[str(d['ext_mem'])]} channels. {m['throughput']:.2f} tokens per second, "
            f"{m['power_mw']:.0f} mW, {m['area_mm2']:.1f} mm², limited by "
            f"{rec['bottleneck']}. Shall I generate and verify it?",
        ),
        ("c-user", "Yes."),
        ("c-tool", "generate_soc(...)  verify_soc(...)"),
    ]
    return lines, d


def sram_kib(d: dict[str, Any]) -> int:
    return int(d["banks"]) * int(d["bank_kib"]) + int(d.get("il_banks") or 0) * int(
        d.get("il_bank_kib") or 0
    )


# What each trimming move of the explorer makes smaller.
TRIMS = {
    "less_sram": "SRAM",
    "smaller_banks": "SRAM",
    "shrink_buffer": "buffers",
    "shrink_array": "array",
    "fewer_ports": "array",
    "fewer_channels": "memory channels",
    "smaller_cpu": "CPU",
}


def summarize(ex: dict[str, Any]) -> str:
    """The explorer's decisions in one plain sentence, built from the decision log."""
    steps = [s for s in ex["steps"] if s["action"]]
    kinds = [s["kind"] for s in steps]

    def last_value(*moves: str) -> str | None:
        # A move's action reads "external memory a -> b", "CPU a -> b" or "add b".
        found = [s["action"] for s in steps if s["kind"] in moves]
        if not found:
            return None
        return found[-1].rsplit(" -> ", 1)[-1] if " -> " in found[-1] else found[-1].split()[-1]

    first = []
    if "add_accel" in kinds:
        first.append("added a GEMV engine")
    if "more_channels" in kinds:
        first.append("doubled the memory channels")
    mem = last_value("faster_mem", "slower_mem", "add_ext_mem")
    if mem:
        first.append(f"moved the weights to {MEM.get(mem, mem)}")
    cpu = last_value("faster_cpu")
    if cpu:
        first.append(f"switched to a {cpu.upper()} core")
    trims = [TRIMS[k] for k in kinds if k in TRIMS]
    text = f"{ex['evaluated']:,} designs evaluated. The agent " + ", ".join(first)
    if trims:
        parts = list(dict.fromkeys(trims))
        what = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        text += f", then made {len(trims)} trims to the {what}"
    return text + ", and stopped at the smallest design that still meets the target."


def console_html(lines: list[tuple[str, str]], verify_line: str) -> str:
    body = "".join(f'<p class="{cls}">{html.escape(text)}</p>' for cls, text in lines)
    body += f'<p class="c-out">{html.escape(verify_line)}</p>'
    return f'<div class="console-body" id="console-body">{body}</div>'


def stats_html(rows: list[dict[str, Any]]) -> str:
    sim = next((r for r in rows if r["step"] == "sim"), {})
    synth = next((r for r in rows if r["step"] == "synth"), {})
    xh = next((r for r in rows if r["step"] == "xheep"), {})
    # (value, decimals, suffix counted with the value, unit set small after it, label)
    items = [
        (float(sim.get("cases", 0)), 0, "", "", "random jobs, bit-exact against a golden model"),
        (
            float(sim.get("cycle_error_no_stall_max") or 0) * 100,
            1,
            "%",
            "",
            "cycle error, model against RTL, without memory stalls",
        ),
        (
            float((xh.get("render") or {}).get("rendered", 0)),
            0,
            "",
            "",
            "X-HEEP templates rendered for the configuration",
        ),
    ]
    pr = next((r for r in rows if r["step"] == "pnr" and r["status"] == "pass"), {})
    if pr.get("clock_mhz") and pr.get("setup_slack_ns") is not None:
        # The slack, not the clock it implies: repair stops once the clock asked for is met,
        # so that clock is no limit of the design.
        items.append(
            (
                float(pr["setup_slack_ns"]),
                1,
                "",
                "ns",
                f"setup slack of the routed accelerator at its {pr['clock_mhz']:g} MHz clock, "
                "slow corner, IHP SG13G2",
            )
        )
    elif synth.get("fmax_mhz_estimate"):
        items.append(
            (
                float(synth["fmax_mhz_estimate"]),
                0,
                "",
                "MHz",
                "clock the accelerator logic reaches on IHP SG13G2",
            )
        )
    cells = "".join(
        f'<div class="stat"><span class="num" data-to="{v}" data-dec="{dec}" data-suffix="{suf}">'
        f"{v:.{dec}f}{suf}{f'<small>{unit}</small>' if unit else ''}</span>"
        f'<span class="lab">{html.escape(lab)}</span></div>'
        for v, dec, suf, unit, lab in items
    )
    return f'<div class="stats">{cells}</div>'


def verify_table(rows: list[dict[str, Any]]) -> str:
    names = {
        "consist": "Consistency",
        "lint": "Lint",
        "sim": "Simulation",
        "synth": "Synthesis",
        "xheep": "X-HEEP",
        "pnr": "Place and route",
    }
    body = []
    for r in rows:
        mark = (
            '<span class="ok">Pass</span>'
            if r["status"] == "pass"
            else html.escape(str(r["status"]))
        )
        text = str(r["summary"]).replace("mm2", "mm\u00b2")
        if text[:1].islower() and "." not in text.split()[0]:
            text = text[0].upper() + text[1:]
        body.append(
            f'<tr><th scope="row">{names.get(str(r["step"]), r["step"])}</th>'
            f"<td>{mark} {html.escape(text)}.</td></tr>"
        )
    body.append(
        '<tr><th scope="row">Model inputs</th><td><span class="ok">Tested</span> '
        "Parameter counts of all twelve LLM presets within 2% of their model cards. IHP "
        "SG13G2 SRAM area within 2% of the liberty data for every macro size. Accelerator "
        "logic area within 3% on average of synthesis across 24 configurations. The C driver "
        "plans the same tiles as the model on 6,000 random shapes.</td></tr>"
    )
    return (
        '<div class="table-scroll"><table class="vt"><tbody>'
        + "".join(body)
        + "</tbody></table></div>"
    )


def steps_html(t: dict[str, Any]) -> str:
    out = []
    for i, p in enumerate(t["points"]):
        label = "Start" if i == 0 else f"Decision {i}"
        extra = f"<p>{html.escape(p['final'])}</p>" if p.get("final") else ""
        out.append(
            f'<li data-i="{i}"><span class="n">{label}</span><h3>{html.escape(p["title"])}</h3>'
            f'<p>{html.escape(p["body"])}</p><p class="delta">{p["delta"]}</p>{extra}</li>'
        )
    return "\n".join(out)


def story_intro(t: dict[str, Any]) -> str:
    # An upper bound in steps of five seconds, so timing noise does not change the page.
    took = f"under {5 * (int(float(t['seconds']) // 5) + 1)} seconds"
    return (
        f"<p>Llama 3.2 1B decoding at {t['target']:g} tokens per second, within 8 mm² and "
        "400 mW. The agent starts from a bare microcontroller, names what limits it, tries "
        "every single change, keeps the best and says why. The floorplan is drawn to scale at "
        f"every step. This run evaluated {t['evaluated']:,} designs in {took}.</p>"
    )


def callouts_html(pos: dict[str, list[float]], design: dict[str, Any]) -> tuple[str, str]:
    """Callouts over the render, and the numbered key that replaces their tags on phones."""
    acc = design.get("accel") or {}
    banks = int(design["banks"]) + int(design.get("il_banks") or 0)
    kib = int(design["banks"]) * int(design["bank_kib"]) + int(design.get("il_banks") or 0) * int(
        design.get("il_bank_kib") or 0
    )
    labels = {
        "sram0": ("SRAM", f"{kib} KiB" + (f" in {banks} banks" if banks > 1 else "")),
        "pe": (
            f"{DF.get(str(acc.get('dataflow')), '')} engine",
            f"{acc.get('rows')} x {acc.get('cols')} int8 MACs",
        ),
        "cpu": ("RISC-V core", str(design["cpu"]).upper()),
        "phy": ("Memory interface", f"{design['ext_channels']} x {MEM[str(design['ext_mem'])]}"),
    }
    # Tag offsets from the anchor, in pixels; negative x puts the tag on the left, anchored
    # by its right edge, so the leader line always ends at the tag.
    offsets = {"sram0": (46, -70), "pe": (40, 40), "cpu": (44, -54), "phy": (-48, 34)}
    out, key_items = [], []
    for key, (head, sub) in labels.items():
        if key not in pos:
            continue
        x, y = pos[key]
        tx, ty = offsets[key]
        side = "left" if tx < 0 else "right"
        n = len(out) + 1
        line = f'<svg width="1" height="1"><line x1="0" y1="0" x2="{tx}" y2="{ty + 18}"/></svg>'
        out.append(
            f'<div class="callout {side}" style="--x:{x * 100:.1f}%;--y:{y * 100:.1f}%;'
            f'--tx:{tx}px;--ty:{ty}px">{line}<span class="dot"><i>{n}</i></span><span class="tag">'
            f"<b>{head}</b>{html.escape(sub)}</span></div>"
        )
        key_items.append(f"<li><b>{head}</b> {html.escape(sub)}</li>")
    key_html = '<ol class="callout-key">' + "".join(key_items) + "</ol>"
    return "\n".join(out), key_html


DATAFLOWS = {
    "gemv": "GEMV engine",
    "ws": "weight-stationary array",
    "os": "output-stationary array",
}


def accel_label(bundle: Path) -> str:
    """The bundle's accelerator in words, as the layout band names it: "1 x 8 GEMV engine"."""
    d = yaml.safe_load((bundle / "design.yaml").read_text())
    a = (d.get("design") or d)["accel"]
    return f"{a['rows']} x {a['cols']} {DATAFLOWS.get(a['dataflow'], a['dataflow'] + ' array')}"


def layout_html(rows: list[dict[str, Any]], label: str) -> str:
    """The routed layout beside the numbers LibreLane reported for it."""
    pr = next((r for r in rows if r["step"] == "pnr"), None)
    if pr is None or pr["status"] != "pass":
        return ""
    clean = not (
        pr["drc_errors"]
        or pr["lvs_errors"]
        or pr.get("antenna_nets")
        or _ihp_antenna(pr)
        or pr.get("max_slew_violations")
        or pr.get("max_cap_violations")
    )
    checks = "DRC, LVS and antenna clean" if clean else "see report"
    if pr.get("macros"):
        n = len(pr["macros"])
        buffers = f"Its buffers are {n} of IHP's SRAM macros, in rows above and below the logic."
    else:
        buffers = (
            "Its buffers are SRAM macros in silicon, cut to "
            f"{pr.get('buffer_words', 16)} words here."
        )
    clock = f"{pr['clock_mhz']:g}"
    facts = [
        ("Standard cells", f"{pr['cells']:,}", ""),
        ("Core area", f"{pr['core_um2'] / 1e6:.2f}", "mm²"),
        (f"Setup slack at {clock} MHz, slow corner", f"{pr['setup_slack_ns']:+.1f}", "ns"),
        ("Hold slack, worst corner", f"{pr['hold_slack_ns']:+.3f}", "ns"),
    ]
    dl = "".join(
        f"<div><dt>{k}</dt><dd>{v}{f'<small>{u}</small>' if u else ''}</dd></div>"
        for k, v, u in facts
    )
    return (
        '<div class="layout"><picture><source type="image/webp" srcset="img/layout-1200.webp">'
        '<img src="img/layout-1200.jpg" width="1000" height="1000" loading="lazy" '
        f'alt="The routed layout of the {html.escape(label)} on IHP SG13G2: signal wires on '
        'four metal layers under the power grid"></picture>'
        "<div><h3>And then it is placed and routed</h3>"
        "<p>The same accelerator, through LibreLane on IHP SG13G2: placement, clock tree, "
        f"routing, timing at every corner, {checks}. Routed for its {clock} MHz clock, it meets "
        f"timing with {pr['setup_slack_ns']:.1f} ns to spare at the slow corner (hold "
        f"{pr['hold_slack_ns']:+.3f} ns). This is the GDS it wrote, drawn in the PDK's layer "
        f"colours. {buffers}"
        f"</p><dl>{dl}</dl></div></div>"
    )


# The RTL flow and the chip, from an RTL project's `chipleap verify --steps all`


def _n(v: float) -> str:
    """A number as the page prints it: no trailing zeros."""
    return f"{v:g}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _lint(r: dict[str, Any]) -> tuple[str, str]:
    t = r.get("tools") or {}
    names = {"verilator": "Verilator", "slang": "slang"}
    tools = ", ".join(f"{names.get(k, k)} {str(v).split('+')[0]}" for k, v in t.items())
    found = int(r.get("errors", 0)) + int(r.get("warnings", 0))
    return tools, "no warnings" if not found else _plural(found, "message")


def _sim(r: dict[str, Any]) -> tuple[str, str]:
    sim = f"{str(r.get('simulator', '')).capitalize()} {r.get('simulator_version', '')}".strip()
    total = int(r["passed"]) + int(r["failed"])
    return f"cocotb on {sim}", f"{r['passed']} of {total} tests pass"


def _coverage(r: dict[str, Any]) -> tuple[str, str]:
    k = r["kinds"]
    text = ", ".join(f"{kind} {_n(k[kind]['percent'])} %" for kind in ("line", "branch", "toggle"))
    left = int(r.get("uncovered_total", 0))
    if left:
        text += f"; {_plural(left, 'point')} no test reaches"
    return f"Verilator {r.get('simulator_version', '')}", text


def _formal(r: dict[str, Any]) -> tuple[str, str]:
    checks = r["checks"]
    engines = sorted({str(c["engine"]) for c in checks})
    proved = [c for c in checks if c["status"] == "pass" and c["mode"] == "prove"]
    if len(proved) == len(checks):
        text = (
            "the design's assertions proved for every cycle"
            if len(checks) == 1
            else f"all {len(checks)} checks proved for every cycle"
        )
    else:
        text = f"{len(proved)} of {len(checks)} checks proved for every cycle"
    return f"SymbiYosys, {', '.join(engines)}", text


def _cdc(r: dict[str, Any]) -> tuple[str, str]:
    acc = r.get("accepted") or []
    text = f"{_plural(int(r['crossings']), 'crossing')}, " + (
        "all synchronized" if not r["errors"] else f"{r['errors']} unsafe"
    )
    sync = [a for a in acc if a.get("rule") == "synchronizer"]
    if sync:
        text += f" ({sync[0]['stages']}-flop synchronizer on {sync[0]['from']})"
    said = {"reset_release": "the reset is released asynchronously"}
    warned = [f for f in r.get("findings") or [] if f.get("severity") == "warning"]
    for f in warned[:1]:
        text += f"; a warning: {said.get(str(f.get('rule')), str(f.get('rule')))}"
    return "ChipLeap's checker on Yosys's netlist", text


def _synth(r: dict[str, Any]) -> tuple[str, str]:
    return (
        "Yosys and ABC on IHP's cells",
        f"{r['cells']} cells, {r['flops']} flip-flops, {r['area_um2'] / 1e6:.4f} mm², "
        f"critical path {r['critical_path_ns']:.2f} ns",
    )


def _equiv(r: dict[str, Any]) -> tuple[str, str]:
    parts = []
    for p in r["pairs"]:
        what = {"rtl-synth": "RTL to netlist", "synth-pnr": "netlist to layout"}.get(
            str(p["pair"]), str(p["pair"])
        )
        if p["proved"] == p["points"]:
            parts.append(f"{what}: all {p['points']} points proved")
        else:
            parts.append(
                f"{what}: {p['proved']} of {p['points']} proved, the rest equal for "
                f"{p['depth']} cycles"
            )
    return "Yosys equivalence", "; ".join(parts)


def _ihp_antenna(r: dict[str, Any]) -> int | None:
    """The violations of IHP's antenna deck in a result, None when it did not run."""
    ant = r.get("ihp_antenna") or {}
    return None if "violations" not in ant else int(ant["violations"])


def _pnr(r: dict[str, Any]) -> tuple[str, str]:
    ihp = _ihp_antenna(r)
    slew = int(r.get("max_slew_violations") or 0) + int(r.get("max_cap_violations") or 0)
    clean = not (r["drc_errors"] or r["lvs_errors"] or r["antenna_nets"] or ihp or slew)
    if clean:
        checks = "DRC, LVS and antenna clean"
        if ihp is not None:
            checks += " (IHP's antenna deck too)"
        checks += ", no slew or capacitance violations"
    else:
        checks = "DRC, LVS, antenna, slew or capacitance violations"
    return (
        "LibreLane (OpenROAD, KLayout), signed off at the slow corner",
        f"{r['cells']} cells on a {r['core_um2'] / 1e6:.4f} mm\u00b2 core; {_n(r['clock_mhz'])} "
        f"MHz met at 125 \u00b0C (about {r['fmax_worst_mhz']:.0f} MHz there); {checks}",
    )


def _gls(r: dict[str, Any]) -> tuple[str, str]:
    sim = f"{str(r.get('simulator', '')).capitalize()} {r.get('simulator_version', '')}".strip()
    total = int(r["passed"]) + int(r["failed"])
    where = "the routed netlist with SDF delays" if r.get("sdf") else "the synthesized netlist"
    return f"cocotb on {sim}, IHP's cell models", f"{r['passed']} of {total} tests pass on {where}"


def _dft(r: dict[str, Any]) -> tuple[str, str]:
    chains = r["chains"]
    lengths = sorted({c["length"] for c in chains})
    of = str(lengths[0]) if len(lengths) == 1 else f"{lengths[0]} to {lengths[-1]}"
    text = f"{_plural(len(chains), 'chain')} of {of} flip-flops"
    tools = "OpenROAD scan insertion"
    a = r.get("atpg")
    if a:
        tools += ", " + str(a["tool"])
        text += f", {a['coverage_pct']:.1f} % stuck-at coverage with {a['patterns']} patterns"
    sims = [k for k in ("simulation", "simulation_routed") if r.get(k)]
    if sims and all((r[k] or {}).get("status", "pass") == "pass" for k in sims):
        text += ", which pass through the chains" + (
            " of the routed netlist" if "simulation_routed" in sims else ""
        )
    return tools, text


def _power(r: dict[str, Any]) -> tuple[str, str]:
    c = next((c for c in r["corners"] if c["corner"] == "typ"), r["corners"][0])
    if c.get("vcd_mw") is not None:
        text = (
            f"{c['vcd_mw']:.3g} mW at the typical corner from the tests' activity "
            f"({c['vectorless_mw']:.3g} mW vectorless)"
        )
    else:
        text = f"{c['vectorless_mw']:.3g} mW vectorless at the typical corner"
    return "OpenROAD with the project's own tests", text


def _ir_drop(r: dict[str, Any]) -> tuple[str, str]:
    worst = max(r["nets"], key=lambda n: n["worst_mv"])
    return (
        "OpenROAD PDNSim on the routed grid",
        f"{worst['worst_mv']:.2g} mV at worst, {worst['worst_pct']:.2g} % of {_n(r['supply_v'])} V",
    )


def _upf(r: dict[str, Any]) -> tuple[str, str]:
    doms = r["domains"]
    nets = sum(int(c["nets"]) for c in r["crossings"])
    inside = [d["name"] for d in doms if d.get("placed_inside")]
    text = f"{_plural(len(doms), 'domain')}"
    if inside:
        text += f", {', '.join(inside)} placed in its own region"
    text += f"; {nets} signals between them"
    return "UPF read and placed by OpenROAD", text


def _summary(r: dict[str, Any]) -> tuple[str, str]:
    return "", str(r["summary"]).split("; ")[0]


RTL_GROUPS: list[tuple[str, list[tuple[str, str, Any]]]] = [
    (
        "Check the RTL",
        [
            ("lint", "Lint", _lint),
            ("sim", "Simulation", _sim),
            ("coverage", "Coverage", _coverage),
            ("formal", "Formal", _formal),
            ("cdc", "Clock and reset crossings", _cdc),
        ],
    ),
    (
        "Implement",
        [
            ("synth", "Synthesis", _synth),
            ("pnr", "Place and route", _pnr),
            ("equiv", "Equivalence", _equiv),
            ("gls", "Gate-level simulation", _gls),
            ("eco", "ECO", _summary),
        ],
    ),
    (
        "After the layout",
        [
            ("dft", "Design for test", _dft),
            ("power", "Power", _power),
            ("ir_drop", "IR drop", _ir_drop),
            ("upf", "Power intent", _upf),
        ],
    ),
]


def step_rows(base: Path, extra: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """The `chipleap verify` results in ``base`` by step; a step in a later ``extra``
    verify.json (a step run again on its own) replaces the same step's row, in its place.
    A bundle's verify.json is a list of rows, a project's has them under ``steps``."""
    rows: dict[str, dict[str, Any]] = {}
    for f in [base, *(Path(e) for e in extra or [])]:
        data = json.loads(Path(f).read_text())
        for r in data["steps"] if isinstance(data, dict) else data:
            rows[str(r["step"])] = r
    return rows


def rtl_rows(project: Path, extra: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """The project's last `chipleap verify` results, by step, with ``extra``'s rows."""
    return step_rows(project / "build" / "verify" / "verify.json", extra)


def chip_data(project: Path) -> dict[str, Any]:
    return json.loads((project / "build" / "chip" / "chip.json").read_text())


def chip_rows(
    chip: dict[str, Any], macros: dict[str, dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """The chip group of the flow: (step, tools, result) from chip.json."""
    o = chip["outline_um"]
    side = (o[2] - o[0]) / 1000
    io = chip.get("io_timing") or {}
    slow = next((c for c in io.get("corners", []) if c["corner"] == "slow"), None)
    blocks = ", ".join(
        f"{i['producer']}'s {i['cell']}, {_n(i['size'][0])} x {_n(i['size'][1])} um"
        for i in macros.values()
    )
    rows = [
        ("Macros", "macro/v1 packages, checked, their views rewritten", blocks or "none"),
        (
            "Chip assembly",
            "LibreLane with IHP's pad ring; bond pads, seal ring and fill in KLayout",
            f"{chip['pads']} pads on a {side:.2f} x {side:.2f} mm die",
        ),
    ]
    if slow is not None and slow["setup_slack_ns"] is not None:
        rows.append(
            (
                "Timing at the ports",
                "OpenSTA through IHP's IO cells, every corner",
                f"{slow['setup_slack_ns']:+.3f} ns of setup slack at the slow corner "
                f"({chip['setup_slack_ns']:+.3f} ns at the core's side of the pads)",
            )
        )
    lvs = "LVS match" if chip["lvs"]["result"] == "pass" else f"LVS {chip['lvs']['result']}"
    ihp = _ihp_antenna(chip)
    rows.append(
        (
            "Signoff",
            "IHP's DRC, LVS and antenna decks, OpenROAD's antenna check"
            if ihp is not None
            else "IHP's DRC and LVS decks, antenna",
            f"{chip['drc']['violations']} DRC violations, {lvs}, "
            f"{chip['antenna_nets'] + (ihp or 0)} antenna violations",
        )
    )
    return rows


def rtl_flow_html(rows: dict[str, dict[str, Any]], chip: list[tuple[str, str, str]]) -> str:
    groups = []
    for title, steps in RTL_GROUPS:
        items = []
        for key, name, fmt in steps:
            r = rows.get(key)
            if r is None:
                continue
            if r["status"] == "skipped":
                tools, res, cls = "", f"not run here: {r['summary']}", "res skip"
            else:
                tools, res = fmt(r)
                cls = "res" if r["status"] == "pass" else "res warn"
            items.append((name, tools, res, cls))
        groups.append((title, items))
    groups.append(("The chip", [(n, t, res, "res") for n, t, res in chip]))
    out = []
    for title, items in groups:
        lis = "".join(
            f"<li><b>{html.escape(n)}</b>"
            + (f'<span class="tool">{html.escape(t)}</span>' if t else "")
            + f'<span class="{c}">{html.escape(res)}</span></li>'
            for n, t, res, c in items
        )
        out.append(
            f'<details class="rtl-group" open><summary>{html.escape(title)}'
            f"<small>{_plural(len(items), 'step')}</small></summary><ol>{lis}</ol></details>"
        )
    return '<div class="rtl-flow">' + "".join(out) + "</div>"


def rtl_intro(where: str, description: str, clock_mhz: float) -> str:
    what = description.rstrip(".")
    if what[:1].isupper() and not what[1:2].isupper():
        what = what[0].lower() + what[1:]
    return (
        "<p>Point ChipLeap at a directory of Verilog or SystemVerilog with cocotb tests and it "
        "runs what a tape-out asks of RTL, on open tools and the IHP SG13G2 PDK. Below is what "
        f"each step reported for <code>{html.escape(where)}</code>, {html.escape(what)}, at "
        f"{_n(clock_mhz)} MHz.</p>"
    )


def chip_images(src: Path) -> None:
    from PIL import Image

    im = Image.open(src).convert("RGB")
    out = SITE / "img"
    out.mkdir(exist_ok=True)
    for w in (1000, 1400):
        r = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS) if im.width != w else im
        r.save(out / f"chip-{w}.webp", quality=72 if w == 1000 else 55, method=6)
        if w == 1000:
            r.save(out / "chip-1000.jpg", quality=78, optimize=True, progressive=True)


def render_chip(project: Path, chip: dict[str, Any], dest: Path) -> None:
    """The chip before fill, drawn from its GDS in IHP's layer colours, cropped to its
    outline so floorplan coordinates map straight onto the image."""
    from chipleap.chip import io

    pdk = io.pdk_dir()
    klayout = shutil.which("klayout")
    if pdk is None or klayout is None:
        raise SystemExit("rendering the chip needs KLayout and the IHP PDK")
    name = str(chip["name"])
    gds = project / "build" / "chip" / "finish" / f"{name}_nofill.gds"
    o = chip["outline_um"]
    subprocess.run(
        [
            klayout,
            "-zz",
            "-r",
            str(ROOT / "scripts" / "render_gds.py"),
            "-rd",
            f"gds={gds}",
            "-rd",
            f"lyp={pdk / 'libs.tech' / 'klayout' / 'tech' / 'sg13g2.lyp'}",
            "-rd",
            f"out={dest}",
            "-rd",
            "width=2000",
            "-rd",
            "bg=#0d1512",
            "-rd",
            f"box={o[0]},{o[1]},{o[2]},{o[3]}",
        ],
        check=True,
    )


def chip_callouts(
    project: Path, chip: dict[str, Any], macros: dict[str, dict[str, Any]]
) -> list[tuple[str, str, float, float, int, int]]:
    """(head, sub, x, y, tag dx, tag dy) for the chip picture, from its floorplan: x and y
    as fractions of the image, which spans the outline."""
    from chipleap.chip import assemble, floorplan
    from chipleap.chip import io as chip_io
    from chipleap.rtl.project import load_project

    proj = load_project(project)
    pdk = chip_io.pdk_dir()
    assert pdk is not None and proj.chip is not None
    cells = chip_io.read_lef(chip_io.lef_path(pdk).read_text())
    plan = floorplan.plan(proj.chip, cells, assemble.manifest_sizes(proj))
    o = chip["outline_um"]

    def at(x: float, y: float) -> tuple[float, float]:
        return (x - o[0]) / (o[2] - o[0]), 1 - (y - o[1]) / (o[3] - o[1])

    bond = json.loads((project / "build" / "chip" / f"{chip['name']}.bondmap.json").read_text())
    analog = next((b for b in bond if b["kind"] == "analog"), None)
    south = [b for b in bond if b["side"] == "south" and b["kind"] == "signal"]
    out = []
    c = chip["core_um"]
    cx, cy = at((c[0] + c[2]) / 2, (c[1] + c[3]) / 2)
    out.append(("Core", f"{proj.top}, {chip['cells']} standard cells", cx, cy, 46, 34))
    for m in chip["macros"][:1]:
        info = macros[m["instance"]]
        w, h = info["size"]
        x, y = m["at_um"]
        head = "Analog macro" if info["kind"] == "analog" else "Macro"
        sub = f"{info['cell']}, from {info['producer']}"
        out.append((head, sub, *at(x + w / 2, y + h / 2), 40, -100))
    if analog is not None:
        users = [i for i, info in macros.items() if analog["net"] in info["nets"].values()]
        sub = f"{analog['net']}" + (f", into {macros[users[0]]['cell']}" if users else "")
        out.append(("Analog pad", sub, *at(analog["x_um"], analog["y_um"]), 46, -60))
    if south:
        pad = south[len(south) // 2]
        ring_y = plan.bond_um + plan.pad_depth / 2
        sub = f"{chip['pads']} pads of IHP's sg13g2_io"
        out.append(("IO ring", sub, *at(pad["x_um"], ring_y), -44, -70))
    return out


def chip_html(chip: dict[str, Any], marks: list[tuple[str, str, float, float, int, int]]) -> str:
    o = chip["outline_um"]
    side = (o[2] - o[0]) / 1000
    io = chip.get("io_timing") or {}
    slow = next((c for c in io.get("corners", []) if c["corner"] == "slow"), None)
    callouts, keys = [], []
    for i, (head, sub, x, y, tx, ty) in enumerate(marks, 1):
        sidecls = "left" if tx < 0 else "right"
        line = f'<svg width="1" height="1"><line x1="0" y1="0" x2="{tx}" y2="{ty + 18}"/></svg>'
        callouts.append(
            f'<div class="callout {sidecls}" style="--x:{x * 100:.1f}%;--y:{y * 100:.1f}%;'
            f'--tx:{tx}px;--ty:{ty}px">{line}<span class="dot"><i>{i}</i></span>'
            f'<span class="tag"><b>{html.escape(head)}</b>{html.escape(sub)}</span></div>'
        )
        keys.append(f"<li><b>{html.escape(head)}</b> {html.escape(sub)}</li>")
    facts = [
        ("Die with seal ring", f"{side:.2f} x {side:.2f}", "mm"),
        ("Pads", str(chip["pads"]), ""),
        ("DRC violations, IHP deck", str(chip["drc"]["violations"]), ""),
        (
            "LVS, IHP deck",
            "match" if chip["lvs"]["result"] == "pass" else str(chip["lvs"]["result"]),
            "",
        ),
    ]
    if slow is not None and slow["setup_slack_ns"] is not None:
        facts.append(
            ("Setup slack at the ports, slow corner", f"{slow['setup_slack_ns']:+.3f}", "ns")
        )
        holds = [c["hold_slack_ns"] for c in io["corners"] if c["hold_slack_ns"] is not None]
        if holds:
            facts.append(("Hold slack at the ports, worst corner", f"{min(holds):+.3f}", "ns"))
    dl = "".join(
        f"<div><dt>{html.escape(k)}</dt><dd>{v}{f'<small>{u}</small>' if u else ''}</dd></div>"
        for k, v, u in facts
    )
    worst = ((io.get("paths") or {}).get("core_to_pad_setup") or [None])[0]
    timing = ""
    if worst is not None and slow is not None and slow["setup_slack_ns"] is not None:
        which = (
            "the chip's worst path at the slow corner"
            if abs(worst["slack_ns"] - slow["setup_slack_ns"]) < 1e-3
            else "its worst path to an output pad at the slow corner"
        )
        timing = (
            f"<p>Timed at its ports through IHP's IO cells, {which} spends "
            f"{worst['pad_delay_ns']:.1f} ns inside the output pad, driving "
            f"{_n(io['budget']['output_load_pf'])} pF: setup slack falls from "
            f"{chip['setup_slack_ns']:+.3f} ns at the core's side of the pads to "
            f"{slow['setup_slack_ns']:+.3f} ns at the ports.</p>"
        )
    alt = (
        f"The {chip['name']} layout drawn from its GDS before fill: {chip['pads']} bond pads "
        "and IHP's IO cells around the core, a comparator from aispice in the core, and the "
        "seal ring"
    )
    return (
        '<div class="chip-grid"><figure class="chip-art"><div class="chip-frame"><picture>'
        '<source type="image/webp" srcset="img/chip-1000.webp 1000w, img/chip-1400.webp 1400w" '
        'sizes="(max-width: 980px) 100vw, 640px">'
        f'<img src="img/chip-1000.jpg" width="1000" height="1000" loading="lazy" alt="{html.escape(alt)}">'
        "</picture>" + "".join(callouts) + "</div>"
        f'<ol class="callout-key">{"".join(keys)}</ol>'
        f"<figcaption>{html.escape(str(chip['name']))}, drawn from the GDS ChipLeap wrote, "
        "before fill.</figcaption>"
        f'</figure><div class="chip-facts"><dl>{dl}</dl>{timing}'
        "<p>Every check is IHP's own: the DRC deck on the whole die with its density rules, the "
        "LVS deck down to the macros' transistors"
        + (", the antenna deck" if _ihp_antenna(chip) is not None else "")
        + ". "
        '<a href="https://github.com/danieltyukov/chipleap/blob/main/docs/chips.md">How a chip '
        "is built and checked</a>.</p></div></div>"
    )


def macro_info(project: Path) -> dict[str, dict[str, Any]]:
    """Each macro instance of the chip section: its cell, size, kind, producer and the
    nets its pins connect to, from the project and its packages' manifests."""
    from chipleap.macro.manifest import load_manifest
    from chipleap.rtl.project import load_project

    proj = load_project(project)
    assert proj.chip is not None
    out = {}
    for m in proj.chip.macros:
        man = load_manifest(m.package)
        out[m.instance] = {
            "cell": man.name,
            "size": (man.width_um, man.height_um),
            "kind": man.kind,
            "producer": man.producer.tool,
            "nets": dict(m.connect),
        }
    return out


def layout_images(src: Path) -> None:
    from PIL import Image

    im = Image.open(src).convert("RGB")
    # Shown at about 560 px; a layout is all fine detail, so 1000 px at moderate quality
    # keeps it sharp on high-density screens without a heavy download.
    im = im.resize((1000, round(im.height * 1000 / im.width)), Image.LANCZOS)
    im.save(SITE / "img" / "layout-1200.webp", quality=70, method=6)
    im.save(SITE / "img" / "layout-1200.jpg", quality=76, optimize=True, progressive=True)


def stamp_assets(page: Path) -> None:
    """Version the stylesheet and script by content, so a cached copy is never stale."""
    text = page.read_text()
    for name in ("styles.css", "app.js"):
        digest = hashlib.sha256((SITE / name).read_bytes()).hexdigest()[:10]
        text = re.sub(rf'"{re.escape(name)}(\?v=[0-9a-f]+)?"', f'"{name}?v={digest}"', text)
    page.write_text(text)


def inject(page: Path, marker: str, content: str) -> None:
    text = page.read_text()
    pattern = re.compile(rf"(<!-- {marker}:start -->).*?(<!-- {marker}:end -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"marker {marker} not found in {page}")
    page.write_text(pattern.sub(lambda m: m.group(1) + "\n" + content + "\n" + m.group(2), text))


def images(hero: Path, callouts: dict[str, list[float]] | None) -> dict[str, list[float]] | None:
    """Trim the render, write web sizes and a social card. Returns callouts in trimmed coordinates."""
    from PIL import Image

    out = SITE / "img"
    out.mkdir(exist_ok=True)
    im = Image.open(hero).convert("RGBA")
    W0, H0 = im.size
    bbox = im.getbbox() or (0, 0, W0, H0)
    pad = 24
    box = (
        max(0, bbox[0] - pad),
        max(0, bbox[1] - pad),
        min(W0, bbox[2] + pad),
        min(H0, bbox[3] + pad),
    )
    im = im.crop(box)
    # Feather the edges so the faint shadow never shows the frame of the render. Only the
    # shadow fades: opaque pixels (the package itself) and their antialiased rim are kept.
    from PIL import ImageChops, ImageDraw, ImageFilter

    fade = Image.new("L", im.size, 0)
    mx, my = int(im.width * 0.07), int(im.height * 0.08)
    ImageDraw.Draw(fade).rectangle((mx, my, im.width - mx, im.height - my), fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(min(mx, my) * 0.6))
    alpha = im.getchannel("A")
    solid = alpha.point(lambda v: 255 if v > 200 else 0)
    solid = solid.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))
    im.putalpha(ImageChops.multiply(alpha, ImageChops.lighter(solid, fade)))
    for w in (2400, 1200):
        r = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS) if im.width != w else im
        r.save(out / f"die-{w}.webp", quality=86, method=6)
        if w == 1200:
            r.save(out / "die-1200.png", optimize=True)
    card = Image.new("RGBA", (1200, 630), (246, 247, 245, 255))
    art = im.resize((800, round(im.height * 800 / im.width)), Image.LANCZOS)
    card.alpha_composite(art, (440, max(0, (630 - art.height) // 2)))
    card.save(SITE / "og-base.png")
    if callouts is None:
        return None
    cw, ch = box[2] - box[0], box[3] - box[1]
    return {k: [(x * W0 - box[0]) / cw, (y * H0 - box[1]) / ch] for k, (x, y) in callouts.items()}


def og_base_from_site() -> None:
    """The social card's background from the hero image already on the site, for a new
    card without rendering the die again."""
    from PIL import Image

    im = Image.open(SITE / "img" / "die-1200.png").convert("RGBA")
    card = Image.new("RGBA", (1200, 630), (246, 247, 245, 255))
    art = im.resize((800, round(im.height * 800 / im.width)), Image.LANCZOS)
    card.alpha_composite(art, (440, max(0, (630 - art.height) // 2)))
    card.save(SITE / "og-base.png")


def published_console_line(page: Path) -> str:
    """The console's last line as the page has it, kept when no bundle is given."""
    text = page.read_text()
    m = re.search(
        r'<!-- console:start -->.*<p class="c-out">(.*?)</p></div>\s*<!-- console:end -->',
        text,
        re.S,
    )
    return (
        html.unescape(m.group(1)) if m else "Consistency, lint, simulation and X-HEEP checks pass."
    )


def og_card() -> None:
    exe = shutil.which("rsvg-convert")
    base = SITE / "og-base.png"
    if not exe or not base.exists():
        return
    b64 = base64.b64encode(base.read_bytes()).decode()
    mark = (ROOT / "assets" / "logo-mark.svg").read_text()
    inner = mark[mark.index(">", mark.index("<svg")) + 1 : mark.rindex("</svg>")]
    font = "Space Grotesk, Helvetica, Arial, sans-serif"
    body = "IBM Plex Sans, Helvetica, Arial, sans-serif"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">
<image href="data:image/png;base64,{b64}" width="1200" height="630"/>
<g transform="translate(72 70) scale(0.8)">{inner}</g>
<text x="134" y="108" font-family="{font}" font-weight="600" font-size="36" fill="#0e1311">ChipLeap</text>
<text x="72" y="290" font-family="{font}" font-weight="600" font-size="62" fill="#0e1311" letter-spacing="-2">Design the chip</text>
<text x="72" y="360" font-family="{font}" font-weight="600" font-size="62" fill="#0e1311" letter-spacing="-2">your model needs.</text>
<text x="72" y="430" font-family="{body}" font-size="24" fill="#57605b">From a model or your RTL to a chip.</text>
<text x="72" y="556" font-family="{body}" font-size="21" fill="#0b6340">Open source, MIT licensed</text>
</svg>"""
    tmp = SITE / "og.svg"
    tmp.write_text(svg)
    subprocess.run(
        [exe, "-w", "1200", "-h", "630", str(tmp), "-o", str(SITE / "og.png")], check=True
    )
    tmp.unlink()
    base.unlink()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", help="bundle whose verify/verify.json to show")
    ap.add_argument("--hero", help="Blender render of the hero die (PNG with alpha)")
    ap.add_argument("--callouts", help="block positions from render_die.py ... project")
    ap.add_argument("--hero-floorplan", help="the floorplan JSON the hero was rendered from")
    ap.add_argument("--layout", help="KLayout render of the routed GDS (scripts/render_gds.py)")
    ap.add_argument(
        "--rtl-project",
        help="an RTL project with a chip section, after `chipleap verify PROJECT --steps all`",
    )
    ap.add_argument(
        "--verify-extra",
        action="append",
        help="a verify.json of the bundle's steps run on their own, replacing those rows "
        "(repeatable)",
    )
    ap.add_argument(
        "--rtl-verify",
        action="append",
        help="a verify.json of steps run again on their own, replacing those rows (repeatable)",
    )
    ap.add_argument(
        "--og", action="store_true", help="redraw the social card from the hero image on the site"
    )
    ap.add_argument(
        "--write-hero-floorplan", help="write the floorplan to render for the hero, then exit"
    )
    args = ap.parse_args()
    if args.write_hero_floorplan:
        _, hero = console()
        Path(args.write_hero_floorplan).write_text(json.dumps(hero_floorplan(hero), indent=1))
        print(f"wrote {args.write_hero_floorplan}")
        return

    page = SITE / "index.html"
    previous: dict[str, Any] = {}
    if (SITE / "data.json").exists():
        previous = json.loads((SITE / "data.json").read_text())
    t = trace()
    # The run time is shown rounded in the intro; leaving it out of data.json keeps rebuilds
    # from changing the file for timing noise.
    shown = {k: v for k, v in t.items() if k != "seconds"}
    data: dict[str, Any] = {"version": __version__, "trace": shown, "calculator": calculator()}
    inject(page, "story-intro", story_intro(t))
    inject(page, "steps", steps_html(t))
    lines, hero_design = console()
    verify_line = "Consistency, lint, simulation and X-HEEP checks pass."
    if args.verify:
        bundle_rows = step_rows(Path(args.verify) / "verify" / "verify.json", args.verify_extra)
        rows = list(bundle_rows.values())
        # Only what the page shows: the full rows carry local paths and tool logs.
        data["verification"] = [
            {k: r[k] for k in ("step", "status", "summary") if k in r} for r in rows
        ]
        inject(page, "stats", stats_html(rows))
        inject(page, "verify", verify_table(rows))
        if args.layout:
            layout_images(Path(args.layout))
        if (SITE / "img" / "layout-1200.jpg").exists():
            inject(page, "layout", layout_html(rows, accel_label(Path(args.verify))))
        sim = next((r for r in rows if r["step"] == "sim"), None)
        if all(r["status"] == "pass" for r in rows) and sim:
            verify_line = (
                f"All checks pass. {sim['cases']} randomized jobs bit-exact; cycle counts "
                "match the model exactly without memory stalls."
            )
        else:
            names = {
                "consist": "consistency",
                "sim": "simulation",
                "synth": "synthesis",
                "xheep": "X-HEEP",
            }
            passed = [names.get(r["step"], r["step"]) for r in rows if r["status"] == "pass"]
            if passed:
                joined = ", ".join(passed[:-1]) + (" and " if len(passed) > 1 else "") + passed[-1]
                verify_line = joined[0].upper() + joined[1:] + " pass."
            else:
                verify_line = "Verification did not pass."
    if not args.verify:
        # The accelerator's results stay as published until a bundle is given again.
        if "verification" in previous:
            data["verification"] = previous["verification"]
        verify_line = previous.get("console_verify") or published_console_line(page)
    data["console_verify"] = verify_line
    inject(page, "console", console_html(lines, verify_line))
    if args.rtl_project:
        project = Path(args.rtl_project)
        rows = rtl_rows(project, args.rtl_verify)
        chip = chip_data(project)
        macros = macro_info(project)
        from chipleap.rtl.project import load_project

        proj = load_project(project)
        clk = proj.fastest_clock
        inject(
            page,
            "rtl-intro",
            rtl_intro(
                project.resolve().relative_to(ROOT).as_posix(),
                proj.description or proj.name,
                clk.mhz if clk else 100.0,
            ),
        )
        inject(page, "rtl-flow", rtl_flow_html(rows, chip_rows(chip, macros)))
        render = SITE / "chip-render.png"
        render_chip(project, chip, render)
        chip_images(render)
        render.unlink()
        inject(page, "chip", chip_html(chip, chip_callouts(project, chip, macros)))
        data["rtl"] = {
            "project": proj.name,
            "steps": [
                {k: r[k] for k in ("step", "status", "summary") if k in r} for r in rows.values()
            ],
        }
    elif "rtl" in previous:
        data["rtl"] = previous["rtl"]
    if args.hero_floorplan:
        rendered = json.loads(Path(args.hero_floorplan).read_text())
        current = hero_floorplan(hero_design)
        if not same_floorplan(rendered, current):
            raise SystemExit(
                f"the hero render shows {rendered['design']!r} with a different floorplan from "
                f"the one the open-PDK example now gives ({current['design']!r}); write it "
                "with --write-hero-floorplan and re-render with scripts/render_die.py"
            )
    pos = json.loads(Path(args.callouts).read_text()) if args.callouts else None
    if args.hero:
        pos = images(Path(args.hero), pos)
        og_card()
    elif args.og:
        og_base_from_site()
        og_card()
    if pos:
        marks, key_html = callouts_html(pos, hero_design)
        inject(page, "callouts", marks)
        inject(page, "callout-key", key_html)
    (SITE / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    stamp_assets(page)
    for name in ("logo-mark.svg", "favicon.svg"):
        shutil.copy2(ROOT / "assets" / name, SITE / name)
    if shutil.which("rsvg-convert"):
        subprocess.run(
            [
                "rsvg-convert",
                "-w",
                "180",
                "-h",
                "180",
                str(ROOT / "assets" / "logo-mark.svg"),
                "-o",
                str(SITE / "apple-touch-icon.png"),
            ],
            check=True,
        )
    print("site updated")


if __name__ == "__main__":
    main()
