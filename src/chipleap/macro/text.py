"""Formatting shared by every macro package message.

aispice's validator (Rust) formats the same values the same way, so both tools print
identical messages. Change these together with ``aispice_core::macropkg::text``.
"""

from __future__ import annotations

import json
from typing import Any


def fmt_g(x: float) -> str:
    """A number as C's ``%.6g`` prints it: 20 -> "20", 1.5e6 -> "1.5e+06"."""
    return format(float(x), ".6g")


def quote(s: str) -> str:
    """A name or value from a file, in double quotes with control characters escaped."""
    return json.dumps(s, ensure_ascii=False)


def plural(n: int, word: str, words: str | None = None) -> str:
    return f"{n} {word if n == 1 else (words or word + 's')}"


def describe(value: Any) -> str:
    """A TOML or JSON value in a message."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, (int, float)):
        return fmt_g(value)
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    return "a date or time"


def join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key
