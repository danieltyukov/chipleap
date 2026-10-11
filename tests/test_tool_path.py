"""A project cannot have its own programs run in place of the tools.

An empty entry, ``.`` or any other relative entry in PATH names the current directory,
and during a run that is the project or its build directory. A project that ships an
executable ``z3``, ``yosys`` or ``sby`` would have it run instead of the real tool, by
ChipLeap or by a tool that starts another by name (SymbiYosys starts yosys and z3, cocotb
starts the simulator, a ``#!/usr/bin/env python3`` script starts Python). So tools are
found only in absolute directories outside the project, and every program ChipLeap
starts gets a PATH made of those alone."""

import os
import stat
from pathlib import Path

import pytest

from chipleap.rtl import confine
from chipleap.rtl import sim as sim_mod
from chipleap.rtl.project import load_project
from chipleap.rtl.verify import run_step
from chipleap.verify import tools
from chipleap.verify.remote import RemoteError, Step, run_steps

# Every program a step starts by name, directly or through another tool.
PLANTED = (
    "z3",
    "yices-smt2",
    "yosys",
    "yosys-smtbmc",
    "yosys-abc",
    "yosys-config",
    "sby",
    "eqy",
    "iverilog",
    "vvp",
    "verilator",
    "verilator_bin",
    "perl",
    "python3",
    "make",
    "c++",
    "g++",
    "sv2v",
    "slang",
    "genus",
)
STEPS = ("lint", "sim", "coverage", "formal", "cdc", "synth", "equiv", "gls")


def plant(directory: Path, sentinel: Path) -> None:
    """Executables named like the tools that record that they ran and fail."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in PLANTED:
        p = directory / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{sentinel}"\nexit 1\n')
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def hostile(counter_project: Path, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """The counter project with planted tools in its directory, in ``bin`` and in
    ``build``, a PATH that starts with "", ".", relative entries and an absolute one into
    the project, and the project as the current directory. Returns the project and the
    sentinel file a planted tool would write."""
    sentinel = tmp_path / "sentinel"
    for d in ("", "bin", "abs-bin", "build"):
        plant(counter_project / d, sentinel)
    entries = ["", ".", "bin", "./bin", str(counter_project / "abs-bin")]
    monkeypatch.setenv("PATH", os.pathsep.join([*entries, os.environ.get("PATH", "")]))
    monkeypatch.chdir(counter_project)
    # A netlist keeps no parameters for the test to read at gate level.
    test = counter_project / "test/test_counter.py"
    test.write_text(test.read_text().replace("int(dut.W.value)", "6"))
    # A project's own .sby file runs from its directory, the project's here.
    (counter_project / "quick.sby").write_text(
        "[options]\nmode bmc\ndepth 4\n\n[engines]\nsmtbmc z3\n\n"
        "[script]\nread -formal counter.sv\nprep -top counter\n\n[files]\nrtl/counter.sv\n"
    )
    yaml = counter_project / "project.yaml"
    yaml.write_text(
        yaml.read_text()
        + "formal: {checks: [{name: prove, depth: 4}], sby: [quick.sby]}\n"
        + "gate_sim: {netlist: synth}\n"
    )
    return counter_project, sentinel


def test_the_search_path_keeps_only_absolute_directories_outside_the_project(tmp_path):
    project = tmp_path / "p"
    (project / "bin").mkdir(parents=True)
    path = os.pathsep.join(
        ["", ".", "bin", "./x", "../y", "/usr/bin", str(project / "bin"), "/usr/bin", "/bin"]
    )
    assert tools.search_path([project], path) == os.pathsep.join(["/usr/bin", "/bin"])
    # An entry that leads into the project through "..", or through a symlink, is inside.
    (tmp_path / "link").symlink_to(project / "bin", target_is_directory=True)
    sneaky = os.pathsep.join([f"{project}/../p/bin", str(tmp_path / "link"), "/bin"])
    assert tools.search_path([project], sneaky) == "/bin"


def test_programs_are_never_found_in_the_current_or_a_project_directory(hostile):
    root, sentinel = hostile
    for name in PLANTED:
        found = tools.which(name, exclude=[root])
        assert found is None or (
            os.path.isabs(found) and not Path(found).resolve().is_relative_to(root)
        ), found
    # Without the project named, "" and "." still never reach its directory.
    for name in PLANTED:
        found = tools.which(name)
        assert found is None or Path(found).resolve().parent != root.resolve(), found
    assert not sentinel.exists()


def test_a_relative_tool_variable_is_not_followed(hostile, monkeypatch):
    root, _ = hostile
    monkeypatch.setenv("CHIPLEAP_SBY", "./sby")
    assert tools.locate("sby", "CHIPLEAP_SBY") is None
    assert not tools.sby().found
    monkeypatch.setenv("CHIPLEAP_SBY", "bin/sby")
    assert tools.locate("sby", "CHIPLEAP_SBY") is None
    monkeypatch.setenv("CHIPLEAP_SBY", str(root / "sby"))
    assert tools.locate("sby", "CHIPLEAP_SBY") == str(root / "sby")  # the user named it


def test_every_environment_chipleap_builds_has_a_clean_path(hostile):
    root, _ = hostile
    p = load_project(root)
    work = root / "build" / "x"
    # Steps run inside their project's context (runs_tools); the simulator found outside
    # it goes first, and one found in the project would not.
    sim_env = sim_mod._env("icarus", [*p.roots, work])
    with tools.untrusted(root):
        sim_env_in_step = sim_mod._env("icarus", [*p.roots, work])
    envs = {
        "clean_env": tools.clean_env(["relative/dir", "/opt/tool/bin"], [root]),
        "simulation": sim_env,
        "simulation in a step": sim_env_in_step,
        "solver": {"PATH": tools.solver_path("/opt/sby/bin/sby", [root])},
        "solver (relative tool)": {"PATH": tools.solver_path("bin/sby", [root])},
    }
    with confine.confined(["/usr/bin/true"], [*p.roots, work]) as (_, env):
        envs["confined"] = env
    for what, env in envs.items():
        entries = env["PATH"].split(os.pathsep)
        assert entries and all(os.path.isabs(e) for e in entries), (what, entries)
        assert not any(Path(e).resolve().is_relative_to(root.resolve()) for e in entries), what
    assert envs["clean_env"]["PATH"].split(os.pathsep)[0] == "/opt/tool/bin"
    assert envs["solver"]["PATH"].split(os.pathsep)[0] == "/opt/sby/bin"


def test_a_local_tool_step_never_runs_a_program_from_the_project(hostile):
    # Commercial steps run their tools by name; "genus" is planted in the work directory,
    # in the project and in an absolute PATH entry into it.
    root, sentinel = hostile
    with tools.untrusted(root), pytest.raises(RemoteError, match="genus is not on the path"):
        run_steps([Step(("genus", "-batch"), "genus.out")], root / "build", None, 30)
    assert not sentinel.exists()


@pytest.mark.parametrize("step", STEPS)
def test_a_step_runs_the_real_tools_or_skips(hostile, step):
    root, sentinel = hostile
    r = run_step(load_project(root), step)
    ran = sentinel.read_text() if sentinel.exists() else ""
    assert not ran, f"{step} ran a planted program: {ran}"
    assert r.status in ("pass", "skipped"), (r.status, r.summary)
