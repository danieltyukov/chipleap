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


def test_coverage_skips_old_verilator(counter_project, monkeypatch):
    from chipleap.verify.tools import Tool

    monkeypatch.setattr(sim_mod, "verilator", lambda: Tool("verilator", "/x", "5.020", ""))
    monkeypatch.setattr(sim_mod, "cocotb_version", lambda _python: "2.1.0")
    r = coverage(load_project(counter_project))
    assert r.status == "skipped" and "older than 5.036" in r.summary
