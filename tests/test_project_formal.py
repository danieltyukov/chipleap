"""Formal proofs of a project's properties with SymbiYosys."""

import dataclasses
from pathlib import Path

import pytest

from chipleap.rtl import formal
from chipleap.rtl.project import FormalCheck, load_project
from chipleap.verify.tools import sby, z3

FIXTURE = Path(__file__).parent / "fixtures/formal/fifo.sv"
PROJECT = """\
top: fifo
sources: [rtl/fifo.sv]
parameters: {AW: 2}
clocks: [{port: clk_i, period_ns: 10}]
resets: [{port: rst_ni}]
"""
needs_sby = pytest.mark.skipif(
    not sby().found or not z3().found, reason="needs SymbiYosys (CHIPLEAP_SBY) and z3"
)


def fifo_project(root: Path, section: str = "formal: {}\n", *, broken: bool = False) -> Path:
    text = FIXTURE.read_text()
    if broken:  # pops even when empty, so the read pointer runs ahead of the data
        text = text.replace("if (do_pop) rptr", "if (pop_i) rptr")
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/fifo.sv").write_text(text)
    (root / "project.yaml").write_text(PROJECT + section)
    return root


def test_the_sby_file_comes_from_checked_values(tmp_path):
    p = load_project(fifo_project(tmp_path / "a b"))
    check = FormalCheck("deep", "bmc", 40, "", "smtbmc z3")
    text = formal.sby_text(p, check, [*p.sources])
    src = str(p.sources[0])
    assert "[options]\nmode bmc\ndepth 40\n" in text
    assert "[engines]\nsmtbmc z3\n" in text
    assert f'read -formal "{src}"' in text  # a project path with a space stays one word
    assert "chparam -set AW 2 fifo" in text and text.rstrip().endswith("prep -top fifo")
    evil = dataclasses.replace(p, top="fifo; exec id")
    with pytest.raises(ValueError):
        formal.sby_text(evil, check, [*p.sources])


SBY_FAIL = """\
SBY 19:47:43 [prove] engine_0.basecase: ##   0:00:00  Status: failed
SBY 19:47:43 [prove] summary: engine_0 (smtbmc z3) returned FAIL for basecase
SBY 19:47:43 [prove] summary: counterexample trace [basecase]: prove/engine_0/trace.vcd
SBY 19:47:43 [prove] summary:   failed assertion fifo.pointers_agree at {src}:40.5-40.63 in step 2
SBY 19:47:43 [prove] summary: counterexample trace [induction]: prove/engine_0/trace_induct.vcd
SBY 19:47:43 [prove] summary:   failed assertion fifo.never_over at {src}:39.5-39.39 in step 0
SBY 19:47:43 [prove] DONE (FAIL, rc=2)
"""
SBY_COVER = """\
SBY 19:49:29 [cov] engine_0: ##   0:00:00  Reached cover statement at {src}:52.14-52.29 (fills_up) in step 5.
SBY 19:49:29 [cov] engine_0: ##   0:00:00  Unreached cover statement at {src}:53.14-53.41 ($cover$x.sv:53$73).
SBY 19:49:29 [cov] summary: cover trace: cov/engine_0/trace0.vcd
SBY 19:49:29 [cov] summary:   reached cover statement fifo.fills_up at {src}:52.14-52.29 in step 5
SBY 19:49:29 [cov] DONE (FAIL, rc=2)
"""
# SymbiYosys 0.33 with labelled covers: the unreached ones are listed in the summary.
SBY_COVER_033 = """\
SBY 20:04:59 [cov] engine_0: ##   0:00:00  Unreached cover statement at never.
SBY 20:04:59 [cov] summary:   reached cover statement fifo.fills_up at {src}:49.15-49.39 in step 5
SBY 20:04:59 [cov] summary: unreached cover statements:
SBY 20:04:59 [cov] summary:   fifo.never at {src}:50.15-50.43
SBY 20:04:59 [cov] DONE (FAIL, rc=2)
"""


def test_the_log_gives_status_failures_and_covers(tmp_path):
    p = load_project(fifo_project(tmp_path))
    src = str(p.sources[0])
    r = formal.parse_log(SBY_FAIL.format(src=src), p, tmp_path / "build/formal", "prove")
    assert r["status"] == "fail"
    (f,) = r["failures"]
    assert (f["property"], f["file"], f["line"], f["step"]) == (
        "fifo.pointers_agree",
        "rtl/fifo.sv",
        40,
        2,
    )
    assert f["trace"].endswith("build/formal/prove/engine_0/trace.vcd")
    # A failure only the induction step finds is not a counterexample from reset.
    assert r["induction"][0]["property"] == "fifo.never_over"
    c = formal.parse_log(SBY_COVER.format(src=src), p, tmp_path / "build/formal", "cover")
    assert c["status"] == "fail"
    assert c["covers"] == [
        {
            "property": "fifo.fills_up",
            "file": "rtl/fifo.sv",
            "line": 52,
            "reached": True,
            "step": 5,
            "trace": c["covers"][0]["trace"],
        },
        {"property": "", "file": "rtl/fifo.sv", "line": 53, "reached": False},
    ]
    c = formal.parse_log(SBY_COVER_033.format(src=src), p, tmp_path, "cover")
    assert [(x["property"], x["line"], x["reached"]) for x in c["covers"]] == [
        ("fifo.fills_up", 49, True),
        ("fifo.never", 50, False),
    ]
    assert formal.parse_log("DONE (PASS, rc=0)\n", p, tmp_path, "prove")["status"] == "pass"
    assert formal.parse_log("DONE (UNKNOWN, rc=4)\n", p, tmp_path, "prove")["status"] == "unknown"
    err = formal.parse_log(
        f"base: {src}:3: ERROR: syntax error\nDONE (ERROR, rc=16)\n", p, tmp_path, "x"
    )
    assert err["status"] == "error" and "syntax error" in err["error"]


def test_without_a_section_or_a_tool_the_step_skips(tmp_path, monkeypatch):
    p = load_project(fifo_project(tmp_path, ""))
    assert formal.formal(p).status == "skipped"
    p = load_project(fifo_project(tmp_path / "b"))
    monkeypatch.setattr(formal, "sby", lambda: dataclasses.replace(sby(), path=None))
    r = formal.formal(p)
    assert r.status == "skipped" and "install_formal_tools.sh" in r.summary


@needs_sby
def test_a_correct_fifo_proves(tmp_path):
    r = formal.formal(load_project(fifo_project(tmp_path)))
    assert r.status == "pass", r.summary
    (check,) = r.data["checks"]
    assert check["name"] == "prove" and check["status"] == "pass" and check["failures"] == []


@needs_sby
def test_a_broken_fifo_has_a_counterexample(tmp_path):
    p = load_project(fifo_project(tmp_path, broken=True))
    r = formal.formal(p)
    assert r.status == "fail"
    f = r.data["checks"][0]["failures"][0]
    assert f["file"] == "rtl/fifo.sv" and f["property"].startswith("fifo.")
    assert (tmp_path / f["trace"]).is_file()
    # The trace is data: the pointers and the count, one value per step to the failure.
    values = f["values"]["signals"]
    assert {"rptr", "wptr", "count"} <= set(values)
    # At the failing step the values break the property: the pointers disagree with the count.
    n = f["step"]
    assert len(values["count"]) > n
    assert values["wptr"][n] != (values["rptr"][n] + values["count"][n]) % 4
    assert "fails in step" in r.summary and "rtl/fifo.sv" in r.summary


@needs_sby
def test_cover_says_what_is_reached_and_what_is_not(tmp_path):
    root = fifo_project(tmp_path, "formal: {checks: [{name: cov, mode: cover, depth: 12}]}\n")
    src = root / "rtl/fifo.sv"
    src.write_text(
        src.read_text().replace("`endif", "  always_comb never: cover (count > DEPTH);\n`endif")
    )
    r = formal.formal(load_project(root))
    covers = r.data["checks"][0]["covers"]
    assert [c["reached"] for c in covers] == [True, False]
    assert covers[0]["property"] == "fifo.fills_up" and covers[0]["step"] > 0
    assert r.status == "fail" and "1 of 2 covers reached" in r.summary


@needs_sby
def test_a_project_sby_file_runs_as_it_is(tmp_path):
    root = fifo_project(tmp_path, "formal: {sby: [formal/quick.sby]}\n")
    (root / "formal").mkdir()
    (root / "formal/quick.sby").write_text(
        "[options]\nmode bmc\ndepth 6\n\n[engines]\nsmtbmc z3\n\n"
        "[script]\nread -formal fifo.sv\nprep -top fifo\n\n[files]\n../rtl/fifo.sv\n"
    )
    r = formal.formal(load_project(root))
    names = [c["name"] for c in r.data["checks"]]
    assert names == ["prove", "sby:quick"], r.summary
    assert all(c["status"] == "pass" for c in r.data["checks"]), r.data
