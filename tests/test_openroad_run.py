"""Running OpenROAD, staging its inputs, and finding the PDK and routed views it reads."""

import json
import os

import pytest

from chipleap.rtl import openroad as orr
from chipleap.rtl.project import load_project


def fake_pdk(root):
    sc = root / "ihp-sg13g2" / "libs.ref" / "sg13g2_stdcell"
    for rel in (
        "lef/sg13g2_tech.lef",
        "lef/sg13g2_stdcell.lef",
        "lib/sg13g2_stdcell_typ_1p20V_25C.lib",
        "lib/sg13g2_stdcell_slow_1p08V_125C.lib",
        "verilog/sg13g2_stdcell.v",
        "verilog/sg13g2_udp.v",
    ):
        (sc / rel).parent.mkdir(parents=True, exist_ok=True)
        (sc / rel).write_text("x\n")
    return sc


@pytest.fixture
def pdk(tmp_path, monkeypatch):
    root = tmp_path / "pdk"
    sc = fake_pdk(root)
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.delenv("CHIPLEAP_LIBERTY", raising=False)
    monkeypatch.setattr(orr, "ihp_liberty", lambda: sc / "lib/sg13g2_stdcell_typ_1p20V_25C.lib")
    return sc


def test_views_beside_the_liberty(counter_project, pdk):
    v = orr.pdk_views(load_project(counter_project))
    assert v.tech_lef == (pdk / "lef/sg13g2_tech.lef").resolve()
    assert set(v.liberty) == {"typ", "slow"}
    assert [m.name for m in v.models] == ["sg13g2_udp.v", "sg13g2_stdcell.v"]
    with pytest.raises(orr.OpenroadUnavailable, match="no fast liberty"):
        v.lib("fast")


def routed(project_dir, pdk, cfg=None):
    run = project_dir / "build/pnr/runs/chipleap"
    final = run / "final"
    for rel in (
        "nl/counter.nl.v",
        "def/counter.def",
        "sdc/counter.sdc",
        "spef/nom/counter.nom.spef",
    ):
        (final / rel).parent.mkdir(parents=True, exist_ok=True)
        (final / rel).write_text("x\n")
    (final / "metrics.json").write_text(json.dumps({"timing__setup__ws": 1.0}))
    resolved = cfg or {
        "TECH_LEFS": {"nom_*": str(pdk / "lef/sg13g2_tech.lef")},
        "CELL_LEFS": [str(pdk / "lef/sg13g2_stdcell.lef")],
        "LIB": {"nom_typ_1p20V_25C": [str(pdk / "lib/sg13g2_stdcell_typ_1p20V_25C.lib")]},
        "CELL_VERILOG_MODELS": [str(pdk / "verilog/sg13g2_stdcell.v")],
    }
    (run / "resolved.json").write_text(json.dumps(resolved))
    return run


def test_views_of_a_run_are_confined(counter_project, pdk, tmp_path):
    p = load_project(counter_project)
    run = routed(counter_project, pdk)
    v = orr.pdk_views(p, run)
    assert set(v.liberty) == {"typ"} and len(v.models) == 2
    outside = tmp_path / "elsewhere.lef"
    outside.write_text("secret\n")
    routed(counter_project, pdk, {"TECH_LEFS": {"nom_*": str(outside)}, "CELL_LEFS": [], "LIB": {}})
    with pytest.raises(orr.OpenroadError, match="not inside a PDK directory"):
        orr.pdk_views(p, run)


def test_routed_views_and_eco_marker(counter_project, pdk, tmp_path):
    p = load_project(counter_project)
    assert orr.routed_run(p) is None
    run = routed(counter_project, pdk)
    assert orr.routed_run(p) == run
    views = orr.routed_views(p)
    assert views.netlist.name == "counter.nl.v" and views.spef is not None
    eco = counter_project / "build/pnr/runs/eco"
    (eco / "final").mkdir(parents=True)
    (eco / "final/metrics.json").write_text("{}")
    (eco / orr.ECO_MARKER).write_text(json.dumps({"base_metrics_sha256": "stale"}))
    assert orr.routed_run(p) == run  # an ECO of an older routed run is ignored
    (eco / orr.ECO_MARKER).write_text(json.dumps({"base_metrics_sha256": orr.base_digest(p)}))
    assert orr.routed_run(p) == eco
    # A final view that is a link out of the run is refused.
    (run / "final/sdc/counter.sdc").unlink()
    secret = tmp_path / "secret.sdc"
    secret.write_text("x\n")
    os.symlink(secret, run / "final/sdc/counter.sdc")
    with pytest.raises(orr.OpenroadError, match="not a file of the run"):
        orr.routed_views(p, run)


def test_stage_links_or_copies(tmp_path):
    src = tmp_path / "big.spef"
    src.write_text("spef\n")
    rel = orr.stage(src, tmp_path / "work")
    assert rel == "inputs/big.spef" and (tmp_path / "work" / rel).read_text() == "spef\n"
    assert orr.stage(src, tmp_path / "work", "renamed.spef") == "inputs/renamed.spef"


def test_run_and_failure(tmp_path, monkeypatch):
    tool = tmp_path / "openroad"
    tool.write_text(
        '#!/bin/sh\necho "args: $*"\ncat "$3" >/dev/null\necho \'[ERROR GPL-0302] too dense\'\nexit 3\n'
    )
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_OPENROAD", str(tool))
    log, code = orr.run_openroad("puts hi\n", tmp_path / "w", "step")
    assert code == 3 and "args: -no_init -exit step.tcl" in log
    assert (tmp_path / "w/step.tcl").read_text() == "puts hi\n"
    assert "GPL-0302" in str(orr.failure("placement", log, code))


def test_reports_have_a_size_limit(tmp_path):
    big = tmp_path / "r.rpt"
    big.write_text("x" * 100)
    with pytest.raises(orr.OpenroadError, match="larger than"):
        orr.read_limited(big, limit=10)
