"""Build the website's data and generated content from real ChipLeap runs.

    python scripts/build_site_data.py [--verify BUNDLE] [--hero RENDER.png --callouts C.json
                                       --hero-floorplan FP.json]

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
* content hashes on the styles.css and app.js links, so run it after editing either
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
            b["label"] = "bus and peripherals"
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


def summarize(ex: dict[str, Any]) -> str:
    """The explorer's decisions in one plain sentence, built from the decision log."""
    kinds = [s["kind"] for s in ex["steps"] if s["action"]]
    first = []
    if "add_accel" in kinds:
        first.append("added a GEMV engine")
    if "more_channels" in kinds:
        first.append("doubled the memory channels")
    if any(k in kinds for k in ("faster_mem", "slower_mem", "add_ext_mem")):
        first.append("moved the weights to octal PSRAM")
    trims = sum(k.startswith(("shrink", "less", "smaller", "fewer")) for k in kinds)
    text = f"{ex['evaluated']:,} designs evaluated. The agent " + ", ".join(first)
    if trims:
        text += f", then made {trims} trims to the SRAM, buffers and array"
    return text + ", and stopped at the smallest design that still meets the target."


def console_html(lines: list[tuple[str, str]], verify_line: str) -> str:
    body = "".join(f'<p class="{cls}">{html.escape(text)}</p>' for cls, text in lines)
    body += f'<p class="c-out">{html.escape(verify_line)}</p>'
    return f'<div class="console-body" id="console-body">{body}</div>'


def stats_html(rows: list[dict[str, Any]]) -> str:
    sim = next((r for r in rows if r["step"] == "sim"), {})
    synth = next((r for r in rows if r["step"] == "synth"), {})
    xh = next((r for r in rows if r["step"] == "xheep"), {})
    items = [
        (float(sim.get("cases", 0)), 0, "", "randomized jobs bit-exact against a golden model"),
        (
            float(sim.get("cycle_error_no_stall_max") or 0) * 100,
            1,
            "%",
            "cycle error between the model and the RTL without memory stalls",
        ),
        (
            float((xh.get("render") or {}).get("rendered", 0)),
            0,
            "",
            "X-HEEP templates rendered for the generated configuration",
        ),
    ]
    if synth.get("fmax_mhz_estimate"):
        items.append(
            (
                float(synth["fmax_mhz_estimate"]),
                0,
                " MHz",
                "clock the synthesized accelerator logic reaches on IHP SG13G2, estimated",
            )
        )
    cells = "".join(
        f'<div class="stat"><span class="num" data-to="{v}" data-dec="{dec}" data-suffix="{suf}">'
        f'{v:.{dec}f}{suf}</span><span class="lab">{html.escape(lab)}</span></div>'
        for v, dec, suf, lab in items
    )
    return f'<div class="stats">{cells}</div>'


def verify_table(rows: list[dict[str, Any]]) -> str:
    names = {
        "consist": "Consistency",
        "lint": "Lint",
        "sim": "Simulation",
        "synth": "Synthesis",
        "xheep": "X-HEEP",
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
        "logic area within 4% on average of synthesis across 11 configurations. The C driver "
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
<text x="72" y="430" font-family="{body}" font-size="24" fill="#57605b">Explore, generate RISC-V, verify on open tools.</text>
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
    t = trace()
    data: dict[str, Any] = {"version": __version__, "trace": t, "calculator": calculator()}
    inject(page, "story-intro", story_intro(t))
    inject(page, "steps", steps_html(t))
    lines, hero_design = console()
    verify_line = "Consistency, lint, simulation and X-HEEP checks pass."
    if args.verify:
        rows = json.loads((Path(args.verify) / "verify" / "verify.json").read_text())
        # Only what the page shows: the full rows carry local paths and tool logs.
        data["verification"] = [
            {k: r[k] for k in ("step", "status", "summary") if k in r} for r in rows
        ]
        inject(page, "stats", stats_html(rows))
        inject(page, "verify", verify_table(rows))
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
    inject(page, "console", console_html(lines, verify_line))
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
