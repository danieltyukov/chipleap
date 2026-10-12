"""Counterexample traces from VCD files, as data an assistant can read.

SymbiYosys (through yosys-smtbmc) and EQY write their counterexamples as VCD. A waveform
viewer is for people; an assistant needs the values: the signals of the top scope, one
value per step. yosys-smtbmc counts steps in a root variable ``smt_step`` and toggles the
clock between them, so a step is a time at which that counter changes. A file without it
(EQY's) has one step per timestamp.

Values are integers when every bit is 0 or 1, else the bit string (``xx01``). Signals
whose names hold ``$`` are Yosys's own and left out, as are nested scopes (the witness
scope among them). Large traces are cut to ``max_signals`` and ``max_steps`` and say so.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

STEP_VAR = "smt_step"


def _value(bits: str) -> int | str:
    return int(bits, 2) if bits and set(bits) <= {"0", "1"} else bits.lower()


def read_trace(
    text: str,
    *,
    keep: Callable[[str], bool] | None = None,
    max_signals: int = 64,
    max_steps: int = 200,
) -> dict[str, Any]:
    """The top scope's signals, one value per step: ``{"steps": n, "signals": {name:
    [values]}}``, plus ``"truncated": true`` when signals or steps were cut."""
    names: dict[str, str] = {}  # VCD id -> signal name, top scope only, in order
    step_id = ""
    depth = 0
    lines = text.splitlines()
    body = len(lines)
    for i, raw in enumerate(lines):
        words = raw.split()
        if not words:
            continue
        if words[0] == "$scope":
            depth += 1
        elif words[0] == "$upscope":
            depth -= 1
        elif words[0] == "$var" and len(words) >= 5:
            ident, name = words[3], words[4].lstrip("\\")
            if depth == 0 and name == STEP_VAR:
                step_id = ident
            elif depth == 1 and "$" not in name and (keep is None or keep(name)):
                names.setdefault(ident, name)
        elif words[0] == "$enddefinitions":
            body = i + 1
            break
    order = list(dict.fromkeys(names.values()))
    truncated = len(order) > max_signals
    order = order[:max_signals]
    current: dict[str, int | str] = {}
    steps: list[dict[str, int | str]] = []
    changed = False  # whether the step counter moved at the current time

    def close() -> None:
        if changed or not step_id:
            steps.append(dict(current))

    started = False
    for raw in lines[body:]:
        line = raw.strip()
        if not line or line.startswith("$"):
            continue
        if line[0] == "#":
            if started:
                close()
            started, changed = True, False
            continue
        if line[0] in "bBrR":
            parts = line[1:].split()
            if len(parts) != 2:
                continue
            bits, ident = parts
        else:
            bits, ident = line[0], line[1:]
        if ident == step_id:
            changed = True
        elif ident in names:
            current[names[ident]] = _value(bits)
    if started:
        close()
    truncated = truncated or len(steps) > max_steps
    shown = steps[:max_steps]
    out: dict[str, Any] = {
        "steps": len(steps),
        "signals": {n: [s.get(n, "x") for s in shown] for n in order if any(n in s for s in shown)},
    }
    if truncated:
        out["truncated"] = True
    return out
