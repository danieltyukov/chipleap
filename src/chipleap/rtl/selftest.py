"""A self-test of the formal tools ChipLeap would use, for ``chipleap doctor``.

SymbiYosys and EQY drive the Yosys on the path and must come from that Yosys's own release
tag. A newer SymbiYosys runs against an older Yosys without complaint and gets answers
wrong: with Yosys 0.33, one built from SymbiYosys's main branch reports a cover its engine
reached as unreached, or leaves it out, and crashes in ``abc pdr``. Version strings cannot
tell (a build from a tag prints none), so the self-test asks questions whose answers are
known, on a three-bit counter, with the very tools and code paths the formal and
equivalence steps use:

* a cover the counter reaches in three cycles, in cover mode with ``smtbmc z3``;
* a property that holds in every cycle, proved by ``abc pdr``;
* when EQY is installed, the counter against the same counter written another way.

Every run is confined and time-limited like the steps' own, in a temporary directory,
never in a project.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.project import FormalCheck, load_project

TOP = "chipleap_selftest"
# The counter counts 0 to 7 and wraps: "reach" is reached in step 3, and "holds" holds in
# every cycle, which a proof needs induction for.
DESIGN = f"""\
module {TOP} (input logic clk, input logic rst, output logic [2:0] q);
  logic [2:0] count = 3'd0;
  always_ff @(posedge clk) count <= rst ? 3'd0 : count + 3'd1;
  assign q = count;
`ifdef FORMAL
  always_comb reach: cover (count == 3'd3);
  always_comb holds: assert (count <= 3'd7);
`endif
endmodule
"""
# The same counter written another way, which EQY has to prove equal: comparing a design
# with itself, or a design with no outputs, leaves it nothing to prove.
GATE = f"""\
module {TOP} (input logic clk, input logic rst, output logic [2:0] q);
  logic [2:0] count = 3'd0;
  always_ff @(posedge clk)
    if (rst || count == 3'd7) count <= 3'd0;
    else count <= count + 3'd1;
  assign q = count;
endmodule
"""
PROJECT = f"""\
schema: project/v1
name: {TOP}
top: {TOP}
sources: [{TOP}.sv]
"""
TIMEOUT_S = 60.0
HINT = (
    "set {var} to a build from the tag of the Yosys on the path, which "
    "scripts/install_formal_tools.sh makes"
)


@dataclass(frozen=True)
class Check:
    name: str  # what was asked: "sby cover", "sby abc pdr", "eqy"
    tool: str  # the binary that answered
    ok: bool
    detail: str  # what it answered, or why it is wrong

    @property
    def message(self) -> str:
        if self.ok:
            return f"{self.name}: {self.detail}"
        var = "CHIPLEAP_EQY" if self.name == "eqy" else "CHIPLEAP_SBY"
        hint = "" if var in self.detail else f"; {HINT.format(var=var)}"
        return f"{self.name} with {self.tool}: {self.detail}{hint}"


def formal_selftest(timeout: float = TIMEOUT_S) -> list[Check]:
    """Run the self-test with the SymbiYosys and EQY ChipLeap would use; one result per
    question. Empty when SymbiYosys is not installed."""
    from chipleap.rtl import formal
    from chipleap.verify.tools import eqy, sby

    tool = sby()
    if tool.path is None:
        return []
    with tempfile.TemporaryDirectory(prefix="chipleap-selftest-") as tmp:
        root = Path(tmp) / TOP
        builddir.write_text(Path(tmp), f"{TOP}/{TOP}.sv", DESIGN)
        builddir.write_text(Path(tmp), f"{TOP}/project.yaml", PROJECT)
        project = load_project(root)
        work = project.work_dir("formal", empty=True)
        answers = [
            _cover(formal.run_check(project, _check("cover", "cover"), work, timeout=timeout)),
            _proof(
                formal.run_check(project, _check("pdr", "prove", "abc pdr"), work, timeout=timeout)
            ),
        ]
        results = [
            Check(name, tool.path, ok, detail)
            for name, (ok, detail) in zip(SBY_CHECKS, answers, strict=True)
        ]
        e = eqy()
        if e.path is not None:
            ok, detail = _equivalence(root, project.work_dir("equiv", empty=True), e.path, timeout)
            results.append(Check("eqy", e.path, ok, detail))
    return results


SBY_CHECKS = ("sby cover", "sby abc pdr")


def _check(name: str, mode: str, engine: str = "smtbmc z3") -> FormalCheck:
    return FormalCheck(name, mode, 8, "", engine)


def _cover(r: dict[str, object]) -> tuple[bool, str]:
    covers = r.get("covers") or []
    assert isinstance(covers, list)
    reached = [c["property"] for c in covers if c.get("reached")]
    if r.get("status") == "pass" and reached == [f"{TOP}.reach"]:
        return True, "a reachable cover is reached"
    if r.get("status") == "error":
        return False, f"error: {r.get('error', '')}"
    return False, (
        f"a cover reachable in three cycles came out {r.get('status')} with "
        f"{len(reached)} of {len(covers)} covers reached; the right answer is 1 of 1"
    )


def _proof(r: dict[str, object]) -> tuple[bool, str]:
    if r.get("status") == "pass":
        return True, "a property that always holds is proved"
    if r.get("status") == "error":
        return False, f"error: {r.get('error', '')}"
    return False, f"a property that always holds came out {r.get('status')}, not proved"


def _equivalence(root: Path, work: Path, tool: str, timeout: float) -> tuple[bool, str]:
    """The counter against the same counter written another way, as the equivalence step
    runs EQY."""
    from chipleap.rtl import equiv
    from chipleap.verify.tools import yosys

    gate_file = builddir.write_text(root, "gate.sv", GATE)
    gold = equiv.Side("the counter", [f"read_verilog -sv {q.yosys(root / f'{TOP}.sv')}"], [])
    gate = equiv.Side("the counter rewritten", [f"read_verilog -sv {q.yosys(gate_file)}"], [])
    cfg = builddir.write_text(work, "selftest.eqy", equiv.eqy_text(gold, gate, TOP))
    cmd = [tool, "-f", "-k", "-j", str(min(4, os.cpu_count() or 2))]
    ys = yosys()
    if ys.path:
        cmd += ["--yosys", ys.path]
    out = equiv._run([*cmd, cfg.name], work, [root, work], timeout)
    proof = equiv.parse_eqy(out)
    if "error" in proof:
        return False, f"error: {proof['error']}"
    if proof["unproven"] or not proof["points"]:
        return False, (
            f"two equal designs came out with {proof['proved']} of {proof['points']} "
            "points proved; the right answer is all of them, at least one"
        )
    return True, f"two equal designs are proved equal ({proof['points']} points)"
