"""Run a project's cocotb tests on Icarus, Verilator or a commercial simulator.

The tests run through ``cocotb_run.py`` in a separate process, started with whichever
Python has cocotb (this one, the one ``cocotb-config`` belongs to, or
``CHIPLEAP_COCOTB_PYTHON``). The work directory gets a manifest, the build and test logs,
cocotb's xUnit results and an outcome file. On a design server (``--remote``) the
project's sources and tests are copied into the work directory first, so the run needs
nothing from this machine but cocotb on the server.

Results are deterministic for the same inputs: cocotb's random seed is the project's,
and what is returned holds no wall times and no absolute paths.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from chipleap.rtl.diag import PARSERS, Diagnostic, relative
from chipleap.rtl.project import Project
from chipleap.verify.remote import Remote, RemoteError, Step, run_steps
from chipleap.verify.result import StepResult
from chipleap.verify.tools import Tool, iverilog, verilator

OPEN = ("icarus", "verilator")
COMMERCIAL = {"xcelium": "xrun", "vcs": "vcs", "questa": "vsim"}
NAMES = {
    "icarus": "Icarus",
    "verilator": "Verilator",
    "xcelium": "Xcelium",
    "vcs": "VCS",
    "questa": "Questa",
}
# cocotb 2 needs this Verilator for its VPI scheduling.
VERILATOR_MIN = (5, 36)
RUNNER = "cocotb_run.py"


class SimError(RuntimeError):
    pass


class SimUnavailable(SimError):
    """The tests cannot run here: no cocotb, no simulator, or no tests."""


@dataclass(frozen=True)
class TestResult:
    name: str
    module: str
    status: str  # pass | fail | skip
    message: str = ""
    file: str = ""  # where it failed, else where the test is defined
    line: int = 0
    sim_time_ns: float = 0.0

    @property
    def full_name(self) -> str:
        return f"{self.module}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0, 0.0)}


@dataclass
class SimRun:
    simulator: str
    version: str
    tests: list[TestResult] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    stage: str = "done"  # the stage reached: import | build | test | done
    error: str = ""
    workdir: Path | None = None

    @property
    def failed(self) -> list[TestResult]:
        return [t for t in self.tests if t.status == "fail"]

    @property
    def ok(self) -> bool:
        return self.stage == "done" and bool(self.tests) and not self.failed


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:2])


def cocotb_python() -> str | None:
    """A Python that can import cocotb's runner."""
    env = os.environ.get("CHIPLEAP_COCOTB_PYTHON")
    if env:
        return env
    if importlib.util.find_spec("cocotb_tools") is not None:
        return sys.executable
    cfg = shutil.which("cocotb-config")
    if cfg:
        try:
            out = subprocess.run(
                [cfg, "--python-bin"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        py = out.stdout.strip()
        return py if out.returncode == 0 and py else None
    return None


def cocotb_version(python: str) -> str:
    try:
        out = subprocess.run(
            [python, "-c", "import cocotb; print(cocotb.__version__)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _open_tool(key: str) -> Tool:
    return iverilog() if key == "icarus" else verilator()


def choose(project: Project, requested: str = "", cocotb: str = "2") -> tuple[str, str]:
    """The simulator to use, and why it cannot run ("" when it can)."""
    key = requested or project.tests.simulator
    if key == "auto":
        if iverilog().found:
            key = "icarus"
        elif verilator().found:
            key = "verilator"
        else:
            return "icarus", "neither Icarus nor Verilator is installed"
    if key in OPEN:
        tool = _open_tool(key)
        if not tool.found:
            return key, f"{NAMES[key]} is not installed"
        if (
            key == "verilator"
            and _version_tuple(cocotb)[:1] >= (2,)
            and tool.version
            and _version_tuple(tool.version) < VERILATOR_MIN
        ):
            return key, (
                f"Verilator {tool.version} is older than 5.036, the oldest cocotb 2 runs with; "
                "point CHIPLEAP_VERILATOR at a newer one"
            )
        return key, ""
    if key in COMMERCIAL:
        return key, ""
    known = ", ".join([*OPEN, *COMMERCIAL])
    raise SimError(f"unknown simulator {key!r}. Known: {known}")


def _staged(project: Project, path: Path, area: str) -> str:
    """Where a project file goes in a work directory that travels to a server."""
    rel = project.rel(path)
    if Path(rel).is_absolute():
        raise SimError(
            f"{path} is outside the project directory, so it cannot be copied to a server"
        )
    return f"{area}/{rel}"


def manifest(
    project: Project,
    simulator: str,
    workdir: Path,
    *,
    coverage: bool = False,
    test_filter: str = "",
    stage: bool = False,
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The runner's manifest. With ``stage`` the sources, include directories, tests and
    golden model are copied under ``workdir/src`` and named relative to it. ``extra_args``
    and ``env`` are more simulator options and environment (gate-level simulation's)."""
    tests_dir = project.tests.dir
    if tests_dir is None:
        raise SimError("the project has no tests (add a tests section)")

    def name(p: Path) -> str:
        if stage:
            dest = _staged(project, p, "src")
            # Only the project's own files travel (Project.stage), never what a symlink
            # or a build directory under them points at.
            project.stage(p, workdir / dest)
            return dest
        return str(p)

    py_path = [name(tests_dir)]
    if project.golden is not None:
        py_path.append(name(project.golden.parent))
    build_args: list[str] = []
    if simulator == "verilator":
        # Lint is its own step; warnings must not stop the simulation.
        build_args.append("-Wno-fatal")
        if coverage:
            build_args.append("--coverage")
    # cocotb's runners for VCS and Questa ignore the timescale; their compilers take it.
    scale = "/".join(project.timescale)
    if simulator == "vcs":
        build_args.append(f"-timescale={scale}")
    elif simulator == "questa":
        build_args += ["-timescale", scale]
    build_args += list(extra_args)
    run_env = {
        "COCOTB_ANSI_OUTPUT": "0",
        # A test can read the project's parameters here, also on a netlist that has none.
        "CHIPLEAP_PARAMETERS": json.dumps(project.parameters, sort_keys=True),
        **(env or {}),
    }
    return {
        "simulator": simulator,
        "sources": [name(s) for s in (*project.sources, *project.tests.sources)],
        "includes": [name(d) for d in project.include_dirs],
        # cocotb writes NAME=VALUE; a bare define is defined as 1.
        "defines": {k: (1 if v is None else v) for k, v in project.defines.items()},
        "parameters": dict(project.parameters),
        "toplevel": project.tests.toplevel,
        "modules": list(project.tests.modules),
        "seed": project.tests.seed,
        "timescale": list(project.timescale),
        "build_args": build_args,
        "coverage": coverage and simulator == "verilator",
        "test_filter": test_filter,
        "python_path": py_path,
        "golden": name(project.golden) if project.golden is not None else "",
        "env": run_env,
    }


def _frame(traceback: str, project: Project, staged: bool) -> tuple[str, int]:
    """The deepest traceback frame inside the project: the line that failed."""
    best = ("", 0)
    for f, ln in re.findall(r'File "([^"]+)", line (\d+)', traceback):
        rel = f[f.index("/src/") + 5 :] if staged and "/src/" in f else project.rel(f)
        if not Path(rel).is_absolute() and not rel.startswith(".."):
            best = (rel, int(ln))
    return best


def parse_results(xml_text: str, project: Project, staged: bool = False) -> list[TestResult]:
    """cocotb's xUnit file: one result per test case. The file comes from the project's own
    test run, which already executes the project's Python, so it is parsed as it is."""
    root = ET.fromstring(xml_text)
    out = []
    for case in root.iter("testcase"):
        props = {
            p.get("name", ""): p.get("value", "")
            for p in case.iter("property")
            if p.get("name") is not None
        }
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        status = (
            "fail" if bad is not None else ("skip" if case.find("skipped") is not None else "pass")
        )
        message, file, line = "", "", 0
        if bad is not None:
            message = bad.get("message") or bad.get("type") or "failed"
            file, line = _frame(bad.text or "", project, staged)
        if not file and props.get("file"):
            path = props["file"]
            rel = (
                path[path.index("/src/") + 5 :] if staged and "/src/" in path else project.rel(path)
            )
            file, line = rel, int(props.get("line") or 0)
        unit = props.get("sim_time_unit", "ns")
        scale = {"fs": 1e-6, "ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}.get(unit, 1.0)
        try:
            sim_ns = round(float(props.get("sim_time_duration") or 0) * scale, 3)
        except ValueError:
            sim_ns = 0.0
        out.append(
            TestResult(
                name=case.get("name", ""),
                module=case.get("classname", ""),
                status=status,
                message=message.strip(),
                file=file,
                line=line,
                sim_time_ns=sim_ns,
            )
        )
    return out


def _tail(path: Path, lines: int = 15) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(errors="replace").strip().splitlines()[-lines:])


def _env(simulator: str) -> dict[str, str]:
    env = dict(os.environ)
    # Under pytest, cocotb's runner changes where it writes results and raises on a
    # failing test instead of returning; the runner must behave the same everywhere.
    env.pop("PYTEST_CURRENT_TEST", None)
    env["COCOTB_ANSI_OUTPUT"] = "0"
    if simulator in OPEN:
        tool = _open_tool(simulator)
        if tool.path:
            # cocotb finds the simulator on PATH; put the one ChipLeap chose first.
            env["PATH"] = str(Path(tool.path).parent) + os.pathsep + env.get("PATH", "")
    return env


def _run_local(python: str, workdir: Path, simulator: str, timeout: float) -> None:
    log = workdir / "runner.log"
    with log.open("w") as fh:
        proc = subprocess.Popen(
            [python, RUNNER, "manifest.json"],
            cwd=workdir,
            env=_env(simulator),
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout)
        except BaseException:
            # The simulator is a grandchild; stop the whole group.
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise


def run_tests(
    project: Project,
    simulator: str = "",
    *,
    coverage: bool = False,
    test_filter: str = "",
    remote: Remote | None = None,
    workdir: Path | None = None,
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> SimRun:
    """Build the project and run its cocotb tests. Raises SimError when they cannot run
    at all (no cocotb, no simulator, no tests); a failing build or test is a result."""
    if project.tests.dir is None:
        raise SimUnavailable("the project has no tests (add a tests section)")
    python = "python3" if remote is not None else cocotb_python()
    if python is None:
        raise SimUnavailable("cocotb is not installed: pip install 'chipleap[rtl]'")
    version = "" if remote is not None else cocotb_version(python)
    key, why = choose(project, simulator, version or "2")
    if why:
        raise SimUnavailable(why)
    if key in COMMERCIAL and remote is None and shutil.which(COMMERCIAL[key]) is None:
        raise SimUnavailable(
            f"{NAMES[key]} ({COMMERCIAL[key]}) is not on the path; run it on a server with "
            "--remote HOST"
        )
    tool_version = _open_tool(key).version if key in OPEN else ""
    work = project.work_dir(workdir or ("coverage" if coverage else f"sim-{key}"), empty=True)
    staged = remote is not None
    m = manifest(
        project,
        key,
        work,
        coverage=coverage,
        test_filter=test_filter,
        stage=staged,
        extra_args=extra_args,
        env=env,
    )
    (work / "manifest.json").write_text(json.dumps(m, indent=1))
    (work / RUNNER).write_text(resources.files("chipleap.rtl").joinpath(RUNNER).read_text())
    timeout = float(os.environ.get("CHIPLEAP_SIM_TIMEOUT", "1800"))
    if remote is None:
        _run_local(python, work, key, timeout)
    else:
        try:
            run_steps(
                [Step((python, RUNNER, "manifest.json"), "runner.log")], work, remote, timeout
            )
        except RemoteError as exc:
            raise SimError(str(exc)) from exc
    run = SimRun(key, tool_version, workdir=work)
    outcome_file = work / "outcome.json"
    outcome = json.loads(outcome_file.read_text()) if outcome_file.is_file() else {}
    run.stage = str(outcome.get("stage", "import"))
    run.error = str(outcome.get("error", "")) or (
        "" if outcome else _tail(work / "runner.log", 5) or "the runner did not start"
    )
    if run.stage == "import":
        raise SimError(run.error or "cocotb could not be imported")
    results = work / "results.xml"
    if run.stage != "build" and results.is_file():
        run.tests = parse_results(results.read_text(errors="replace"), project, staged)
    if run.stage == "build":
        build_log = work / "build.log"
        text = build_log.read_text(errors="replace") if build_log.is_file() else ""
        diags = PARSERS[key](text) if key in PARSERS else []
        diags = [d for d in diags if d.severity == "error"] or [
            Diagnostic(key, "error", f"the build failed: {run.error}")
        ]
        run.diagnostics = relative(diags, project.root, {"src": ""} if staged else None)
    for t in run.failed:
        run.diagnostics.append(
            Diagnostic("cocotb", "error", t.message, t.file, t.line, 0, "", t.full_name)
        )
    if run.stage != "build" and not run.tests:
        # The simulator stopped before cocotb wrote results: an elaboration error, a missing
        # licence, a crash. Its own messages say which.
        test_log = work / "test.log"
        text = test_log.read_text(errors="replace") if test_log.is_file() else ""
        found = [d for d in (PARSERS[key](text) if key in PARSERS else []) if d.severity == "error"]
        run.diagnostics += relative(found, project.root, {"src": ""} if staged else None) or [
            Diagnostic("cocotb", "error", "no test results: " + (_tail(test_log, 3) or "no log"))
        ]
    return run


def _who(run: SimRun) -> str:
    return f"{NAMES.get(run.simulator, run.simulator)}{' ' + run.version if run.version else ''}"


def sim_data(run: SimRun) -> dict[str, Any]:
    return {
        "simulator": run.simulator,
        "simulator_version": run.version,
        "stage": run.stage,
        "tests": [t.to_dict() for t in run.tests],
        "passed": sum(t.status == "pass" for t in run.tests),
        "failed": len(run.failed),
        "diagnostics": [d.to_dict() for d in run.diagnostics],
    }


def summarize(run: SimRun) -> tuple[str, str]:
    """The step's status and summary for a run."""
    who = _who(run)
    if run.stage == "build":
        first = run.diagnostics[0] if run.diagnostics else None
        return "fail", f"{who}: the design does not build" + (f": {first}" if first else "")
    if not run.tests:
        first = run.diagnostics[0] if run.diagnostics else None
        return "fail", f"{who}: no test results" + (f": {first}" if first else "")
    n = len(run.tests)
    if run.failed:
        names = ", ".join(
            f"{t.full_name}" + (f" ({t.message.splitlines()[0][:120]})" if t.message else "")
            for t in run.failed[:3]
        )
        more = f" and {len(run.failed) - 3} more" if len(run.failed) > 3 else ""
        return "fail", f"{who}: {len(run.failed)} of {n} cocotb tests fail: {names}{more}"
    skipped = sum(t.status == "skip" for t in run.tests)
    note = f", {skipped} skipped" if skipped else ""
    return "pass", f"{who}: {n - skipped} of {n} cocotb tests pass{note}"


def simulate(
    project: Project, simulator: str = "", remote: Remote | None = None, test_filter: str = ""
) -> StepResult:
    """The simulation step: the project's cocotb tests."""
    if project.tests.dir is None:
        return StepResult("sim", "skipped", "the project has no tests (add a tests section)")
    try:
        run = run_tests(project, simulator, remote=remote, test_filter=test_filter)
    except SimUnavailable as exc:
        return StepResult("sim", "skipped", str(exc).splitlines()[0])
    except SimError as exc:
        return StepResult("sim", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    status, summary = summarize(run)
    return StepResult("sim", status, summary, sim_data(run))
