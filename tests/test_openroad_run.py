"""Running OpenROAD on staged copies, and the PDK and routed views it reads: a routed run
is used only as ChipLeap recorded it."""

import json
import os
import re

import pytest

from chipleap.rtl import openroad as orr
from chipleap.rtl import quote as q
from chipleap.rtl.pnr import seal_routed
from chipleap.rtl.project import load_project
from chipleap.rtl.provenance import open_sealed, seal


@pytest.fixture
def pdk(ihp_pdk):
    return ihp_pdk


def routed(project_dir, pdk, cfg=None, sealed=True):
    """A routed run as LibreLane leaves it, recorded by ChipLeap unless ``sealed`` is off."""
    pnr = project_dir / "build/pnr"
    run = pnr / "runs/chipleap"
    final = run / "final"
    for rel in (
        "nl/counter.nl.v",
        "def/counter.def",
        "sdc/counter.sdc",
        "spef/nom/counter.nom.spef",
    ):
        (final / rel).parent.mkdir(parents=True, exist_ok=True)
        (final / rel).write_text(f"{rel}\n")
    (final / "metrics.json").write_text(json.dumps({"timing__setup__ws": 1.0}))
    resolved = cfg or {
        "TECH_LEFS": {"nom_*": str(pdk / "lef/sg13g2_tech.lef")},
        "CELL_LEFS": [str(pdk / "lef/sg13g2_stdcell.lef")],
        "LIB": {"nom_typ_1p20V_25C": [str(pdk / "lib/sg13g2_stdcell_typ_1p20V_25C.lib")]},
        "CELL_VERILOG_MODELS": [str(pdk / "verilog/sg13g2_stdcell.v")],
    }
    (run / "resolved.json").write_text(json.dumps(resolved))
    (pnr / "config.json").write_text(json.dumps({"DESIGN_NAME": "counter"}))
    if sealed:
        seal_routed(pnr)
    return run


def test_views_beside_the_liberty(counter_project, pdk):
    v = orr.pdk_views(load_project(counter_project))
    assert v.tech_lef == (pdk / "lef/sg13g2_tech.lef").resolve()
    assert set(v.liberty) == {"typ", "slow"}
    assert [m.name for m in v.models] == ["sg13g2_udp.v", "sg13g2_stdcell.v"]
    with pytest.raises(orr.OpenroadUnavailable, match="no fast liberty"):
        v.lib("fast")


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


def test_a_run_chipleap_did_not_record_is_refused(counter_project, pdk, tmp_path, monkeypatch):
    p = load_project(counter_project)
    assert orr.routed_run(p) is None
    routed(counter_project, pdk, sealed=False)
    with pytest.raises(orr.OpenroadError, match=r"manifest\.json is missing"):
        orr.routed_run(p)
    # A manifest edited by hand (here, to vouch for a planted file) does not verify.
    routed(counter_project, pdk)
    manifest = counter_project / "build/pnr" / orr.PNR_MANIFEST
    data = json.loads(manifest.read_text())
    data["files"]["runs/chipleap/planted.tcl"] = "0" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(orr.OpenroadError, match="does not verify"):
        orr.routed_run(p)
    # Nor does one written under another machine's key.
    routed(counter_project, pdk)
    assert orr.routed_run(p) is not None
    monkeypatch.setenv("CHIPLEAP_BUILD_KEY", str(tmp_path / "other.key"))
    with pytest.raises(orr.OpenroadError, match="does not verify"):
        orr.routed_run(p)


def test_a_file_changed_after_recording_is_refused(counter_project, pdk):
    p = load_project(counter_project)
    routed(counter_project, pdk)
    views = orr.routed_views(p)
    assert views.netlist.name == "counter.nl.v" and views.spef is not None
    job = orr.Job(p.work_dir("power"), "j")
    assert job.stage(views.netlist, sealed=views.sealed) == "inputs/counter.nl.v"
    views.def_.write_text("DESIGN planted ;\n")
    with pytest.raises(orr.OpenroadError, match="changed after ChipLeap recorded it"):
        job.stage(views.def_, sealed=views.sealed)
    assert not (job.inputs / "counter.def").exists()


def test_a_view_that_became_a_link_is_refused(counter_project, pdk, tmp_path):
    p = load_project(counter_project)
    run = routed(counter_project, pdk)
    secret = tmp_path / "secret.sdc"
    secret.write_text("sdc/counter.sdc\n")  # even with the recorded contents
    sdc = run / "final/sdc/counter.sdc"
    sdc.unlink()
    os.symlink(secret, sdc)
    views = orr.routed_views(p)
    job = orr.Job(p.work_dir("power"), "j")
    with pytest.raises(orr.OpenroadError, match="cannot be staged"):
        job.stage(views.sdc, sealed=views.sealed)
    # A link that was there when the run was recorded was never recorded.
    seal_routed(counter_project / "build/pnr")
    with pytest.raises(orr.OpenroadError, match="not a file ChipLeap recorded"):
        orr.routed_views(p)


def test_a_symlinked_pnr_directory_is_refused(counter_project, pdk, tmp_path):
    """Pointing build/pnr at another project's routed run, which ChipLeap did record,
    still fails: results are read only from the project's own build/pnr."""
    other = tmp_path / "other"
    (other / "build").mkdir(parents=True)
    routed(other, pdk)
    (counter_project / "build").mkdir(exist_ok=True)
    (counter_project / "build/pnr").symlink_to(other / "build/pnr")
    with pytest.raises(ValueError, match="pnr is a symlink"):
        orr.routed_run(load_project(counter_project))


def test_an_eco_counts_only_for_the_run_it_was_made_from(counter_project, pdk):
    p = load_project(counter_project)
    run = routed(counter_project, pdk)
    pnr = counter_project / "build/pnr"
    eco = pnr / "runs/eco"
    (eco / "final").mkdir(parents=True)
    (eco / "final/metrics.json").write_text("{}")
    seal(pnr, orr.ECO_MANIFEST, "eco", ["runs/eco"], {"base": "an older run"})
    assert orr.routed_run(p) == run
    base = open_sealed(pnr, orr.PNR_MANIFEST, "pnr")
    seal(pnr, orr.ECO_MANIFEST, "eco", ["runs/eco"], {"base": base.mac})
    assert orr.routed_run(p) == eco
    # Routing again makes the ECO stale, however alike the two runs are.
    seal_routed(pnr)
    (run / "final/metrics.json").write_text(json.dumps({"timing__setup__ws": 2.0}))
    seal_routed(pnr)
    assert orr.routed_run(p) == run


def test_a_job_stages_copies_into_a_private_directory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    src = tmp_path / "big.spef"
    src.write_text("spef\n")
    job = orr.Job(work, "j")
    rel = job.stage(src)
    assert rel == "inputs/big.spef" and (job.dir / rel).read_text() == "spef\n"
    assert not (job.dir / rel).is_symlink()
    assert job.stage(src, "renamed.spef") == "inputs/renamed.spef"
    with pytest.raises(orr.OpenroadError, match="staged twice"):
        job.stage(src)
    for bad in ("../x", "a/b", "-x", "$x.v", "x y"):
        with pytest.raises(orr.OpenroadError, match="not a plain file name"):
            job.stage(src, bad)
    # A job starts empty: nothing an earlier run, or a repository, left there remains.
    (job.dir / "planted.tcl").write_text("exec touch pwned\n")
    assert list(orr.Job(work, "j").dir.iterdir()) == [job.dir / "inputs"]


def test_a_job_runs_in_its_directory(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    tool = tmp_path / "bin/openroad"
    tool.parent.mkdir()
    tool.write_text(
        '#!/bin/sh\necho "args: $*"\necho "pwd: $(pwd)"\nls inputs\n'
        "echo '[ERROR GPL-0302] too dense'\nexit 3\n"
    )
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_OPENROAD", str(tool))
    src = tmp_path / "in.v"
    src.write_text("module m; endmodule\n")
    job = orr.Job(work, "step")
    job.stage(src)
    log, code = job.run("puts hi\n")
    assert code == 3 and "args: -no_init -exit step.tcl" in log
    assert f"pwd: {job.dir}" in log and "in.v" in log
    assert (job.dir / "step.tcl").read_text() == "puts hi\n"
    assert "GPL-0302" in str(orr.failure("placement", log, code))


def test_scripts_name_only_staged_inputs_and_quote_every_value(counter_project, pdk):
    """What each step's script reads is a quoted path inside the job; values from the
    project are quoted words or plain numbers."""
    from chipleap.rtl import dft, eco, irdrop, power, upf
    from chipleap.rtl.project import DftConfig

    p = load_project(counter_project)
    job = orr.Job(p.work_dir("x"), "j")
    files = orr.pdk_views(p).stage_all(job, ("typ", "slow"))
    nl = "inputs/counter.nl.v"
    scripts = [
        dft.scan_script(p, files, nl, DftConfig()),
        power.power_script(
            p, files, "typ", nl, "inputs/c.sdc", "inputs/c.spef", "inputs/gl.vcd", "tb/dut"
        ),
        irdrop.ir_script(p, files, "typ", "inputs/c.def", "inputs/c.sdc", None, None, ""),
        upf.upf_script(p, files, nl, "inputs/counter.upf", 40.0),
        eco.paths_script(p, files, "slow", nl, "inputs/c.sdc", None, "max", 5),
        eco.edit_script(files, "inputs/c.def", "inputs/changes.txt"),
    ]
    for script in scripts:
        for m in re.finditer(r"^(read_\w+|set changes_file).*?(\{[^}]*\})\s*$", script, re.M):
            assert m.group(2).startswith("{inputs/"), m.group(0)
        assert str(counter_project) not in script and str(pdk) not in script
    for bad in ("a}b", "[exec rm -rf ~]", "$env(HOME)", 'x"y', "a;b", "x\\", "\n"):
        with pytest.raises(q.QuoteError):
            q.tcl(bad)
    for bad in ("1", float("nan"), float("inf"), True, None):
        with pytest.raises(q.QuoteError):
            q.number(bad)
    assert q.number(0.5) == "0.5" and q.number(3) == "3"


def test_reports_have_a_size_limit(tmp_path):
    big = tmp_path / "r.rpt"
    big.write_text("x" * 100)
    with pytest.raises(orr.OpenroadError, match="larger than"):
        orr.read_limited(big, limit=10)
    link = tmp_path / "link.rpt"
    link.symlink_to(big)
    with pytest.raises(orr.OpenroadError):
        orr.read_limited(link)
