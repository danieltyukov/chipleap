"""Measure X-HEEP's logic per peripheral on IHP SG13G2 and fit the model's prices to it.

    python scripts/calibrate_xheep.py measure OUT [--configs NAME,...] [--yosys CMD] [--jobs N]
    python scripts/calibrate_xheep.py collect OUT
    python scripts/calibrate_xheep.py fit OUT [OUT...]

``measure`` writes, for each configuration below, the SmolLM2 SoC with X-HEEP configured
that way, as ``chipleap verify --steps soc`` prepares it (X-HEEP rendered by its own
generator, its sources from FuseSoC, ChipLeap's top, banks and accelerator), into
``OUT/NAME/``, and synthesizes it with Yosys and ABC against IHP SG13G2's typical liberty,
the hierarchy kept (slang's ``--best-effort-hierarchy`` names every instance's module
after its path). It needs what the soc step needs (X-HEEP's FuseSoC, a RISC-V GCC, the IHP
PDK with its SRAM macros, an X-HEEP checkout) and a Yosys with the slang plugin, such as
LibreLane's: ``--yosys`` is the command that runs it, words split as a shell would, for
instance a container wrapper. Each synthesis takes about an hour on one core.

``collect`` writes ``blocks.json`` for a synthesis in ``OUT`` that finished without
``measure`` there to read it, when its X-HEEP configuration is the one ChipLeap writes now.

``fit`` reads every ``OUT/*/blocks.json`` (a later ``OUT``'s replaces an earlier one's) and
prints:

* each block's area per configuration: the subtree of the instances that make it up, so
  the blocks of one configuration add up to its total exactly, since no logic crosses
  a module boundary in a hierarchical synthesis;
* the prices fitted to the configurations in ``CONFIGS``, the least-squares fits among
  them: the DMA against its channels and master ports, the one-to-many bus against its
  masters, the user domain's interconnect against its peripherals, and the NtoM
  crossbar per master and slave pair;
* the model's total (``chipleap.model.evaluate.xheep_kge`` and ``bus_kge``) against each
  configuration's measured total, with the shipped prices, the fitted ones and the ones
  fitted without that configuration, and against the configurations the fit never sees
  (``CHECKS``).

Compare the output with the XHEEP_* constants in chipleap/model/evaluate.py.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from chipleap.chip import io
from chipleap.gen import soc as top
from chipleap.gen.xheep import xheep_config
from chipleap.hw import Accel, Design
from chipleap.hw.design import Peripherals
from chipleap.model import evaluate
from chipleap.verify import soc, xheep
from chipleap.verify.tools import ihp_liberty

# The SmolLM2 SoC of the example spec (examples/smollm2-135m-open-pdk.yaml), as #20 lays it out.
SMOLLM2 = Design(
    name="smollm2-open-pdk",
    tech="ihp-sg13g2",
    freq_mhz=90.0,
    cpu="cv32e20",
    bus="onetoM",
    banks=1,
    bank_kib=32,
    ext_mem="octal-psram",
    ext_channels=2,
    accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2, double_buffer=True),
)
FULL = Peripherals(spi_hosts=2, i2c=True, i2s=True, dma_channels=4, dma_ports=2)
# Name: the design. Each block is in and out of at least two, and the DMA and the bus
# cover four channel counts, three port counts and both bus types.
CONFIGS: dict[str, Design] = {
    # X-HEEP's default peripherals (configs/general.py).
    "full": replace(SMOLLM2, peripherals=FULL),
    # ChipLeap's default, the SmolLM2 SoC of #20.
    "trim": SMOLLM2,
    "min": replace(
        SMOLLM2,
        peripherals=Peripherals(uart=False, gpio=0, timers=2, spi_slave=False, dma_channels=1),
    ),
    "io": replace(
        SMOLLM2,
        peripherals=Peripherals(
            gpio=8,
            timers=2,
            spi_hosts=1,
            i2c=True,
            i2s=True,
            pdm2pcm=True,
            spi_slave=False,
            dma_channels=4,
        ),
    ),
    "dma": replace(
        SMOLLM2,
        peripherals=Peripherals(uart=False, gpio=24, spi_hosts=2, dma_channels=8, dma_ports=4),
    ),
    # The accelerator keeps one port: its buffers are macros only with one.
    "ntom": replace(
        SMOLLM2, bus="NtoM", banks=2, peripherals=Peripherals(dma_channels=2, dma_ports=2)
    ),
}
# Configurations the fit never sees, to check the model on: the keyword-spotting example's
# peripherals, and two mixes of blocks, channel counts and banks no fitted one has.
CHECKS: dict[str, Design] = {
    "kws": replace(SMOLLM2, peripherals=Peripherals(uart=False, gpio=8, timers=2, pdm2pcm=True)),
    "mix": replace(
        SMOLLM2,
        peripherals=Peripherals(
            uart=False,
            gpio=24,
            spi_hosts=1,
            i2c=True,
            spi_slave=False,
            dma_channels=6,
            dma_ports=2,
        ),
    ),
    "ntom_il": replace(
        SMOLLM2,
        bus="NtoM",
        banks=2,
        il_banks=2,
        il_bank_kib=16,
        peripherals=Peripherals(spi_hosts=1, i2s=True, dma_channels=4, dma_ports=4),
    ),
}

MCU = f"{top.TOP}.{top.MCU_INST}"
AO = f"{MCU}.ao_peripheral_subsystem_i"
USER = f"{MCU}.peripheral_subsystem_i"
DEBUG = f"{MCU}.debug_subsystem_i"
# Block: the instance paths whose subtrees make it up. What is left of the MCU is "glue".
BLOCKS: dict[str, tuple[str, ...]] = {
    "cpu": (f"{MCU}.cpu_subsystem_i",),
    "bus": (f"{MCU}.system_bus_i",),
    "memory": (f"{MCU}.memory_subsystem_i",),
    "debug": (f"{DEBUG}.dm_obi_top_i", f"{DEBUG}.dmi_jtag_i"),
    "spi_slave": (f"{DEBUG}.gen_spi_slave",),
    "dma": (f"{AO}.dma_subsystem_i",),
    "flash": (f"{AO}.spi_subsystem_i",),
    "ao_gpio": (f"{AO}.gpio_ao_i",),
    "ao_timer": (f"{AO}.rv_timer_0_1_i",),
    "power_manager": (f"{AO}.power_manager_i",),
    "plic": (f"{USER}.rv_plic_i",),
    "spi_host": (f"{USER}.spi_host_dma_i",),
    "spi2": (f"{USER}.spi2_host",),
    "user_gpio": (f"{USER}.gpio_i",),
    "i2c": (f"{USER}.i2c_i",),
    "user_timer": (f"{USER}.rv_timer_2_3_i",),
    "pdm2pcm": (f"{USER}.pdm2pcm_i",),
    "i2s": (f"{USER}.i2s_i",),
    "uart": (f"{USER}.uart_i",),
    # The user domain's bus: its decoder and demultiplexer grow with its peripherals.
    "user_xbar": (
        f"{USER}.i_addr_decode_soc_regbus_periph_xbar",
        f"{USER}.reg_demux_i",
    ),
}
ACCEL = f"{top.TOP}.{top.ACCEL_INST}"
GE_UM2 = 7.2576  # sg13g2_nand2_1
_AREA_RE = re.compile(r"Chip area for module '\\?([^']+)': ([0-9.]+)")
_TOP_RE = re.compile(r"Chip area for top module '\\?([^']+)': ([0-9.]+)")


def hier_script(prep: soc.Prepared, liberty: Path) -> str:
    """Yosys's script: slang reads what LibreLane reads, every instance a module of its
    own, then synthesis with the hierarchy kept and the statistics per module."""
    src = prep.sources
    stubs = " ".join(str(v.stub) for v in prep.views.values())
    defines = " ".join(f"-D{d}" for d in soc.DEFINES)
    incs = " ".join(f"-I{d}" for d in src.includes)
    args = " ".join(soc.SLANG_ARGUMENTS)
    return "\n".join(
        [
            f"read_liberty -lib {liberty}",
            f"read_verilog -lib {stubs}",
            "plugin -i slang",
            f"read_slang --top {top.TOP} {args} --best-effort-hierarchy {defines} {incs} "
            + " ".join(src.files),
            f"hierarchy -check -top {top.TOP}",
            f"synth -top {top.TOP}",
            f"dfflibmap -liberty {liberty}",
            f"abc -liberty {liberty}",
            f"tee -o hier_stat.txt stat -liberty {liberty}",
            "",
        ]
    )


def module_areas(stat: str) -> tuple[dict[str, float], float]:
    """Each instance path's own cell area in um2 (``module$path`` in slang's names; the
    top under its own name), and the top's total."""
    own: dict[str, float] = {}
    for name, area in _AREA_RE.findall(stat):
        path = name.split("$", 1)[1] if "$" in name else name
        own[path] = own.get(path, 0.0) + float(area)
    top = _TOP_RE.findall(stat)
    if not top:
        raise ValueError("Yosys's statistics have no top module")
    return own, float(top[-1][1])


def blocks(own: dict[str, float]) -> dict[str, float]:
    """kGE per block (``BLOCKS``), the rest of the MCU as "glue", the accelerator apart."""

    def under(prefix: str) -> float:
        return sum(a for p, a in own.items() if p == prefix or p.startswith(prefix + "."))

    out = {name: sum(under(p) for p in paths) / GE_UM2 / 1000 for name, paths in BLOCKS.items()}
    mcu = under(MCU) / GE_UM2 / 1000
    out["glue"] = mcu - sum(out.values())
    out["accelerator"] = under(ACCEL) / GE_UM2 / 1000
    out["mcu"] = mcu
    return out


def measure_one(name: str, design: Design, out: Path, yosys: list[str], checkout: Path) -> None:
    pdk = io.pdk_dir()
    liberty = ihp_liberty()
    if pdk is None or liberty is None:
        raise SystemExit(f"no IHP PDK with its SRAM macros ({io.PDK_ENV})")
    work = out / name
    work.mkdir(parents=True, exist_ok=True)
    design.validate()
    _, prep = soc.prepare(design, xheep_config(design), checkout, work, pdk)
    (work / "hier.ys").write_text(hier_script(prep, liberty))
    with open(work / "hier.log", "w") as log:
        r = subprocess.run([*yosys, "-q", "-s", "hier.ys"], cwd=work, stdout=log, stderr=log)
    if r.returncode != 0:
        raise SystemExit(f"{name}: Yosys failed (exit {r.returncode}), see {work / 'hier.log'}")
    write_blocks(name, design, work)


def write_blocks(name: str, design: Design, work: Path) -> None:
    """``work/blocks.json`` from Yosys's statistics in ``work``."""
    own, top = module_areas((work / "hier_stat.txt").read_text())
    data = {
        "design": design.to_dict(),
        "top_kge": top / GE_UM2 / 1000,
        "blocks": blocks(own),
    }
    (work / "blocks.json").write_text(json.dumps(data, indent=1) + "\n")
    print(f"{name}: {data['blocks']['mcu']:.1f} kGE of X-HEEP", flush=True)


def collect(args: argparse.Namespace) -> None:
    """``blocks.json`` for each configuration under ``OUT`` whose synthesis finished
    without ``measure`` there to read it, when its X-HEEP configuration is the one ChipLeap
    writes for it now."""
    every = {**CONFIGS, **CHECKS}
    for work in sorted(p.parent for p in args.out.glob("*/hier_stat.txt")):
        name = work.name
        if (work / "blocks.json").is_file() or name not in every:
            continue
        design = every[name]
        cfg = work / "xheep_cfg" / f"{design.name}.py"
        if not cfg.is_file() or cfg.read_text() != xheep_config(design):
            print(f"{name}: synthesized from another configuration, skipped", file=sys.stderr)
            continue
        write_blocks(name, design, work)


def measure(args: argparse.Namespace) -> None:
    checkout = xheep.find_checkout(args.xheep)
    if checkout is None:
        raise SystemExit("no X-HEEP checkout (--xheep DIR or CHIPLEAP_XHEEP_DIR)")
    every = {**CONFIGS, **CHECKS}
    names = args.configs.split(",") if args.configs else list(every)
    bad = [n for n in names if n not in every]
    if bad:
        raise SystemExit(f"unknown configurations: {', '.join(bad)}")
    yosys = shlex.split(args.yosys)
    failed = []
    with ThreadPoolExecutor(args.jobs) as pool:
        jobs = {n: pool.submit(measure_one, n, every[n], args.out, yosys, checkout) for n in names}
        for n, f in jobs.items():
            try:
                f.result()
            except (SystemExit, Exception) as exc:  # report every configuration's outcome
                print(f"{n}: {exc}", file=sys.stderr, flush=True)
                failed.append(n)
    if failed:
        raise SystemExit(f"failed: {', '.join(failed)}")


def _lstsq(rows: list[list[float]], ys: list[float]) -> list[float]:
    """Least squares by the normal equations (a handful of terms, no numpy needed)."""
    n = len(rows[0])
    a = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * y for r, y in zip(rows, ys, strict=True)) for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda k: abs(a[k][col]))
        a[col], a[piv], b[col], b[piv] = a[piv], a[col], b[piv], b[col]
        for k in range(n):
            if k != col and a[col][col]:
                f = a[k][col] / a[col][col]
                a[k] = [x - f * y for x, y in zip(a[k], a[col], strict=True)]
                b[k] -= f * b[col]
    return [b[i] / a[i][i] if a[i][i] else 0.0 for i in range(n)]


# The blocks the model prices one by one (chipleap.model.evaluate.XHEEP_BLOCK_KGE).
OPTIONAL = (
    "spi_slave",
    "ao_gpio",
    "user_gpio",
    "user_timer",
    "plic",
    "uart",
    "spi_host",
    "spi2",
    "i2c",
    "i2s",
    "pdm2pcm",
)
# The configuration the prices reproduce exactly: the SmolLM2 SoC of #20, whose layout
# and clock were fitted with the model's area for it.
ANCHOR = "trim"
Runs = dict[str, tuple[Design, dict[str, float]]]


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _line(xs: list[list[float]], ys: list[float], at: tuple[list[float], float] | None):
    """``ys`` against ``xs`` by least squares, as (intercept, slopes...): through the point
    ``at`` when it is given, else free."""
    if at is None:
        return tuple(_lstsq([[1.0, *x] for x in xs], ys))
    x0, y0 = at
    slopes = _lstsq([[a - b for a, b in zip(x, x0, strict=True)] for x in xs], [y - y0 for y in ys])
    return (y0 - sum(s * x for s, x in zip(slopes, x0, strict=True)), *slopes)


def _dma_x(d: Design) -> list[float]:
    return [float(d.peripherals.dma_channels), float(d.peripherals.dma_ports)]


def _periph_x(d: Design) -> list[float]:
    return [float(len(evaluate.xheep_user_peripherals(d.peripherals)))]


def prices(
    runs: Runs, anchor: str | None = ANCHOR, fallback: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """The model's constants fitted to ``runs``, and the terms no run in ``runs`` measures,
    which come from ``fallback`` (0 without it). With ``anchor`` among the runs the prices
    reproduce that configuration exactly: what it has comes from it, the lines go through
    it and their slopes are least squares over the others; what it lacks is the mean over
    the configurations that have it. Without one, means and free least squares."""
    vals = list(runs.values())
    one = [(d, b) for d, b in vals if d.bus == "onetoM"]
    ntom = [(d, b) for d, b in vals if d.bus == "NtoM"]
    ref = runs.get(anchor) if anchor else None
    missing: list[str] = []

    def mean(key: str, xs: list[float]) -> float:
        m = _mean(xs)
        if m is None:
            missing.append(key)
            return _term(fallback, key) if fallback else 0.0
        return m

    def own(key: str, part: str, xs: list[float]) -> float:
        if ref is not None and ref[1][part] > 0:
            return ref[1][part]
        return mean(key, xs)

    def ao(b: dict[str, float]) -> float:
        return b["ao_timer"] + b["power_manager"] + b["glue"]

    out: dict[str, Any] = {
        "fixed": {
            "flash SPI host": own("flash", "flash", [b["flash"] for _, b in vals]),
            "debug module": own("debug", "debug", [b["debug"] for _, b in vals]),
            "always-on": ao(ref[1]) if ref else mean("always-on", [ao(b) for _, b in vals]),
        },
        "blocks": {k: own(k, k, [b[k] for _, b in vals if b[k] > 0]) for k in OPTIONAL},
    }
    out["dma"] = _line(
        [_dma_x(d) for d, _ in vals],
        [b["dma"] for _, b in vals],
        (_dma_x(ref[0]), ref[1]["dma"]) if ref else None,
    )
    out["periph bus"] = _line(
        [_periph_x(d) for d, _ in vals],
        [b["user_xbar"] for _, b in vals],
        (_periph_x(ref[0]), ref[1]["user_xbar"]) if ref else None,
    )
    out["bank"] = (
        ref[1]["memory"] / (ref[0].banks + ref[0].il_banks)
        if ref
        else mean("bank", [b["memory"] / (d.banks + d.il_banks) for d, b in vals])
    )
    if len(one) < 2:
        missing.append("onetoM")
        out["onetoM"] = fallback["onetoM"] if fallback else (0.0, 0.0)
    else:
        out["onetoM"] = _line(
            [[float(evaluate.bus_masters(d))] for d, _ in one],
            [b["bus"] for _, b in one],
            ([float(evaluate.bus_masters(ref[0]))], ref[1]["bus"])
            if ref and ref[0].bus == "onetoM"
            else None,
        )
    out["ntom pair"] = mean(
        "ntom pair",
        [b["bus"] / (evaluate.bus_masters(d) * evaluate.bus_slaves(d)) for d, b in ntom],
    )
    return out, missing


def _term(p: dict[str, Any], key: str) -> Any:
    """The price ``key`` names in ``p``, a block's or a fixed part's (as ``prices`` names
    what it misses) or a whole entry."""
    if key in p["blocks"]:
        return p["blocks"][key]
    fixed = {"flash": "flash SPI host", "debug": "debug module", "always-on": "always-on"}
    if key in fixed:
        return p["fixed"][fixed[key]]
    return p[key]


def needs(d: Design) -> set[str]:
    """The prices ``d``'s model reads that a configuration may lack: its blocks and its
    bus's."""
    have = set(evaluate.xheep_user_peripherals(d.peripherals))
    have |= {k for k in ("ao_gpio", "spi_slave") if getattr(d.peripherals, k)}
    return have | {"ntom pair" if d.bus == "NtoM" else "onetoM"}


def apply(p: dict[str, Any]) -> None:
    """Set the model's constants to ``p``."""
    evaluate.XHEEP_FIXED_KGE = dict(p["fixed"])
    evaluate.XHEEP_BLOCK_KGE = dict(p["blocks"])
    evaluate.XHEEP_DMA_KGE = p["dma"]
    evaluate.XHEEP_PERIPH_BUS_KGE = p["periph bus"]
    evaluate.XHEEP_BANK_KGE = p["bank"]
    evaluate.ONE_TO_M_BUS_KGE = p["onetoM"]
    evaluate.NTOM_PAIR_KGE = p["ntom pair"]


def shipped() -> dict[str, Any]:
    """The model's constants as chipleap.model.evaluate has them."""
    return {
        "fixed": dict(evaluate.XHEEP_FIXED_KGE),
        "blocks": dict(evaluate.XHEEP_BLOCK_KGE),
        "dma": tuple(evaluate.XHEEP_DMA_KGE),
        "periph bus": tuple(evaluate.XHEEP_PERIPH_BUS_KGE),
        "bank": evaluate.XHEEP_BANK_KGE,
        "onetoM": tuple(evaluate.ONE_TO_M_BUS_KGE),
        "ntom pair": evaluate.NTOM_PAIR_KGE,
    }


def model_kge(d: Design) -> float:
    """The model's X-HEEP without the CPU: its logic and its bus."""
    return evaluate.xheep_kge(d) + evaluate.bus_kge(d)


def measured_kge(b: dict[str, float]) -> float:
    """A synthesis's X-HEEP without the CPU."""
    return b["mcu"] - b["cpu"]


def load(outs: list[Path]) -> Runs:
    """Every ``blocks.json`` under ``outs``, by configuration; a later directory's
    replaces an earlier one's."""
    runs: Runs = {}
    for out in outs:
        for f in sorted(out.glob("*/blocks.json")):
            data = json.loads(f.read_text())
            runs[f.parent.name] = (Design.from_dict(data["design"]), data["blocks"])
    if not runs:
        raise SystemExit(f"no blocks.json under {', '.join(map(str, outs))}")
    return runs


def _row(name: str, meas: float, preds: dict[str, float | None]) -> str:
    cells = "".join(
        f"  {v:8.1f} ({100 * (v - meas) / meas:+6.2f} %)" if v is not None else f"{'':>20}"
        for v in preds.values()
    )
    return f"  {name:<8} {meas:8.1f}{cells}"


def fit(args: argparse.Namespace) -> None:
    runs = load(args.out)
    fitted = {n: r for n, r in runs.items() if n in CONFIGS}
    held = {n: r for n, r in runs.items() if n not in CONFIGS}
    names = list(runs)
    print("kGE per block" + "".join(f"{n:>9}" for n in names))
    for c in [*BLOCKS, "glue", "mcu", "accelerator"]:
        print(f"{c:<13}" + "".join(f"{runs[n][1].get(c, 0.0):9.2f}" for n in names))
    spread = {
        k: (max(xs) - min(xs)) / _mean(xs)  # type: ignore[operator]
        for k in ("flash", "debug", *OPTIONAL)
        if len(xs := [b[k] for b in (r[1] for r in runs.values()) if b[k] > 0]) > 1
    }
    worst = max(spread, key=lambda k: spread[k])
    print(
        f"\nA block's largest spread between configurations: {worst}, {100 * spread[worst]:.2f} %"
    )

    as_shipped = shipped()
    full, missing = prices(fitted)
    if missing:
        print(f"\nNo fitted configuration measures: {', '.join(missing)}")
    print(f"\nFitted to {', '.join(fitted)}, through {ANCHOR}:")
    for k, v in full.items():
        print(f"  {k}: {_fmt(v)}")

    cols = ("shipped", "fitted", "left out")
    print("\nX-HEEP without the CPU, kGE: measured; the model " + ", ".join(cols))
    errs: dict[str, list[float]] = {c: [] for c in cols}
    for n, (d, b) in fitted.items():
        meas = measured_kge(b)
        apply(as_shipped)
        row: dict[str, float | None] = {"shipped": model_kge(d)}
        apply(full)
        row["fitted"] = model_kge(d)
        rest = {k: v for k, v in fitted.items() if k != n}
        loo, lacking = prices(rest, ANCHOR if n != ANCHOR else None, fallback=full)
        lacks = sorted(set(lacking) & needs(d))
        if lacks:
            row["left out"] = None
        else:
            apply(loo)
            row["left out"] = model_kge(d)
        for k, v in row.items():
            if v is not None:
                errs[k].append(abs(v - meas) / meas)
        note = f"  (only {n} measures {', '.join(lacks)})" if lacks else ""
        print(_row(n, meas, row) + note)
    if held:
        print("Configurations the fit never sees:")
        for n, (d, b) in held.items():
            meas = measured_kge(b)
            apply(as_shipped)
            row = {"shipped": model_kge(d)}
            apply(full)
            row["fitted"] = model_kge(d)
            row["left out"] = row["fitted"]
            for k, v in row.items():
                errs[k].append(abs(v - meas) / meas)  # type: ignore[operator]
            print(_row(n, meas, row))
    apply(as_shipped)
    for k, e in errs.items():
        if e:
            print(f"  {k}: mean |error| {100 * sum(e) / len(e):.2f} %, worst {100 * max(e):.2f} %")


def _fmt(x: Any) -> str:
    if isinstance(x, dict):
        return ", ".join(f"{k} {v:.2f}" for k, v in x.items())
    if isinstance(x, tuple):
        return ", ".join(f"{v:.3f}" for v in x)
    return f"{x:.3f}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("out", type=Path)
    m.add_argument("--configs", default="")
    m.add_argument("--yosys", default="yosys")
    m.add_argument("--jobs", type=int, default=1)
    m.add_argument("--xheep", type=Path, default=None)
    m.set_defaults(func=measure)
    c = sub.add_parser("collect")
    c.add_argument("out", type=Path)
    c.set_defaults(func=collect)
    f = sub.add_parser("fit")
    f.add_argument("out", type=Path, nargs="+")
    f.set_defaults(func=fit)
    args = ap.parse_args(argv)
    args.out = [p.resolve() for p in args.out] if args.cmd == "fit" else args.out.resolve()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
