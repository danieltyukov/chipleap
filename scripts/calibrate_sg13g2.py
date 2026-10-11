"""Re-measure the IHP SG13G2 numbers in chipleap/hw/tech.py from the PDK itself.

    python scripts/calibrate_sg13g2.py [--pdk PATH] [--no-synth] [--jobs N]

Prints, from the liberty files of the IHP open PDK:

* NAND2 and flip-flop areas and the NAND2 leakage,
* every SRAM macro's area and read and write energy per access,
* the least-squares fit of macro area against capacity (periphery + per-KiB array),
* the fit of read energy per byte against capacity (a power law),

and, with Yosys installed, the logic area of the generated accelerator across twelve
configurations, fitted to the terms of `accel_logic_kge` with its leave-one-out error.
Compare the output with the ihp-sg13g2 entry and the ACCEL_* constants in tech.py.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from chipleap.hw import Accel
from chipleap.verify.synth import synthesize
from chipleap.verify.tools import ihp_liberty

# (dataflow, rows, cols, ports): every dataflow, one to four ports, 8 to 64 MACs.
ACCEL_CONFIGS = [
    ("gemv", 1, 8, 1),
    ("gemv", 2, 8, 4),
    ("gemv", 2, 16, 1),
    ("gemv", 4, 8, 2),
    ("gemv", 8, 8, 2),
    ("ws", 4, 4, 1),
    ("ws", 4, 8, 1),
    ("ws", 8, 8, 2),
    ("os", 2, 8, 1),
    ("os", 4, 4, 1),
    ("os", 4, 8, 1),
    ("os", 8, 8, 2),
]
TERMS = ["base", "port", "gemv mac", "gemv lane", "ws mac", "ws column", "os mac", "os edge"]


def features(df: str, rows: int, cols: int, ports: int) -> list[float]:
    macs = rows * cols
    return [
        1,
        ports,
        macs if df == "gemv" else 0,
        rows if df == "gemv" else 0,
        macs if df == "ws" else 0,
        cols if df == "ws" else 0,
        macs if df == "os" else 0,
        rows + cols if df == "os" else 0,
    ]


def lstsq(a: list[list[float]], y: list[float]) -> list[float]:
    """Least squares through the normal equations, solved by Gauss-Jordan elimination.

    A tiny ridge keeps the system solvable when a left-out configuration leaves two terms
    collinear; the answer then tends to the minimum-norm solution.
    """
    n = len(a[0])
    m = [
        [sum(r[i] * r[j] for r in a) for j in range(n)]
        + [sum(r[i] * v for r, v in zip(a, y, strict=True))]
        for i in range(n)
    ]
    ridge = 1e-7 * max(m[i][i] for i in range(n))
    for i in range(n):
        m[i][i] += ridge
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * z for x, z in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def synth_kge(cfg: tuple[str, int, int, int], nand_um2: float) -> float:
    df, rows, cols, ports = cfg
    r = synthesize(Accel(df, rows, cols, 8, ports=ports, abuf_kib=4, wbuf_kib=4))
    print(
        f"{df} {rows}x{cols}, {ports} port(s): {r.logic_um2 / nand_um2 / 1000:.1f} kGE, "
        f"critical path {r.delay_ns:.2f} ns",
        flush=True,
    )
    return r.logic_um2 / nand_um2 / 1000


def cell_area(lib: str, cell: str) -> tuple[float, float]:
    m = re.search(rf"cell\s*\(\s*\"?{cell}\"?\s*\)\s*\{{(.*?)\n\s*pin", lib, re.S)
    if not m:
        raise SystemExit(f"cell {cell} not found")
    area = float(re.search(r"area\s*:\s*([\d.]+)", m.group(1)).group(1))  # type: ignore[union-attr]
    leak = re.search(r"cell_leakage_power\s*:\s*([\d.]+)", m.group(1))
    return area, float(leak.group(1)) if leak else 0.0


def macro_numbers(path: Path) -> tuple[int, int, float, float, float]:
    text = path.read_text()
    m = re.search(r"RM_IHPSG13_1P_(\d+)x(\d+)", path.name)
    if not m:
        raise ValueError(path.name)
    words, bits = int(m.group(1)), int(m.group(2))
    area = float(re.search(r"^\s*area\s*:\s*([\d.]+)", text, re.M).group(1))  # type: ignore[union-attr]

    def energy(when: str) -> float:
        mm = re.search(re.escape(f'when : "{when}"') + r".*?values \(([\d.]+)\)", text, re.S)
        return float(mm.group(1)) if mm else math.nan

    return words, bits, area, energy("A_MEN  & !A_WEN & A_REN"), energy("A_MEN  & A_WEN  & !A_REN")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdk", help="path to ihp-sg13g2/libs.ref")
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--jobs", type=int, default=len(ACCEL_CONFIGS))
    args = ap.parse_args()
    std = (
        Path(args.pdk) / "sg13g2_stdcell/lib/sg13g2_stdcell_typ_1p20V_25C.lib"
        if args.pdk
        else ihp_liberty()
    )
    if std is None or not std.is_file():
        raise SystemExit("IHP SG13G2 liberty not found; pass --pdk or set CHIPLEAP_LIBERTY")
    lib = std.read_text()
    nand, nleak = cell_area(lib, "sg13g2_nand2_1")
    dff, _ = cell_area(lib, "sg13g2_dfrbp_1")
    print(f"NAND2 {nand} um2, leakage {nleak} pW; DFF {dff} um2 = {dff / nand:.2f} GE")

    sram_dir = std.parent.parent.parent / "sg13g2_sram" / "lib"
    rows = []
    for f in sorted(sram_dir.glob("RM_IHPSG13_1P_*_typ_1p20V_25C.lib")):
        words, bits, area, rd, wr = macro_numbers(f)
        kib = words * bits / 8 / 1024
        rows.append((kib, area / 1e6, rd, wr, bits))
        print(
            f"{f.name[:-4]:42s} {kib:6.2f} KiB {area / 1e6:.4f} mm2 read {rd:7.2f} pJ "
            f"write {wr:7.2f} pJ ({rd / (bits / 8):.2f} pJ/B)"
        )
    if rows:
        xs = [r[0] for r in rows]
        ys = [r[1] for r in rows]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum(
            (x - mx) ** 2 for x in xs
        )
        a = my - b * mx
        print(f"area fit: {a:.4f} mm2 per macro + {b:.5f} mm2 per KiB")
        wide = [(r[0], r[2] / (r[4] / 8)) for r in rows if r[4] == 64 and r[0] >= 2]
        if len(wide) >= 2:
            lx = [math.log(k / 16) for k, _ in wide]
            ly = [math.log(e) for _, e in wide]
            mx, my = sum(lx) / len(lx), sum(ly) / len(ly)
            p = sum((x - mx) * (y - my) for x, y in zip(lx, ly, strict=True)) / sum(
                (x - mx) ** 2 for x in lx
            )
            print(f"read energy fit: {math.exp(my - p * mx):.2f} pJ/B at 16 KiB, exponent {p:.2f}")

    if args.no_synth or shutil.which("yosys") is None:
        return
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        kge = list(ex.map(synth_kge, ACCEL_CONFIGS, [nand] * len(ACCEL_CONFIGS)))
    a = [features(*c) for c in ACCEL_CONFIGS]
    coef = lstsq(a, kge)
    print(
        "accelerator logic fit (kGE): "
        + ", ".join(f"{t} {v:.3f}" for t, v in zip(TERMS, coef, strict=True))
    )
    errs = []
    for i in range(len(a)):
        loo = lstsq(a[:i] + a[i + 1 :], kge[:i] + kge[i + 1 :])
        pred = sum(f * c for f, c in zip(a[i], loo, strict=True))
        errs.append(abs(pred - kge[i]) / kge[i])
    print(
        f"leave-one-out error: {100 * sum(errs) / len(errs):.1f}% mean, {100 * max(errs):.1f}% max"
    )


if __name__ == "__main__":
    main()
