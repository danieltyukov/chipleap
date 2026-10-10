"""Make the README's images from real ChipLeap output.

    python scripts/make_readme_visuals.py [--render RENDER.png]

* docs/img/decisions.gif: the explorer's accepted decisions for the Llama 3.2 1B example,
  each floorplan drawn to the same scale, morphing from one step to the next.
* docs/img/die.jpg: the Blender render of the open-PDK design on a light card (when
  --render is given, from scripts/render_die.py).

The routed layout (docs/img/layout.jpg) is scripts/render_gds.py's drawing of the GDS that
`chipleap verify --steps pnr` writes, with the routing metals on the site's dark colour, and
docs/img/report.png is a browser screenshot of the top of a bundle's report.html.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from build_site_data import trace  # noqa: E402

OUT = ROOT / "docs" / "img"
W, H, SS = 960, 520, 2  # output size and supersampling
PAPER = (246, 247, 245)
INK = (14, 19, 17)
MUTED = (87, 96, 91)
FAINT = (100, 108, 103)
LINE = (221, 226, 222)
GREEN = (15, 122, 79)
COLORS = {
    "sram": (58, 111, 140),
    "accel": (15, 122, 79),
    "buffer": (111, 174, 141),
    "cpu": (184, 105, 46),
    "soc": (141, 148, 143),
    "phy": (74, 79, 110),
}
NAMES = {
    "sram": "SRAM",
    "accel": "Accelerator",
    "buffer": "Buffers",
    "cpu": "CPU",
    "soc": "Bus, peripherals",
    "phy": "Memory interface",
}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    paths = {
        "display": [
            Path.home() / ".local/share/fonts/space-grotesk/SpaceGrotesk-Medium.ttf",
            Path("/usr/share/fonts/opentype/inter/InterDisplay-SemiBold.otf"),
        ],
        "num": [Path("/usr/share/fonts/opentype/inter/Inter-SemiBold.otf")],
        "text": [Path("/usr/share/fonts/opentype/inter/Inter-Regular.otf")],
    }[name]
    for p in paths:
        if p.is_file():
            return ImageFont.truetype(str(p), size * SS)
    return ImageFont.load_default(size * SS)


F_TITLE = font("display", 30)
F_STEP = font("text", 15)
F_NUM = font("num", 54)
F_UNIT = font("text", 16)
F_KEY = font("text", 14)
F_VAL = font("num", 18)
F_LEG = font("text", 13)
F_LBL = font("text", 12)


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        nxt = f"{cur} {word}".strip()
        if draw.textlength(nxt, font=f) <= width * SS:
            cur = nxt
        else:
            lines.append(cur)
            cur = word
    return [*lines, cur] if cur else lines


def lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def ease(k: float) -> float:
    return 4 * k**3 if k < 0.5 else 1 - (-2 * k + 2) ** 3 / 2


def geometry(p: dict[str, Any], scale: float) -> dict[str, Any]:
    fp = p["floorplan"]
    side = fp["side"] * scale
    cx, cy = 250 * SS, 262 * SS
    ox, oy = cx - side / 2, cy - side / 2
    blocks = {
        b["id"]: (
            ox + b["x"] * scale,
            oy + (fp["side"] - b["y"] - b["h"]) * scale,
            b["w"] * scale,
            b["h"] * scale,
            b["kind"],
            b.get("label", ""),
            b.get("short", ""),
        )
        for b in fp["blocks"]
    }
    return {"side": side, "ox": ox, "oy": oy, "blocks": blocks}


def draw_die(
    d: ImageDraw.ImageDraw, g: dict[str, Any], alpha: dict[str, float], labels: bool
) -> None:
    side, ox, oy = g["side"], g["ox"], g["oy"]
    d.rounded_rectangle((ox, oy, ox + side, oy + side), radius=6 * SS, fill=(29, 38, 34))
    ring = side * 0.06
    n = max(6, round(side / (14 * SS)))
    pitch = (side - 2 * ring) / n
    s = min(ring * 0.55, pitch * 0.6)
    for i in range(n):
        u = ox + ring + pitch * (i + 0.5) - s / 2
        v = oy + ring + pitch * (i + 0.5) - s / 2
        for x, y in (
            (u, oy + ring / 2 - s / 2),
            (u, oy + side - ring / 2 - s / 2),
            (ox + ring / 2 - s / 2, v),
            (ox + side - ring / 2 - s / 2, v),
        ):
            d.rectangle((x, y, x + s, y + s), fill=(185, 160, 106))
    d.rectangle((ox + ring, oy + ring, ox + side - ring, oy + side - ring), fill=(38, 48, 43))
    for bid, (x, y, w, h, kind, label, short) in g["blocks"].items():
        # Blocks that appear grow from their centre; blocks that go away shrink into it.
        a = alpha.get(bid, 1.0)
        cx, cy = x + w / 2, y + h / 2
        w, h = w * a, h * a
        x, y = cx - w / 2, cy - h / 2
        if w < 1 or h < 1:
            continue
        d.rectangle((x, y, x + w, y + h), fill=COLORS[kind], outline=(29, 38, 34), width=SS)
        for text in (label, short) if labels else ():
            if text and d.textlength(text, font=F_LBL) + 12 * SS < w and h > 24 * SS and a > 0.9:
                d.text((x + 6 * SS, y + 5 * SS), text, font=F_LBL, fill=(255, 255, 255))
                break


def frame(
    pts: list[dict[str, Any]], i: int, ga: dict[str, Any], gb: dict[str, Any], k: float
) -> Image.Image:
    im = Image.new("RGB", (W * SS, H * SS), PAPER)
    d = ImageDraw.Draw(im)
    # Die, tweened from step i to i + 1.
    e = ease(k)
    g = {
        "side": lerp(ga["side"], gb["side"], e),
        "ox": lerp(ga["ox"], gb["ox"], e),
        "oy": lerp(ga["oy"], gb["oy"], e),
        "blocks": {},
    }
    alpha: dict[str, float] = {}
    for bid, b in gb["blocks"].items():
        a = ga["blocks"].get(bid)
        if a:
            g["blocks"][bid] = tuple(lerp(a[j], b[j], e) for j in range(4)) + b[4:]
        else:
            g["blocks"][bid] = b
            alpha[bid] = e
    for bid, a in ga["blocks"].items():
        if bid not in gb["blocks"] and e < 1:
            g["blocks"][bid] = a
            alpha[bid] = 1 - e
    # Labels only on held frames: mid-move they would overlap the blocks sliding past.
    draw_die(d, g, alpha, labels=k in (0.0, 1.0))
    # Scale bar.
    d.line((70 * SS, 488 * SS, 70 * SS + ga["mm"], 488 * SS), fill=FAINT, width=SS)
    d.text((76 * SS + ga["mm"], 480 * SS), "1 mm", font=F_LEG, fill=FAINT)
    # Text panel: the step the die is moving to once past halfway.
    p = pts[i + 1] if k >= 0.5 else pts[i]
    j = i + 1 if k >= 0.5 else i
    x0 = 520 * SS
    d.text((x0, 52 * SS), "Start" if j == 0 else f"Decision {j}", font=F_STEP, fill=FAINT)
    y = 76 * SS
    for line in wrap(d, p["title"], F_TITLE, 400)[:3]:
        d.text((x0, y), line, font=F_TITLE, fill=INK)
        y += 38 * SS
    tps = lerp(pts[i]["tps"], pts[i + 1]["tps"], e) if pts[i]["tps"] > 0 else pts[i + 1]["tps"]
    value = f"{tps:.2f}" if tps >= 1 else f"{tps:.2g}"
    y += 18 * SS
    d.text((x0, y), value, font=F_NUM, fill=INK)
    d.text(
        (x0 + d.textlength(value, font=F_NUM) + 12 * SS, y + 32 * SS),
        "tokens per second",
        font=F_UNIT,
        fill=MUTED,
    )
    y += 92 * SS
    for col, (key, val) in enumerate(
        (("Process", p["node_label"]), ("Memory", p["mem_label"]), ("Core", f"{p['mm2']:.2f} mm²"))
    ):
        cx = x0 + col * 140 * SS
        d.text((cx, y), key, font=F_KEY, fill=FAINT)
        d.text((cx, y + 20 * SS), val, font=F_VAL, fill=INK)
    # Progress dots.
    y += 76 * SS
    for n in range(len(pts)):
        cx = x0 + n * 22 * SS
        on = n <= j
        d.ellipse(
            (cx, y, cx + 10 * SS, y + 10 * SS),
            fill=GREEN if on else PAPER,
            outline=GREEN if on else (195, 203, 198),
            width=2 * SS,
        )
    # Legend.
    lx, ly = x0, 470 * SS
    for kind in ("sram", "accel", "buffer", "cpu", "soc", "phy"):
        d.rectangle((lx, ly + 3 * SS, lx + 10 * SS, ly + 13 * SS), fill=COLORS[kind])
        d.text((lx + 15 * SS, ly), NAMES[kind], font=F_LEG, fill=MUTED)
        lx += d.textlength(NAMES[kind], font=F_LEG) + 32 * SS
        if lx > (W - 120) * SS and kind != "phy":
            lx, ly = x0, ly + 22 * SS
    return im.resize((W, H), Image.LANCZOS)


def decisions_gif(t: dict[str, Any], out: Path) -> None:
    pts = t["points"]
    scale = 360 * SS / max(p["floorplan"]["side"] for p in pts)
    geos = [geometry(p, scale) for p in pts]
    for g in geos:
        g["mm"] = scale
    frames, durations = [], []
    hold = 1900
    for i in range(len(pts) - 1):
        frames.append(frame(pts, i, geos[i], geos[i + 1], 0.0))
        durations.append(hold if i else hold + 600)
        for s in range(1, 13):
            frames.append(frame(pts, i, geos[i], geos[i + 1], s / 12))
            durations.append(45)
    last = len(pts) - 2
    frames.append(frame(pts, last, geos[last], geos[last + 1], 1.0))
    durations.append(hold + 1600)
    # One palette for every frame, built from a sample of all of them, so a colour that is
    # absent from one frame's die (the accelerator's green early on) keeps its exact value.
    sample = Image.new("RGB", (W, H * 4))
    for n, idx in enumerate(range(0, len(frames), max(1, len(frames) // 4))):
        if n < 4:
            sample.paste(frames[idx], (0, n * H))
    swatch = Image.new("RGB", (len(COLORS) * 8 + 24, 8))
    for n, c in enumerate([*COLORS.values(), PAPER, INK, GREEN]):
        swatch.paste(c, (n * 8, 0, n * 8 + 8, 8))
    sample.paste(swatch, (0, 0))
    ref = sample.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    pal = [f.quantize(palette=ref, dither=Image.Dither.NONE) for f in frames]
    out.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(
        out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True
    )
    print(f"wrote {out} ({len(frames)} frames, {out.stat().st_size // 1024} KiB)")


def die_card(render: Path, out: Path) -> None:
    im = Image.open(render).convert("RGBA")
    bbox = im.getbbox() or (0, 0, *im.size)
    im = im.crop(bbox)
    w = 1400
    im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
    card = Image.new("RGBA", (w + 160, im.height + 120), (*PAPER, 255))
    card.alpha_composite(im, (80, 60))
    card.convert("RGB").save(out, quality=88, optimize=True, progressive=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", help="the hero die render from scripts/render_die.py")
    args = ap.parse_args()
    decisions_gif(trace(), OUT / "decisions.gif")
    if args.render:
        die_card(Path(args.render), OUT / "die.jpg")


if __name__ == "__main__":
    main()
