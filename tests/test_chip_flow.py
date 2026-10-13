"""The UART example as a chip, with aispice's StrongARM comparator in a pad ring: built by
LibreLane and checked with IHP's DRC and LVS decks. Runs for 15 minutes or more, so only
with CHIPLEAP_RUN_CHIP=1, LibreLane, KLayout and IHP-Open-PDK installed."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from chipleap.chip import assemble, checks, io
from chipleap.rtl.project import load_project
from chipleap.rtl.verify import run_step

pytestmark = [
    pytest.mark.pnr,
    pytest.mark.skipif(
        os.environ.get("CHIPLEAP_RUN_CHIP") != "1", reason="set CHIPLEAP_RUN_CHIP=1 to build a chip"
    ),
]
UART = Path(__file__).resolve().parents[1] / "examples" / "uart"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("chip") / "uart"
    shutil.copytree(UART, root, ignore=shutil.ignore_patterns("build"))
    result = run_step(load_project(root), "chip")
    return root, result


def test_the_uart_chip_is_drc_and_lvs_clean(built):
    root, r = built
    assert r.status == "pass", r.summary
    d = r.data
    assert d["lvs"]["result"] == "pass" and d["drc"]["violations"] == 0
    assert d["route_drc_errors"] == 0
    assert all(c["pass"] for c in d["precheck"])
    assert sum(d["supply_hookup"]["VDD"].values()) and sum(d["supply_hookup"]["VSS"].values())
    assert (root / "build/chip/uart_chip.gds").is_file()
    # Timed at its ports through IHP's pads, at every corner.
    io = d["io_timing"]
    assert io["met"] and not io["untimed_pads"]
    assert {c["corner"] for c in io["corners"]} == {"typ", "slow", "fast"}
    out = io["paths"]["core_to_pad_setup"][0]
    assert out["endpoint"].startswith("pad_") and out["pad_delay_ns"] > 1.0
    rows = json.loads((root / "build/chip/uart_chip.bondmap.json").read_text())
    assert len(rows) == 33 and rows[0]["net"] == "ain_m"


def test_lvs_fails_when_two_pads_nets_are_swapped(built, tmp_path):
    """The LVS view with the labels of two pads exchanged: the netlist is the same, so
    IHP's deck must find the layout no longer matches."""
    root, _ = built
    lvs = root / "build/chip/lvs"
    plan = json.loads((lvs / "lvs_view.json").read_text())
    pins = plan["pins"]
    a = next(p for p in pins if p["net"] == "tx_data_i_0_core")
    b = next(p for p in pins if p["net"] == "tx_data_i_1_core")
    a["net"], b["net"] = b["net"], a["net"]
    plan["output"] = str(tmp_path / "swapped.gds")
    plan_file = tmp_path / "swapped.json"
    plan_file.write_text(json.dumps(plan))
    assemble.run_script(
        "lvs_view.py", plan_file, tmp_path, [Path(plan["input"]).parent], "view.log"
    )
    log = assemble.run_lvs(
        io.pdk_dir(), tmp_path, tmp_path / "swapped.gds", lvs / "uart_chip_lvs.cdl", "uart_chip"
    )
    assert checks.lvs_outcome(log) == "fail"
