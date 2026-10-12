"""The implementation sections of a project file: dft, power, ir_drop, upf and eco."""

import re

import pytest

from chipleap.rtl.project import (
    RESERVED,
    DftConfig,
    EcoChange,
    IrDropConfig,
    PowerConfig,
    PowerDomain,
    ProjectError,
    load_project,
)


def with_sections(project_dir, text):
    proj = project_dir / "project.yaml"
    proj.write_text(proj.read_text() + text)
    return load_project(project_dir)


def test_defaults_without_sections(counter_project):
    p = load_project(counter_project)
    assert p.dft is None and p.upf is None and p.eco == ()
    assert p.power == PowerConfig() and p.ir_drop == IrDropConfig()
    assert p.power.corners == ("typ",) and p.power.netlist == "auto"
    for key in ("dft", "power", "ir_drop", "upf", "eco", "macros"):
        assert key not in RESERVED and key not in p.to_dict()


def test_dft_section(counter_project):
    p = with_sections(counter_project, "dft:\n")
    assert p.dft == DftConfig()
    p = with_sections(
        counter_project,
        "dft: {max_chain_length: 20, max_chains: 3, clock_mixing: clock_mix, insert: false, "
        "min_coverage: 90, patterns: 4, atpg: false}\n",
    )
    assert p.dft == DftConfig(20, 3, "clock_mix", False, False, 90.0, 4)
    assert p.to_dict()["dft"]["max_chain_length"] == 20


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("dft: {max_chain_length: 0}", "dft.max_chain_length must be a positive integer"),
        ("dft: {max_chains: -1}", "dft.max_chains must be a non-negative integer"),
        ("dft: {clock_mixing: mix}", "dft.clock_mixing must be one of no_mix, clock_mix"),
        ("dft: {min_coverage: 101}", "dft.min_coverage must be at most 100"),
        ("dft: {insert: yes please}", "dft.insert must be true or false"),
        ("dft: {chains: 2}", "dft: unknown keys chains"),
        ("dft: [1]", "dft must be a mapping"),
    ],
)
def test_dft_errors(counter_project, text, message):
    with pytest.raises(ProjectError, match=re.escape(message)):
        with_sections(counter_project, text + "\n")


def test_power_and_ir_drop(counter_project):
    p = with_sections(
        counter_project,
        "power: {corners: [typ, slow, fast], netlist: routed, scope: tb/u_dut, "
        "input_activity: 0.2}\nir_drop: {corner: slow, max_drop_pct: 5}\n",
    )
    assert p.power == PowerConfig(("typ", "slow", "fast"), "routed", "tb/u_dut", 0.2)
    assert p.ir_drop == IrDropConfig("slow", 5.0)
    assert p.to_dict()["power"]["corners"] == ["typ", "slow", "fast"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("power: {corners: [nominal]}", "power.corners: nominal is not one of typ, slow, fast"),
        ("power: {corners: []}", "power.corners needs at least one corner"),
        ("power: {netlist: post}", "power.netlist must be one of auto, routed, synth"),
        ("power: {scope: 'tb/$x'}", "power.scope must be instance names separated by /"),
        ("power: {input_activity: 3}", "power.input_activity must be at most 2"),
        ("ir_drop: {corner: worst}", "ir_drop.corner must be one of typ, slow, fast"),
        ("ir_drop: {max_drop_pct: 0}", "ir_drop.max_drop_pct must be a positive number"),
    ],
)
def test_power_errors(counter_project, text, message):
    with pytest.raises(ProjectError, match=re.escape(message)):
        with_sections(counter_project, text + "\n")


def test_upf_section(counter_project):
    p = with_sections(
        counter_project,
        "upf:\n  domains:\n    - {name: PD_TOP}\n"
        "    - {name: PD_A, elements: [u_a, u_b/u_c], area_um: [5, 5, 60.5, 100], voltage: 1.2}\n",
    )
    assert p.upf is not None
    top, a = p.upf.domains
    assert top == PowerDomain("PD_TOP") and p.upf.top == top
    assert a == PowerDomain("PD_A", ("u_a", "u_b/u_c"), (5.0, 5.0, 60.5, 100.0), 1.2)
    assert p.to_dict()["upf"]["domains"][1]["elements"] == ["u_a", "u_b/u_c"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("upf: {domains: []}", "upf.domains needs at least one domain"),
        ("upf: {domains: [{name: A, elements: [u]}]}", "upf.domains: exactly one domain"),
        ("upf: {domains: [{name: A}, {name: B}]}", "upf.domains: exactly one domain"),
        ("upf: {domains: [{name: A}, {name: A, elements: [u]}]}", "upf.domains: A is listed twice"),
        (
            "upf: {domains: [{name: A}, {name: B, elements: [u]}, {name: C, elements: [u]}]}",
            "upf.domains: u is in two domains",
        ),
        ("upf: {domains: [{name: A}, {name: B, elements: ['u;x']}]}", "upf.domains[1].elements"),
        ("upf: {domains: [{name: 'A B'}]}", "upf.domains[0].name must be a Verilog identifier"),
        (
            "upf: {domains: [{name: A, area_um: [0, 0, 5, 5]}]}",
            "upf.domains[0]: the top domain covers the core",
        ),
        (
            "upf: {domains: [{name: A}, {name: B, elements: [u], area_um: [5, 5, 4, 9]}]}",
            "upf.domains[1].area_um must be [x0, y0, x1, y1]",
        ),
        (
            "upf: {domains: [{name: A}, {name: B, elements: [u], voltage: 9}]}",
            "upf.domains[1].voltage must be at most 5",
        ),
    ],
)
def test_upf_errors(counter_project, text, message):
    with pytest.raises(ProjectError, match=re.escape(message)):
        with_sections(counter_project, text + "\n")


def test_eco_section(counter_project):
    p = with_sections(
        counter_project,
        "eco:\n"
        "  - {resize: _174_, cell: sg13g2_a21oi_2}\n"
        "  - {buffer: '\\u_rx.data_q[3]/D', cell: sg13g2_buf_2}\n"
        "  - {repair: setup, margin_ns: 0.5}\n"
        "  - {repair: hold}\n",
    )
    assert p.eco == (
        EcoChange("resize", "_174_", "sg13g2_a21oi_2"),
        EcoChange("buffer", "\\u_rx.data_q[3]/D", "sg13g2_buf_2"),
        EcoChange("repair", "setup", "", 0.5),
        EcoChange("repair", "hold"),
    )
    assert p.eco[1].instance == "\\u_rx.data_q[3]" and p.eco[1].pin == "D"
    assert p.to_dict()["eco"][0] == {"resize": "_174_", "cell": "sg13g2_a21oi_2"}
    assert p.to_dict()["eco"][2] == {"repair": "setup", "margin_ns": 0.5}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("eco: {resize: x}", "eco must be a list of changes"),
        ("eco: [{resize: x}]", "eco[0] needs a cell"),
        ("eco: [{resize: x, buffer: y/A, cell: c}]", "eco[0] is one of resize, buffer or repair"),
        ("eco: [{resize: 'a b', cell: c}]", "eco[0].resize: 'a b' is not a netlist name"),
        ("eco: [{resize: 'a;b', cell: c}]", "eco[0].resize: 'a;b' is not a netlist name"),
        ("eco: [{resize: x, cell: 'c$'}]", "eco[0].cell must be a Verilog identifier"),
        ("eco: [{buffer: x, cell: c}]", "eco[0].buffer must be INSTANCE/PIN"),
        ("eco: [{repair: area}]", "eco[0].repair must be one of setup, hold"),
        ("eco: [{repair: setup, margin_ns: -1}]", "eco[0].margin_ns must be"),
        ("eco: [{repair: setup, cell: c}]", "eco[0]: unknown keys cell"),
    ],
)
def test_eco_errors(counter_project, text, message):
    with pytest.raises(ProjectError, match=re.escape(message)):
        with_sections(counter_project, text + "\n")


def test_eco_change_from_text():
    assert EcoChange.parse("resize:_1_=sg13g2_nand2_2") == EcoChange(
        "resize", "_1_", "sg13g2_nand2_2"
    )
    assert EcoChange.parse("buffer:_1_/A=sg13g2_buf_1") == EcoChange(
        "buffer", "_1_/A", "sg13g2_buf_1"
    )
    assert EcoChange.parse("repair:hold") == EcoChange("repair", "hold")
    assert EcoChange.parse("repair:setup@0.3") == EcoChange("repair", "setup", "", 0.3)
    with pytest.raises(ProjectError):
        EcoChange.parse("swap:_1_")
