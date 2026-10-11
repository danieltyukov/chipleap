"""Equivalence of a project's RTL and its netlists.

Two pairs, each a question a tape-out asks:

* ``rtl-synth``: does ChipLeap's synthesized netlist (``build/synth/<top>.synth.v``)
  implement the RTL? The RTL side is the project read and synthesized to generic gates by
  the same Yosys commands synthesis runs, up to the mapping of flip-flops to the liberty
  (``chipleap.rtl.synth.front``); the netlist side is what ABC then mapped to the
  technology's cells. Yosys's front end and its generic passes (FSM recoding among them)
  are trusted, and the mapping that gives the area and timing is checked.
* ``synth-pnr``: did place and route change the logic? LibreLane's synthesized netlist
  against its final netlist, from the routed run in ``build/pnr``. LibreLane synthesizes
  again from the RTL, so its netlist, not ChipLeap's, is where routing started.

Both sides read the liberty with ``read_liberty``, which turns each cell into its logic, and
``async2sync`` models asynchronous resets for the solvers. Signals are matched by name and
proved equal by induction with Yosys's ``equiv_make``, ``equiv_simple`` and
``equiv_induct``. On the UART example they proved 752 of 752 points between LibreLane's
netlists and 74 of 75 between the RTL and ChipLeap's netlist.

EQY (``equivalence.engine: eqy``) partitions the design between matched points first. It
is offered for ``rtl-synth`` only, against the RTL as read: EQY 0.33 proved a netlist with
one gate changed equal to an RTL side made of liberty cells, while it caught the same
change against the plain RTL, and it could not partition the routed UART at all.

What induction leaves unproven (usually a register whose value is stored inverted, as a
flip-flop that resets to 1 is on a library with only resetting flip-flops) is checked
from reset instead: a miter of the two designs, reset in the first cycle, compared for
``equivalence.depth`` cycles by Yosys's SAT solver. A difference there is real, and comes
back as a counterexample from reset; none means the pair is equal for that many cycles,
which the result states rather than calling it proved.

Yosys and EQY run under ``chipleap.rtl.confine``, from scripts whose every path and name
is quoted (``chipleap.rtl.quote``).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.rtl import confine
from chipleap.rtl import quote as q
from chipleap.rtl.pnr import routed_run
from chipleap.rtl.project import Project, ProjectError
from chipleap.rtl.synth import (
    SynthError,
    front,
    liberty_for,
    prepare_verilog,
    read_commands,
    synthesize,
)
from chipleap.rtl.vcd import read_trace
from chipleap.verify.result import StepResult
from chipleap.verify.tools import eqy, yosys

PAIRS = ("rtl-synth", "synth-pnr")
# Names that place and route and ABC make up: they mean different nets in two netlists.
AUTO_NAMES = ("_*_", "net*", "clknet_*")
_UNPROVEN_RE = re.compile(r"Unproven \$equiv \S+: \\?(\S+?)_gold(?: \[(\d+)\])?\s")
_EQY_FAIL_RE = re.compile(r"Failed to prove equivalence of partition (\S+)")
_EQY_OK_RE = re.compile(r"Successfully proved equivalence of partition (\S+)")


class EquivUnavailable(RuntimeError):
    """The pair cannot be checked here: no tool, no liberty or no routed run."""


@dataclass(frozen=True)
class Side:
    label: str  # what the side is, for people
    script: list[str]  # Yosys commands that leave the side's top module in the design
    shown: list[str]  # the files it is made from, relative to the project


def _librelane_synth(run: Path, top: str) -> Path | None:
    found = sorted(run.glob(f"*-yosys-synthesis/{top}.nl.v"))
    return found[-1] if found else None


def _netlist(liberty: Path, netlist: Path) -> list[str]:
    return [
        f"read_liberty -ignore_miss_func {q.yosys(liberty)}",
        f"read_verilog {q.yosys(netlist)}",
    ]


def sides(
    project: Project,
    pair: str,
    liberty: Path,
    workdir: Path,
    gate_netlist: Path | None = None,
    engine: str = "yosys",
) -> tuple[Side, Side]:
    """The two designs of ``pair``. ``gate_netlist`` stands in for the netlist side. For
    EQY the RTL side is the RTL as read: EQY 0.33 proved a netlist with a changed gate equal
    to an RTL side made of liberty cells, and caught it against the plain RTL."""
    if engine == "eqy" and pair != "rtl-synth":
        raise EquivUnavailable(
            "EQY 0.33 compares two netlists of liberty cells unsoundly (it proved a changed "
            "gate equal); use equivalence.engine: yosys for this pair"
        )
    if pair == "rtl-synth":
        if gate_netlist is None:
            try:
                gate_netlist = synthesize(project).netlist
            except SynthError as exc:
                raise EquivUnavailable(f"synthesis failed: {exc}") from exc
        files, raw = prepare_verilog(project, workdir)
        shown = [project.rel(s) for s in project.sources]
        if engine == "eqy":
            gold = Side("the RTL", read_commands(project, files, raw), shown)
        else:
            gold = Side(
                "the RTL, synthesized up to the liberty mapping",
                [
                    *front(project, files, raw),
                    f"dfflibmap -liberty {q.yosys(liberty)}",
                    f"read_liberty -ignore_miss_func {q.yosys(liberty)}",
                ],
                shown,
            )
        gate = Side(
            "ChipLeap's synthesized netlist",
            _netlist(liberty, gate_netlist),
            [project.rel(gate_netlist)],
        )
        return gold, gate
    if pair == "synth-pnr":
        run = routed_run(project)
        synth = _librelane_synth(run, project.top) if run else None
        if run is None or synth is None:
            raise EquivUnavailable(
                "no routed run in build/pnr: run chipleap verify --steps pnr first"
            )
        final = gate_netlist or run / "final" / "nl" / f"{project.top}.nl.v"
        gold = Side(
            "LibreLane's synthesized netlist", _netlist(liberty, synth), [project.rel(synth)]
        )
        gate = Side("the routed netlist", _netlist(liberty, final), [project.rel(final)])
        return gold, gate
    raise ValueError(f"unknown pair {pair!r}; pairs: {', '.join(PAIRS)}")


def _stash(side: Side, top: str, name: str) -> list[str]:
    return [
        *side.script,
        f"prep -flatten -top {top}",
        "async2sync",
        f"rename {top} {name}",
        f"design -stash {name}",
    ]


def _both(gold: Side, gate: Side, top: str) -> list[str]:
    t = q.plain(top)
    return [
        *_stash(gold, t, "gold"),
        *_stash(gate, t, "gate"),
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
    ]


def yosys_script(gold: Side, gate: Side, top: str, seq: int) -> str:
    """Yosys's equivalence passes: match by name, prove by simple and inductive SAT."""
    return "\n".join(
        [
            *_both(gold, gate, top),
            "equiv_make gold gate equiv",
            "hierarchy -top equiv",
            f"equiv_simple -seq {int(seq)}",
            f"equiv_induct -seq {int(seq)}",
            "equiv_status",
            "",
        ]
    )


def eqy_text(gold: Side, gate: Side, top: str) -> str:
    """An EQY configuration: both designs, names that mean nothing left unmatched, and the
    sat strategy (registers matched by name, each partition proved with short induction)."""
    t = q.plain(top)
    nomatch = [f"{side}-nomatch {pat}" for pat in AUTO_NAMES for side in ("gold", "gate")]
    return "\n".join(
        [
            "[gold]",
            *gold.script,
            f"prep -top {t}",
            "async2sync",
            "",
            "[gate]",
            *gate.script,
            f"prep -top {t}",
            "async2sync",
            "",
            "[match *]",
            *nomatch,
            "",
            "[strategy sat]",
            "use sat",
            "depth 5",
            "",
        ]
    )


def miter_script(
    gold: Side, gate: Side, top: str, depth: int, resets: list[tuple[str, bool]], vcd: Path
) -> str:
    """A miter of both designs, reset in the first cycle and compared for ``depth`` cycles
    by Yosys's SAT solver; a difference is written to ``vcd``."""
    sets = [f"-set-at 1 in_{q.plain(p)} {0 if low else 1}" for p, low in resets]
    sat = ["sat", "-seq", str(int(depth)), *sets, "-set-init-undef", "-set-def-inputs"]
    sat += ["-prove", "trigger", "0", "-show-ports", "-dump_vcd", q.yosys(vcd), "miter"]
    return "\n".join(
        [
            *_both(gold, gate, top),
            "miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter",
            "hierarchy -top miter",
            "flatten",
            "opt_clean",
            " ".join(sat),
            "",
        ]
    )


def parse_yosys(log: str) -> dict[str, Any]:
    """Points matched, proved and left unproven by ``equiv_status``."""
    m = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", log)
    if m is None:
        return {"error": _first_error(log)}
    proved, unproven = int(m.group(1)), int(m.group(2))
    names = sorted({f"{n}[{i}]" if i else n for n, i in _UNPROVEN_RE.findall(log)})
    return {"points": proved + unproven, "proved": proved, "unproven": names}


def parse_eqy(log: str) -> dict[str, Any]:
    """Partitions EQY proved and failed to prove."""
    failed = sorted(set(_EQY_FAIL_RE.findall(log)))
    m = re.search(r"Failed to prove equivalence for (\d+)/(\d+) partitions", log)
    if m:
        total = int(m.group(2))
        return {"points": total, "proved": total - int(m.group(1)), "unproven": failed}
    if "Successfully proved designs equivalent" in log:
        n = len(set(_EQY_OK_RE.findall(log)))
        return {"points": n, "proved": n, "unproven": []}
    return {"error": _first_error(log)}


def parse_sat(log: str) -> str:
    """``equal``, ``differs`` or ``error`` from Yosys's ``sat -prove``."""
    if "SAT proof finished - no model found" in log:
        return "equal"
    if "SAT proof finished - model found" in log:
        return "differs"
    return "error"


def _first_error(log: str) -> str:
    errors = [ln.strip() for ln in log.splitlines() if "ERROR" in ln]
    return (errors or [ln.strip() for ln in log.strip().splitlines()[-1:]] or ["no output"])[0]


def _run(cmd: list[str], cwd: Path, reads: list[Path]) -> str:
    """A confined run; returns its output. A timeout comes back as an error line."""
    timeout = float(os.environ.get("CHIPLEAP_EQUIV_TIMEOUT", "1800"))
    with confine.confined(cmd, reads) as (argv, env):
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # EQY's make and solvers are its children
            proc.wait()
            return f"ERROR: timed out after {timeout:.0f} s (CHIPLEAP_EQUIV_TIMEOUT)\n"
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    return out or ""


def _yosys(script: str, workdir: Path, name: str, reads: list[Path]) -> str:
    tool = yosys()
    if tool.path is None:
        raise EquivUnavailable("Yosys is not installed")
    path = workdir / f"{name}.ys"
    path.write_text(script)
    out = _run([tool.path, "-q", "-l", f"{name}.log", path.name], workdir, reads)
    log = workdir / f"{name}.log"
    return (log.read_text(errors="replace") if log.is_file() else "") + out


def check_pair(project: Project, pair: str, *, gate_netlist: Path | None = None) -> dict[str, Any]:
    """Prove one pair equal, and check what the proof leaves from reset."""
    engine = project.equivalence.engine
    head: dict[str, Any] = {"pair": pair, "engine": engine}
    liberty = liberty_for(project.tech)
    if liberty is None:
        return {**head, "status": "skipped", "reason": f"no liberty file for {project.tech}"}
    if engine == "eqy" and not eqy().found:
        return {**head, "status": "skipped", "reason": "EQY is not installed (CHIPLEAP_EQY)"}
    workdir = (project.build_dir / "equiv" / pair).resolve()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        gold, gate = sides(project, pair, liberty.resolve(), workdir, gate_netlist, engine)
    except EquivUnavailable as exc:
        return {**head, "status": "skipped", "reason": str(exc)}
    head.update(gold=gold.label, gate=gate.shown[0])
    reads = [*project.roots, workdir, liberty.resolve()]
    if engine == "eqy":
        tool = eqy()
        cfg = workdir / f"{pair}.eqy"
        cfg.write_text(eqy_text(gold, gate, project.top))
        cmd = [str(tool.path), "-f", "-k", "-j", str(os.cpu_count() or 2)]
        ys = yosys()
        if ys.path:
            cmd += ["--yosys", ys.path]
        out = _run([*cmd, cfg.name], workdir, reads)
        (workdir / f"{pair}.log").write_text(out)
        proof = parse_eqy(out)
    else:
        log = _yosys(yosys_script(gold, gate, project.top, 5), workdir, "equiv", reads)
        proof = parse_yosys(log)
    if "error" in proof:
        return {**head, "status": "error", "error": proof["error"]}
    result = {**head, **proof}
    if not proof["unproven"]:
        return {**result, "status": "proved"}
    # What induction leaves, from reset: a difference here is a real one.
    depth = project.equivalence.depth
    vcd = workdir / "cex.vcd"
    resets = [(r.port, r.active_low) for r in project.resets]
    log = _yosys(
        miter_script(gold, gate, project.top, depth, resets, vcd), workdir, "bounded", reads
    )
    verdict = parse_sat(log)
    result["depth"] = depth
    if verdict == "error":
        return {**result, "status": "error", "error": _first_error(log)}
    if verdict == "equal":
        return {**result, "status": "bounded"}
    result.update(status="differs", trace=project.rel(vcd))
    try:
        text = project.read(vcd).decode(errors="replace")
        keep = ("in_", "gold_", "gate_")
        result["values"] = read_trace(text, keep=lambda n: n.startswith(keep) or n == "trigger")
    except (ProjectError, OSError):
        pass
    return result


def _pairs(project: Project) -> list[str]:
    if project.equivalence.checks:
        return list(project.equivalence.checks)
    return ["rtl-synth", *(["synth-pnr"] if routed_run(project) else [])]


def _describe(r: dict[str, Any]) -> str:
    pair = r["pair"]
    if r["status"] == "skipped":
        return f"{pair}: skipped, {r['reason']}"
    if r["status"] == "error":
        return f"{pair}: error: {r['error']}"
    if r["status"] == "proved":
        return f"{pair}: proved, all {r['points']} matched points equal"
    rest = r["points"] - r["proved"]
    shown = ", ".join(r["unproven"][:3]) + (" and more" if len(r["unproven"]) > 3 else "")
    if r["status"] == "bounded":
        return (
            f"{pair}: proved {r['proved']} of {r['points']} points; the other {rest} ({shown}) "
            f"equal for {r['depth']} cycles from reset, not proved for every cycle"
        )
    return (
        f"{pair}: the netlist differs from {r['gold']} within {r['depth']} cycles of reset "
        f"({r['trace']})"
    )


def equiv(project: Project, only: str = "") -> StepResult:
    """The equivalence step: every pair proved or equal from reset, else the first that
    differs, with its counterexample."""
    if not yosys().found:
        return StepResult("equiv", "skipped", "Yosys is not installed")
    pairs = [only] if only else _pairs(project)
    results = [check_pair(project, p) for p in pairs]
    data = {"tool": "EQY" if project.equivalence.engine == "eqy" else "Yosys", "pairs": results}
    summary = "; ".join(_describe(r) for r in results)
    ran = [r for r in results if r["status"] != "skipped"]
    if not ran:
        return StepResult("equiv", "skipped", summary, data)
    bad = [r for r in ran if r["status"] in ("differs", "error")]
    return StepResult("equiv", "fail" if bad else "pass", summary, data)
