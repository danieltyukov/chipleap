"""IHP's DRC deck run by ChipLeap after LibreLane, in parallel parts (the soc step's
signoff): what it counts, what it sets apart, and a run that must finish to count."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipleap.chip import run as chip_run
from chipleap.verify import antenna
from chipleap.verify import pnr as P


def _report(path: Path, items: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<report-database><items>"
        + "".join(
            f"<item><category>'{rule}'</category><cell>{cell}</cell><values>"
            "<value>box: (1,1;2,2)</value></values></item>"
            for rule, cell in items
        )
        + "</items></report-database>"
    )


def _runner(monkeypatch, code: int, items: list[tuple[str, str]] | None, seen: dict) -> None:
    def fake(pdk, which, out_dir, inputs, args, keep, timeout):
        seen.update(which=which, inputs=inputs, args=args(Path("run")))
        if items is not None:
            _report(out_dir / "layout_top_full.lyrdb", items)
        return code, "the runner's last line\n"

    monkeypatch.setattr(chip_run, "ihp_runner", fake)


ROUTED = P.PnrResult(36.0, 1.0, 1.0, 1.0, 0.5, 10, 0.4, 1.0, 0.05, 0, 0, 0, 1.0, Path("x.gds"))
HELD = {"RM_IHPSG13_1P_BLDRV": ("RM_IHPSG13_1P_1024x32_c2_bm_bist",)}


def test_the_deck_counts_its_violations_and_sets_the_macros_pin_markers_apart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    monkeypatch.setenv("LIBRELANE_MAX_CORES", "32")
    seen: dict = {}
    items = [("M2.a", "top"), ("metal1_pin_Offgrid", "RM_IHPSG13_1P_BLDRV")]
    _runner(monkeypatch, 1, items, seen)
    r = P.with_ihp_drc(ROUTED, tmp_path, "top", tmp_path, HELD)
    assert r.drc_errors == 1 and not r.clean
    assert r.pin_markers[0]["rule"] == "metal1_pin_Offgrid" and r.pin_markers[0]["count"] == 1
    assert seen["which"] == "drc" and seen["inputs"] == {"layout.gds": Path("x.gds")}
    # Four tables at once, the run's 32 threads shared, density off, as LibreLane checks a block.
    assert seen["args"][3:] == ["--mp=4", "--density_thr=8", "--run_mode=deep", "--no_density"]
    _runner(monkeypatch, 1, [("metal1_pin_Offgrid", "RM_IHPSG13_1P_BLDRV")], {})
    assert P.with_ihp_drc(ROUTED, tmp_path, "top", tmp_path, HELD).clean


@pytest.mark.parametrize(
    "code, items, match",
    [
        (1, None, r"did not finish \(exit 1\): the runner's last line"),
        (0, [("M2.a", "top")], "exited 0 with 1 violations"),
        (1, [], "exited 1 with 0 violations"),
    ],
)
def test_a_deck_run_must_finish_and_agree_with_its_report(
    tmp_path, monkeypatch, code, items, match
):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    _runner(monkeypatch, code, items, {})
    with pytest.raises(P.PnrError, match=match):
        P.with_ihp_drc(ROUTED, tmp_path, "top", tmp_path, HELD)


def test_signoff_needs_the_deck(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "no IHP PDK (CHIPLEAP_IHP_PDK)")
    with pytest.raises(P.PnrError, match="IHP's DRC cannot run: no IHP PDK"):
        P.with_ihp_drc(ROUTED, None, "top", tmp_path, HELD)
    from dataclasses import replace

    with pytest.raises(P.PnrError, match="left no GDS"):
        P.with_ihp_drc(replace(ROUTED, gds=None), tmp_path, "top", tmp_path, HELD)
