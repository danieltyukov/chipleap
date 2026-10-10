import ast
import random
import shutil
import subprocess

import pytest
import yaml

from chipleap.gen.bundle import load_design, write_bundle
from chipleap.gen.datasheet import REGISTERS, datasheet, reggen_hjson
from chipleap.gen.rtl import driver_header, rtl_params
from chipleap.gen.xheep import EXT_LARGE, xheep_config
from chipleap.hw import Accel, Design
from chipleap.model.accel import TilingError, plan_tiles

DESIGN = Design(
    name="demo",
    tech="generic-28nm",
    freq_mhz=400,
    cpu="cv32e40p",
    bus="NtoM",
    banks=4,
    il_banks=4,
    ext_mem="lpddr4-x16",
    accel=Accel("gemv", 8, 8, 8, ports=4, abuf_kib=16, wbuf_kib=8),
)


def test_xheep_config_is_valid_python():
    text = xheep_config(DESIGN, ext_bytes=7e8)
    tree = ast.parse(text)
    assert any(isinstance(n, ast.FunctionDef) and n.name == "config" for n in tree.body)
    assert "memory_ss.add_ram_banks([32] * 4)" in text
    assert 'add_ram_banks_il(4, 16, "data_interleaved")' in text
    assert "BusType.NtoM" in text and "cv32e40p(corev_pulp=True)" in text
    assert "0xc0000000" in text


def test_xheep_flash_cache_needs_the_ntom_bus():
    assert 'cache="yes"' in xheep_config(DESIGN)
    one = Design(bus="onetoM", accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2))
    assert 'cache="no"' in xheep_config(one)


def test_xheep_config_large_external_window():
    text = xheep_config(DESIGN, ext_bytes=1.5 * 2**30)
    assert f"{EXT_LARGE[0]:#010x}" in text


def test_register_map_is_consistent():
    offsets = [r.offset for r in REGISTERS]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    hj = reggen_hjson("demo")
    for r in REGISTERS:
        assert f'name: "{r.name}"' in hj


def test_consistency_check_catches_drift(tmp_path):
    from chipleap.verify.consistency import check

    write_bundle(tmp_path, DESIGN, weights="int4")
    assert check(tmp_path, DESIGN) == []
    top = tmp_path / "rtl" / "chipleap_accel_top.sv"
    top.write_text(top.read_text().replace(".PORTS(4)", ".PORTS(2)"))
    cfg = tmp_path / "xheep" / "demo.py"
    cfg.write_text(cfg.read_text().replace("[32] * 4", "[32] * 2"))
    hdr = tmp_path / "sw" / "chipleap_driver.h"
    hdr.write_text(hdr.read_text().replace("CHIPLEAP_REG_TK 0x24u", "CHIPLEAP_REG_TK 0x28u"))
    problems = check(tmp_path, DESIGN)
    assert any("PORTS" in p for p in problems)
    assert any("banks differ" in p for p in problems)
    assert any("register TK" in p for p in problems)


def test_datasheet_mentions_everything():
    text = datasheet(DESIGN, spec_name="s")
    for needle in ("Memory map", "0x20070000", "OBI read masters", "chipleap_plan", "CTRL"):
        assert needle in text


def test_bundle_roundtrip(tmp_path):
    files = write_bundle(tmp_path, DESIGN, weights="int4")
    names = {str(f.relative_to(tmp_path)) for f in files}
    for expected in (
        "design.yaml",
        "DATASHEET.md",
        "xheep/demo.py",
        "rtl/chipleap_accel.sv",
        "rtl/chipleap_accel_xheep.sv",
        "rtl/chipleap_accel.hjson",
        "sw/chipleap_driver.h",
        "tb/tb_chipleap.cpp",
        "README.md",
    ):
        assert expected in names
    d, w = load_design(tmp_path)
    assert d == DESIGN and w == "int4"
    assert yaml.safe_load((tmp_path / "design.yaml").read_text())["design"]["accel"]["rows"] == 8


def test_bundle_without_accelerator(tmp_path):
    files = write_bundle(tmp_path, Design(name="cpu-only"))
    assert not any("rtl" in str(f) for f in files)


HARNESS = r"""
#include <stdio.h>
#include "chipleap_driver.h"
int main(void) {
  unsigned m, k, n, fmt;
  while (scanf("%u %u %u %u", &m, &k, &n, &fmt) == 4) {
    chipleap_plan_t p;
    int rc = chipleap_plan(m, k, n, fmt, &p);
    if (rc) printf("err\n");
    else printf("%u %u %u %u %u\n", p.k_pad, p.tm, p.tn, p.tk, p.x_resident);
  }
  return 0;
}
"""


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
@pytest.mark.parametrize(
    "accel",
    [
        Accel("gemv", 8, 8, 8, 4, 16, 8, True),
        Accel("gemv", 4, 2, 4, 1, 1, 1, False),
        Accel("ws", 4, 8, 8, 2, 2, 4, True),
        Accel("os", 16, 4, 2, 8, 8, 2, True),
    ],
)
def test_c_driver_plans_like_the_model(tmp_path, accel):
    (tmp_path / "chipleap_driver.h").write_text(driver_header(accel, "t"))
    (tmp_path / "h.c").write_text(HARNESS)
    subprocess.run(
        [
            "cc",
            "-std=c99",
            "-Wall",
            "-Werror",
            "-O1",
            "-o",
            str(tmp_path / "h"),
            str(tmp_path / "h.c"),
        ],
        check=True,
        cwd=tmp_path,
    )
    rng = random.Random(9)
    cases = [
        (
            rng.randint(1, 40),
            rng.randint(1, 9000),
            rng.randint(1, 500),
            rng.choice([f for f in (8, 4, 2) if f <= accel.w_bits]),
        )
        for _ in range(1500)
    ]
    out = subprocess.run(
        [str(tmp_path / "h")],
        input="\n".join(" ".join(map(str, c)) for c in cases),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for case, line in zip(cases, out, strict=True):
        try:
            p = plan_tiles(accel, *case)
        except TilingError:
            assert line == "err"
            continue
        assert line == f"{p.k} {p.tm} {p.tn} {p.tk} {int(p.x_resident)}", case


@pytest.mark.skipif(shutil.which("riscv64-unknown-elf-gcc") is None, reason="no RISC-V compiler")
def test_driver_compiles_for_rv32(tmp_path):
    (tmp_path / "chipleap_driver.h").write_text(driver_header(DESIGN.accel, "t"))
    (tmp_path / "use.c").write_text(
        '#include "chipleap_driver.h"\n'
        "int run(void) { chipleap_plan_t p; if (chipleap_plan(1, 2048, 8192, 4, &p)) return -1;\n"
        "  chipleap_start(CHIPLEAP_BASE, 0x1000, 0x2000, 0x3000, 1, 8192, 4, &p);\n"
        "  while (chipleap_busy(CHIPLEAP_BASE)) {}\n  return (int)chipleap_cycles(CHIPLEAP_BASE); }\n"
    )
    subprocess.run(
        [
            "riscv64-unknown-elf-gcc",
            "-march=rv32imc",
            "-mabi=ilp32",
            "-O2",
            "-Wall",
            "-Werror",
            "-c",
            "use.c",
        ],
        check=True,
        cwd=tmp_path,
    )


def test_rtl_params():
    p = rtl_params(DESIGN.accel)
    assert p.abuf_words == 16 * 256 and p.dataflow == 0
    assert "-GPORTS=4" in p.verilator_args()
