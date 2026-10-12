"""The verification-depth steps never delete, write or read through a symlinked build/.

A repository can ship ``build/``, ``build/<step>`` or ``build/pnr`` as a symlink to the
home directory. Formal, proposed assertions, CDC, equivalence and gate-level simulation
must then refuse with a clear error, and everything outside must be left as it was. The
tools are stand-ins: the refusal comes before any of them would run."""

from pathlib import Path

import pytest

from chipleap.rtl import cdc, equiv, formal, gls
from chipleap.rtl.builddir import BuildDirError
from chipleap.rtl.project import load_project
from chipleap.rtl.verify import run_step
from chipleap.verify.tools import Tool

FOUND = Tool("stand-in", "/bin/true", "13.0", "tests")
STEPS = ("formal", "cdc", "equiv", "gls")
RUN = "pnr/runs/chipleap/final"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A stand-in for the home directory, with a file wherever a step that followed a
    symlink would delete or write, and a routed run a step must not read."""
    h = tmp_path / "home"
    for d in ("", "formal", "formal/proposed", "cdc", "equiv/rtl-synth", "gls"):
        (h / d).mkdir(parents=True, exist_ok=True)
        (h / d / "keep.txt").write_text("precious\n")
    (h / RUN / "nl").mkdir(parents=True)
    (h / RUN / "nl/counter.nl.v").write_text("module counter; endmodule\n")
    return h


@pytest.fixture
def tools(monkeypatch):
    """Every tool found, so each step goes as far as its work directory."""
    monkeypatch.setattr(formal, "sby", lambda: FOUND)
    monkeypatch.setattr(cdc, "yosys", lambda: FOUND)
    monkeypatch.setattr(equiv, "yosys", lambda: FOUND)
    monkeypatch.setattr(equiv, "liberty_for", lambda tech: Path("/nonexistent/cells.lib"))
    monkeypatch.setattr(gls, "iverilog", lambda: FOUND)


def tree(d: Path) -> dict[str, str]:
    return {
        p.relative_to(d).as_posix(): p.read_text() if p.is_file() else "/"
        for p in sorted(d.rglob("*"))
    }


def project(root: Path):
    text = (root / "project.yaml").read_text()
    (root / "project.yaml").write_text(text + "formal: {checks: [{name: prove, depth: 4}]}\n")
    return load_project(root)


@pytest.mark.parametrize("step", STEPS)
def test_a_symlinked_build_directory_is_refused(counter_project, home, tools, step):
    p = project(counter_project)
    (counter_project / "build").symlink_to(home, target_is_directory=True)
    before = tree(home)
    r = run_step(p, step)
    assert r.status == "fail" and "BuildDirError" in r.summary, r.summary
    assert "symlink" in r.summary
    assert tree(home) == before


@pytest.mark.parametrize("step", ["formal", "cdc", "gls"])
def test_a_symlinked_step_directory_is_refused(counter_project, home, tools, step):
    p = project(counter_project)
    (counter_project / "build").mkdir()
    (counter_project / "build" / step).symlink_to(home / step, target_is_directory=True)
    before = tree(home)
    r = run_step(p, step)
    assert r.status == "fail" and f"build/{step} is a symlink" in r.summary, r.summary
    assert tree(home) == before


def test_a_symlinked_equivalence_directory_is_refused(counter_project, home, tools):
    p = project(counter_project)
    (counter_project / "build/equiv").mkdir(parents=True)
    (counter_project / "build/equiv/rtl-synth").symlink_to(
        home / "equiv/rtl-synth", target_is_directory=True
    )
    before = tree(home)
    with pytest.raises(BuildDirError, match="rtl-synth is a symlink"):
        equiv.check_pair(p, "rtl-synth")
    assert tree(home) == before


def test_proposed_assertions_refuse_a_symlinked_build_directory(counter_project, home, tools):
    p = project(counter_project)
    (counter_project / "build").mkdir()
    (counter_project / "build/formal").symlink_to(home / "formal", target_is_directory=True)
    before = tree(home)
    with pytest.raises(BuildDirError, match="formal is a symlink"):
        formal.check_assertions(p, "counter", [{"expr": "q_o != 4'd9"}])
    assert tree(home) == before


@pytest.mark.parametrize("step", ["equiv", "gls"])
def test_a_routed_run_behind_a_symlink_is_not_read(counter_project, home, tools, step):
    # The steps' own directories are real; only build/pnr leads out.
    project(counter_project)
    (counter_project / "build").mkdir()
    (counter_project / "build/pnr").symlink_to(home / "pnr", target_is_directory=True)
    yaml = counter_project / "project.yaml"
    yaml.write_text(yaml.read_text() + "equivalence: {checks: [synth-pnr]}\n")
    p = load_project(counter_project)
    before = tree(home)
    r = run_step(p, step)
    assert r.status == "fail" and "build/pnr is a symlink" in r.summary, r.summary
    assert tree(home) == before


def test_symlinks_left_in_a_work_directory_are_removed_not_followed(counter_project, home, tools):
    p = project(counter_project)
    w = counter_project / "build/cdc"
    w.mkdir(parents=True)
    (w / "netlist.ys").symlink_to(home / "keep.txt")
    (w / "old").symlink_to(home / "cdc", target_is_directory=True)
    before = tree(home)
    run_step(p, "cdc")  # the stand-in Yosys writes no netlist, so the step fails after
    assert tree(home) == before
    assert not (w / "old").exists() and not (w / "netlist.ys").is_symlink()
