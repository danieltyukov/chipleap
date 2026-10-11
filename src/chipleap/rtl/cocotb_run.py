"""Build a project's RTL and run its cocotb tests: ``python cocotb_run.py manifest.json``.

ChipLeap starts this script in a separate process with whichever Python has cocotb, here
or on a design server, so it imports nothing but cocotb and the standard library. The
manifest (written by ``chipleap.rtl.sim``) names the simulator, the sources, the top and
the tests. The script writes ``outcome.json``: the stage it reached and, if a stage
failed, why. Test results are in the xUnit file cocotb writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _write(path: Path, outcome: dict[str, Any]) -> None:
    path.write_text(json.dumps(outcome, indent=1))


def main(argv: list[str]) -> int:
    manifest = Path(argv[1] if len(argv) > 1 else "manifest.json").resolve()
    m = json.loads(manifest.read_text())
    work = manifest.parent
    outcome = work / "outcome.json"
    # Tests and the golden model import by module name; cocotb hands sys.path to the
    # simulator's Python as PYTHONPATH.
    for d in reversed(m.get("python_path", [])):
        sys.path.insert(0, str((work / d).resolve()))
    try:
        from cocotb_tools.runner import get_runner
    except ImportError as exc:
        _write(outcome, {"stage": "import", "error": f"cocotb is not installed: {exc}"})
        return 2
    runner = get_runner(m["simulator"])
    build_dir = work / m.get("build_dir", "sim_build")
    try:
        runner.build(
            sources=[str(work / s) for s in m["sources"]],
            includes=[str(work / d) for d in m.get("includes", [])],
            defines=m.get("defines", {}),
            parameters=m.get("parameters", {}),
            build_args=m.get("build_args", []),
            hdl_toplevel=m["toplevel"],
            build_dir=build_dir,
            timescale=tuple(m["timescale"]),
            always=True,
            log_file=work / "build.log",
        )
    except (Exception, SystemExit) as exc:  # the runner raises SystemExit for a missing tool
        _write(outcome, {"stage": "build", "error": str(exc)})
        return 1
    env = dict(m.get("env", {}))
    if m.get("golden"):
        env["CHIPLEAP_GOLDEN"] = str((work / m["golden"]).resolve())
    plusargs = list(m.get("plusargs", []))
    if m.get("coverage"):
        # Verilator writes its counts where this says, whichever directory it runs in.
        plusargs.append(f"+verilator+coverage+file+{work / 'coverage.dat'}")
    try:
        # The simulation runs in the build directory, where Questa keeps its work library.
        runner.test(
            test_module=m["modules"],
            hdl_toplevel=m["toplevel"],
            build_dir=build_dir,
            seed=m.get("seed"),
            plusargs=plusargs,
            extra_env=env,
            results_xml=str(work / "results.xml"),
            test_filter=m.get("test_filter") or None,
            log_file=work / "test.log",
        )
    except (Exception, SystemExit) as exc:
        _write(outcome, {"stage": "test", "error": str(exc)})
        return 1
    _write(outcome, {"stage": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
