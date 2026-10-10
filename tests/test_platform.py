"""The standalone platform: the accelerator as AXI4 IP for any host, next to X-HEEP."""

import shutil
import subprocess

import pytest
import yaml

from chipleap import mcp_server
from chipleap.cli import main
from chipleap.explore import explore
from chipleap.gen.bundle import load_design, write_bundle
from chipleap.gen.rtl import driver_header
from chipleap.hw import Accel, Design
from chipleap.hw.design import DesignError
from chipleap.model.evaluate import evaluate
from chipleap.spec import SpecError, load_spec, parse_spec
from chipleap.verify.consistency import check
from chipleap.verify.run import check_xheep
from chipleap.workload import LLMWorkloadSpec, build_llm

ACCEL = Accel("gemv", 4, 8, 8, ports=2, abuf_kib=4, wbuf_kib=2)
XHEEP = Design(name="demo", bus="NtoM", ext_mem="octal-psram", ext_channels=2, accel=ACCEL)
STANDALONE = XHEEP.with_(platform="standalone")
SPEC = {
    "name": "smol",
    "workload": {"llm": {"model": "smollm2-135m", "context": 256, "weights": "int4"}},
    "targets": {"throughput": 1},
    "constraints": {"tech": "ihp-sg13g2"},
    "objective": "area",
}


def test_platform_is_validated_and_kept():
    with pytest.raises(DesignError, match="platform"):
        Design(platform="zynq").validate()
    d = Design.from_dict(STANDALONE.to_dict())
    assert d == STANDALONE and d.platform == "standalone"
    assert "standalone" in STANDALONE.summary()
    assert "standalone" not in XHEEP.summary() and "xheep" not in XHEEP.summary()


def test_standalone_bundle_has_the_axi_ip_and_no_xheep_config(tmp_path):
    files = {str(f.relative_to(tmp_path)) for f in write_bundle(tmp_path, STANDALONE)}
    for f in (
        "rtl/chipleap_accel.sv",
        "rtl/chipleap_accel_axi.sv",
        "rtl/chipleap_accel_axi_top.sv",
        "rtl/chipleap_accel.core",
        "sw/chipleap_driver.h",
        "tb/tb_chipleap_axi.cpp",
    ):
        assert f in files
    assert not (tmp_path / "xheep").exists()
    assert "rtl/chipleap_accel_xheep.sv" not in files
    assert "toplevel: chipleap_accel_axi_top" in (tmp_path / "rtl/chipleap_accel.core").read_text()
    top = (tmp_path / "rtl/chipleap_accel_axi_top.sv").read_text()
    assert ".PORTS(2)" in top and "chipleap_accel_axi #(" in top
    sheet = (tmp_path / "DATASHEET.md").read_text()
    assert "AXI4-Lite slave" in sheet and "Integration with any host" in sheet
    assert "Integration into X-HEEP" not in sheet and "## Memory map" not in sheet
    assert load_design(tmp_path)[0].platform == "standalone"
    assert check(tmp_path, STANDALONE) == []


def test_xheep_bundle_is_unchanged(tmp_path):
    files = {str(f.relative_to(tmp_path)) for f in write_bundle(tmp_path, XHEEP)}
    assert "xheep/demo.py" in files and "rtl/chipleap_accel_xheep.sv" in files
    assert "rtl/chipleap_accel_axi.sv" not in files
    assert check(tmp_path, XHEEP) == []


def test_consistency_catches_standalone_drift(tmp_path):
    write_bundle(tmp_path, STANDALONE)
    top = tmp_path / "rtl/chipleap_accel_axi_top.sv"
    top.write_text(top.read_text().replace(".PORTS(2)", ".PORTS(4)"))
    (tmp_path / "xheep").mkdir()
    (tmp_path / "xheep/demo.py").write_text("# stray\n")
    problems = check(tmp_path, STANDALONE)
    assert any("PORTS" in p for p in problems)
    assert any("platform is standalone" in p for p in problems)
    (tmp_path / "rtl/chipleap_accel_axi.sv").unlink()
    assert any("chipleap_accel_axi.sv is missing" in p for p in check(tmp_path, STANDALONE))


def test_xheep_step_skips_standalone(tmp_path):
    r = check_xheep(tmp_path, STANDALONE, None, False)
    assert r.status == "skipped" and "not an X-HEEP design" in r.summary


def test_standalone_driver_needs_a_base_address():
    sa = driver_header(ACCEL, "demo", platform="standalone")
    assert "#error" in sa and "X-HEEP" not in sa
    xh = driver_header(ACCEL, "demo")
    assert "#define CHIPLEAP_BASE 0x20070000u" in xh and "#ifndef CHIPLEAP_BASE" in xh


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_standalone_driver_compiles_with_a_base(tmp_path):
    (tmp_path / "chipleap_driver.h").write_text(driver_header(ACCEL, "demo", platform="standalone"))
    src = tmp_path / "use.c"
    src.write_text(
        '#include "chipleap_driver.h"\n'
        "int go(void) { chipleap_plan_t p; return chipleap_plan(1, 64, 8, 4, &p); }\n"
    )
    base = ["cc", "-std=c99", "-Wall", "-Werror", "-c", f"-I{tmp_path}", str(src), "-o"]
    bad = subprocess.run([*base, str(tmp_path / "a.o")], capture_output=True, text=True)
    assert bad.returncode != 0 and "CHIPLEAP_BASE" in bad.stderr
    ok = subprocess.run(
        [*base, str(tmp_path / "b.o"), "-DCHIPLEAP_BASE=0x43C00000u"],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr


def test_standalone_area_separates_the_ip_from_the_host():
    wl = build_llm(LLMWorkloadSpec("smollm2-135m", context=256, weights="int4"))
    xh, sa = evaluate(wl, XHEEP), evaluate(wl, STANDALONE)
    assert "axi adapter" not in xh.area_mm2 and sa.area_mm2["axi adapter"] > 0
    ip = sa.area_mm2["accelerator logic"] + sa.area_mm2["accelerator buffers"]
    assert sa.ip_area == pytest.approx(ip + sa.area_mm2["axi adapter"])
    assert xh.ip_area == pytest.approx(ip)
    assert sa.total_area == pytest.approx(xh.total_area + sa.area_mm2["axi adapter"])
    assert sa.throughput == xh.throughput
    assert sa.metrics()["ip_area_mm2"] == pytest.approx(sa.ip_area)


def test_spec_platform():
    assert parse_spec(SPEC).platform == "xheep"
    s = parse_spec({**SPEC, "platform": "standalone"})
    assert s.platform == "standalone" and s.to_dict()["platform"] == "standalone"
    assert "platform" not in parse_spec(SPEC).to_dict()
    with pytest.raises(SpecError, match="platform"):
        parse_spec({**SPEC, "platform": "arm"})
    started = parse_spec({**SPEC, "platform": "standalone", "start": {"tech": "ihp-sg13g2"}})
    assert started.start is not None and started.start.platform == "standalone"
    with pytest.raises(SpecError, match="differs"):
        parse_spec({**SPEC, "platform": "standalone", "start": {"platform": "xheep"}})


def test_explorer_never_changes_the_platform():
    r = explore(parse_spec({**SPEC, "platform": "standalone"}), samples=150, seed=3)
    assert r.best.design.platform == "standalone"
    assert r.baseline.design.platform == "standalone"
    assert all(c.design.platform == "standalone" for c in r.pareto)
    assert all(s.chosen is None or s.after.design.platform == "standalone" for s in r.steps)


def test_mcp_generate_soc_platform(tmp_path):
    spec = yaml.safe_dump(SPEC)
    design = yaml.safe_dump(XHEEP.to_dict())
    out = mcp_server.generate_soc(spec, design, str(tmp_path), platform="standalone")
    assert "rtl/chipleap_accel_axi.sv" in out["files"]
    assert not any(f.startswith("xheep/") for f in out["files"])
    assert "platform:" in mcp_server.SPEC_REFERENCE
    with pytest.raises(mcp_server.ToolError):
        mcp_server.generate_soc(spec, design, str(tmp_path / "x"), platform="zynq")


def test_cli_platform(tmp_path):
    src = tmp_path / "xh"
    write_bundle(src, XHEEP)
    out = tmp_path / "sa"
    assert main(["generate", str(src), "-o", str(out), "--platform", "standalone"]) == 0
    assert load_design(out)[0].platform == "standalone"
    assert (out / "rtl/chipleap_accel_axi_top.sv").is_file()
    spec = tmp_path / "s.yaml"
    spec.write_text(yaml.safe_dump(SPEC))
    out2 = tmp_path / "ex"
    main(["explore", str(spec), "--samples", "60", "--platform", "standalone", "-o", str(out2)])
    assert load_design(out2)[0].platform == "standalone"
    assert load_spec(spec).platform == "xheep"
