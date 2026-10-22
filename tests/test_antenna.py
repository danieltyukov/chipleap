"""IHP's antenna deck in signoff: where each violation is, a run that must finish to count,
and what place and route, an ECO and the chip step say about it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from chipleap.chip import assemble, io
from chipleap.verify import antenna, drc
from chipleap.verify import pnr as P

# Two gates of one net, each flagged at Metal5 (IHP's report of the SmolLM2 accelerator's
# layout before the second antenna repair, with its properties cut).
GATE_A = "polygon: (953.165,168.455;953.165,169.455;953.295,169.455;953.295,168.455)"
GATE_B = "polygon: (953.165,167.01;953.165,167.65;953.295,167.65;953.295,167.01)"


def report(path: Path, items: list[tuple[str, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<report-database><items>"
        + "".join(
            f"<item><category>'{rule}'</category><cell>{cell}</cell><values>"
            f"<value>{shape}</value><value>[#has_diode] float: 0</value></values></item>"
            for rule, cell, shape in items
        )
        + "</items></report-database>"
    )
    return path


def test_each_violation_is_kept_with_where_it_is(tmp_path):
    db = report(
        tmp_path / "a.lyrdb",
        [
            ("Ant.b_Metal5", "top", GATE_A),
            ("Ant.b_Metal5", "top", GATE_B),
            ("Ant.b_TopMetal1", "sub:r0", "edge-pair: (1,2;3,4)/(5,6;7,8)"),
            ("Ant.g", "top", "[#ratio] float: 3"),
        ],
    )
    r = drc.DrcResult(top="top")
    drc.read_lyrdb(db, r)
    assert r.violations == {"Ant.b_Metal5": 2, "Ant.b_TopMetal1": 1, "Ant.g": 1}
    assert r.places["Ant.b_Metal5"] == [
        ("top", 953.23, 168.955),
        ("top", 953.23, pytest.approx(167.33)),
    ]
    # A marker's centre in its own cell, the cell named without KLayout's variant.
    assert r.places["Ant.b_TopMetal1"] == [("sub", 4.0, 5.0)]
    assert r.places["Ant.g"] == []  # no shape to place it by
    data = antenna.summary(r)
    assert data["violations"] == 4 and list(data["places"]) == ["Ant.b_Metal5", "Ant.b_TopMetal1"]
    assert antenna.describe(data, "top") == (
        "Ant.b_Metal5 at (953.23, 168.96) um, (953.23, 167.33) um; "
        "Ant.b_TopMetal1 at (4.00, 5.00) um in sub; Ant.g (1)"
    )
    assert antenna.describe(data, "top", rules=1, places=1) == (
        "Ant.b_Metal5 at (953.23, 168.96) um and 1 more; 2 more rules"
    )


def test_only_the_first_places_of_a_rule_are_kept(tmp_path):
    db = report(tmp_path / "a.lyrdb", [("Ant.a", "top", GATE_A)] * (drc.PLACES_KEPT + 5))
    r = drc.DrcResult()
    drc.read_lyrdb(db, r)
    assert r.violations == {"Ant.a": drc.PLACES_KEPT + 5}
    assert len(r.places["Ant.a"]) == drc.PLACES_KEPT


def _deck(tmp_path: Path) -> Path:
    pdk = tmp_path / "ihp-sg13g2"
    (pdk / antenna.DECK).parent.mkdir(parents=True)
    (pdk / antenna.DECK).write_text("")
    return pdk


def _runner(monkeypatch, code: int, items: list[tuple[str, str, str]] | None, seen: dict):
    """IHP's runner: exits ``code`` and, unless ``items`` is None, leaves its report."""

    def fake(pdk, which, out_dir, inputs, args, keep, timeout):
        seen.update(which=which, inputs=inputs, args=args(Path("run")), timeout=timeout)
        if items is not None:
            report(out_dir / "layout_top_antenna.lyrdb", items)
        return code, "the runner's last line\n"

    monkeypatch.setattr(antenna, "ihp_runner", fake)


def test_the_deck_runs_on_the_gds_alone_and_its_threads_are_bounded(tmp_path, monkeypatch):
    seen: dict = {}
    _runner(monkeypatch, 0, [], seen)
    gds = tmp_path / "top.gds"
    r = antenna.check(_deck(tmp_path), gds, "top", tmp_path / "out", 60.0)
    assert r.total == 0 and r.top == "top"
    assert seen["which"] == "drc" and seen["inputs"] == {"layout.gds": gds}
    assert seen["args"][:4] == [
        "--path=layout.gds",
        "--topcell=top",
        "--run_dir=run",
        "--antenna_only",
    ]
    # Without a bound the deck takes a thread per CPU of the machine.
    assert seen["args"][4] == f"--density_thr={antenna.threads()}" and 1 <= antenna.threads() <= 8
    assert seen["timeout"] == 60.0


@pytest.mark.parametrize(
    "code, items, match",
    [
        (1, None, r"did not finish \(exit 1\): the runner's last line"),
        (-9, [], r"did not finish \(exit -9\)"),
        (0, [("Ant.a", "top", GATE_A)], "exited 0 with 1 violations"),
        (1, [], "exited 1 with 0 violations"),
    ],
)
def test_a_deck_run_must_finish_and_agree_with_its_report(
    tmp_path, monkeypatch, code, items, match
):
    _runner(monkeypatch, code, items, {})
    with pytest.raises(antenna.AntennaError, match=match):
        antenna.check(_deck(tmp_path), tmp_path / "top.gds", "top", tmp_path / "out", 60.0)


def _gate_gds(top: str, poly_um: int) -> bytes:
    """A 0.13 x 1 um gate whose GatPoly runs on for ``poly_um`` over the field; IHP's Ant.a
    allows 200 times the gate's area, 260 um of it."""
    import struct

    def rec(rtype: int, dtype: int, body: bytes = b"") -> bytes:
        return struct.pack(">HBB", 4 + len(body), rtype, dtype) + body

    def box(layer: int, x0: int, y0: int, x1: int, y1: int) -> bytes:  # nm
        xy = struct.pack(">10i", x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
        return (
            rec(0x08, 0)
            + rec(0x0D, 2, struct.pack(">h", layer))
            + rec(0x0E, 2, struct.pack(">h", 0))
            + rec(0x10, 3, xy)
            + rec(0x11, 0)
        )

    units = bytes([0x3E, 0x41, 0x89, 0x37, 0x4B, 0xC6, 0xA7, 0xF0])  # 1e-3 um
    meters = bytes([0x39, 0x44, 0xB8, 0x2F, 0xA0, 0x9B, 0x5A, 0x54])  # 1e-9 m
    name = top.encode() + b"\0" * (len(top) % 2)
    return (
        rec(0x00, 2, struct.pack(">h", 600))
        + rec(0x01, 2, b"\0" * 24)
        + rec(0x02, 6, b"lib\0")
        + rec(0x03, 5, units + meters)
        + rec(0x05, 2, b"\0" * 24)
        + rec(0x06, 6, name)
        + box(1, 0, 0, 1000, 1000)  # Activ
        + box(5, 435, 0, 565, 1000 + 1000 * poly_um)  # GatPoly
        + rec(0x07, 0)
        + rec(0x04, 0)
    )


@pytest.mark.skipif(
    antenna.unavailable(io.pdk_dir()) != "", reason="needs IHP-Open-PDK and KLayout's Python"
)
def test_ihps_deck_flags_a_gate_on_a_long_poly_and_says_where(tmp_path):
    # IHP's own deck, run as signoff runs it: the parts above with no stand-ins.
    pdk = io.pdk_dir()
    assert pdk is not None
    gds = tmp_path / "ant.gds"
    gds.write_bytes(_gate_gds("ant", 1000))
    r = antenna.check(pdk, gds, "ant", tmp_path / "out", 600.0)
    assert r.violations == {"Ant.a": 1}
    # The marker is the poly over the field, from 1 um to 1001 um.
    assert r.places["Ant.a"] == [("ant", 0.5, 501.0)]
    assert antenna.describe(antenna.summary(r), "ant") == "Ant.a at (0.50, 501.00) um"
    gds.write_bytes(_gate_gds("ant", 100))
    assert antenna.check(pdk, gds, "ant", tmp_path / "out2", 600.0).total == 0


def test_what_the_deck_needs(tmp_path, monkeypatch):
    assert "CHIPLEAP_IHP_PDK" in antenna.unavailable(None)
    assert "has no antenna deck" in antenna.unavailable(tmp_path)
    pdk = _deck(tmp_path)
    monkeypatch.setattr(antenna, "python_with_klayout", lambda: None)
    assert "CHIPLEAP_KLAYOUT_PYTHON" in antenna.unavailable(pdk)
    monkeypatch.setattr(antenna, "python_with_klayout", lambda: ("python3", "/x"))
    assert antenna.unavailable(pdk) == ""


ROUTED = P.PnrResult(
    clock_mhz=100.0,
    die_um2=1.0,
    core_um2=1.0,
    cell_um2=1.0,
    utilisation=0.5,
    cells=10,
    setup_slack_ns=1.0,
    setup_slack_typ_ns=2.0,
    hold_slack_ns=0.1,
    drc_errors=0,
    lvs_errors=0,
    antenna_nets=0,
    power_mw=1.0,
    gds=Path("top.gds"),
)


def _flagged(tmp_path) -> drc.DrcResult:
    r = drc.DrcResult(top="top")
    drc.read_lyrdb(report(tmp_path / "a.lyrdb", [("Ant.b_Metal5", "top", GATE_A)]), r)
    return r


def test_place_and_route_fails_on_the_decks_violations_and_says_where(tmp_path):
    clean = replace(ROUTED, ihp_antenna=drc.DrcResult(top="top"))
    assert clean.checks_clean
    assert clean.checks() == (
        "DRC, LVS and antenna clean, IHP's antenna deck included, no slew or capacitance violations"
    )
    assert clean.ihp_antenna_data() == {"ihp_antenna": {"violations": 0, "rules": {}, "places": {}}}
    flagged = replace(ROUTED, ihp_antenna=_flagged(tmp_path))
    assert not flagged.checks_clean
    assert flagged.checks() == (
        "0 DRC, 0 LVS and 0 antenna violations, 0 slew and 0 capacitance violations; "
        "IHP's antenna deck: 1 violation, Ant.b_Metal5 at (953.23, 168.96) um"
    )
    assert flagged.ihp_antenna_data()["ihp_antenna"]["places"] == {
        "Ant.b_Metal5": [{"cell": "top", "x_um": 953.23, "y_um": 168.955}]
    }
    skipped = replace(ROUTED, ihp_antenna_skipped="no IHP PDK (CHIPLEAP_IHP_PDK)")
    assert skipped.checks_clean
    assert skipped.checks() == (
        "DRC, LVS and antenna clean, no slew or capacitance violations; "
        "IHP's antenna deck not run: no IHP PDK (CHIPLEAP_IHP_PDK)"
    )
    assert skipped.ihp_antenna_data() == {
        "ihp_antenna": {"skipped": "no IHP PDK (CHIPLEAP_IHP_PDK)"}
    }
    # A flow that does not run it (Innovus) says nothing about it.
    assert ROUTED.checks() == "DRC, LVS and antenna clean, no slew or capacitance violations"
    assert ROUTED.ihp_antenna_data() == {}


def test_place_and_route_runs_the_deck_on_its_gds(tmp_path, monkeypatch):
    pdk = _deck(tmp_path)
    monkeypatch.setattr(antenna, "python_with_klayout", lambda: ("python3", "/x"))
    seen = {}

    def check(pdk_, gds, top, out, limit):
        seen.update(pdk=pdk_, gds=gds, top=top, out=out)
        (out / "layout_top_antenna.lyrdb").write_text("x")
        return drc.DrcResult(top=top)

    monkeypatch.setattr(antenna, "check", check)
    work = tmp_path / "pnr"
    work.mkdir()
    (work / P.ANTENNA_DIR).mkdir()
    (work / P.ANTENNA_DIR / "stale.lyrdb").write_text("an earlier run's report")
    r = P.with_ihp_antenna(ROUTED, pdk, "top", work)
    assert r.ihp_antenna is not None and r.ihp_antenna.total == 0
    assert seen == {
        "pdk": pdk,
        "gds": Path("top.gds"),
        "top": "top",
        "out": work.resolve() / "antenna",
    }
    assert not (work / P.ANTENNA_DIR / "stale.lyrdb").exists()  # emptied first
    assert (
        "no GDS"
        in P.with_ihp_antenna(replace(ROUTED, gds=None), pdk, "top", work).ihp_antenna_skipped
    )
    assert "CHIPLEAP_IHP_PDK" in P.with_ihp_antenna(ROUTED, None, "top", work).ihp_antenna_skipped

    def crashed(*a):
        raise antenna.AntennaError(
            "IHP's antenna deck did not finish (exit -11): Segmentation fault"
        )

    monkeypatch.setattr(antenna, "check", crashed)
    with pytest.raises(P.PnrError, match=r"did not finish \(exit -11\)"):
        P.with_ihp_antenna(ROUTED, pdk, "top", work)


def _chip_data(ihp: dict | None) -> dict:
    data = {
        "name": "c",
        "outline_um": [0, 0, 1000, 1000],
        "pads": 4,
        "macros": [],
        "route_drc_errors": 0,
        "antenna_nets": 0,
        "lvs": {"result": "pass"},
        "drc": {"violations": 0, "rules": {}},
        "supply_hookup": {"VDD": {"a": 1}, "VSS": {"a": 1}},
        "precheck": [],
        "setup_slack_ns": 1.0,
        "hold_slack_ns": 0.1,
    }
    if ihp is not None:
        data["ihp_antenna"] = ihp
    return data


def test_the_chip_fails_on_the_decks_violations_and_says_where(tmp_path):
    ok, summary = assemble.verdict(_chip_data({"violations": 0, "rules": {}, "places": {}}))
    assert ok and "DRC clean on IHP's deck (density and antenna included" in summary
    ok, summary = assemble.verdict(_chip_data(antenna.summary(_flagged(tmp_path))))
    assert not ok
    assert summary.endswith(
        "1 violation of IHP's antenna deck: Ant.b_Metal5 at (953.23, 168.96) um in top"
    )
