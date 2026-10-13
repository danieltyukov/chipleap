"""The steps after synthesis on a project with hard macros: each treats a macro as a box
with its ports, and a check that would need what the macro does says so or fails."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from chipleap.rtl import formal
from chipleap.rtl.project import load_project
from chipleap.verify.tools import sby, z3

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "sram"
CELL = "RM_IHPSG13_1P_256x32_c2_bm_bist"

needs_sby = pytest.mark.skipif(
    not sby().found or not z3().found, reason="needs SymbiYosys (CHIPLEAP_SBY) and z3"
)


def sram_project(root: Path, rtl_extra: str = "", **changes: object) -> Path:
    """The SRAM example in ``root``, with ``rtl_extra`` before its ``endmodule``."""
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("build", "__pycache__"))
    rtl = root / "rtl" / "scratchpad.sv"
    text = rtl.read_text()
    head, _, tail = text.rpartition("endmodule")
    rtl.write_text(head + rtl_extra + "endmodule" + tail)
    f = root / "project.yaml"
    data = yaml.safe_load(f.read_text())
    data.update(changes)
    f.write_text(yaml.safe_dump(data))
    return root


# Formal: each macro is a cut point.

PROPERTIES = """
`ifdef FORMAL
  logic past_valid = 1'b0;
  always_ff @(posedge clk_i) past_valid <= 1'b1;
  always_comb if (!past_valid) assume (!rst_ni);
  // The logic around the macro: rvalid_o follows a read request.
  always_ff @(posedge clk_i)
    if (past_valid && $past(rst_ni) && rst_ni) assert (rvalid_o == $past(req_i && !we_i));
  // What the macro does: its output holds between reads. True of IHP's SRAM, unknown of
  // a box.
  always_ff @(posedge clk_i)
    if (past_valid && !$past(req_i)) assert ($stable(rdata_o));
`endif
"""


@needs_sby
def test_formal_proves_the_logic_around_a_macro_and_leaves_the_macro_free(tmp_path):
    root = sram_project(
        tmp_path / "sram",
        PROPERTIES,
        formal={"checks": [{"name": "bmc", "mode": "bmc", "depth": 6}]},
    )
    p = load_project(root)
    boxes, cuts = formal.macro_cuts(p)
    assert [b.name for b in boxes] == [f"{CELL}.v"] and cuts == [f"cutpoint t:{CELL}"]
    r = formal.formal(p)
    assert r.status == "fail", r.summary
    (check,) = r.data["checks"]
    failures = check["failures"]
    # Only the property about the macro's output fails: the macro is a cut point, and
    # rdata_o changes without a read in the counterexample.
    assert len(failures) == 1 and failures[0]["line"] == text_line(root, "$stable(rdata_o)")
    values = failures[0]["values"]["signals"]
    assert "rdata_o" in values


def text_line(root: Path, needle: str) -> int:
    lines = (root / "rtl" / "scratchpad.sv").read_text().splitlines()
    return next(i for i, line in enumerate(lines, 1) if needle in line)
