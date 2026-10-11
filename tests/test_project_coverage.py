"""Verilator coverage of RTL projects."""

import pytest

from chipleap.rtl import sim as sim_mod
from chipleap.rtl.coverage import coverage, coverage_data, parse_dat, summarize_points
from chipleap.rtl.project import load_project
from chipleap.verify.tools import verilator


def entry(file, line, kind, name, count, page=True):
    fields = [("f", file), ("l", str(line)), ("n", "3")]
    fields += [("t", kind)] if not page else []
    fields += [("page", f"v_{kind}/counter"), ("o", name), ("h", "counter")]
    return "C '" + "".join(f"\x01{k}\x02{v}" for k, v in fields) + f"' {count}"


def test_parse_and_summarize(counter_project):
    p = load_project(counter_project)
    src = str(counter_project / "rtl/counter.sv")
    text = "\n".join(
        [
            "# SystemC::Coverage-3",
            entry(src, 9, "line", "block", 8),
            entry(src, 10, "branch", "if", 2, page=False),
            entry(src, 10, "branch", "else", 0, page=False),
            entry(src, 6, "toggle", "en_i:0->1", 1),
            entry(src, 6, "toggle", "en_i:1->0", 0),
            entry("/elsewhere/tb.sv", 3, "line", "block", 0),
        ]
    )
    points = parse_dat(text)
    assert [x.kind for x in points] == ["line", "branch", "branch", "toggle", "toggle", "line"]
    assert points[1].name == "if" and points[1].count == 2 and points[1].line == 10
    s = summarize_points(points, p)
    assert s.kinds == {"line": (1, 1), "branch": (1, 2), "toggle": (1, 2)}
    assert s.percent("toggle") == 50.0 and s.percent("expr") is None
    assert list(s.files) == ["rtl/counter.sv"]
    assert [(u.line, u.kind, u.name) for u in s.uncovered] == [
        (6, "toggle", "en_i:1->0"),
        (10, "branch", "else"),
    ]
    data = coverage_data(s)
    assert data["files"] == [
        {"file": "rtl/counter.sv", "line": [1, 1], "branch": [1, 2], "toggle": [1, 2]}
    ]
    assert data["kinds"]["branch"] == {"covered": 1, "total": 2, "percent": 50.0}
    assert data["uncovered_total"] == 2


@pytest.mark.skipif(
    sim_mod.cocotb_python() is None
    or not verilator().found
    or sim_mod._version_tuple(verilator().version) < sim_mod.VERILATOR_MIN,
    reason="needs cocotb and Verilator 5.036 or newer (CHIPLEAP_VERILATOR)",
)
def test_coverage_run(counter_project):
    r = coverage(load_project(counter_project))
    assert r.status == "pass", r.summary
    assert r.summary.startswith("line ") and "toggle" in r.summary
    kinds = r.data["kinds"]
    assert kinds["line"]["covered"] == kinds["line"]["total"]  # both branches of the counter run
    assert 0 < kinds["toggle"]["covered"] < kinds["toggle"]["total"]  # the top bits never fall
    assert all(u["file"] == "rtl/counter.sv" for u in r.data["uncovered"])
    assert "newly_covered" not in r.data  # the first run has nothing to compare with
    # A test that also counts down from all ones reaches the toggles the first run missed.
    test = counter_project / "test/test_counter.py"
    test.write_text(
        test.read_text()
        + """

@cocotb.test()
async def wraps_around(dut):
    cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())
    dut.rst_ni.value = 0
    dut.en_i.value = 0
    await ClockCycles(dut.clk_i, 2)
    dut.rst_ni.value = 1
    dut.en_i.value = 1
    await ClockCycles(dut.clk_i, 70)
"""
    )
    again = coverage(load_project(counter_project))
    assert again.data["newly_covered"] > 0 and again.data["newly_uncovered"] == 0
    assert "newly reached since the last run" in again.summary
    from chipleap.rtl.coverage import gaps

    g = gaps(load_project(counter_project))
    assert g["stale"] is False and g["gaps_total"] < len(r.data["uncovered"])
    assert "wraps_around" in g["tests"]["modules"]["test_counter"]


def test_coverage_skips_old_verilator(counter_project, monkeypatch):
    from chipleap.verify.tools import Tool

    monkeypatch.setattr(sim_mod, "verilator", lambda: Tool("verilator", "/x", "5.020", ""))
    monkeypatch.setattr(sim_mod, "cocotb_version", lambda _python: "2.1.0")
    r = coverage(load_project(counter_project))
    assert r.status == "skipped" and "older than 5.036" in r.summary


def summary_file(project, uncovered, digest=None):
    """A last run's summary, as the coverage step writes it."""
    import json

    from chipleap.rtl.coverage import SUMMARY

    path = project.build_dir / "coverage" / SUMMARY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "inputs_sha256": digest or project.inputs_digest(),
                "kinds": {"line": {"covered": 3, "total": 4, "percent": 75.0}},
                "uncovered": [
                    {
                        "kind": k,
                        "file": "rtl/counter.sv",
                        "line": ln,
                        "name": n,
                        "hier": "c",
                        "count": 0,
                    }
                    for k, ln, n in uncovered
                ],
            }
        )
    )


def test_the_delta_against_the_last_run():
    from chipleap.rtl.coverage import delta

    before = [{"kind": "line", "file": "a.sv", "line": 3, "name": "block", "hier": "t"}]
    before += [{"kind": "branch", "file": "a.sv", "line": 9, "name": "else", "hier": "t"}]
    now = [before[1], {"kind": "toggle", "file": "a.sv", "line": 2, "name": "x:0->1", "hier": "t"}]
    assert delta(before, now) == {"newly_covered": 1, "newly_uncovered": 1}
    assert delta(None, now) is None


def test_gaps_show_the_source_around_each_point(counter_project):
    from chipleap.rtl.coverage import gaps

    p = load_project(counter_project)
    with pytest.raises(ValueError, match="run coverage first"):
        gaps(p)
    summary_file(
        p, [("branch", 10, "else"), ("toggle", 6, "en_i:1->0"), ("toggle", 6, "en_i:0->1")]
    )
    g = gaps(p, context=1)
    assert g["stale"] is False and g["gaps_total"] == 2
    first = g["gaps"][0]
    assert (first["file"], first["line"]) == ("rtl/counter.sv", 6)
    assert [x["name"] for x in first["points"]] == ["en_i:0->1", "en_i:1->0"]
    lines = (counter_project / "rtl/counter.sv").read_text().splitlines()
    assert first["source"] == {"start": 5, "lines": lines[4:7]}
    assert g["tests"] == {
        "dir": "test",
        "modules": {"test_counter": ["counts_when_enabled", "holds_when_disabled"]},
    }
    assert [x["line"] for x in gaps(p, kinds="branch")["gaps"]] == [10]
    assert gaps(p, file="rtl/other.sv")["gaps"] == []
    assert len(gaps(p, limit=1)["gaps"]) == 1
    # After the RTL changes, the last run no longer describes it.
    (counter_project / "rtl/counter.sv").write_text("\n".join(lines) + "\n// edited\n")
    assert gaps(load_project(counter_project))["stale"] is True


def test_gaps_never_read_outside_the_project(counter_project, tmp_path):
    from chipleap.rtl.coverage import gaps

    p = load_project(counter_project)
    (tmp_path / "secret.sv").write_text("TOP SECRET\n")
    summary_file(p, [])
    path = p.build_dir / "coverage/summary.json"
    import json

    data = json.loads(path.read_text())
    data["uncovered"] = [
        {
            "kind": "line",
            "file": "../secret.sv",
            "line": 1,
            "name": "block",
            "hier": "t",
            "count": 0,
        }
    ]
    path.write_text(json.dumps(data))
    g = gaps(p)
    assert "TOP SECRET" not in json.dumps(g)
    assert g["gaps"][0]["source"] is None
