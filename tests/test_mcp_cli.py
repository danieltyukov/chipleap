import asyncio
import json
from pathlib import Path

import pytest

from chipleap import mcp_server
from chipleap.cli import main

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SPEC = (EXAMPLES / "smollm2-135m-open-pdk.yaml").read_text()
DESIGN = """
name: t
tech: ihp-sg13g2
freq_mhz: 100
cpu: cv32e20
bus: NtoM
banks: 2
il_banks: 2
ext_mem: hyperram
ext_channels: 2
accel: {dataflow: gemv, rows: 4, cols: 8, ports: 2, abuf_kib: 8, wbuf_kib: 4}
"""


def _client(server):
    try:
        from mcp import Client
    except ImportError:  # mcp 1.x
        Client = None
    return Client(server) if Client else None


def test_mcp_tools_end_to_end(tmp_path):
    server = mcp_server.build_server(log_dir=str(tmp_path))
    client = _client(server)
    if client is None:
        pytest.skip("in-process client needs mcp 2.x")

    async def go():
        async with client as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {
                "explore_designs",
                "explain_bottleneck",
                "required_resources",
                "generate_soc",
                "verify_soc",
            } <= names
            r = await c.call_tool("required_resources", {"spec": SPEC})
            req = json.loads(r.content[0].text)
            assert req["throughput"] == 2
            r = await c.call_tool(
                "explain_bottleneck", {"spec": SPEC, "design": DESIGN, "limit": 5}
            )
            ex = json.loads(r.content[0].text)
            assert ex["diagnosis"] and len(ex["changes"]) == 5
            r = await c.call_tool("evaluate_design", {"spec": SPEC, "design": DESIGN})
            ev = json.loads(r.content[0].text)
            assert ev["feasible"]
            res = await c.read_resource("chipleap://spec-reference")
            assert "workload" in res.contents[0].text

    asyncio.run(go())
    logs = list(tmp_path.glob("*.jsonl"))
    assert logs
    ok, total = mcp_server.replay(logs[0])
    assert total == 3 and ok == 3


def test_tool_functions_directly():
    out = mcp_server.explore_designs(SPEC, samples=100)
    assert out["recommended"]["meets_spec"]
    assert "accel" in out["recommended_design_yaml"]
    with pytest.raises(ValueError):
        mcp_server.required_resources({"workload": {"llm": {"model": "smollm2-135m"}}})


def test_cli_commands(tmp_path, capsys):
    assert main(["models"]) == 0
    assert main(["require", "-m", "llama-3.2-1b", "-t", "5"]) == 0
    assert "GB/s" in capsys.readouterr().out
    spec = EXAMPLES / "smollm2-135m-open-pdk.yaml"
    out = tmp_path / "bundle"
    assert main(["explore", str(spec), "-o", str(out), "--samples", "100"]) == 0
    for f in ("report.html", "report.md", "explore.json", "DATASHEET.md", "design.yaml"):
        assert (out / f).is_file()
    html = (out / "report.html").read_text()
    assert "<svg" in html and "Pareto" in html
    assert main(["evaluate", str(spec), str(out)]) == 0
    assert main(["generate", str(out), "-o", str(tmp_path / "g"), "--spec", str(spec)]) == 0
    assert (tmp_path / "g" / "xheep").is_dir()
    assert main(["init", str(tmp_path / "s.yaml")]) == 0
    assert main(["explore", str(tmp_path / "missing.yaml")]) == 2


def test_cli_verify_lint_only(tmp_path):
    import shutil

    if shutil.which("verilator") is None:
        pytest.skip("Verilator not installed")
    out = tmp_path / "b"
    main(["generate", str(_design_file(tmp_path)), "-o", str(out)])
    assert main(["verify", str(out), "--steps", "lint"]) == 0
    assert "lint" in (out / "verify" / "VERIFY.md").read_text()


def _design_file(tmp_path):
    p = tmp_path / "design.yaml"
    p.write_text(DESIGN)
    return p
