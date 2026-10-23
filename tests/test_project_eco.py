"""ECO: the base state of a routed run, the edits OpenROAD applies, the resumed flow, and
the timing paths to choose changes from; a real ECO when LibreLane runs are allowed."""

import json
import os
import shutil
from pathlib import Path

import pytest

from chipleap.rtl import eco as E
from chipleap.rtl.pnr import SDC_NAME, seal_routed
from chipleap.rtl.project import EcoChange, load_project
from chipleap.verify.pnr import signoff
from chipleap.verify.tools import librelane, librelane_version_key, openroad

FIXTURE = Path(__file__).parent / "fixtures" / "eco_timing"


def _librelane_before_3_0_14() -> bool:
    tool = librelane()
    return tool.found and librelane_version_key(tool.version) < (3, 0, 14, 2, 0)


STEPS = [
    "Yosys.Synthesis",
    "OpenROAD.ResizerTimingPostGRT",
    "OpenROAD.DetailedRouting",
    "OpenROAD.FillInsertion",
    "KLayout.DRC",
    "Netgen.LVS",
]

REPORT = """
CHIPLEAP-PATHS begin
Startpoint: _255_ (rising edge-triggered flip-flop clocked by clk_i)
Endpoint: _270_ (rising edge-triggered flip-flop clocked by clk_i)
Path Group: clk_i
Path Type: max

  Delay    Time   Description
---------------------------------------------------------
   0.0000    0.0000   clock clk_i (rise edge)
   0.3309    0.3309   clock network delay (propagated)
   0.0000    0.3309 ^ _255_/CLK (sg13g2_dfrbpq_2)
   0.2756    0.6065 ^ _255_/Q (sg13g2_dfrbpq_2)
   0.3360    0.9425 v _150_/Y (sg13g2_nand4_1)
   0.0001    0.9426 v _270_/D (sg13g2_dfrbpq_1)
             0.9426   data arrival time

  10.0000   10.0000   clock clk_i (rise edge)
   0.3000   10.3000   clock network delay (propagated)
   0.0000   10.3000 ^ _270_/CLK (sg13g2_dfrbpq_1)
            -0.1000   library setup time
             9.8000   data required time
---------------------------------------------------------
             8.8574   slack (MET)

Startpoint: tx_valid_i (input port clocked by clk_i)
Endpoint: _1_ (rising edge-triggered flip-flop clocked by clk_i)
Path Group: clk_i
Path Type: max
   0.0000    0.0000   clock clk_i (rise edge)
   2.0000    2.0000 ^ tx_valid_i (in)
   0.5000    2.5000 ^ _1_/D (sg13g2_dfrbpq_1)
             2.5000   data arrival time
            -0.2500   slack (VIOLATED)
CHIPLEAP-PATHS end
"""


def routed(project_dir, state=None, pdk=None, meta=None):
    """A routed run that ChipLeap recorded: its step directories, the state before
    detailed routing, the configuration beside it (with ``meta``, the flow it names) and,
    with ``pdk``, its PDK views."""
    pnr = project_dir / "build/pnr"
    run = pnr / "runs/chipleap"
    for n, step in enumerate(STEPS, 1):
        d = run / f"{n:02d}-{step.lower().replace('.', '-')}"
        d.mkdir(parents=True)
        (d / "config.json").write_text(json.dumps({"meta": {"step": step}}))
    route = run / "03-openroad-detailedrouting"
    (route / "counter.def").write_text("VERSION 5.8 ;\n")
    (route / "state_in.json").write_text(
        json.dumps(
            state or {"def": str(route / "counter.def"), "sdf": {"typ": None}, "metrics": {"a": 1}}
        )
    )
    (run / "final").mkdir()
    (run / "final/metrics.json").write_text("{}")
    if pdk is not None:
        (run / "resolved.json").write_text(
            json.dumps(
                {
                    "TECH_LEFS": {"nom_*": str(pdk / "lef/sg13g2_tech.lef")},
                    "CELL_LEFS": [str(pdk / "lef/sg13g2_stdcell.lef")],
                    "LIB": {
                        "nom_typ_1p20V_25C": [str(pdk / "lib/sg13g2_stdcell_typ_1p20V_25C.lib")]
                    },
                }
            )
        )
    (pnr / "src").mkdir()
    (pnr / "src/counter.v").write_text("module counter; endmodule\n")
    (pnr / SDC_NAME).write_text("create_clock -period 10 [get_ports clk_i]\n")
    (pnr / "config.json").write_text(
        json.dumps(
            {
                "DESIGN_NAME": "counter",
                "VERILOG_FILES": ["dir::src/counter.v"],
                "PNR_SDC_FILE": f"dir::{SDC_NAME}",
                "CLOCK_PERIOD": 10,
                **({"meta": meta} if meta is not None else {}),
            }
        )
    )
    seal_routed(pnr)
    return run


def test_base_resumes_at_detailed_routing(counter_project):
    p = load_project(counter_project)
    run = routed(counter_project)
    got_run, sealed, state, steps = E.base(p)
    assert got_run == run and steps == STEPS[2:] and sealed.what == "pnr"
    assert state["def"].endswith("counter.def") and state["metrics"] == {"a": 1}


def test_base_refuses_files_outside_the_run(counter_project, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("x\n")
    routed(counter_project, {"def": str(secret)})
    with pytest.raises(E.EcoError, match=r"state def: .* is not a file of build/pnr"):
        E.base(load_project(counter_project))


def test_edits_and_the_resumed_flow(counter_project):
    routed(counter_project)
    p = load_project(counter_project)
    _, sealed, _, steps = E.base(p)
    changes = [
        EcoChange("resize", "_150_", "sg13g2_nand4_2"),
        EcoChange("buffer", "\\u_rx.q[3]/D", "sg13g2_buf_2"),
        EcoChange("repair", "setup", "", 0.5),
        EcoChange("repair", "hold"),
    ]
    assert E.changes_file(changes) == (
        "resize\t_150_\tsg13g2_nand4_2\nbuffer\t\\u_rx.q[3]\tD\tsg13g2_buf_2\n"
    )
    cfg = E.eco_config(p, changes, steps, sealed)
    # Antennas are repaired again after the timing repair, as in place and route.
    assert cfg["meta"]["flow"] == [*E.PREFIX, E.REPAIR, "OpenROAD.RepairAntennas", *STEPS[2:]]
    assert cfg["GRT_RESIZER_SETUP_SLACK_MARGIN"] == 0.5
    assert cfg["GRT_RESIZER_HOLD_SLACK_MARGIN"] == 0.1
    flow = E.eco_config(p, changes[:1], steps, sealed)["meta"]["flow"]
    assert flow[3] == "OpenROAD.DetailedRouting"
    # Names reach OpenROAD as data, never as script text.
    script = E.edit_script({"tech_lef": "t.lef", "cell_lefs": ["c.lef"]}, "in.def", "changes.txt")
    assert "_150_" not in script and "set changes_file {changes.txt}" in script


def test_the_flow_a_routed_run_names_is_its_own_or_refused(counter_project):
    """Place and route names the Classic flow with the late antenna repair; an ECO writes
    its own step list in its place, and refuses any other flow."""
    routed(counter_project, meta=E.ROUTED_META)
    p = load_project(counter_project)
    _, sealed, _, steps = E.base(p)
    flow = E.eco_config(p, [], steps, sealed)["meta"]["flow"]
    assert flow == [*E.PREFIX, *STEPS[2:]]
    shutil.rmtree(counter_project / "build")
    routed(counter_project, meta={**E.ROUTED_META, "substituting_steps": {"+A.B": "Misc.C"}})
    _, sealed, _, steps = E.base(p)
    with pytest.raises(E.EcoError, match=r"sets meta to .*not the flow ChipLeap's place and"):
        E.eco_config(p, [], steps, sealed)


def test_edit_results(counter_project):
    edits = [
        EcoChange("resize", "_1_", "sg13g2_inv_2"),
        EcoChange("buffer", "_2_/A", "sg13g2_buf_1"),
    ]
    log = "CHIPLEAP-ECO done 0 sg13g2_inv_1\nCHIPLEAP-ECO done 1 eco_buf_0\nCHIPLEAP-ECO end\n"
    assert E.parse_edits(log, edits) == ["sg13g2_inv_1", "eco_buf_0"]
    with pytest.raises(E.EcoError, match="resize _1_ to sg13g2_inv_2: the new cell's pins differ"):
        E.parse_edits("CHIPLEAP-ECO error 0 pins-differ sg13g2_a21oi_1\n", edits)
    with pytest.raises(E.EcoError, match="no instance of that name"):
        E.parse_edits("CHIPLEAP-ECO error 1 no-instance\n", edits)


def test_timing_paths_report():
    paths = E.parse_paths(REPORT, "max")
    assert [(p.startpoint, p.endpoint, p.slack_ns, p.met) for p in paths] == [
        ("_255_", "_270_", 8.8574, True),
        ("tx_valid_i", "_1_", -0.25, False),
    ]
    stages = paths[0].stages
    assert [(s.instance, s.cell) for s in stages] == [
        ("_255_", "sg13g2_dfrbpq_2"),
        ("_255_", "sg13g2_dfrbpq_2"),
        ("_150_", "sg13g2_nand4_1"),
        ("_270_", "sg13g2_dfrbpq_1"),
    ]
    assert paths[0].to_dict()["stages"][2] == {
        "pin": "_150_/Y",
        "instance": "_150_",
        "cell": "sg13g2_nand4_1",
        "edge": "fall",
        "delay_ns": 0.336,
        "time_ns": 0.9425,
    }
    assert paths[1].stages[0].pin == "tx_valid_i" and paths[1].stages[0].instance == ""


def test_step_skips(counter_project):
    p = load_project(counter_project)
    assert E.eco(p).status == "skipped"
    r = E.eco(p, [EcoChange("repair", "setup")])
    assert r.status == "skipped" and "no routed design" in r.summary


@pytest.mark.pnr
@pytest.mark.skipif(
    not (librelane().found and openroad().found), reason="needs LibreLane, OpenROAD"
)
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_PNR"), reason="set CHIPLEAP_RUN_PNR=1")
def test_counter_eco_signs_off_again(counter_project):
    from chipleap.rtl.pnr import pnr

    p = load_project(counter_project)
    assert pnr(p).status == "pass"
    paths, run = E.timing_paths(p, "slow", "setup", 3)
    assert run == "build/pnr/runs/chipleap" and paths
    cell = next(s for s in paths[0].stages if s.cell.endswith("_1") and s.instance)
    bigger = cell.cell[:-1] + "2"
    r = E.eco(p, [EcoChange("resize", cell.instance, bigger)])
    assert r.status == "pass", r.summary
    assert r.data["changes"][0]["result"] == f"was {cell.cell}"
    assert r.data["after"]["lvs_errors"] == 0


# Only what ChipLeap recorded is reused, and nothing planted in the project runs.


@pytest.fixture
def fake_flow(tmp_path, monkeypatch, ihp_pdk):
    """OpenROAD and LibreLane stand-ins outside the project. Each notes that it ran;
    OpenROAD applies one edit and writes the DEF, LibreLane copies the configuration and
    the state it was given to ``seen/``. Asked to stop after detailed routing, it leaves
    a routed state with nothing under the minimum metal area; asked for the rest, it
    finishes the run."""
    bin_dir = tmp_path / "trusted-bin"
    seen = tmp_path / "seen"
    bin_dir.mkdir()
    seen.mkdir()
    tools = {
        "openroad": (
            f'echo openroad >> "{seen}/ran"\n'
            'echo "VERSION 5.8 ; # edited" > eco.def\n'
            "echo 'CHIPLEAP-ECO done 0 sg13g2_inv_1'\n"
            "echo 'CHIPLEAP-ECO end'\n"
        ),
        "librelane": (
            f'echo librelane >> "{seen}/ran"\n'
            'for a in "$@"; do last=$a; done\n'
            f'cp "$last" "{seen}/config.json"\n'
            "to=\n"
            "while [ $# -gt 0 ]; do\n"
            f'  if [ "$1" = --with-initial-state ]; then cp "$2" "{seen}/state.json"; fi\n'
            '  if [ "$1" = --to ]; then to=1; fi\n'
            "  shift\n"
            "done\n"
            'if [ -n "$to" ]; then\n'
            "  d=runs/eco/03-openroad-detailedrouting && mkdir -p $d\n"
            "  printf 'UNITS DISTANCE MICRONS 1000 ;\\nNETS 0 ;\\nEND NETS\\n' > $d/counter.def\n"
            '  echo "{\\"def\\": \\"$PWD/$d/counter.def\\"}" > $d/state_out.json\n'
            "  echo 'MANUFACTURINGGRID 0.005 ;' > runs/eco/tech.lef\n"
            '  echo "{\\"TECH_LEFS\\": {\\"nom_*\\": \\"$PWD/runs/eco/tech.lef\\"}}" > runs/eco/resolved.json\n'
            "else\n"
            "  mkdir -p runs/eco/final && echo '{}' > runs/eco/final/metrics.json\n"
            "fi\n"
        ),
    }
    for name, body in tools.items():
        exe = bin_dir / name
        # A version probe is not a run.
        exe.write_text('#!/bin/sh\ncase "$1" in -version | --version) exit 0 ;; esac\n' + body)
        exe.chmod(0o755)
        monkeypatch.setenv(f"CHIPLEAP_{name.upper()}", str(exe))
    return seen


def ran(seen):
    f = seen / "ran"
    return f.read_text().split() if f.exists() else []


RESIZE = [EcoChange("resize", "_150_", "sg13g2_nand4_2")]


def test_an_eco_passes_on_only_what_chipleap_recorded(counter_project, ihp_pdk, fake_flow):
    """A Tcl script, a step and a hook planted in the routed run after ChipLeap recorded it
    reach neither OpenROAD nor LibreLane."""
    run = routed(counter_project, pdk=ihp_pdk)
    (run / "03-openroad-detailedrouting/planted.tcl").write_text("exec touch pwned\n")
    planted_step = run / "99-planted-hook"
    planted_step.mkdir()
    (planted_step / "config.json").write_text(json.dumps({"meta": {"step": "Odb.PlantedHook"}}))
    r = E.eco(load_project(counter_project), RESIZE)
    assert r.status == "pass", r.summary
    # LibreLane twice: up to detailed routing, then the rest.
    assert ran(fake_flow) == ["openroad", "librelane", "librelane"]
    cfg = (fake_flow / "config.json").read_text()
    state = json.loads((fake_flow / "state.json").read_text())
    assert "planted" not in cfg.lower() and "planted" not in json.dumps(state).lower()
    assert json.loads(cfg)["meta"]["flow"] == [*E.PREFIX, *STEPS[2:]]
    assert state["def"].endswith("/eco/edit/eco.def")
    assert not (counter_project / "pwned").exists()


@pytest.mark.parametrize(
    "plant",
    [
        # A configuration replaced after the run was recorded.
        lambda pnr: (pnr / "config.json").write_text(
            json.dumps({"DESIGN_NAME": "counter", "PLACEMENT_SCRIPT": "dir::planted.tcl"})
        ),
        # The state before detailed routing pointed at a planted script.
        lambda pnr: (pnr / "runs/chipleap/03-openroad-detailedrouting/state_in.json").write_text(
            json.dumps({"def": str(pnr / "planted.tcl")})
        ),
        # The database the ECO edits, swapped for another (its hash no longer matches).
        lambda pnr: (pnr / "runs/chipleap/03-openroad-detailedrouting/counter.def").write_text(
            "VERSION 5.8 ;\nDESIGN planted ;\n"
        ),
        # A source LibreLane would read again.
        lambda pnr: (pnr / "src/counter.v").write_text(
            'module counter; initial $system("touch pwned"); endmodule\n'
        ),
    ],
    ids=["config", "state", "database", "source"],
)
def test_a_changed_run_is_refused_before_any_tool_runs(counter_project, ihp_pdk, fake_flow, plant):
    routed(counter_project, pdk=ihp_pdk)
    pnr = counter_project / "build/pnr"
    (pnr / "planted.tcl").write_text("exec touch pwned\n")
    plant(pnr)
    r = E.eco(load_project(counter_project), RESIZE)
    assert r.status == "fail" and "changed after ChipLeap recorded it" in r.summary, r.summary
    assert ran(fake_flow) == []


def test_a_routed_run_from_elsewhere_is_refused(counter_project, ihp_pdk, fake_flow, tmp_path):
    """A build/pnr shipped with the repository, manifest and all, was not recorded with
    this user's key."""
    routed(counter_project, pdk=ihp_pdk)
    os.environ["CHIPLEAP_BUILD_KEY"], key = (
        str(tmp_path / "theirs.key"),
        os.environ["CHIPLEAP_BUILD_KEY"],
    )
    try:
        r = E.eco(load_project(counter_project), RESIZE)
    finally:
        os.environ["CHIPLEAP_BUILD_KEY"] = key
    assert r.status == "fail" and "does not verify" in r.summary, r.summary
    assert ran(fake_flow) == []


def test_a_macro_projects_place_and_route_constraints_are_passed_on(counter_project):
    """With macros, placement and routing read chipleap.pnr.sdc, which the routed run
    records; the ECO passes it on, and signoff's chipleap.sdc."""
    from chipleap.rtl.pnr import PNR_SDC_NAME, pnr_sdc

    run = routed(counter_project)
    pnr = run.parent.parent
    (pnr / PNR_SDC_NAME).write_text(pnr_sdc())
    seal_routed(pnr, with_macros=True)
    p = load_project(counter_project)
    _, sealed, _, _ = E.base(p)
    cfg = {
        "DESIGN_NAME": "counter",
        "PNR_SDC_FILE": f"dir::{PNR_SDC_NAME}",
        "SIGNOFF_SDC_FILE": f"dir::{SDC_NAME}",
    }
    assert E._settings(p, cfg, sealed) == cfg
    (pnr / PNR_SDC_NAME).write_text("set_max_transition 9 [current_design]\n")
    with pytest.raises(E.EcoError):
        E._settings(p, cfg, sealed)


def test_the_configuration_passes_on_only_what_chipleap_writes(counter_project):
    """Even in a recorded run, a variable that names a script, hook or plugin, a
    substitution, or a file outside the sources ChipLeap copied is not passed on."""
    routed(counter_project)
    p = load_project(counter_project)
    _, sealed, _, _ = E.base(p)
    good = {
        "DESIGN_NAME": "counter",
        "VERILOG_FILES": ["dir::src/counter.v"],
        "DIODE_ON_PORTS": "in",  # every project's layout (chipleap.verify.pnr.port_diodes)
        "DIODE_CELL": "sg13g2_antennanp/A",
    }
    assert E._settings(p, good, sealed) == good
    # Every setting place and route shares passes on.
    assert E._settings(p, signoff(), sealed) == signoff()
    # KLayout's DRC threads as IHP's deck reads them: ChipLeap's, which the ECO writes again.
    threads = {"KLAYOUT_DRC_THREADS": 4, "KLAYOUT_DRC_DEFINES": {"threads": "4"}}
    assert E._settings(p, threads, sealed) == {"KLAYOUT_DRC_THREADS": 4}
    # Timing-driven placement as ChipLeap turns it on for LibreLane 3.0.14.
    placed = {"PL_TIMING_DRIVEN": True}
    assert E._settings(p, placed, sealed) == placed
    for bad, why in [
        ({"PLACEMENT_SCRIPT": "x.tcl"}, "does not pass on"),
        ({"PRE_GLOBAL_PLACEMENT_HOOK": 1}, "does not pass on"),
        ({"DESIGN_NAME": "expr::[exec touch pwned]"}, "cannot go into a tool script"),
        ({"DESIGN_NAME": "refg::$PDK_ROOT"}, "cannot go into a tool script"),
        ({"DESIGN_NAME": "pdk_dir::x"}, "substitution"),
        ({"VERILOG_FILES": ["dir::../../../etc/passwd"]}, "not a file ChipLeap wrote"),
        ({"VERILOG_FILES": ["/abs/counter.v"]}, "not a file ChipLeap wrote"),
        ({"VERILOG_FILES": ["dir::src/other.v"]}, "not a file ChipLeap recorded"),
        ({"CLOCK_PERIOD": "10"}, "not a number"),
        ({"KLAYOUT_DRC_THREADS": 4, "KLAYOUT_DRC_DEFINES": {"threads": "4", "x": "1"}}, "DEFINES"),
        ({"KLAYOUT_DRC_THREADS": 4, "KLAYOUT_DRC_DEFINES": {"threads": 4}}, "DEFINES"),
        ({"PL_TIMING_DRIVEN": "true"}, "PL_TIMING_DRIVEN"),
    ]:
        with pytest.raises(E.EcoError, match=why):
            E._settings(p, bad, sealed)


@pytest.mark.pnr
@pytest.mark.skipif(
    not (librelane().found and openroad().found), reason="needs LibreLane, OpenROAD"
)
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_PNR"), reason="set CHIPLEAP_RUN_PNR=1")
@pytest.mark.skipif(
    _librelane_before_3_0_14(),
    reason="the fixture meets timing at place and route on LibreLane 3.0.0.dev44 (+0.133 ns), "
    "so the test cannot exercise the ECO",
)
def test_a_repair_closes_timing_the_routed_run_missed(tmp_path):
    """The fixture is routed without timing repair and misses setup at the slow corner;
    its eco section's repair: setup closes it and signs off clean."""
    from chipleap.rtl.pnr import pnr

    root = tmp_path / "eco_timing"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("build"))
    p = load_project(root)
    routed_result = pnr(p)
    assert routed_result.data["setup_slack_ns"] < 0, routed_result.summary
    r = E.eco(p)
    assert r.status == "pass", r.summary
    assert r.data["after"]["setup_slack_ns"] >= 0 and r.data["after"]["lvs_errors"] == 0


def test_an_eco_is_signed_off_as_place_and_route_is(counter_project, tmp_path):
    """The ECO's result meets pnr's criteria: a slew or capacitance violation fails it, a
    driver over its fanout limit is reported."""
    from chipleap.verify import pnr as P

    p = load_project(counter_project)
    base = {"timing__setup__ws": 0.5, "timing__hold__ws": 0.1}
    before = P.parse(base, 100.0, None)
    for extra, status, words in (
        ({"design__max_fanout_violation__count": 1}, "pass", "1 driver over the fanout limit"),
        ({"design__max_cap_violation__count__corner:nom_fast_1p32V_m40C": 1}, "fail", "1 cap"),
    ):
        after = P.parse({**base, **extra}, 100.0, None)
        status_, msg = E.summarize(p, E.EcoResult([{}], before, after, tmp_path))
        assert status_ == status and words in msg, msg
