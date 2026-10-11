"""The verification sections of a project file: formal, equivalence, cdc and gate_sim."""

from pathlib import Path

import pytest
import yaml

from chipleap.rtl.project import (
    CdcWaiver,
    FormalCheck,
    ProjectError,
    load_project,
    parse_project,
)

BASE = {
    "top": "counter",
    "sources": ["rtl/counter.sv"],
    "clocks": [{"port": "clk_i", "period_ns": 10}, {"port": "clk_b", "period_ns": 7}],
    "resets": [{"port": "rst_ni"}],
}


def parse(root: Path, **sections):
    return parse_project({**BASE, **sections}, root)


def refused(root: Path, message: str, **sections) -> None:
    with pytest.raises(ProjectError) as exc:
        parse(root, **sections)
    assert message in str(exc.value), str(exc.value)


def test_no_sections_means_defaults(counter_project):
    p = parse(counter_project)
    assert p.formal is None
    assert p.equivalence.checks == () and p.cdc.async_inputs == ()
    assert p.gate_sim.netlist == "routed" and p.gate_sim.delays == "typ"
    for key in ("formal", "equivalence", "cdc", "gate_sim"):
        assert key not in p.to_dict()


def test_sections_parse_and_round_trip(counter_project):
    (counter_project / "formal").mkdir()
    (counter_project / "formal/check.sv").write_text("module check; endmodule\n")
    (counter_project / "formal/extra.sby").write_text("[options]\nmode bmc\n")
    p = parse(
        counter_project,
        formal={
            "sources": ["formal/*.sv"],
            "checks": [
                {"name": "prove", "depth": 12},
                {"name": "cov", "mode": "cover", "depth": 30, "top": "check"},
                {"name": "pdr", "engine": "abc pdr"},
            ],
            "sby": ["formal/extra.sby"],
        },
        equivalence={"checks": ["rtl-synth", "synth-pnr"]},
        cdc={
            "async_inputs": ["en_i"],
            "input_domains": {"data_i": "clk_b"},
            "reset_synchronized": ["rst_ni"],
            "waive": [{"from": "dbg_*", "to": "*", "reason": "set before the clock starts"}],
        },
        gate_sim={"netlist": "synth", "corner": "nom_slow_1p08V_125C", "delays": "max"},
    )
    assert p.formal is not None
    assert [s.name for s in p.formal.sources] == ["check.sv"]
    assert p.formal.checks == (
        FormalCheck("prove", "prove", 12),
        FormalCheck("cov", "cover", 30, "check"),
        FormalCheck("pdr", "prove", 20, "", "abc pdr"),
    )
    assert [s.name for s in p.formal.sby] == ["extra.sby"]
    assert p.equivalence.checks == ("rtl-synth", "synth-pnr")
    assert p.cdc.async_inputs == ("en_i",) and p.cdc.input_domains == {"data_i": "clk_b"}
    assert p.cdc.waive == (CdcWaiver("dbg_*", "*", "set before the clock starts"),)
    assert p.gate_sim.corner == "nom_slow_1p08V_125C"
    names = {p.rel(f) for f in p.input_files()}
    assert {"formal/check.sv", "formal/extra.sby"} <= names
    assert parse_project(yaml.safe_load(yaml.safe_dump(p.to_dict())), counter_project) == p


def test_an_empty_formal_section_proves_by_default(counter_project):
    p = parse(counter_project, formal={})
    assert p.formal is not None and p.formal.checks == (FormalCheck("prove"),)


@pytest.mark.parametrize(
    ("sections", "message"),
    [
        ({"formal": {"checks": [{"name": "a b"}]}}, "formal.checks[0].name must be"),
        ({"formal": {"checks": [{"name": "p"}, {"name": "p"}]}}, "formal.checks: p is listed"),
        ({"formal": {"checks": [{"name": "p", "mode": "live"}]}}, "formal.checks[0].mode"),
        ({"formal": {"checks": [{"name": "p", "depth": 0}]}}, "formal.checks[0].depth"),
        ({"formal": {"checks": [{"name": "p", "depth": 5000}]}}, "at most 1000"),
        ({"formal": {"checks": [{"name": "p", "depth": True}]}}, "formal.checks[0].depth"),
        ({"formal": {"checks": [{"name": "p", "engine": "smtbmc boolector"}]}}, "engine must"),
        ({"formal": {"checks": [{"name": "p", "engine": "smtbmc z3; rm"}]}}, "engine must"),
        (
            {"formal": {"checks": [{"name": "p", "mode": "cover", "engine": "abc pdr"}]}},
            "abc pdr only proves",
        ),
        ({"formal": {"checks": [{"name": "p", "top": "x;y"}]}}, "formal.checks[0].top"),
        ({"formal": {"checks": [{"name": "p", "solver": "z3"}]}}, "unknown keys solver"),
        ({"formal": {"sources": ["rtl/missing.sv"]}}, "formal.sources"),
        ({"formal": {"sby": ["rtl/counter.sv"]}}, "formal.sby: rtl/counter.sv is not a .sby"),
        ({"formal": {"bogus": 1}}, "formal: unknown keys bogus"),
        ({"equivalence": {"checks": ["rtl-gds"]}}, "equivalence.checks: 'rtl-gds'"),
        ({"cdc": {"async_inputs": ["clk_i"]}}, "cdc.async_inputs: clk_i is a clock"),
        ({"cdc": {"async_inputs": ["a;b"]}}, "must be a Verilog identifier"),
        ({"cdc": {"input_domains": {"d": "clk_x"}}}, "cdc.input_domains.d: clk_x is not"),
        ({"cdc": {"reset_synchronized": ["rst_x"]}}, "rst_x is not one of the resets"),
        ({"cdc": {"waive": [{"from": "a", "to": "b"}]}}, "cdc.waive[0] needs a reason"),
        ({"cdc": {"waive": [{"from": "a(", "to": "b", "reason": "r"}]}}, "cdc.waive[0].from"),
        ({"gate_sim": {"netlist": "gds"}}, "gate_sim.netlist must be"),
        ({"gate_sim": {"corner": "../x"}}, "gate_sim.corner must be"),
        ({"gate_sim": {"delays": "avg"}}, "gate_sim.delays must be"),
    ],
)
def test_errors_name_the_key(counter_project, sections, message):
    refused(counter_project, message, **sections)


def test_formal_files_are_confined_and_scanned(counter_project, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "p.sv").write_text("module p; endmodule\n")
    (outside / "x.sby").write_text("[options]\n")
    refused(
        counter_project, "outside the project directory", formal={"sources": [f"{outside}/p.sv"]}
    )
    refused(counter_project, "outside the project directory", formal={"sby": [f"{outside}/x.sby"]})
    (counter_project / "formal").mkdir()
    (counter_project / "formal/p.sv").write_text(f'`include "{outside}/p.sv"\n')
    refused(
        counter_project,
        "absolute paths and .. are not allowed",
        formal={"sources": ["formal/p.sv"]},
    )


def test_sections_load_from_a_file(counter_project):
    text = (counter_project / "project.yaml").read_text()
    (counter_project / "project.yaml").write_text(
        text + "formal: {checks: [{name: p, depth: 4}]}\n"
    )
    assert load_project(counter_project).formal.checks[0].depth == 4  # type: ignore[union-attr]
