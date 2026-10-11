"""Limits that keep a check of a package from someone else small and quick.

A package is untrusted input: a check reads every file in it, and a file built to be
large, deeply nested or self-referencing must end in a finding, never in a hang, a
crash or a run out of memory. Each limit is far above what a real macro needs. The
spec (docs/macro-package.md, "Limits") lists them; aispice's validator uses the same
values (``aispice_core::macropkg::Limits``), so both tools report the same findings.
Tests pass smaller limits to reach them with small files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MIB = 1 << 20


@dataclass(frozen=True)
class Limits:
    # File sizes in bytes, checked before a file is read.
    manifest_bytes: int = 4 * MIB
    specs_bytes: int = 1 * MIB
    evidence_bytes: int = 4 * MIB
    liberty_bytes: int = 16 * MIB
    view_bytes: int = 64 * MIB  # LEF, Verilog and SPICE
    gds_bytes: int = 256 * MIB
    # Tables, arrays and liberty groups nested inside each other.
    nesting: int = 64
    # Pin bits in macro.toml, pins in each view.
    pins: int = 10_000
    corners: int = 64
    evidence_files: int = 64
    # Package files the evidence inputs name, each hashed once.
    hashed_files: int = 32
    # Tokens of a LEF, liberty or Verilog file; lines of a SPICE file.
    tokens: int = 8_000_000
    gds_records: int = 32_000_000
    gds_elements: int = 8_000_000
    gds_cells: int = 100_000
    gds_depth: int = 64
    # Points measured for references at angles other than quarter turns.
    gds_points: int = 4_000_000
    findings: int = 1_000


LIMITS = Limits()


class LimitError(ValueError):
    """A file goes past one of the limits; the message says which, in spec wording."""


class FileTooLarge(LimitError):
    """A file is larger than the limit for its kind."""


def read_capped(path: Path, limit: int) -> bytes:
    """A file's bytes, after its size says it is within ``limit``. Raises
    :class:`FileTooLarge` otherwise, and ``OSError`` when it cannot be read."""
    size = path.stat().st_size
    if size <= limit:
        with path.open("rb") as f:
            data = f.read(limit + 1)
        if len(data) <= limit:
            return data
        size = max(size, len(data))  # it grew after the stat
    raise FileTooLarge(too_large(size, limit))


def too_large(size: int, limit: int) -> str:
    return f"file is {size} bytes, larger than the limit of {limit} bytes"


def toml_depth(text: str) -> int:
    """An upper bound on how deeply a TOML document nests tables and arrays, from its
    brackets, braces, table headers and dotted keys, without parsing it.

    Python's TOML reader recurses on nested arrays and slows down quadratically on long
    dotted keys, and the toml crate stops at 80 levels, so both tools check this bound
    against ``Limits.nesting`` before parsing. aispice computes it the same way.

    A value sits below the last table header, below each open array or inline table,
    and below every part of the dotted keys that lead to it, so the bound adds those up;
    a dot in a number adds one more, which only overestimates."""
    deepest = base = dots = pending = 0
    # For each open array or inline table, the dots of the key it is the value of.
    keys: list[int] = []
    key_dots = 0  # sum of keys
    line_start = True
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "\"'":
            i = _toml_string_end(text, i)
            line_start = False
            continue
        if c == "\n":
            line_start = True
            dots = 0
            if not keys:
                pending = 0
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "[" and not keys and line_start:
            # A table header, [a.b] or [[a.b]]: the keys below it nest under it.
            array = text.startswith("[[", i)
            i += 2 if array else 1
            header_dots = 0
            while i < n and text[i] not in "]\n":
                if text[i] in "\"'":
                    i = _toml_string_end(text, i)
                    continue
                header_dots += text[i] == "."
                i += 1
            for _ in range(2 if array else 1):
                if i < n and text[i] == "]":
                    i += 1
            base = header_dots + (2 if array else 1)
            deepest = max(deepest, base)
            line_start = False
            dots = pending = 0
            continue
        line_start = False
        if c in "[{":
            keys.append(pending)
            key_dots += pending
            dots = pending = 0
        elif c in "]}":
            if keys:
                key_dots -= keys.pop()
            dots = pending = 0
        elif c == "=":
            pending, dots = dots, 0
        elif c == ",":
            dots = pending = 0
        elif c == ".":
            dots += 1
        deepest = max(deepest, base + len(keys) + key_dots + pending + dots + 1)
        i += 1
    return deepest


def _toml_string_end(text: str, i: int) -> int:
    """The index just after the string that starts at ``text[i]``. An unterminated
    one-line string ends at the line end, an unterminated multi-line one at the end."""
    q = text[i]
    n = len(text)
    triple = text.startswith(q * 3, i)
    j = i + (3 if triple else 1)
    while j < n:
        c = text[j]
        if c == "\\" and q == '"':
            j += 2
            continue
        if c == "\n" and not triple:
            return j
        if c == q:
            if not triple:
                return j + 1
            if text.startswith(q * 3, j):
                # One or two more quotes just inside the closing delimiter are content.
                k = j + 3
                while k < n and k < j + 5 and text[k] == q:
                    k += 1
                return k
        j += 1
    return n


_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"?|[\[\]{}]', re.S)


def json_depth(text: str) -> int:
    """How deeply a JSON document nests objects and arrays, from its brackets outside
    strings. Python's JSON reader recurses; serde_json stops at 128 levels."""
    depth = deepest = 0
    for m in _JSON_TOKEN.finditer(text):
        t = m.group()
        if t in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif t in "]}":
            depth = max(depth - 1, 0)
    return deepest
