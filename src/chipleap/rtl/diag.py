"""Structured diagnostics from the tools a project runs through.

Every front end reports problems in its own words. An assistant fixing RTL in a loop
needs them in one shape: the tool, the severity, the file and line, the tool's code for
the message and, for a failing test, the test's name. The parsers here read each tool's
plain output (the formats below were taken from the tools themselves) and keep the order
the tool reported in, without repeats.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

SEVERITIES = ("error", "warning", "note")


@dataclass(frozen=True)
class Diagnostic:
    tool: str
    severity: str  # error | warning | note
    message: str
    file: str = ""
    line: int = 0
    column: int = 0
    code: str = ""  # the tool's own name for the message, e.g. WIDTHTRUNC
    test: str = ""  # the failing test, for test failures

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0)}

    def where(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    def __str__(self) -> str:
        loc = self.where()
        code = f" [{self.code}]" if self.code else ""
        test = f" in {self.test}" if self.test else ""
        return f"{loc + ': ' if loc else ''}{self.severity}{test}: {self.message}{code}"


def _unique(diags: Iterable[Diagnostic]) -> list[Diagnostic]:
    return list(dict.fromkeys(diags))


def _num(text: str | None) -> int:
    return int(text) if text else 0


_VERILATOR = re.compile(r"^%(Warning|Error)(?:-([A-Z0-9_]+))?: (?:(\S+?):(\d+):(?:(\d+):)? )?(.*)$")


def parse_verilator(text: str, tool: str = "verilator") -> list[Diagnostic]:
    """``%Warning-CODE: file:line:col: message`` and ``%Error: ...`` lines."""
    out = []
    for line in text.splitlines():
        m = _VERILATOR.match(line.rstrip())
        if not m:
            continue
        kind, code, file, ln, col, msg = m.groups()
        if msg.startswith(("Exiting due to", "Cannot continue")):
            continue
        out.append(
            Diagnostic(
                tool,
                "error" if kind == "Error" else "warning",
                msg.strip(),
                file or "",
                _num(ln),
                _num(col),
                code or "",
            )
        )
    return _unique(out)


_SLANG = re.compile(
    r"^(\S+?):(\d+):(\d+): (fatal error|error|warning|note): (.*?)(?: \[-W([\w-]+)\])?$"
)


def parse_slang(text: str) -> list[Diagnostic]:
    """``file:line:col: warning: message [-Wcode]``; notes are left out, they explain the
    diagnostic before them."""
    out = []
    for line in text.splitlines():
        m = _SLANG.match(line.rstrip())
        if not m or m.group(4) == "note":
            continue
        file, ln, col, sev, msg, code = m.groups()
        out.append(
            Diagnostic(
                "slang",
                "warning" if sev == "warning" else "error",
                msg,
                file,
                int(ln),
                int(col),
                code or "",
            )
        )
    return _unique(out)


_HDL_FILE = r"\S+?\.(?:sv|v|svh|vh|vlt|inc|h)"
_ICARUS = re.compile(rf"^({_HDL_FILE}):(\d+): (?:(error|warning|sorry): )?(.*)$")


def parse_icarus(text: str) -> list[Diagnostic]:
    """``file:line: error: message`` and ``file:line: syntax error``."""
    out = []
    for line in text.splitlines():
        m = _ICARUS.match(line.rstrip())
        if not m:
            continue
        file, ln, sev, msg = m.groups()
        severity = "warning" if sev == "warning" else "error"
        out.append(Diagnostic("icarus", severity, msg.strip(), file, int(ln)))
    return _unique(out)


_YOSYS_AT = re.compile(rf"^({_HDL_FILE}):(\d+): ERROR: (.*)$")
_YOSYS_LOC = re.compile(rf"({_HDL_FILE}):(\d+)(?:\.(\d+))?")


def parse_yosys(text: str) -> list[Diagnostic]:
    """``ERROR: ...``, ``file:line: ERROR: ...`` and ``Warning: ...``, with the first
    ``file:line`` in a message taken as its location."""
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _YOSYS_AT.match(line)
        if m:
            out.append(Diagnostic("yosys", "error", m.group(3), m.group(1), int(m.group(2))))
            continue
        for prefix, sev in (("ERROR: ", "error"), ("Warning: ", "warning")):
            if line.startswith(prefix):
                msg = line[len(prefix) :].strip()
                loc = _YOSYS_LOC.search(msg)
                out.append(
                    Diagnostic(
                        "yosys",
                        sev,
                        msg,
                        loc.group(1) if loc else "",
                        int(loc.group(2)) if loc else 0,
                        _num(loc.group(3)) if loc else 0,
                    )
                )
    return _unique(out)


_SV2V = re.compile(r"^(?:sv2v: )?(\S+?):(\d+):(\d+): (.*)$")


def parse_sv2v(text: str) -> list[Diagnostic]:
    """``file:line:col: Parse error: message``."""
    out = []
    for line in text.splitlines():
        m = _SV2V.match(line.rstrip())
        if m:
            out.append(
                Diagnostic(
                    "sv2v", "error", m.group(4), m.group(1), int(m.group(2)), int(m.group(3))
                )
            )
    if not out and text.strip():
        out.append(Diagnostic("sv2v", "error", text.strip().splitlines()[-1]))
    return out


_XCELIUM = re.compile(r"^\s*(\w+): \*([EWFN]),(\w+)(?: \(([^,()]+),(\d+)\|(\d+)\))?: (.*)$")


def parse_xcelium(text: str) -> list[Diagnostic]:
    """``xmvlog: *E,CODE (file,line|col): message``."""
    out = []
    for line in text.splitlines():
        m = _XCELIUM.match(line.rstrip())
        if not m:
            continue
        _, sev, code, file, ln, col, msg = m.groups()
        if sev == "N":
            continue
        out.append(
            Diagnostic(
                "xcelium",
                "warning" if sev == "W" else "error",
                msg.strip(),
                (file or "").removeprefix("./"),
                _num(ln),
                _num(col),
                code,
            )
        )
    return _unique(out)


_VCS_HEAD = re.compile(r"^(Error|Warning|Lint)-\[([\w-]+)\] (.*)$")
_VCS_LOC = re.compile(rf'^\s*"?({_HDL_FILE})"?,\s*(\d+)')


def parse_vcs(text: str) -> list[Diagnostic]:
    """``Error-[CODE] title`` followed within a few lines by ``"file", line``."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _VCS_HEAD.match(line.rstrip())
        if not m:
            continue
        sev, code, title = m.groups()
        file, ln = "", 0
        for nxt in lines[i + 1 : i + 6]:
            loc = _VCS_LOC.match(nxt)
            if loc:
                file, ln = loc.group(1), int(loc.group(2))
                break
        out.append(
            Diagnostic(
                "vcs", "error" if sev == "Error" else "warning", title.strip(), file, ln, 0, code
            )
        )
    return _unique(out)


_QUESTA = re.compile(
    r"^(?:# )?\*\* (Error|Warning|Fatal)(?: \(suppressible\))?: "
    r"(?:\(([\w-]+)\) )?(?:(\S+?)\((\d+)\): )?(?:\(([\w-]+)\) )?(.*)$"
)


def parse_questa(text: str) -> list[Diagnostic]:
    """``** Error: file(line): (vlog-NNNN) message``, also with ``# `` in front."""
    out = []
    for line in text.splitlines():
        m = _QUESTA.match(line.rstrip())
        if not m:
            continue
        sev, code1, file, ln, code2, msg = m.groups()
        out.append(
            Diagnostic(
                "questa",
                "warning" if sev == "Warning" else "error",
                msg.strip(),
                file or "",
                _num(ln),
                0,
                code1 or code2 or "",
            )
        )
    return _unique(out)


PARSERS = {
    "verilator": parse_verilator,
    "slang": parse_slang,
    "icarus": parse_icarus,
    "yosys": parse_yosys,
    "sv2v": parse_sv2v,
    "xcelium": parse_xcelium,
    "vcs": parse_vcs,
    "questa": parse_questa,
}


def relative(
    diags: Iterable[Diagnostic], root: Path, staged: dict[str, str] | None = None
) -> list[Diagnostic]:
    """File names relative to ``root``, the project; ``staged`` maps directories a run
    copied the sources into back to their place in the project."""
    root = root.resolve()
    out = []
    for d in diags:
        f = d.file
        for prefix, dest in (staged or {}).items():
            if f.startswith(prefix.rstrip("/") + "/"):
                f = (dest.rstrip("/") + "/" if dest else "") + f[len(prefix.rstrip("/")) + 1 :]
                break
        if f and Path(f).is_absolute():
            with contextlib.suppress(ValueError):  # outside the project: keep it absolute
                f = Path(f).resolve().relative_to(root).as_posix()
        out.append(replace(d, file=f))
    return out


def counts(diags: Iterable[Diagnostic]) -> dict[str, int]:
    out = {s: 0 for s in SEVERITIES}
    for d in diags:
        out[d.severity] = out.get(d.severity, 0) + 1
    return out
