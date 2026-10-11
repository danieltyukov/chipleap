"""Work and output directories that a project or a bundle from elsewhere cannot redirect.

A repository can ship ``build/`` (or ``build/pnr``, ``build/<tool>``, a bundle's
``verify/``, or the output directory itself) as a symlink to the home directory. Every
step must then refuse with a clear error, and nothing outside may be deleted or written.
A symlink further inside a work directory is removed before the step writes there."""

from pathlib import Path

import pytest

from chipleap.rtl import builddir
from chipleap.rtl.builddir import BuildDirError


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
