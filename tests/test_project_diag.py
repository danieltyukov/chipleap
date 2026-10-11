"""Diagnostic parsers, on output copied from each tool."""

from pathlib import Path

from chipleap.rtl.diag import (
    Diagnostic,
    counts,
    parse_icarus,
    parse_questa,
    parse_slang,
    parse_sv2v,
    parse_vcs,
    parse_verilator,
    parse_xcelium,
    parse_yosys,
    relative,
)

VERILATOR = """\
%Warning-WIDTHTRUNC: rtl/w.sv:3:12: Operator ASSIGNW expects 4 bits on the Assign RHS, but Assign RHS's VARREF 'a' generates 8 bits.
                                 : ... note: In instance 'w'
    3 |   assign y = a;
      |            ^
                       ... For warning description see https://verilator.org/warn/WIDTHTRUNC?v=5.020
%Warning-UNUSEDSIGNAL: rtl/w.sv:1:28: Bits of signal are not used: 'a'[7:4]
%Error: rtl/bad.sv:3:1: syntax error, unexpected endmodule, expecting ',' or ';'
%Error: Cannot continue
%Error: Exiting due to 3 warning(s)
%Error: Cannot find file containing module: 'missing'
"""


def test_verilator():
    d = parse_verilator(VERILATOR)
    assert [x.code for x in d] == ["WIDTHTRUNC", "UNUSEDSIGNAL", "", ""]
    assert d[0] == Diagnostic(
        "verilator",
        "warning",
        "Operator ASSIGNW expects 4 bits on the Assign RHS, but Assign RHS's VARREF 'a' "
        "generates 8 bits.",
        "rtl/w.sv",
        3,
        12,
        "WIDTHTRUNC",
    )
    assert d[2].severity == "error" and d[2].file == "rtl/bad.sv" and d[2].line == 3
    assert d[3].file == "" and "missing" in d[3].message
    assert counts(d) == {"error": 2, "warning": 2, "note": 0}
    assert str(d[1]) == (
        "rtl/w.sv:1: warning: Bits of signal are not used: 'a'[7:4] [UNUSEDSIGNAL]"
    )


def test_slang():
    text = """\
rtl/w.sv:3:14: warning: implicit conversion truncates from 8 to 4 bits [-Wwidth-trunc]
    assign y = a;
             ~ ^
rtl/w.sv:1:8: note: declared here
rtl/bad.sv:3:1: error: expected ';'
Build failed: 1 error, 1 warning
"""
    d = parse_slang(text)
    assert [(x.severity, x.code, x.line, x.column) for x in d] == [
        ("warning", "width-trunc", 3, 14),
        ("error", "", 3, 1),
    ]


def test_icarus():
    text = """\
rtl/bad.sv:3: syntax error
I give up.
rtl/m.sv:2: error: Unknown module type: missing
rtl/m.sv:5: warning: Port 3 (q) of top expects 8 bits, got 4.
2 error(s) during elaboration.
"""
    d = parse_icarus(text)
    assert [(x.severity, x.file, x.line) for x in d] == [
        ("error", "rtl/bad.sv", 3),
        ("error", "rtl/m.sv", 2),
        ("warning", "rtl/m.sv", 5),
    ]
    assert d[1].message == "Unknown module type: missing"


def test_yosys():
    text = """\
rtl/bad.sv:3: ERROR: syntax error, unexpected TOK_ENDMODULE
Warning: wire '\\y' is assigned in a block at rtl/l.sv:4.12-4.20.
Warning: Replacing memory \\m with list of registers. See rtl/l.sv:3
ERROR: Module `\\missing' referenced in module `\\t' in cell `\\u' is not part of the design.
Warnings: 2 unique messages, 2 total
"""
    d = parse_yosys(text)
    assert [(x.severity, x.file, x.line) for x in d] == [
        ("error", "rtl/bad.sv", 3),
        ("warning", "rtl/l.sv", 4),
        ("warning", "rtl/l.sv", 3),
        ("error", "", 0),
    ]
    assert d[1].column == 12


def test_sv2v():
    d = parse_sv2v("rtl/bad.sv:3:1: Parse error: unexpected token 'endmodule' (KW_endmodule)\n")
    assert d == [
        Diagnostic(
            "sv2v",
            "error",
            "Parse error: unexpected token 'endmodule' (KW_endmodule)",
            "rtl/bad.sv",
            3,
            1,
        )
    ]
    assert parse_sv2v("sv2v: something odd\n")[0].message == "sv2v: something odd"


def test_xcelium():
    text = """\
xmvlog: *E,EXPSMC (./rtl/bad.sv,3|0): expecting a semicolon (';') [12.3.4(IEEE)].
xmelab: *W,CUVWSP (rtl/top.sv,5|20): 1 output port was not connected:
xrun: *N,BADPRF: notice only
xmelab: *F,NOTOPL: no top-level unit found, must have recursive instances.
"""
    d = parse_xcelium(text)
    assert [(x.severity, x.code, x.file, x.line, x.column) for x in d] == [
        ("error", "EXPSMC", "rtl/bad.sv", 3, 0),
        ("warning", "CUVWSP", "rtl/top.sv", 5, 20),
        ("error", "NOTOPL", "", 0, 0),
    ]


def test_vcs():
    text = """\
Error-[SE] Syntax error
  Following verilog source has syntax error :
  "rtl/bad.sv", 3: token is 'endmodule'
  endmodule
          ^

Warning-[PCWM-W] Port connection width mismatch
rtl/top.sv, 5
"u_cnt"
"""
    d = parse_vcs(text)
    assert [(x.severity, x.code, x.file, x.line, x.message) for x in d] == [
        ("error", "SE", "rtl/bad.sv", 3, "Syntax error"),
        ("warning", "PCWM-W", "rtl/top.sv", 5, "Port connection width mismatch"),
    ]


def test_questa():
    text = """\
** Error: rtl/bad.sv(3): (vlog-13069) near "endmodule": syntax error, unexpected endmodule.
** Error (suppressible): rtl/top.sv(5): (vlog-2388) 'q' already declared in this scope.
# ** Warning: (vsim-3015) rtl/top.sv(7): [PCDPC] - Port size (4) does not match connection size (8).
# ** Fatal: (vsim-3348) Bad top.
"""
    d = parse_questa(text)
    assert [(x.severity, x.code, x.file, x.line) for x in d] == [
        ("error", "vlog-13069", "rtl/bad.sv", 3),
        ("error", "vlog-2388", "rtl/top.sv", 5),
        ("warning", "vsim-3015", "rtl/top.sv", 7),
        ("error", "vsim-3348", "", 0),
    ]


def test_relative(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "rtl").mkdir(parents=True)
    d = [
        Diagnostic("icarus", "error", "x", str(root / "rtl/a.sv"), 1),
        Diagnostic("xcelium", "error", "y", "src/rtl/b.sv", 2),
        Diagnostic("yosys", "error", "z", "/elsewhere/c.sv", 3),
        Diagnostic("cocotb", "error", "w"),
    ]
    out = relative(d, root, staged={"src": ""})
    assert [x.file for x in out] == ["rtl/a.sv", "rtl/b.sv", "/elsewhere/c.sv", ""]
    assert out[0].to_dict() == {
        "tool": "icarus",
        "severity": "error",
        "message": "x",
        "file": "rtl/a.sv",
        "line": 1,
    }
