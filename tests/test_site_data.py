"""scripts/build_site_data.py: the rows the website shows come from verify.json files that
runs wrote, a step run again on its own replacing that step's row."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _site(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_site_data.py"
    spec = importlib.util.spec_from_file_location("build_site_data", path)
    assert spec is not None and spec.loader is not None
    site = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "build_site_data", site)
    spec.loader.exec_module(site)
    return site


def _row(step: str, status: str, summary: str) -> dict[str, str]:
    return {"step": step, "status": status, "summary": summary}


def test_a_bundles_rows_take_the_steps_run_elsewhere_in_their_place(tmp_path, monkeypatch):
    site = _site(monkeypatch)
    bundle = tmp_path / "verify.json"
    bundle.write_text(
        json.dumps(
            [
                _row("consistency", "pass", "consistent"),
                _row("lint", "skipped", "Verilator is not installed"),
                _row("sim", "skipped", "Verilator is not installed"),
                _row("pnr", "pass", "routed"),
            ]
        )
    )
    extra = tmp_path / "lint-sim.json"
    extra.write_text(
        json.dumps([_row("lint", "pass", "no warnings"), _row("sim", "pass", "bit-exact")])
    )
    rows = site.step_rows(bundle, [str(extra)])
    assert list(rows) == ["consistency", "lint", "sim", "pnr"]
    assert [r["status"] for r in rows.values()] == ["pass"] * 4
    assert rows["sim"]["summary"] == "bit-exact"
    assert site.step_rows(bundle)["lint"]["status"] == "skipped"


def test_a_projects_rows_are_read_from_its_build_folder_and_replaced_the_same_way(
    tmp_path, monkeypatch
):
    site = _site(monkeypatch)
    verify = tmp_path / "build" / "verify"
    verify.mkdir(parents=True)
    (verify / "verify.json").write_text(
        json.dumps(
            {
                "project": "uart",
                "steps": [_row("lint", "pass", "no warnings"), _row("gls", "skipped", "no Icarus")],
            }
        )
    )
    extra = tmp_path / "gls.json"
    extra.write_text(json.dumps({"project": "uart", "steps": [_row("gls", "pass", "5 of 5")]}))
    rows = site.rtl_rows(tmp_path, [str(extra)])
    assert list(rows) == ["lint", "gls"]
    assert rows["gls"]["summary"] == "5 of 5"
