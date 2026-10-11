"""Work and output directories that a project or a bundle from elsewhere cannot redirect.

A repository can ship ``build/`` (or ``build/pnr``, ``build/<tool>``, a bundle's
``verify/``, or the output directory itself) as a symlink to the home directory. Every
step must then refuse with a clear error, and nothing outside may be deleted or written.
A symlink further inside a work directory is removed before the step writes there."""

import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from chipleap.cli import main
from chipleap.gen.bundle import write_bundle
from chipleap.hw import Accel, Design
from chipleap.macro import write_evidence
from chipleap.rtl import builddir, commercial
from chipleap.rtl import pnr as pnr_mod
from chipleap.rtl import sim as sim_mod
from chipleap.rtl import synth as synth_mod
from chipleap.rtl import verify as verify_mod
from chipleap.rtl.builddir import BuildDirError
from chipleap.rtl.project import init_project, load_project
from chipleap.rtl.verify import run_step, verify_project
from chipleap.verify import launch
from chipleap.verify.pnr import PnrError
from chipleap.verify.remote import Remote, RemoteError
from chipleap.verify.result import StepResult
from chipleap.verify.run import verify_bundle
from chipleap.verify.tools import Tool

SERVER = Remote("server")
FOUND = Tool("stand-in", "/bin/true", "1", "tests")
DESIGN = Design(
    name="demo",
    tech="generic-28nm",
    freq_mhz=400,
    cpu="cv32e40p",
    bus="NtoM",
    banks=4,
    il_banks=4,
    ext_mem="lpddr4-x16",
    accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A stand-in for the home directory, with a file wherever a step that followed a
    symlink would delete or write."""
    h = tmp_path / "home"
    for d in ("", "pnr/src", "synth", "sim-xcelium", "coverage", "genus/src", "verify"):
        (h / d).mkdir(parents=True, exist_ok=True)
        (h / d / "keep.txt").write_text("precious\n")
    return h


def tree(d: Path) -> dict[str, str]:
    """Every file and directory under ``d``, with each file's content."""
    return {
        p.relative_to(d).as_posix(): p.read_text() if p.is_file() else "/"
        for p in sorted(d.rglob("*"))
    }


def link(at: Path, to: Path) -> None:
    at.parent.mkdir(parents=True, exist_ok=True)
    at.symlink_to(to, target_is_directory=to.is_dir())


@pytest.fixture(params=["descriptors", "paths"])
def mode(request, monkeypatch):
    """The checks run through directory descriptors (Linux, macOS) or, where the system
    has no descriptor-relative calls, on paths; both must hold."""
    if request.param == "paths":
        monkeypatch.setattr(builddir, "_FD", False)
    return request.param


# The helpers.


def test_work_dir_makes_what_is_missing(tmp_path, mode):
    w = builddir.work_dir(tmp_path, "build", "pnr")
    assert w == tmp_path.resolve() / "build/pnr" and w.is_dir()
    assert builddir.work_dir(tmp_path, "build", "pnr") == w


@pytest.mark.parametrize("at", ["build", "build/pnr", "build/pnr/src"])
@pytest.mark.parametrize("empty", [False, True])
def test_a_symlink_on_the_way_is_refused(tmp_path, home, mode, at, empty):
    root = tmp_path / "proj"
    root.mkdir()
    link(root / at, home)
    before = tree(home)
    with pytest.raises(BuildDirError, match=f"{at} is a symlink"):
        builddir.work_dir(root, "build", "pnr", "src", empty=empty)
    assert tree(home) == before


def test_a_file_on_the_way_is_refused(tmp_path, mode):
    (tmp_path / "build").write_text("not a directory\n")
    with pytest.raises(BuildDirError, match="build is not a directory"):
        builddir.work_dir(tmp_path, "build", "synth")


def test_symlinks_inside_are_removed_and_what_they_lead_to_is_kept(tmp_path, home, mode):
    w = tmp_path / "build/pnr"
    (w / "runs/a").mkdir(parents=True)
    (w / "runs/a/metrics.json").write_text("{}\n")
    link(w / "src", home)
    link(w / "config.json", home / "keep.txt")
    link(w / "runs/a/final", home / "verify")
    (w / "dangling").symlink_to(home / "nothing")
    before = tree(home)
    builddir.work_dir(tmp_path, "build", "pnr")
    assert tree(home) == before
    assert tree(w) == {"runs": "/", "runs/a": "/", "runs/a/metrics.json": "{}\n"}


def test_empty_removes_everything_inside_and_nothing_outside(tmp_path, home, mode):
    w = tmp_path / "build/sim"
    (w / "sim_build/obj").mkdir(parents=True)
    (w / "sim_build/obj/x.o").write_text("x")
    (w / "results.xml").write_text("<x/>")
    link(w / "sim_build/out", home)
    link(w / "log", home / "keep.txt")
    before = tree(home)
    assert builddir.work_dir(tmp_path, "build", "sim", empty=True) == w.resolve()
    assert tree(home) == before
    assert list(w.iterdir()) == []


def test_only_plain_names_below_the_root(tmp_path):
    for bad in ("..", ".", "", "a/b"):
        with pytest.raises(ValueError, match="plain"):
            builddir.work_dir(tmp_path, "build", bad)
    for empty in (False, True):
        with pytest.raises(ValueError, match="below the root"):
            builddir.work_dir(tmp_path, empty=empty)


def test_a_directory_swapped_for_a_symlink_during_the_check_is_refused(tmp_path, home, monkeypatch):
    """A race: another process replaces ``build`` with a symlink once ChipLeap has opened
    it. The emptying still happens in the directory that was checked, through its
    descriptor, never in the home directory, and the step is refused."""
    root = tmp_path / "proj"
    (root / "build/synth").mkdir(parents=True)
    (root / "build/synth/old.log").write_text("old\n")
    clean = builddir._clean

    def swap_then_clean(fd, everything, keep=""):
        if not (root / "moved").exists():
            (root / "build").rename(root / "moved")
            link(root / "build", home)
        clean(fd, everything, keep)

    monkeypatch.setattr(builddir, "_clean", swap_then_clean)
    before = tree(home)
    with pytest.raises(BuildDirError, match="changed while ChipLeap was checking it"):
        builddir.work_dir(root, "build", "synth", empty=True)
    assert tree(home) == before
    assert list((root / "moved/synth").iterdir()) == []


def test_write_text_never_follows_a_symlink(tmp_path, home, mode):
    out = tmp_path / "out"
    assert builddir.write_text(out.parent, "out/rtl/a.sv", "module a; endmodule\n") == (
        out / "rtl/a.sv"
    )
    assert (out / "rtl/a.sv").read_text() == "module a; endmodule\n"
    link(out / "sw", home)
    link(out / "README.md", home / "keep.txt")
    (out / "dangling.md").symlink_to(home / "new.md")
    before = tree(home)
    for rel in ("sw/driver.h", "README.md", "dangling.md"):
        with pytest.raises(BuildDirError, match="is a symlink"):
            builddir.write_text(out, rel, "text\n")
    assert tree(home) == before


def test_output_dir_refuses_a_symlink_and_marks_what_it_makes(tmp_path, home, mode):
    out = tmp_path / "results/out"
    assert builddir.output_dir(out) == out.resolve()
    assert (out / builddir.MARKER).is_file()
    link(tmp_path / "alias", home)
    before = tree(home)
    with pytest.raises(BuildDirError, match="alias is a symlink"):
        builddir.output_dir(tmp_path / "alias", empty=True)
    assert tree(home) == before


def test_only_a_directory_chipleap_made_is_emptied(tmp_path, home, mode):
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.txt").write_text("the user's\n")
    with pytest.raises(BuildDirError, match="not made by ChipLeap"):
        builddir.output_dir(mine, empty=True)
    assert (mine / "notes.txt").is_file()
    out = tmp_path / "out"
    builddir.output_dir(out)
    (out / "rtl").mkdir()
    (out / "rtl/old.sv").write_text("old\n")
    link(out / "sw", home)
    before = tree(home)
    builddir.output_dir(out, empty=True)
    assert tree(home) == before
    assert [p.name for p in out.iterdir()] == [builddir.MARKER]


# The path fallback, where the system has no descriptor-relative calls (Windows). It looks
# at every entry with lstat just before using it and never enters a symlink, a junction or
# any other reparse point.


class JunctionOs:
    """``os`` as builddir sees it on Windows when the tree holds directory junctions: lstat
    reports one as a directory carrying the reparse-point attribute, and scandir as a
    directory that is not a symlink, so neither os.path.islink nor DirEntry.is_symlink
    gives it away. Here every symlink to a directory looks like that."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def lstat(path, *args, **kwargs):
        st = os.lstat(path, *args, **kwargs)
        if not (stat.S_ISLNK(st.st_mode) and os.path.isdir(path)):
            return st
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
        )

    @staticmethod
    @contextmanager
    def scandir(path):
        with os.scandir(path) as it:
            yield [JunctionEntry(e) for e in it]

    @staticmethod
    def rmdir(path, *args, **kwargs):
        # Windows removes a junction itself with rmdir; the directory it leads to stays.
        if os.path.islink(path):
            return os.unlink(path)
        return os.rmdir(path, *args, **kwargs)


class JunctionEntry:
    def __init__(self, entry):
        self.name, self.path = entry.name, entry.path
        self._entry = entry
        self._junction = entry.is_symlink() and entry.is_dir()

    def is_symlink(self):
        return self._entry.is_symlink() and not self._junction

    def is_dir(self, *, follow_symlinks=True):
        return self._junction or self._entry.is_dir(follow_symlinks=follow_symlinks)


@pytest.fixture
def junctions(monkeypatch):
    monkeypatch.setattr(builddir, "_FD", False)
    monkeypatch.setattr(builddir, "os", JunctionOs())


def test_a_reparse_point_counts_as_a_link():
    def st(mode, attributes=None):
        if attributes is None:  # as on Linux and macOS, where there is no such field
            return SimpleNamespace(st_mode=mode)
        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)

    directory = stat.S_IFDIR | 0o755
    reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY
    assert builddir._link(st(directory)) == ""
    assert builddir._link(st(directory, stat.FILE_ATTRIBUTE_DIRECTORY)) == ""
    assert builddir._link(st(stat.S_IFREG | 0o644, 0)) == ""
    assert builddir._link(st(stat.S_IFLNK | 0o777)) == "a symlink"
    assert builddir._link(st(directory, reparse)) == "a junction or another reparse point"
    assert builddir._link(st(stat.S_IFREG | 0o644, stat.FILE_ATTRIBUTE_REPARSE_POINT))
    with pytest.raises(BuildDirError, match="x is a junction or another reparse point"):
        builddir._refuse(Path("x"), st(directory, reparse), directory=True)


@pytest.mark.parametrize("empty", [False, True])
def test_the_path_fallback_never_enters_a_junction_inside(tmp_path, home, junctions, empty):
    w = tmp_path / "build/pnr"
    (w / "runs").mkdir(parents=True)
    (w / "runs/metrics.json").write_text("{}\n")
    link(w / "src", home)
    link(w / "runs/final", home / "pnr")
    before = tree(home)
    builddir.work_dir(tmp_path, "build", "pnr", empty=empty)
    assert tree(home) == before
    assert not os.path.lexists(w / "src") and not os.path.lexists(w / "runs/final")
    assert tree(w) == ({} if empty else {"runs": "/", "runs/metrics.json": "{}\n"})


@pytest.mark.parametrize("at", ["build", "build/pnr", "build/pnr/src"])
def test_the_path_fallback_refuses_a_junction_on_the_way(tmp_path, home, junctions, at):
    link(tmp_path / at, home)
    before = tree(home)
    with pytest.raises(BuildDirError, match=f"{at} is a junction or another reparse point"):
        builddir.work_dir(tmp_path, "build", "pnr", "src", empty=True)
    assert tree(home) == before


def test_the_path_fallback_refuses_a_junction_as_output(tmp_path, home, junctions):
    link(tmp_path / "out", home)
    link(tmp_path / "bundle/sw", home)
    before = tree(home)
    with pytest.raises(BuildDirError, match="out is a junction"):
        builddir.output_dir(tmp_path / "out", empty=True)
    with pytest.raises(BuildDirError, match="sw is a junction"):
        builddir.write_text(tmp_path / "bundle", "sw/driver.h", "text\n")
    assert tree(home) == before


def test_the_path_fallback_stops_when_a_directory_is_swapped_while_it_empties(
    tmp_path, home, monkeypatch
):
    """A race: a directory inside the work directory becomes a symlink after ChipLeap
    looked at it and before it goes in. The fallback looks again before it lists or
    removes anything there, so it stops instead of emptying the home directory."""
    monkeypatch.setattr(builddir, "_FD", False)
    w = tmp_path / "build/sim"
    (w / "sim_build").mkdir(parents=True)
    (w / "sim_build/x.o").write_text("x")

    class SwappingOs:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def lstat(path, *args, **kwargs):
            st = os.lstat(path, *args, **kwargs)
            if Path(path).name == "sim_build" and not (tmp_path / "moved").exists():
                (w / "sim_build").rename(tmp_path / "moved")
                link(w / "sim_build", home)
            return st

    monkeypatch.setattr(builddir, "os", SwappingOs())
    before = tree(home)
    with pytest.raises(BuildDirError, match="sim_build changed while ChipLeap was emptying it"):
        builddir.work_dir(tmp_path, "build", "sim", empty=True)
    assert tree(home) == before


def test_the_path_fallback_stops_when_the_tree_moves_outside_while_it_empties(
    tmp_path, home, monkeypatch
):
    """A race: ``build`` moves into the home directory, and a symlink to it takes its place,
    while ChipLeap empties ``build/sim``. Each directory is still the one ChipLeap saw,
    but it no longer leads to a place inside the work directory, so ChipLeap stops before
    it removes one."""
    monkeypatch.setattr(builddir, "_FD", False)
    w = tmp_path / "build/sim"
    (w / "sim_build").mkdir(parents=True)
    (w / "sim_build/x.o").write_text("x")
    moved = home / "build"

    class MovingOs:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def unlink(path, *args, **kwargs):
            os.unlink(path, *args, **kwargs)
            if not moved.exists():
                (tmp_path / "build").rename(moved)
                link(tmp_path / "build", moved)

    monkeypatch.setattr(builddir, "os", MovingOs())
    with pytest.raises(BuildDirError, match="sim_build changed while ChipLeap was emptying it"):
        builddir.work_dir(tmp_path, "build", "sim", empty=True)
    assert (moved / "sim/sim_build").is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions exist only on Windows")
def test_a_real_junction_is_never_entered(tmp_path, home):  # pragma: no cover - Windows
    import _winapi

    w = tmp_path / "build/pnr"
    w.mkdir(parents=True)
    _winapi.CreateJunction(str(home), str(w / "src"))
    before = tree(home)
    builddir.work_dir(tmp_path, "build", "pnr", empty=True)
    assert tree(home) == before and not os.path.lexists(w / "src")
    _winapi.CreateJunction(str(home), str(tmp_path / "out"))
    with pytest.raises(BuildDirError, match="out is a junction"):
        builddir.output_dir(tmp_path / "out", empty=True)
    assert tree(home) == before


# Every step of an RTL project.


@pytest.fixture
def tools(monkeypatch):
    """Every step gets as far as its work directory: the tools are there, and where one
    would run, the step stops with "stopped here"."""

    def stop(*a, **k):
        raise RemoteError("stopped here")

    def stop_pnr(*a, **k):
        raise PnrError("stopped here")

    monkeypatch.setattr(synth_mod, "yosys", lambda: FOUND)
    monkeypatch.setattr(synth_mod, "sv2v", lambda: Tool("sv2v", None, "", "tests"))
    monkeypatch.setattr(synth_mod, "liberty_for", lambda tech: Path("/nonexistent/cells.lib"))
    monkeypatch.setattr(pnr_mod, "librelane", lambda: FOUND)
    monkeypatch.setattr(pnr_mod, "run_librelane", stop_pnr)
    monkeypatch.setattr(commercial, "synth_liberty", lambda tech: ("cells.lib", ""))
    monkeypatch.setattr(commercial, "_lefs", lambda tech, lib: ("cells.lef",))
    monkeypatch.setattr(commercial, "run_steps", stop)
    monkeypatch.setattr(sim_mod, "run_steps", stop)


# Each step, how run_step is asked for it, and its work directory under build/.
STEPS = {
    "yosys": ({"step": "synth"}, "synth"),
    "librelane": ({"step": "pnr"}, "pnr"),
    "xcelium": ({"step": "sim", "sim_tool": "xcelium", "remote": SERVER}, "sim-xcelium"),
    "genus": ({"step": "synth", "synth_tool": "genus", "remote": SERVER}, "genus"),
    "dc": ({"step": "synth", "synth_tool": "dc", "remote": SERVER}, "dc"),
    "innovus": ({"step": "pnr", "pnr_tool": "innovus", "remote": SERVER}, "innovus"),
}


@pytest.mark.parametrize("name", STEPS)
@pytest.mark.parametrize("whole", [True, False], ids=["build", "build-step"])
def test_every_step_refuses_a_symlinked_build_directory(counter_project, home, tools, name, whole):
    kwargs, step_dir = STEPS[name]
    at = "build" if whole else f"build/{step_dir}"
    link(counter_project / at, home)
    before = tree(home)
    r = run_step(load_project(counter_project), **kwargs)
    assert r.status == "fail" and f"{at} is a symlink" in r.summary, r.summary
    assert tree(home) == before


def test_coverage_refuses_a_symlinked_work_directory(counter_project, home, tools):
    link(counter_project / "build/coverage", home)
    before = tree(home)
    with pytest.raises(BuildDirError, match="coverage is a symlink"):
        sim_mod.run_tests(load_project(counter_project), "xcelium", coverage=True, remote=SERVER)
    assert tree(home) == before


@pytest.mark.parametrize(
    "name, inside, to",
    [
        ("librelane", "build/pnr/src", ""),
        ("librelane", "build/pnr/chipleap.sdc", "keep.txt"),
        ("genus", "build/genus/src", "genus"),
        ("innovus", "build/innovus/src", ""),
        ("xcelium", "build/sim-xcelium/src", "sim-xcelium"),
    ],
)
def test_a_symlink_inside_a_work_directory_is_removed_not_followed(
    counter_project, home, tools, name, inside, to
):
    kwargs, _ = STEPS[name]
    link(counter_project / inside, home / to)
    before = tree(home)
    r = run_step(load_project(counter_project), **kwargs)
    assert "stopped here" in r.summary, r.summary
    assert tree(home) == before
    assert not (counter_project / inside).is_symlink()


@pytest.mark.parametrize("at", ["build", "build/verify"])
def test_reports_are_not_written_through_a_symlink(counter_project, home, monkeypatch, at):
    monkeypatch.setattr(verify_mod, "lint", lambda p: StepResult("lint", "pass", "fine"))
    link(counter_project / at, home)
    before = tree(home)
    with pytest.raises(BuildDirError, match=f"{at} is a symlink"):
        verify_project(counter_project, steps="lint")
    assert tree(home) == before


def test_a_report_that_is_a_symlink_is_replaced(counter_project, home, monkeypatch):
    monkeypatch.setattr(verify_mod, "lint", lambda p: StepResult("lint", "pass", "fine"))
    link(counter_project / "build/verify/report.html", home / "keep.txt")
    before = tree(home)
    verify_project(counter_project, steps="lint")
    assert tree(home) == before
    report = counter_project / "build/verify/report.html"
    assert not report.is_symlink() and "<html" in report.read_text()


def test_the_cli_says_why(counter_project, home, monkeypatch, capsys):
    monkeypatch.setattr(verify_mod, "lint", lambda p: StepResult("lint", "pass", "fine"))
    link(counter_project / "build", home)
    before = tree(home)
    assert main(["verify", str(counter_project), "--steps", "lint"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("chipleap: ") and "build is a symlink" in err
    assert tree(home) == before


@pytest.mark.parametrize("to", ["keep.txt", "new.yaml"])
def test_init_does_not_write_through_a_symlinked_project_file(counter_project, home, to):
    (counter_project / "project.yaml").unlink()
    link(counter_project / "project.yaml", home / to)
    before = tree(home)
    with pytest.raises(BuildDirError, match=r"project\.yaml is a symlink"):
        init_project(counter_project, top="counter", force=True)
    assert tree(home) == before


def test_the_run_lock_is_not_created_through_a_symlink(tmp_path, home):
    work = tmp_path / "work"
    work.mkdir()
    (work / ".chipleap-run.lock").symlink_to(home / "made.lock")
    with pytest.raises(OSError):
        launch.run(["true"], work, work / "run.log", 10)
    assert not (home / "made.lock").exists()


# Bundles and files the user names: a new spec, the output directory, a bundle's verify/.


@pytest.mark.parametrize("to", ["keep.txt", "new.yaml"])
def test_init_spec_does_not_write_through_a_symlink(tmp_path, home, to):
    link(tmp_path / "spec.yaml", home / to)
    before = tree(home)
    assert main(["init", str(tmp_path / "spec.yaml"), "--force"]) == 2
    assert tree(home) == before


@pytest.mark.parametrize("at", ["out", "out/rtl", "out/README.md"])
def test_a_bundle_is_not_written_through_a_symlink(tmp_path, home, at):
    link(tmp_path / at, home / "keep.txt" if at.endswith(".md") else home)
    before = tree(home)
    with pytest.raises(BuildDirError, match="is a symlink"):
        write_bundle(tmp_path / "out", DESIGN)
    assert tree(home) == before


def test_the_cli_refuses_a_symlinked_output_directory(tmp_path, home, capsys):
    write_bundle(tmp_path / "first", DESIGN)
    link(tmp_path / "out", home)
    before = tree(home)
    assert main(["generate", str(tmp_path / "first"), "-o", str(tmp_path / "out")]) == 2
    assert "out is a symlink" in capsys.readouterr().err
    assert tree(home) == before


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    b = tmp_path / "bundle"
    write_bundle(b, DESIGN)
    return b


# Each bundle step, how verify_bundle is asked for it, and its work directory in verify/.
BUNDLE_STEPS = {
    "yosys": ({"steps": ("synth",)}, "synth"),
    "librelane": ({"steps": ("pnr",)}, "pnr"),
    "xcelium": ({"steps": ("sim",), "sim_tool": "xcelium", "remote": SERVER}, "xcelium"),
    "genus": ({"steps": ("synth",), "synth_tool": "genus", "remote": SERVER}, "genus"),
    "innovus": ({"steps": ("pnr",), "pnr_tool": "innovus", "remote": SERVER}, "innovus"),
}


@pytest.mark.parametrize("name", BUNDLE_STEPS)
@pytest.mark.parametrize("whole", [True, False], ids=["verify", "verify-step"])
def test_verifying_a_bundle_refuses_a_symlinked_verify_directory(bundle, home, name, whole):
    kwargs, step_dir = BUNDLE_STEPS[name]
    at = "verify" if whole else f"verify/{step_dir}"
    link(bundle / at, home)
    before = tree(home)
    with pytest.raises(BuildDirError, match=f"{at} is a symlink"):
        verify_bundle(bundle, **kwargs)
    assert tree(home) == before


def test_verifying_a_symlinked_bundle_is_refused(bundle, tmp_path):
    link(tmp_path / "alias", bundle)
    with pytest.raises(BuildDirError, match="alias is a symlink"):
        verify_bundle(tmp_path / "alias", steps=("consistency",))
    assert not (bundle / "verify").exists()


def test_a_bundle_report_that_is_a_symlink_is_replaced(bundle, home):
    link(bundle / "verify/VERIFY.md", home / "keep.txt")
    before = tree(home)
    verify_bundle(bundle, steps=("consistency",))
    assert tree(home) == before
    assert "consist" in (bundle / "verify/VERIFY.md").read_text()


# A macro package's evidence/, which a package from someone else could ship as a symlink.


@pytest.mark.parametrize("at", ["evidence", "evidence/drc.json"])
def test_evidence_is_not_written_through_a_symlink(tmp_path, home, at):
    pkg = tmp_path / "tiny.macro"
    pkg.mkdir()
    link(pkg / at, home / "verify" if at == "evidence" else home / "keep.txt")
    before = tree(home)
    with pytest.raises(BuildDirError, match=f"{at} is a symlink"):
        write_evidence(pkg, {"operation": "drc"})
    assert tree(home) == before
    if at != "evidence":
        assert write_evidence(pkg, {"operation": "lvs"}) == pkg / "evidence/lvs.json"
