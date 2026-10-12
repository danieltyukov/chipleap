"""The MCP tools for RTL projects, their run log and replay."""

import asyncio
import json

import pytest

from chipleap import mcp_server
from chipleap.cli import main
from chipleap.rtl.sim import cocotb_python
from chipleap.rtl.synth import liberty_for
from chipleap.verify.tools import iverilog, verilator, yosys

needs_tools = pytest.mark.skipif(
    cocotb_python() is None
    or not iverilog().found
    or not verilator().found
    or not yosys().found
    or liberty_for("ihp-sg13g2") is None,
    reason="needs cocotb, Icarus, Verilator, Yosys and the IHP liberty",
)


def _client(server):
    try:
        from mcp import Client
    except ImportError:  # mcp 1.x
        return None
    return Client(server)


@needs_tools
def test_rtl_tools_over_mcp_and_replay(counter_project, tmp_path, capsys):
    logs = tmp_path / "logs"
    server = mcp_server.build_server(log_dir=str(logs))
    client = _client(server)
    if client is None:
        pytest.skip("in-process client needs mcp 2.x")
    project = str(counter_project)

    async def go():
        async with client as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {
                "describe_project",
                "lint_rtl",
                "simulate_rtl",
                "coverage_rtl",
                "synthesize_rtl",
                "verify_rtl",
            } <= names
            out = {}
            for tool, args in (
                ("describe_project", {"project": project}),
                ("lint_rtl", {"project": project}),
                ("simulate_rtl", {"project": project, "simulator": "icarus"}),
                ("synthesize_rtl", {"project": project}),
            ):
                r = await c.call_tool(tool, args)
                out[tool] = json.loads(r.content[0].text)
            res = await c.read_resource("chipleap://project-reference")
            assert "project.yaml" in res.contents[0].text
            return out

    out = asyncio.run(go())
    assert out["describe_project"]["top"] == "counter"
    assert out["lint_rtl"]["status"] == "pass"
    sim = out["simulate_rtl"]
    assert sim["status"] == "pass" and len(sim["tests"]) == 2
    assert out["synthesize_rtl"]["flops"] == 6

    log = next(logs.glob("*.jsonl"))
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(len(e["inputs_sha256"]) == 64 and "tools" in e for e in entries)
    assert mcp_server.replay(log) == (4, 4)

    # A changed test changes the result, and the replay says the inputs changed.
    t = counter_project / "test/test_counter.py"
    t.write_text(t.read_text().replace("assert int(dut.q_o.value) == 0", "assert False, 'x'"))
    outcomes = {c["tool"]: c["outcome"] for c in mcp_server.replay_calls(log)}
    assert outcomes["simulate_rtl"] == "inputs changed"
    assert outcomes["lint_rtl"] == "same"  # tests are not linted
    assert main(["replay", str(log)]) == 1
    printed = capsys.readouterr().out
    assert "simulate_rtl" in printed and "inputs changed" in printed


def test_replay_names_changed_tools(counter_project, tmp_path):
    context = mcp_server.project_context(str(counter_project))
    entry = {
        "tool": "describe_project",
        "args": {"project": str(counter_project)},
        "result_sha256": "0" * 64,  # what an older ChipLeap or tool returned
        "error": None,
        "inputs_sha256": context["inputs_sha256"],
        "tools": {**context["tools"], "yosys": "0.1"},
    }
    log = tmp_path / "run.jsonl"
    log.write_text(json.dumps(entry) + "\n")
    assert mcp_server.replay_calls(log)[0]["outcome"] == "tools changed"
    entry["tools"] = context["tools"]
    log.write_text(json.dumps(entry) + "\n")
    assert mcp_server.replay_calls(log)[0]["outcome"] == "differs"


def test_tool_errors(counter_project):
    with pytest.raises(mcp_server.ToolError, match="No such file"):
        mcp_server.describe_project(str(counter_project / "nowhere"))
    with pytest.raises(mcp_server.ToolError, match="simulator must be one of"):
        mcp_server.simulate_rtl(str(counter_project), simulator="modelsim")
    with pytest.raises(mcp_server.ToolError, match="tool must be one of"):
        mcp_server.synthesize_rtl(str(counter_project), tool="vivado")
    with pytest.raises(mcp_server.ToolError, match="unknown step"):
        mcp_server.verify_rtl(str(counter_project), steps="xheep")
    d = mcp_server.describe_project(str(counter_project))
    assert d["golden"] == "model/counter_model.py" and d["input_files"] == 4


FIFO = __import__("pathlib").Path(__file__).parent / "fixtures/formal/fifo.sv"


def fifo_project(root):
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/fifo.sv").write_text(FIFO.read_text())
    (root / "project.yaml").write_text(
        "top: fifo\nsources: [rtl/fifo.sv]\nclocks: [{port: clk_i, period_ns: 10}]\n"
        "resets: [{port: rst_ni}]\nformal: {checks: [{name: prove, depth: 8}]}\n"
    )
    return str(root)


def test_verification_tool_errors(tmp_path):
    from chipleap.verify.tools import sby

    project = fifo_project(tmp_path / "fifo")
    if sby().found:
        with pytest.raises(mcp_server.ToolError, match="no formal check named 'deep'"):
            mcp_server.prove_rtl(project, check="deep")
    with pytest.raises(mcp_server.ToolError, match="';' is not allowed"):
        mcp_server.check_assertions(project, "fifo", [{"expr": "count; $finish"}])
    with pytest.raises(mcp_server.ToolError, match="check must be one of rtl-synth, synth-pnr"):
        mcp_server.equivalence_rtl(project, check="rtl-gds")
    with pytest.raises(mcp_server.ToolError, match="run coverage first"):
        mcp_server.coverage_gaps(project)
    assert "coverage_gaps" in mcp_server.SIDE_EFFECTS
    assert {"prove_rtl", "check_assertions", "equivalence_rtl", "cdc_rtl", "gate_sim_rtl"} <= (
        mcp_server.PROJECT_TOOLS
    )


@pytest.mark.skipif(
    not yosys().found or liberty_for("ihp-sg13g2") is None,
    reason="needs Yosys and the IHP liberty",
)
def test_verification_tools_over_mcp_and_replay(tmp_path):
    from chipleap.verify.tools import sby, z3

    if not (sby().found and z3().found):
        pytest.skip("needs SymbiYosys and z3")
    logs = tmp_path / "logs"
    server = mcp_server.build_server(log_dir=str(logs))
    client = _client(server)
    if client is None:
        pytest.skip("in-process client needs mcp 2.x")
    project = fifo_project(tmp_path / "fifo")

    async def go():
        async with client as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {
                "prove_rtl",
                "check_assertions",
                "equivalence_rtl",
                "cdc_rtl",
                "gate_sim_rtl",
                "coverage_gaps",
            } <= names
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            assert {"propose_assertions", "close_coverage", "fix_rtl"} <= prompts
            out = {}
            for tool, args in (
                ("prove_rtl", {"project": project}),
                (
                    "check_assertions",
                    {
                        "project": project,
                        "module": "fifo",
                        "assertions": [{"expr": "count != 3", "name": "never_three"}],
                        "depth": 8,
                    },
                ),
                ("cdc_rtl", {"project": project}),
                ("equivalence_rtl", {"project": project, "check": "rtl-synth"}),
            ):
                r = await c.call_tool(tool, args)
                out[tool] = json.loads(r.content[0].text)
            return out

    out = asyncio.run(go())
    assert out["prove_rtl"]["status"] == "pass"
    (proposal,) = out["check_assertions"]["results"]
    assert proposal["status"] == "fails" and proposal["values"]["signals"]["count"]
    assert out["cdc_rtl"]["status"] == "pass" and out["cdc_rtl"]["domains"] == {"clk_i": 7}
    assert out["equivalence_rtl"]["pairs"][0]["status"] in ("proved", "bounded")
    log = next(logs.glob("*.jsonl"))
    # Every call is replayed and gives the same result.
    assert mcp_server.replay(log) == (4, 4)
