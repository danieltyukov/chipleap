"""The formal tools' self-test in ``chipleap doctor``, and the formal step refusing an
answer SymbiYosys's own log contradicts.

A SymbiYosys built from a newer release than the Yosys it drives runs without complaint
and answers wrong: with Yosys 0.33 it reports a cover its engine reached as unreached (or
leaves it out) and crashes in ``abc pdr``. The logs below are what such a build printed."""

import os
from pathlib import Path

import pytest

from chipleap.cli import main
from chipleap.rtl import formal, selftest
from chipleap.rtl.project import load_project
from chipleap.verify.tools import eqy, sby

# A matched SymbiYosys (built from the yosys-0.33 tag): the engine reaches the cover and
# the summary says so.
MATCHED_COVER = """\
SBY  4:27:31 [cov] engine_0: ##   0:00:00  Reached cover statement at reach in step 3.
SBY  4:27:31 [cov] summary: engine_0 (smtbmc z3) returned pass
SBY  4:27:31 [cov] summary: cover trace: cov/engine_0/trace0.vcd
SBY  4:27:31 [cov] summary:   reached cover statement chipleap_selftest.reach at st.sv:5.15-5.43 in step 3
SBY  4:27:31 [cov] DONE (PASS, rc=0)
"""
# A newer SymbiYosys on Yosys 0.33, with only a reachable cover: the summary leaves it out,
# so the check would pass with no cover at all.
MISMATCHED_COVER_LEFT_OUT = """\
SBY  4:27:32 [cov] engine_0: ##   0:00:00  Reached cover statement at reach in step 3.
SBY  4:27:32 [cov] summary: engine_0 (smtbmc z3) returned pass
SBY  4:27:32 [cov] summary: trace: cov/engine_0/trace0.vcd
SBY  4:27:32 [cov] DONE (PASS, rc=0)
"""
# The same with an unreachable cover beside it: both come out unreached.
MISMATCHED_COVER_UNREACHED = """\
SBY  4:25:47 [cov] engine_0: ##   0:00:00  Reached cover statement at fills_up in step 5.
SBY  4:25:47 [cov] engine_0: ##   0:00:00  Unreached cover statement at never.
SBY  4:25:47 [cov] engine_0: ##   0:00:00  Status: failed
SBY  4:25:47 [cov] summary: engine_0 (smtbmc z3) returned FAIL
SBY  4:25:47 [cov] summary: trace: cov/engine_0/trace0.vcd
SBY  4:25:47 [cov] summary: unreached cover statements:
SBY  4:25:47 [cov] summary:   fifo.fills_up at /p/rtl/fifo.sv:40.17-40.51
SBY  4:25:47 [cov] summary:   fifo.never at /p/rtl/fifo.sv:41.17-41.48
SBY  4:25:47 [cov] DONE (FAIL, rc=2)
"""
ASSERT_FAILED_YET_PASSED = """\
SBY  4:27:43 [bmc] engine_0: ##   0:00:00  Assert failed in chipleap_selftest: holds
SBY  4:27:43 [bmc] summary: engine_0 (smtbmc z3) returned pass
SBY  4:27:43 [bmc] DONE (PASS, rc=0)
"""
PDR_CRASH = """\
SBY  4:27:32 [pdr] engine_0: starting process "cd pdr; yosys-abc -c 'read_aiger model/design_aiger.aig; fold; strash; pdr -v -l; write_cex -a engine_0/trace.aiw'"
Traceback (most recent call last):
  File "/usr/local/bin/../share/yosys/python3/sby_engine_abc.py", line 173, in output_callback
    for path in ywa["asserts"]:
KeyError: 'asserts'
"""


def parse(counter_project: Path, text: str) -> dict:
    return formal.parse_log(text, load_project(counter_project), counter_project, "c")


def test_a_matched_log_is_read_as_it_is(counter_project):
    r = parse(counter_project, MATCHED_COVER)
    assert r["status"] == "pass"
    assert [(c["property"], c["reached"]) for c in r["covers"]] == [
        ("chipleap_selftest.reach", True)
    ]


@pytest.mark.parametrize(
    "log, why",
    [
        (MISMATCHED_COVER_LEFT_OUT, "reached 1 cover statement (reach) but its summary reports 0"),
        (MISMATCHED_COVER_UNREACHED, "reached 1 cover statement (fills_up) but its summary"),
        (ASSERT_FAILED_YET_PASSED, "saw an assertion fail but its summary says the check passed"),
    ],
    ids=["cover-left-out", "cover-unreached", "assert-passed"],
)
def test_a_summary_its_engine_contradicts_is_an_error(counter_project, log, why):
    """Neither a pass nor a fail: the check errs, saying which SymbiYosys to use."""
    r = parse(counter_project, log)
    assert r["status"] == "error" and why in r["error"], r
    assert "CHIPLEAP_SBY" in r["error"] and "install_formal_tools.sh" in r["error"]
    assert r["covers"] == []  # the summary's covers are not to be trusted


def executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def fake_sby(tmp_path: Path, cover_log: str, pdr_log: str) -> Path:
    """A stand-in SymbiYosys that answers with a recorded log: the .sby file's engine
    decides which."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "cover.log").write_text(cover_log)
    (logs / "pdr.log").write_text(pdr_log)
    return executable(
        tmp_path / "bin/sby",
        'for a in "$@"; do last=$a; done\n'
        'if grep -q "abc pdr" "$last"; then cat "' + str(logs / "pdr.log") + '"; exit 1; fi\n'
        'cat "' + str(logs / "cover.log") + '"\n',
    )


PDR_PASS = "SBY  4:27:31 [pdr] summary: engine_0 (abc pdr) returned PASS\nSBY  4:27:31 [pdr] DONE (PASS, rc=0)\n"


def test_the_selftest_catches_a_mismatched_sby(tmp_path, monkeypatch, capsys):
    bad = fake_sby(tmp_path, MISMATCHED_COVER_LEFT_OUT, PDR_CRASH)
    monkeypatch.setenv("CHIPLEAP_SBY", str(bad))
    monkeypatch.setenv("CHIPLEAP_EQY", str(tmp_path / "no-eqy"))  # not there: EQY is skipped
    checks = selftest.formal_selftest(timeout=30)
    assert [(c.name, c.ok) for c in checks] == [("sby cover", False), ("sby abc pdr", False)]
    for c in checks:
        assert str(bad) in c.message and "CHIPLEAP_SBY" in c.message
    assert "KeyError" in checks[1].message
    assert main(["doctor"]) == 1
    out = capsys.readouterr()
    assert "FAIL  sby cover with " + str(bad) in out.out
    assert "would give wrong answers" in out.err


def test_the_selftest_passes_a_matched_sby_and_works_outside_any_project(
    tmp_path, monkeypatch, capsys
):
    good = fake_sby(tmp_path, MATCHED_COVER, PDR_PASS)
    monkeypatch.setenv("CHIPLEAP_SBY", str(good))
    monkeypatch.setenv("CHIPLEAP_EQY", str(tmp_path / "no-eqy"))
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    checks = selftest.formal_selftest(timeout=30)
    assert [(c.name, c.ok) for c in checks] == [("sby cover", True), ("sby abc pdr", True)]
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # its files went to a temp dir
    assert main(["doctor"]) == 0
    assert "pass  sby cover" in capsys.readouterr().out


def test_no_sby_no_selftest(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIPLEAP_SBY", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert selftest.formal_selftest() == []


# The real tools, as CI installs them (scripts/install_formal_tools.sh, CHIPLEAP_SBY and
# CHIPLEAP_EQY): every question answered right, in a few seconds.
needs_sby = pytest.mark.skipif(
    not sby().found or not os.environ.get("CHIPLEAP_SBY"),
    reason="needs SymbiYosys from scripts/install_formal_tools.sh (CHIPLEAP_SBY)",
)


@needs_sby
def test_the_installed_formal_tools_pass_the_selftest():
    checks = selftest.formal_selftest()
    expected = ["sby cover", "sby abc pdr", *(["eqy"] if eqy().found else [])]
    assert [c.name for c in checks] == expected
    assert all(c.ok for c in checks), [c.message for c in checks]
