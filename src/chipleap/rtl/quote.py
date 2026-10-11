"""Quoting for the scripts ChipLeap writes from a project: Yosys and Tcl.

The project file already refuses names and paths that a script language reads as syntax
(``chipleap.rtl.project``). Quoting here is the second layer: every value a script takes
from the project goes through one of these functions, which quote it as a single word
and raise instead of quoting anything that could still end the word or substitute into
it (quotes, backslash, braces, brackets, ``$``, ``;``, backtick, control characters).
Tool command lines need neither: they are argument vectors, and on a server
``chipleap.verify.remote`` quotes every argument with ``shlex.quote``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f"\\{}\[\]$;`]')


class QuoteError(ValueError):
    pass


def plain(text: object) -> str:
    """``text`` as a string, if no script could read anything in it as syntax."""
    s = str(text)
    bad = UNSAFE_RE.search(s)
    if bad:
        raise QuoteError(f"{s!r}: {bad.group()!r} cannot go into a tool script")
    return s


def yosys(text: object) -> str:
    """One word of a Yosys script: double-quoted, so spaces and ``#`` stay in the word."""
    return f'"{plain(text)}"'


def tcl(text: object) -> str:
    """One Tcl word: brace-quoted, so Tcl substitutes nothing inside it."""
    return "{" + plain(text) + "}"


def tcl_list(items: Iterable[object]) -> str:
    """A Tcl list of words, each brace-quoted."""
    return "{" + " ".join(tcl(i) for i in items) + "}"
