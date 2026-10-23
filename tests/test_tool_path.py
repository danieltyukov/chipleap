"""A project cannot have its own programs run in place of the tools.

An empty entry, ``.`` or any other relative entry in PATH names the current directory,
and during a run that is the project or its build directory. A project that ships an
executable ``z3``, ``yosys`` or ``sby`` would have it run instead of the real tool, by
ChipLeap or by a tool that starts another by name (SymbiYosys starts yosys and z3, cocotb
starts the simulator, a ``#!/usr/bin/env python3`` script starts Python). So tools are
found only in absolute directories outside the project, and every program ChipLeap
starts gets a PATH made of those alone."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from chipleap.rtl import confine
from chipleap.rtl import sim as sim_mod
from chipleap.rtl.project import load_project
from chipleap.rtl.verify import run_step
from chipleap.verify import librelane_pdk, tools
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


def test_run_logs_probe_tool_versions_outside_the_project(hostile):
    # With a log directory, every project call records the tool versions. Those probes run
    # as a step finds its tools (never from the project) and from an empty directory, so
    # "python -c 'import cocotb'" cannot import a cocotb package the project ships, even
    # when the server runs in the project.
    from chipleap import mcp_server

    root, sentinel = hostile
    (root / "cocotb").mkdir()
    (root / "cocotb/__init__.py").write_text(
        f"open({str(sentinel)!r}, 'a').write('cocotb from the project\\n')\n__version__ = '0'\n"
    )
    context = mcp_server.project_context(str(root))
    assert context["inputs_sha256"] and "verilator" in context["tools"]
    assert sim_mod.cocotb_version(sys.executable) != "0"
    assert not sentinel.exists(), sentinel.read_text()


def test_an_empty_path_falls_back_to_the_system_directories(monkeypatch):
    # An empty PATH would make a program started by name (ssh, rsync) resolve in its
    # working directory.
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin"]))
    path = tools.clean_env()["PATH"]
    assert path == os.defpath and all(os.path.isabs(e) for e in path.split(os.pathsep))
    assert tools.which("sh") in ("/bin/sh", "/usr/bin/sh")


def test_a_tool_found_only_in_the_project_says_so(hostile, monkeypatch):
    root, sentinel = hostile
    # Only the project's own directories and relative entries are on the path; the
    # system's default directories, which ChipLeap falls back to, hold no sby.
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(root / "abs-bin")]))
    monkeypatch.delenv("CHIPLEAP_SBY", raising=False)
    with tools.untrusted(root):
        found = tools.sby()
    assert not found.found and "inside the project" in found.note
    r = run_step(load_project(root), "formal")
    assert r.status == "skipped", r.summary
    assert "SymbiYosys (sby) is not installed (the only sby found is" in r.summary
    monkeypatch.setenv("CHIPLEAP_SBY", "bin/sby")
    assert "CHIPLEAP_SBY is a relative path, bin/sby" in tools.sby().note
    monkeypatch.setenv("CHIPLEAP_SBY", str(root / "sby"))
    with tools.untrusted(root):
        assert "inside the project" in tools.sby().note
    assert not sentinel.exists()


def test_directories_the_user_allowed_are_not_searched_for_tools(hostile, tmp_path, monkeypatch):
    # Files from an --allow-path directory come from elsewhere too: a step leaves it out
    # of the path as it leaves out the project.
    root, sentinel = hostile
    shared = tmp_path / "shared-ip"
    plant(shared / "bin", sentinel)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shared / "bin"), os.environ["PATH"]]))
    p = load_project(root, allow=[shared])
    for step in ("lint", "synth"):
        r = run_step(p, step)
        assert r.status in ("pass", "skipped"), r.summary
    assert not sentinel.exists(), sentinel.read_text()


def test_relative_liberty_settings_are_refused_with_the_reason(monkeypatch, tmp_path):
    from chipleap.rtl.synth import no_liberty

    monkeypatch.chdir(tmp_path)
    (tmp_path / "cells.lib").write_text("library (x) {}\n")
    monkeypatch.setenv("CHIPLEAP_LIBERTY", "cells.lib")
    assert tools.ihp_liberty() is None
    assert "CHIPLEAP_LIBERTY is a relative path, cells.lib" in no_liberty("ihp-sg13g2")
    monkeypatch.delenv("CHIPLEAP_LIBERTY")
    monkeypatch.setenv("PDK_ROOT", "pdk")
    assert "PDK_ROOT is a relative path, pdk" in tools.liberty_settings_problem()


def test_a_librelane_wrapper_starts_the_image_chipleap_names(monkeypatch, tmp_path):
    wrapper = tmp_path / "librelane"
    wrapper.write_text('#!/bin/sh\nexec docker run "$LIBRELANE_IMAGE" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(wrapper))
    monkeypatch.delenv(librelane_pdk.ENV, raising=False)
    monkeypatch.delenv(tools.LIBRELANE_IMAGE_ENV, raising=False)
    monkeypatch.delenv("LIBRELANE_IMAGE", raising=False)
    # Unset, the wrapper starts the release ChipLeap runs.
    assert tools.librelane().version == "3.0.14"
    assert tools.librelane_env()["LIBRELANE_IMAGE"] == tools.LIBRELANE_DEFAULT_IMAGE
    assert tools.librelane_loads_pad_views()
    # Set empty, the wrapper keeps its own image, and its version is not known.
    monkeypatch.setenv(tools.LIBRELANE_IMAGE_ENV, "")
    assert tools.librelane().version == "container wrapper"
    assert "LIBRELANE_IMAGE" not in tools.librelane_env()
    assert not tools.librelane_loads_pad_views()
    monkeypatch.setenv(tools.LIBRELANE_IMAGE_ENV, "ghcr.io/librelane/librelane:3.0.14")
    assert tools.librelane().version == "3.0.14"
    # The wrapper mounts the PDK root it is told of, so LibreLane can read it.
    (tmp_path / "pdk/ihp-sg13g2").mkdir(parents=True)
    monkeypatch.setenv(librelane_pdk.ENV, str(tmp_path / "pdk"))
    assert tools.librelane_env()["LIBRELANE_PDK_ROOT"] == str(tmp_path / "pdk")
    # A run records the version that ran it; another than the image's fails the run.
    run = tmp_path / "run"
    run.mkdir()
    (run / "resolved.json").write_text(json.dumps({"meta": {"librelane_version": "3.0.14"}}))
    assert tools.librelane_ran(run) == "3.0.14"
    (run / "resolved.json").write_text(json.dumps({"meta": {"librelane_version": "3.0.0.dev44"}}))
    with pytest.raises(ValueError, match=r"3\.0\.0\.dev44 ran, not 3\.0\.14: .* did not start"):
        tools.librelane_ran(run)


@pytest.mark.skipif(not confine.enabled(), reason="Landlock is not available")
def test_a_confined_script_reads_the_interpreter_on_the_path_it_runs_with(tmp_path):
    # SymbiYosys runs with its own directory first on PATH; its "#!/usr/bin/env python3"
    # resolves there, so that interpreter's installation must be readable.
    tool_dir, py_dir = tmp_path / "tool/bin", tmp_path / "py/bin"
    tool_dir.mkdir(parents=True)
    py_dir.mkdir(parents=True)
    script = tool_dir / "sby"
    script.write_text("#!/usr/bin/env fakepython\n")
    script.chmod(0o755)
    (py_dir / "fakepython").write_text("#!/bin/sh\n")
    (py_dir / "fakepython").chmod(0o755)
    path = os.pathsep.join([str(py_dir), "/usr/bin", "/bin"])
    with confine.confined([str(script)], [tmp_path / "project"], path) as (argv, env):
        rules = json.loads(argv[argv.index("--") - 1])
    assert env["PATH"].split(os.pathsep)[0] == str(py_dir)
    assert str(py_dir) in rules or str(py_dir.parent) in rules
