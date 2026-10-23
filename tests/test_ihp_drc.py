"""IHP's DRC deck run by ChipLeap after LibreLane, in parallel parts (the soc step's
signoff): how the extra-rules deck is split, what the run counts and sets apart, and a run
that must finish to count."""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from chipleap.chip import run as chip_run
from chipleap.verify import antenna, maximal
from chipleap.verify import pnr as P

HEADER = """\
logger = MultiLogger.new(stdout_logger, file_logger)
source($input, $topcell)
class DRC::DRCLayer
    def output(*args)
        original_output(*args)
    end
end
$start_time = Time.now
"""
LAYERS = """\
Activ = source.polygons("1/0")
GatPoly = source.polygons("5/0")
Metal1 = source.polygons("8/0")
Gate = Activ.ext_and(GatPoly)
M1_wide = Metal1.sized(-1.um).sized(1.um)
Unused = Metal1.holes
"""
RULES = """\
-> do
    Activ.ext_width(0.15.um)
end.().output("Act.a", "Min. Activ width = 0.15")
-> (;x) do
    x = Gate.dup
    x.ext_width(0.13.um)
end.().output("Gat.a", "Min. GatPoly width = 0.13")

if $fillerRules
\t-> do
\t    Metal1.ext_space(0.4.um)
\tend.().output("M1Fil.c", "Min. Metal1:filler space = 0.42")
end
-> do
    M1_wide.ext_space(0.6.um)
end.().output("M1.f", "Min. space of wide Metal1 lines = 0.60")
"""
FOOTER = """\
puts("Number of DRC errors for maximum rule set: #{$drc_error_count}")
logger.info("KLayout DRC run for maximum ruleSet completed in #{run_time.round(2)} seconds")
"""
DECK = HEADER + "\n" + LAYERS + RULES + "\n" + FOOTER


def test_the_extra_rules_split_into_runs_that_keep_only_the_layers_they_use():
    parts = maximal.split(DECK, 2)
    assert parts is not None and len(parts) == 2
    first, second = parts
    assert [maximal.rules_of(p) for p in parts] == [["Act.a", "Gat.a"], ["M1Fil.c", "M1.f"]]
    for p in parts:
        assert p.startswith(HEADER) and p.endswith(FOOTER)
        assert "Unused" not in p
    # Gat.a needs Gate, so the layers Gate is derived from, in the deck's order.
    assert 'Activ = source.polygons("1/0")\nGatPoly' in first and "Gate = " in first
    assert "Metal1" not in first and "M1_wide" not in second.split("$start_time")[0]
    assert "M1_wide = " in second and "Gate = " not in second
    # The switch's group stays whole, and each block is logged as it starts.
    assert "if $fillerRules\n\t-> do" in second
    assert "logger.info('Executing rules Act.a')\n-> do" in first
    # As many parts as there are blocks at most, and none for fewer than two.
    assert len(maximal.split(DECK, 9) or ()) == 4
    assert maximal.split(DECK, 1) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.replace("$start_time = Time.now\n", ""),
        lambda d: d.replace('puts("Number', 'print("Number'),
        lambda d: d.replace("Gate = Activ", "Gate += Activ"),  # not one line per layer
        lambda d: d.replace("-> (;x) do", "-> do"),  # assigns a name outside its block
        lambda d: d.replace("M1.f", "Act.a"),  # a rule reported twice
        lambda d: d.replace("logger = Multi", 'Metal1.output("X.a", "x")\nlogger = Multi'),
        lambda d: d.replace('end.().output("M1.f"', '  end.().output("M1.f"'),  # unclosed
    ],
)
def test_a_deck_laid_out_otherwise_is_not_split(change):
    assert maximal.split(change(DECK), 2) is None


def _pdk_deck() -> Path | None:
    root = os.environ.get("CHIPLEAP_IHP_PDK") or str(
        Path.home() / ".local/share/pdk/IHP-Open-PDK/ihp-sg13g2"
    )
    deck = Path(root) / "libs.tech/klayout/tech/drc/rule_decks" / maximal.DECK
    return deck if deck.is_file() else None


@pytest.mark.skipif(_pdk_deck() is None, reason="no IHP PDK here")
def test_the_pdks_extra_rules_split_with_every_rule_once():
    text = _pdk_deck().read_text()  # type: ignore[union-attr]
    parts = maximal.split(text, 8)
    assert parts is not None and len(parts) == 8
    rules = [r for p in parts for r in maximal.rules_of(p)]
    assert rules == maximal.rules_of(text) and len(rules) > 200
    assert all(p.rstrip().endswith("#===================================") for p in parts)


def test_commands_run_at_once_and_all_end_when_one_is_too_slow(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPLEAP_CONFINE", "0")
    py = sys.executable
    cmds = {
        "a": [py, "-c", "import time; time.sleep(0.5); print('a done')"],
        "b": [py, "-c", "import sys, time; time.sleep(0.5); print('b failed'); sys.exit(3)"],
    }
    import time

    start = time.monotonic()
    out = chip_run.run_all(cmds, tmp_path, [], tmp_path, 30)
    assert time.monotonic() - start < 1.4  # at once, not one after the other
    assert out["a"][0] == 0 and "a done" in out["a"][1]
    assert out["b"][0] == 3 and (tmp_path / "b.log").read_text().strip() == "b failed"
    slow = {"fast": [py, "-c", "pass"], "slow": [py, "-c", "import time; time.sleep(30)"]}
    start = time.monotonic()
    with pytest.raises(chip_run.ToolError, match="did not finish in 1 s"):
        chip_run.run_all(slow, tmp_path, [], tmp_path, 1)
    assert time.monotonic() - start < 10


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


def _pdk(tmp_path: Path, deck: str = DECK) -> Path:
    pdk = tmp_path / "ihp-sg13g2"
    rules = pdk / "libs.tech/klayout/tech/drc/rule_decks"
    rules.mkdir(parents=True)
    (rules / maximal.DECK).write_text(deck)
    return pdk


def _runs(
    monkeypatch,
    code: int,
    items: list[tuple[str, str]] | None,
    seen: dict,
    part: tuple[int, str, list[tuple[str, str]]] = (0, "done", []),
) -> None:
    """ihp_runs as it would end: run_drc.py with ``code`` and ``items`` in its merged
    report, and each part of the extra rules with ``part``: its exit code, whether its log
    has the deck's last line, and its report's items (each rule's count printed from
    them)."""
    part_code, part_log, part_items = part

    def fake(pdk, out_dir, inputs, decks, cmds, timeout):
        seen.update(
            inputs=inputs, decks=decks, cmds={n: c(Path("/t/run"), "py") for n, c in cmds.items()}
        )
        if items is not None:
            _report(out_dir / "layout_top_full.lyrdb", items)
        runs = {P.DRC_MAIN: (code, "the runner's last line\n")}
        for name in cmds:
            if name == P.DRC_MAIN:
                continue
            rules = maximal.rules_of(decks[f"{name}.drc"])
            mine = [(r, c) for r, c in part_items if r in rules]
            _report(out_dir / f"layout_top_{name}.lyrdb", mine)
            done = maximal.DONE if part_log == "done" else "Executing rules"
            (out_dir / f"layout_top_{name}.log").write_text(done + "\n")
            counts = "".join(
                f"Rule {r}: {sum(1 for x, _ in mine if x == r)} error(s)\n" for r in rules
            )
            (out_dir / f"{name}.log").write_text(counts)
            runs[name] = (part_code, "KLayout's last line\n")
        return runs

    monkeypatch.setattr(chip_run, "ihp_runs", fake)
    monkeypatch.setattr(
        chip_run, "klayout", lambda: chip_run.Tool("klayout", "/usr/bin/klayout", "0.30.4", "DRC")
    )


ROUTED = P.PnrResult(36.0, 1.0, 1.0, 1.0, 0.5, 10, 0.4, 1.0, 0.05, 0, 0, 0, 1.0, Path("x.gds"))
HELD = {"RM_IHPSG13_1P_BLDRV": ("RM_IHPSG13_1P_1024x32_c2_bm_bist",)}


def test_the_deck_counts_its_violations_and_sets_the_macros_pin_markers_apart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    monkeypatch.setenv("LIBRELANE_MAX_CORES", "16")
    pdk = _pdk(tmp_path)
    seen: dict = {}
    items = [("M2.a", "top"), ("metal1_pin_Offgrid", "RM_IHPSG13_1P_BLDRV")]
    _runs(monkeypatch, 1, items, seen, (0, "done", [("Gat.a", "top"), ("M1.f", "top")]))
    r = P.with_ihp_drc(ROUTED, pdk, "top", tmp_path, HELD)
    assert r.drc_errors == 3 and not r.clean
    assert r.pin_markers[0]["rule"] == "metal1_pin_Offgrid" and r.pin_markers[0]["count"] == 1
    assert seen["inputs"] == {"layout.gds": Path("x.gds")}
    # 16 threads: the extra rules in 8 parts (here 4, one per rule), the main tables 8 at
    # once, 4 threads each; density off, as LibreLane checks a block, and run_drc.py
    # leaves the extra rules to the parts.
    assert P.drc_plan(16) == (8, 8, 4) and P.drc_plan(2) == (0, 2, 1)
    main = seen["cmds"][P.DRC_MAIN]
    assert main[0] == "py" and main[1].endswith("run_drc.py")
    assert main[2:] == [
        "--path=/t/layout.gds",
        "--topcell=top",
        "--run_dir=/t/run",
        "--mp=8",
        "--density_thr=4",
        "--run_mode=deep",
        "--no_density",
        "--disable_extra_rules",
    ]
    assert sorted(seen["decks"]) == [f"maximal_{k}.drc" for k in range(1, 5)]
    assert seen["cmds"]["maximal_2"] == [
        "/usr/bin/klayout",
        "-b",
        "-r",
        "/t/maximal_2.drc",
        *("-rd", "input=/t/layout.gds", "-rd", "topcell=top"),
        *("-rd", "report=/t/run/layout_top_maximal_2.lyrdb"),
        *("-rd", "log=/t/run/layout_top_maximal_2.log", "-rd", "threads=4"),
        *("-rd", "run_mode=deep", "-rd", "no_recommended=false"),
    ]
    _runs(monkeypatch, 1, [("metal1_pin_Offgrid", "RM_IHPSG13_1P_BLDRV")], {})
    assert P.with_ihp_drc(ROUTED, pdk, "top", tmp_path, HELD).clean


def test_too_few_threads_or_a_deck_that_cannot_split_run_the_extra_rules_whole(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    for threads, deck in (("2", DECK), ("16", DECK.replace("$start_time = Time.now\n", ""))):
        monkeypatch.setenv("LIBRELANE_MAX_CORES", threads)
        seen: dict = {}
        _runs(monkeypatch, 0, [], seen)
        pdk = _pdk(tmp_path / threads, deck)
        assert P.with_ihp_drc(ROUTED, pdk, "top", tmp_path, HELD).clean
        assert list(seen["cmds"]) == [P.DRC_MAIN] and not seen["decks"]
        assert "--disable_extra_rules" not in seen["cmds"][P.DRC_MAIN]


@pytest.mark.parametrize(
    "code, items, part, match",
    [
        (1, None, (0, "done", []), r"did not finish \(exit 1\): the runner's last line"),
        (0, [("M2.a", "top")], (0, "done", []), "exited 0 with 1 violations"),
        (1, [], (0, "done", []), "exited 1 with 0 violations"),
        (0, [], (1, "done", []), r"maximal_1, did not finish \(exit 1\): KLayout's last line"),
        (0, [], (0, "cut", []), r"maximal_1, did not finish \(exit 0\)"),
    ],
)
def test_a_deck_run_must_finish_and_agree_with_its_report(
    tmp_path, monkeypatch, code, items, part, match
):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    monkeypatch.setenv("LIBRELANE_MAX_CORES", "8")
    _runs(monkeypatch, code, items, {}, part)
    with pytest.raises(P.PnrError, match=match):
        P.with_ihp_drc(ROUTED, _pdk(tmp_path), "top", tmp_path, HELD)


def test_a_part_must_count_each_of_its_rules_and_agree_with_its_report(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    monkeypatch.setenv("LIBRELANE_MAX_CORES", "8")
    pdk = _pdk(tmp_path)
    _runs(monkeypatch, 0, [], {})
    real = chip_run.ihp_runs

    def drop(lines: str) -> str:
        return "".join(ln for ln in lines.splitlines(True) if "Act.a" not in ln)

    def missing(*args):
        out = real(*args)
        log = args[1] / "maximal_1.log"
        log.write_text(drop(log.read_text()))
        return out

    monkeypatch.setattr(chip_run, "ihp_runs", missing)
    with pytest.raises(P.PnrError, match=r"maximal_1, did not run Act\.a"):
        P.with_ihp_drc(ROUTED, pdk, "top", tmp_path, HELD)

    def unreported(*args):
        out = real(*args)
        (args[1] / "maximal_1.log").write_text("Rule Act.a: 2 error(s)\n")
        return out

    monkeypatch.setattr(chip_run, "ihp_runs", unreported)
    with pytest.raises(P.PnrError, match="maximal_1, counted 2 violations and reported 0"):
        P.with_ihp_drc(ROUTED, pdk, "top", tmp_path, HELD)


def test_signoff_needs_the_deck(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "no IHP PDK (CHIPLEAP_IHP_PDK)")
    with pytest.raises(P.PnrError, match="IHP's DRC cannot run: no IHP PDK"):
        P.with_ihp_drc(ROUTED, None, "top", tmp_path, HELD)
    with pytest.raises(P.PnrError, match="left no GDS"):
        P.with_ihp_drc(replace(ROUTED, gds=None), tmp_path, "top", tmp_path, HELD)


# The PDK macros' markers in the checked GDS: cells it renames, and a report in the top cell.


def _sref(cell: str, x: int, y: int, angle: float = 0.0) -> bytes:
    from test_chip_macros import name, real8, record

    out = record(0x0A, 0) + record(0x12, 6, name(cell))
    if angle:
        out += record(0x1A, 1, b"\x00\x00") + record(0x1C, 5, real8(angle))
    return out + record(0x10, 3, struct.pack(">2i", x, y)) + record(0x11, 0)


def _layout() -> bytes:
    """A top placing macro M twice (at (100, 50) um as drawn, and at (300, 0) um turned by
    90 degrees) and a cell of its own; M's driver is renamed EA_DRV, and a cell M places
    is placed by the top's own cell too."""
    from test_chip_macros import box, gds

    return gds(
        {
            "EA_DRV": box(0, 0, 10, 10),
            "M": _sref("EA_DRV", 0, 0) + _sref("shared", 0, 0),
            "own": _sref("shared", 0, 0),
            "shared": box(0, 0, 5, 5),
            "top": _sref("M", 100_000, 50_000)
            + _sref("M", 300_000, 0, angle=90.0)
            + _sref("own", 0, 0),
        }
    )


def test_the_gds_names_the_cells_only_the_macros_place_and_where_each_macro_is():
    from chipleap.verify.drc import macro_layout

    found = macro_layout(_layout(), "top", {"M": (20.0, 10.0)})
    assert found.cells == {"M": ("M",), "EA_DRV": ("M",)}
    assert found.outlines == [
        ("M", pytest.approx((100.0, 50.0, 120.0, 60.0))),
        ("M", pytest.approx((290.0, 0.0, 300.0, 20.0))),
    ]


def _items(path: Path, items: list[tuple[str, str, str]]) -> Path:
    path.write_text(
        "<report-database><items>"
        + "".join(
            f"<item><category>{rule}</category><cell>{cell}</cell><values>"
            f"<value>{shape}</value></values></item>"
            for rule, cell, shape in items
        )
        + "</items></report-database>"
    )
    return path


def test_off_grid_pin_markers_inside_a_placed_macro_are_set_apart_wherever_reported(tmp_path):
    from chipleap.verify.drc import DrcResult, macro_layout, read_lyrdb

    point = "edge-pair: ({x},{y};{x},{y})/({x},{y};{x},{y})"
    report = _items(
        tmp_path / "r.lyrdb",
        [
            # Reported in the top cell, as points: inside either placed M, and outside both.
            ("metal1_pin_Offgrid", "top", point.format(x=110.001, y=55.0)),
            ("metal1_pin_Offgrid", "top", point.format(x=295.001, y=19.0)),
            ("metal1_pin_Offgrid", "top", point.format(x=150.001, y=55.0)),
            # Hierarchical, in the renamed driver and in the layout's own cell.
            ("metal1_pin_Offgrid", "EA_DRV:r0", "box: (1,1;2,2)"),
            ("metal1_pin_Offgrid", "shared", "box: (1,1;2,2)"),
            # Another rule inside a macro still counts.
            ("M1.b", "top", point.format(x=110.0, y=55.0)),
        ],
    )
    found = macro_layout(_layout(), "top", {"M": (20.0, 10.0)})
    r = DrcResult()
    read_lyrdb(report, r, found.cells, found.outlines, "top")
    assert r.violations == {"metal1_pin_Offgrid": 2, "M1.b": 1}
    assert r.pin_markers == {("metal1_pin_Offgrid", "M"): 2, ("metal1_pin_Offgrid", "EA_DRV"): 1}
    # Without the GDS, only the PDK's own names are known.
    plain = DrcResult()
    read_lyrdb(report, plain, {"DRV": ("M",)})
    assert plain.markers == 0 and plain.total == 6


def test_the_step_reads_the_gds_for_markers_it_cannot_place_by_name(tmp_path, monkeypatch):
    from chipleap.macro import sram

    monkeypatch.setattr(antenna, "unavailable", lambda pdk: "")
    monkeypatch.setenv("LIBRELANE_MAX_CORES", "2")
    pdk = _pdk(tmp_path)
    gds = tmp_path / "layout.gds"
    gds.write_bytes(_layout())
    monkeypatch.setattr(sram, "catalog", lambda p: [sram.Sram("M", 256, 64, 20.0, 10.0, True)])

    def fake(pdk, out_dir, inputs, decks, cmds, timeout):
        _items(
            out_dir / "layout_top_full.lyrdb",
            [
                (
                    "metal1_pin_Offgrid",
                    "top",
                    "edge-pair: (110.001,55;110.001,55)/(110.001,55;110.001,55)",
                )
            ],
        )
        return {P.DRC_MAIN: (1, "")}

    monkeypatch.setattr(chip_run, "ihp_runs", fake)
    r = P.with_ihp_drc(replace(ROUTED, gds=gds), pdk, "top", tmp_path, {"DRV": ("M",)})
    assert r.clean and r.pin_markers == [
        {"rule": "metal1_pin_Offgrid", "cell": "M", "macros": ["M"], "count": 1}
    ]
