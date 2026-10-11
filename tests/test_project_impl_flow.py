"""The implementation steps in chipleap verify, the reports, the MCP tools and chipleap eco,
without the tools: steps skip, reports render, arguments are checked."""

import asyncio
import json

import pytest

from chipleap import mcp_server
from chipleap.cli import main
from chipleap.rtl.project import load_project
from chipleap.rtl.report import project_html, project_markdown
from chipleap.rtl.verify import PROJECT_STEPS, steps_from
from chipleap.verify.result import StepResult


def test_step_order():
    assert steps_from("all") == PROJECT_STEPS
    assert PROJECT_STEPS.index("pnr") < PROJECT_STEPS.index("eco") < PROJECT_STEPS.index("power")
    assert steps_from("dft,upf") == ("dft", "upf")


def test_steps_without_sections_skip(counter_project, monkeypatch):
    from chipleap.rtl import verify

    p = load_project(counter_project)
    for step in ("eco", "dft", "upf", "ir_drop"):
        r = verify.run_step(p, step)
        assert r.status == "skipped", (step, r.summary)


DFT = StepResult(
    "dft",
    "pass",
    "3 scan chains",
    {
        "chains": [
            {
                "name": "chain_0",
                "length": 17,
                "scan_in": "scan_in_0",
                "scan_out": "scan_out_0",
                "clock": "clk_i",
            }
        ],
        "scanned": 17,
        "flops": 17,
        "atpg": {"coverage_pct": 99.2, "patterns": 39},
        "simulation": {"flush": "pass", "patterns": 8, "errors": 0, "mismatches": []},
    },
)
POWER = StepResult(
    "power",
    "pass",
    "power",
    {
        "corners": [
            {
                "corner": "typ",
                "vectorless_mw": 0.673,
                "vcd_mw": 0.437,
                "groups": {"Sequential": {"vectorless_mw": 0.34, "vcd_mw": 0.3}},
            }
        ],
        "librelane_mw": 0.727,
    },
)
UPF = StepResult(
    "upf",
    "fail",
    "upf",
    {
        "domains": [
            {
                "name": "PD_TOP",
                "elements": ["."],
                "cells": 3,
                "voltage": None,
                "area_um": None,
                "placed_inside": None,
            },
            {
                "name": "PD_RX",
                "elements": ["u_rx"],
                "cells": 2,
                "voltage": 1.2,
                "area_um": [5, 8, 60, 100],
                "placed_inside": 2,
            },
        ],
        "crossings": [
            {
                "from": "PD_RX",
                "to": "PD_TOP",
                "nets": 1,
                "examples": ["<b>x</b>"],
                "needs_level_shifters": True,
            }
        ],
    },
)


def test_reports_show_the_new_steps(counter_project, tmp_path):
    p = load_project(counter_project)
    svg = p.build_dir / "ir_drop" / "VPWR.svg"
    svg.parent.mkdir(parents=True)
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    ir = StepResult(
        "ir_drop",
        "pass",
        "ir",
        {
            "nets": [
                {
                    "net": "VPWR",
                    "worst_mv": 0.63,
                    "average_mv": 0.27,
                    "worst_pct": 0.05,
                    "worst_instances": [{"instance": "_143_", "drop_mv": 0.63}],
                    "map": "build/ir_drop/VPWR.svg",
                }
            ]
        },
    )
    results = [DFT, POWER, ir, UPF]
    md = project_markdown(p, results)
    assert "## Design for test" in md and "| chain_0 | 17 |" in md
    assert "| typ | Total | 0.673 | 0.437 |" in md and "Map of VPWR" in md
    html = project_html(p, results)
    assert "Power intent (UPF)" in html and "&lt;b&gt;x&lt;/b&gt;" in html
    # The map goes in as an image: a script inside it cannot run in the report.
    assert "<script>" not in html and 'src="data:image/svg+xml;base64,' in html


def test_mcp_tools_check_their_arguments(counter_project):
    with pytest.raises(mcp_server.ToolError, match="corners are typ, slow, fast"):
        mcp_server.power_rtl(str(counter_project), corners="typ,worst")
    with pytest.raises(mcp_server.ToolError, match="give resize:INST=CELL"):
        mcp_server.eco_rtl(str(counter_project), ["swap:_1_=x"])
    with pytest.raises(mcp_server.ToolError, match="not a netlist name"):
        mcp_server.eco_rtl(str(counter_project), ["resize:a;b=sg13g2_inv_2"])
    with pytest.raises(mcp_server.ToolError, match="no routed design"):
        mcp_server.timing_paths_rtl(str(counter_project))
    out = mcp_server.eco_rtl(str(counter_project), ["repair:setup"])
    assert out["status"] == "skipped" and "eco section" in out["keep"]
    assert {"dft_rtl", "upf_rtl"} <= mcp_server.PROJECT_TOOLS
    assert {"power_rtl", "ir_drop_rtl", "timing_paths_rtl", "eco_rtl"} <= mcp_server.SIDE_EFFECTS


def test_mcp_lists_the_tools():
    try:
        from mcp import Client
    except ImportError:
        pytest.skip("in-process client needs mcp 2.x")
    server = mcp_server.build_server()

    async def go():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            ref = (await c.read_resource("chipleap://project-reference")).contents[0].text
            return names, prompts, ref

    names, prompts, ref = asyncio.run(go())
    assert {
        "dft_rtl",
        "power_rtl",
        "ir_drop_rtl",
        "upf_rtl",
        "timing_paths_rtl",
        "eco_rtl",
    } <= names
    assert "fix_timing" in prompts and "upf:" in ref and "eco:" in ref


def test_cli_eco(counter_project, capsys):
    assert main(["eco", str(counter_project)]) == 0
    assert "no eco changes" in capsys.readouterr().out
    assert main(["eco", str(counter_project), "--resize", "u1"]) == 2  # no =CELL
    assert main(["eco", str(counter_project), "--paths"]) == 2
    assert "no routed design" in capsys.readouterr().err


def test_verify_json_has_the_steps(counter_project):
    from chipleap.rtl.verify import write_reports

    out = write_reports(load_project(counter_project), [DFT, POWER])
    data = json.loads((out / "verify.json").read_text())
    assert [s["step"] for s in data["steps"]] == ["dft", "power"]
