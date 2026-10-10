"""A to-scale floorplan sketch of a design point.

Blocks get the area the model gives them and are packed into a square die with a
squarified treemap: SRAM banks one by one, the accelerator's PE array and buffers, the CPU,
the rest of the SoC logic and the memory interface. It is a picture of where the area
goes, not a placement: the website animates it across the agent's steps, the Blender
script renders it, and the HTML report draws it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chipleap.model.evaluate import Evaluation

PAD_RING = 0.06  # fraction of the die edge taken by the pad ring on each side


@dataclass(frozen=True)
class Block:
    id: str
    kind: str  # sram | accel | buffer | cpu | soc | phy
    label: str
    x: float  # in mm, origin at the die's lower-left corner
    y: float
    w: float
    h: float
    area: float  # mm2


@dataclass(frozen=True)
class Floorplan:
    side: float  # die edge in mm, including the pad ring
    blocks: tuple[Block, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "side": round(self.side, 4),
            "blocks": [
                {
                    "id": b.id,
                    "kind": b.kind,
                    "label": b.label,
                    "x": round(b.x, 4),
                    "y": round(b.y, 4),
                    "w": round(b.w, 4),
                    "h": round(b.h, 4),
                    "area": round(b.area, 5),
                }
                for b in self.blocks
            ],
        }


def _worst(row: list[float], w: float) -> float:
    s = sum(row)
    return max(max(w * w * r / (s * s), (s * s) / (w * w * r)) for r in row)


def squarify(
    sizes: list[float], x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float]]:
    """Squarified treemap (Bruls, Huizing, van Wijk 2000). Sizes must sum to w * h."""
    rects: list[tuple[float, float, float, float]] = []
    items = list(sizes)
    while items:
        short = min(w, h)
        row = [items.pop(0)]
        while items and _worst([*row, items[0]], short) <= _worst(row, short):
            row.append(items.pop(0))
        s = sum(row)
        if w >= h:  # lay the row out as a column on the left
            cw = s / h
            cy = y
            for r in row:
                rh = r / cw
                rects.append((x, cy, cw, rh))
                cy += rh
            x, w = x + cw, w - cw
        else:  # lay the row out along the bottom
            rh = s / w
            cx = x
            for r in row:
                rw = r / rh
                rects.append((cx, y, rw, rh))
                cx += rw
            y, h = y + rh, h - rh
    return rects


def floorplan(ev: Evaluation) -> Floorplan:
    d = ev.design
    area = ev.area_mm2
    core = sum(area.values())
    side = math.sqrt(core) / (1 - 2 * PAD_RING)
    pad = side * PAD_RING
    inner = side - 2 * pad

    groups: list[tuple[str, float]] = []
    if area.get("sram"):
        groups.append(("sram", area["sram"]))
    acc = (
        area.get("accelerator logic", 0.0)
        + area.get("accelerator buffers", 0.0)
        + area.get("axi adapter", 0.0)
    )
    if acc:
        groups.append(("accelerator", acc))
    groups.append(("cpu", area["cpu"]))
    groups.append(("soc", area["soc logic"]))
    groups.sort(key=lambda g: -g[1])
    scale = inner * inner / core

    blocks: list[Block] = []
    # The memory interface sits along the bottom edge, next to its pads.
    y0 = pad
    phy = area.get("memory interface", 0.0)
    if phy:
        strip = phy * scale / inner
        blocks.append(Block("phy", "phy", d.ext_mem, pad, pad, inner, strip, phy))
        y0 += strip
    rects = squarify([a * scale for _, a in groups], pad, y0, inner, inner - (y0 - pad))

    for (name, a), (x, y, w, h) in zip(groups, rects, strict=True):
        if name == "sram":
            sizes = ([d.bank_kib] * d.banks) + ([d.il_bank_kib] * d.il_banks)
            total = sum(sizes)
            sub = squarify([s / total * w * h for s in sizes], x, y, w, h)
            for i, ((bx, by, bw, bh), kib) in enumerate(zip(sub, sizes, strict=True)):
                il = i >= d.banks
                blocks.append(
                    Block(
                        f"sram{i}",
                        "sram",
                        f"{kib} KiB" + (" interleaved" if il else ""),
                        bx,
                        by,
                        bw,
                        bh,
                        a * kib / total,
                    )
                )
        elif name == "accelerator":
            # The standalone platform's AXI adapter sits with the accelerator's logic.
            logic = area.get("accelerator logic", 0.0) + area.get("axi adapter", 0.0)
            buf = area.get("accelerator buffers", 0.0)
            sub = squarify([logic / acc * w * h, buf / acc * w * h], x, y, w, h)
            a_ = d.accel
            lbl = f"{a_.rows}x{a_.cols} {a_.dataflow}" if a_ else "accelerator"
            blocks.append(Block("pe", "accel", lbl, *sub[0], logic))
            blocks.append(Block("buf", "buffer", "buffers", *sub[1], buf))
        elif name == "cpu":
            blocks.append(Block("cpu", "cpu", d.cpu.upper(), x, y, w, h, a))
        else:
            blocks.append(Block("soc", "soc", "bus, DMA, peripherals", x, y, w, h, a))
    return Floorplan(side, tuple(blocks))


COLORS = {
    "sram": "var(--fp-sram, #2f6f8f)",
    "accel": "var(--fp-accel, #17784e)",
    "buffer": "var(--fp-buffer, #6aa88a)",
    "cpu": "var(--fp-cpu, #b8692e)",
    "soc": "var(--fp-soc, #8a8f8c)",
    "phy": "var(--fp-phy, #5b5f7a)",
}


def floorplan_svg(fp: Floorplan, size: int = 360) -> str:
    """The floorplan as an inline SVG, die edge ``size`` pixels."""
    k = size / fp.side
    parts = [
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
        f'aria-label="Floorplan sketch, die {fp.side:.2f} mm on a side">',
        f'<rect x="0.5" y="0.5" width="{size - 1}" height="{size - 1}" fill="none" '
        'stroke="currentColor" stroke-opacity=".35"/>',
    ]
    for b in fp.blocks:
        x, y = b.x * k, size - (b.y + b.h) * k
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{b.w * k:.1f}" height="{b.h * k:.1f}" '
            f'fill="{COLORS[b.kind]}" fill-opacity=".85" stroke="#fff" stroke-width="1">'
            f"<title>{b.label}: {b.area:.3g} mm2</title></rect>"
        )
        if b.w * k > 46 and b.h * k > 18:
            parts.append(
                f'<text x="{x + 5:.1f}" y="{y + 14:.1f}" font-size="11" fill="#fff">'
                f"{b.label}</text>"
            )
    parts.append("</svg>")
    return "".join(parts)
