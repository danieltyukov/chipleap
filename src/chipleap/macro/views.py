"""Readers for the text views of a macro: LEF, liberty, Verilog and SPICE.

Each reader extracts what the macro/v1 rules compare (names, directions, sizes, pin
shapes, timing arcs) and nothing more, so it stays small and tolerant of the many
dialects tools write. aispice's ``aispice_core::macropkg`` has the same readers in
Rust; a reader change goes into both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from chipleap.macro.limits import LIMITS, LimitError, Limits
from chipleap.macro.text import quote


class ViewError(ValueError):
    pass


# The views are ASCII formats. Whitespace, letters, digits, case and numbers are ASCII
# only, so a stray Unicode character means the same thing to aispice's Rust readers.
WHITESPACE = " \t\n\r\x0b\x0c"
_UPPER_CASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = str.maketrans(_UPPER_CASE, _UPPER_CASE.lower())
_UPPER = str.maketrans(_UPPER_CASE.lower(), _UPPER_CASE)
_INT = re.compile(r"[+-]?[0-9]+")
_FLOAT = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def ascii_lower(s: str) -> str:
    return s.translate(_LOWER)


def ascii_upper(s: str) -> str:
    return s.translate(_UPPER)


def words(s: str) -> list[str]:
    """Split at runs of ASCII whitespace."""
    return [w for w in re.split("[" + WHITESPACE + "]+", s) if w]


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _int(s: str) -> int | None:
    """A signed decimal that fits in 64 bits, as aispice's ``parse_int`` reads it."""
    if not _INT.fullmatch(s):
        return None
    # Leading zeros are allowed; more than 19 other digits never fit, and int() of a
    # long string is slow or refused.
    digits = s.lstrip("+-").lstrip("0")
    if len(digits) > 19:
        return None
    v = -int(digits or "0") if s.startswith("-") else int(digits or "0")
    return v if -(1 << 63) <= v < (1 << 63) else None


def _too_many_tokens(what: str, limits: Limits) -> LimitError:
    return LimitError(f"the {what} has more than {limits.tokens} tokens")


# LEF


@dataclass
class LefPin:
    name: str
    direction: str = ""  # INPUT, OUTPUT, INOUT, FEEDTHRU; "" when not given
    use: str = ""  # SIGNAL, CLOCK, ANALOG, POWER, GROUND, ...; "" when not given
    layers: list[str] = field(default_factory=list)
    shapes: int = 0


@dataclass
class LefMacro:
    name: str
    cls: str = ""
    origin: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] | None = None
    pins: dict[str, LefPin] = field(default_factory=dict)


def _lef_tokens(text: str, limits: Limits) -> list[str]:
    toks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in WHITESPACE:
            i += 1
            continue
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if len(toks) >= limits.tokens:
            raise _too_many_tokens("LEF", limits)
        if c == '"':
            j = text.find('"', i + 1)
            j = n if j < 0 else j
            toks.append(text[i + 1 : j])
            i = j + 1
        elif c == ";":
            toks.append(";")
            i += 1
        else:
            j = i
            while j < n and text[j] not in WHITESPACE and text[j] != ";":
                j += 1
            toks.append(text[i:j])
            i = j
    return toks


class _Tokens:
    def __init__(self, toks: list[str], what: str) -> None:
        self.toks = toks
        self.i = 0
        self.what = what

    def next(self) -> str:
        if self.i >= len(self.toks):
            raise ViewError(f"unexpected end of file in {self.what}")
        t = self.toks[self.i]
        self.i += 1
        return t

    def statement(self) -> list[str]:
        """The rest of a statement up to and including its semicolon."""
        out = []
        while (t := self.next()) != ";":
            out.append(t)
        return out


def _lef_number(s: str) -> float:
    if not _FLOAT.fullmatch(s):
        raise ViewError(f"expected a number, found {s!r}")
    return float(s)


def parse_lef(text: str, limits: Limits = LIMITS) -> dict[str, LefMacro]:
    """Every MACRO in a LEF file, by name. Raises :class:`ViewError` when it is malformed
    and :class:`LimitError` when it has too many tokens or a macro too many pins."""
    toks = _lef_tokens(text, limits)
    macros: dict[str, LefMacro] = {}
    i = 0
    while i < len(toks):
        if toks[i] == "MACRO" and i + 1 < len(toks):
            t = _Tokens(toks, f"MACRO {toks[i + 1]}")
            t.i = i + 2
            m = _lef_macro(t, toks[i + 1], limits)
            macros.setdefault(m.name, m)
            i = t.i
        else:
            i += 1
    return macros


def _lef_macro(t: _Tokens, name: str, limits: Limits) -> LefMacro:
    m = LefMacro(name)
    pins = 0
    while True:
        kw = t.next()
        if kw == "END":
            if t.next() != name:
                raise ViewError(f"MACRO {name} ends with the wrong name")
            return m
        if kw == "CLASS":
            m.cls = " ".join(t.statement())
        elif kw == "ORIGIN":
            args = t.statement()
            if len(args) < 2:
                raise ViewError(f"ORIGIN of {name} needs two numbers")
            m.origin = (_lef_number(args[0]), _lef_number(args[1]))
        elif kw == "SIZE":
            args = t.statement()
            if len(args) != 3 or args[1] != "BY":
                raise ViewError(f"SIZE of {name} is not 'SIZE w BY h'")
            m.size = (_lef_number(args[0]), _lef_number(args[2]))
        elif kw == "PIN":
            pins += 1
            if pins > limits.pins:
                raise LimitError(f"MACRO {quote(name)} has more than {limits.pins} pins")
            pin = _lef_pin(t, t.next())
            m.pins.setdefault(pin.name, pin)
        elif kw in ("OBS", "DENSITY"):
            _lef_skip_block(t)
        else:
            t.statement()


def _lef_pin(t: _Tokens, name: str) -> LefPin:
    p = LefPin(name)
    while True:
        kw = t.next()
        if kw == "END":
            if t.next() != name:
                raise ViewError(f"PIN {name} ends with the wrong name")
            return p
        if kw == "DIRECTION":
            # OUTPUT TRISTATE is an output.
            args = t.statement()
            p.direction = args[0] if args else ""
        elif kw == "USE":
            args = t.statement()
            p.use = args[0] if args else ""
        elif kw == "PORT":
            while True:
                kw = t.next()
                if kw == "END":
                    break
                args = t.statement()
                if kw == "LAYER" and args:
                    p.layers.append(args[0])
                elif kw in ("RECT", "POLYGON", "VIA"):
                    p.shapes += 1
        else:
            t.statement()


def _lef_skip_block(t: _Tokens) -> None:
    while t.next() != "END":
        t.statement()


# Liberty


@dataclass
class LibPin:
    name: str
    pg: bool = False
    pg_type: str = ""
    direction: str = ""
    timing: int = 0


@dataclass
class _Group:
    kind: str
    args: list[str]
    attrs: dict[str, str] = field(default_factory=dict)
    groups: list[_Group] = field(default_factory=list)


def _lib_tokens(text: str, limits: Limits) -> list[tuple[str, bool, bool]]:
    """(token, starts a line, is a quoted string) for a liberty file."""
    toks: list[tuple[str, bool, bool]] = []
    i, n = 0, len(text)
    line_start = True

    def add(tok: str, quoted: bool) -> None:
        nonlocal line_start
        if len(toks) >= limits.tokens:
            raise _too_many_tokens("liberty file", limits)
        toks.append((tok, line_start, quoted))
        line_start = False

    while i < n:
        c = text[i]
        if c == "\n":
            line_start = True
            i += 1
        elif c in WHITESPACE:
            i += 1
        elif c == "\\" and i + 1 < n and text[i + 1] in "\r\n":
            i += 1  # a line continuation: the newline does not end the line
            while i < n and text[i] in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ViewError("unterminated /* comment")
            line_start = line_start or "\n" in text[i:j]
            i = j + 2
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ViewError("unterminated string")
            add(text[i + 1 : j], True)
            i = j + 1
        elif c in "(){}:;,":
            add(c, False)
            i += 1
        else:
            j = i
            while j < n and text[j] not in WHITESPACE and text[j] not in '(){}:;,"':
                if text.startswith("/*", j) or text.startswith("//", j):
                    break
                j += 1
            add(text[i:j], False)
            i = j
    return toks


def _lib_parse(toks: list[tuple[str, bool, bool]], limits: Limits) -> list[_Group]:
    pos = 0

    def peek(k: int = 0) -> tuple[str, bool, bool] | None:
        return toks[pos + k] if pos + k < len(toks) else None

    def body(top: bool, depth: int) -> list[_Group]:
        nonlocal pos
        groups: list[_Group] = []
        attrs: dict[str, str] = {}
        while True:
            tok = peek()
            if tok is None:
                if top:
                    return groups
                raise ViewError("unexpected end of file inside a group")
            if tok[0] == "}" and not tok[2]:
                if top:
                    raise ViewError("unmatched }")
                pos += 1
                stack[-1].attrs.update(attrs)
                return groups
            if tok[0] == ";" and not tok[2]:
                pos += 1
                continue
            name = tok[0]
            pos += 1
            nxt = peek()
            if nxt is not None and nxt[0] == ":" and not nxt[2]:
                pos += 1
                value: list[str] = []
                while (t := peek()) is not None and not (
                    (t[0] in (";", "}") and not t[2]) or (t[1] and value)
                ):
                    value.append(t[0])
                    pos += 1
                if (t := peek()) is not None and t[0] == ";" and not t[2]:
                    pos += 1
                attrs[name] = " ".join(value)
            elif nxt is not None and nxt[0] == "(" and not nxt[2]:
                pos += 1
                args: list[str] = []
                cur: list[str] = []
                while True:
                    t = peek()
                    if t is None:
                        raise ViewError(f"unclosed ( after {name}")
                    pos += 1
                    if t[0] == ")" and not t[2]:
                        break
                    if t[0] == "," and not t[2]:
                        args.append(" ".join(cur))
                        cur = []
                    else:
                        cur.append(t[0])
                if cur or args:
                    args.append(" ".join(cur))
                t = peek()
                if t is not None and t[0] == "{" and not t[2]:
                    if depth >= limits.nesting:
                        raise LimitError(f"groups nest more than {limits.nesting} deep")
                    pos += 1
                    g = _Group(name, args)
                    stack.append(g)
                    g.groups = body(False, depth + 1)
                    stack.pop()
                    groups.append(g)
                elif t is not None and t[0] == ";" and not t[2]:
                    pos += 1
            else:
                raise ViewError(f"expected : or ( after {name}")

    stack: list[_Group] = [_Group("", [])]
    return body(True, 0)


def parse_liberty(
    text: str, limits: Limits = LIMITS, cell: str | None = None
) -> dict[str, dict[str, LibPin]]:
    """Pins of every cell in the library group (or only of ``cell``), by cell then pin
    bit name. The first cell of a name counts; later ones are skipped.

    Bus bits come from the bus's ``bus_type`` and from ``pin`` groups inside the bus.
    Raises :class:`ViewError` on a syntax error and :class:`LimitError` when the file
    has too many tokens, nests groups too deep or a cell declares too many pins.
    """
    groups = _lib_parse(_lib_tokens(text, limits), limits)
    lib = next((g for g in groups if g.kind == "library"), None)
    if lib is None:
        raise ViewError("no library group")
    types: dict[str, tuple[int, int]] = {}
    for g in lib.groups:
        if g.kind == "type" and g.args:
            lo, hi = _int(g.attrs.get("bit_from", "")), _int(g.attrs.get("bit_to", ""))
            # A type without integer bit_from and bit_to declares no bits.
            if lo is not None and hi is not None:
                types[g.args[0]] = (lo, hi)
    cells: dict[str, dict[str, LibPin]] = {}
    for c in lib.groups:
        if c.kind != "cell" or not c.args or c.args[0] in cells:
            continue
        if cell is not None and c.args[0] != cell:
            continue
        pins: dict[str, LibPin] = {}
        declared = 0

        def declare(n: int, cell_name: str = c.args[0]) -> None:
            # Counted before bits are made, so a bus of 2**63 bits costs nothing.
            nonlocal declared
            declared += n
            if declared > limits.pins:
                raise LimitError(f"cell {quote(cell_name)} has more than {limits.pins} pins")

        for g in c.groups:
            if g.kind == "pg_pin":
                declare(len(g.args))
                for name in g.args:
                    pins.setdefault(name, LibPin(name, pg=True, pg_type=g.attrs.get("pg_type", "")))
            elif g.kind == "pin":
                declare(len(g.args))
                for name in g.args:
                    pins.setdefault(name, _lib_pin(name, g, "", 0))
            elif g.kind == "bus" and g.args:
                direction = g.attrs.get("direction", "")
                timing = sum(1 for x in g.groups if x.kind == "timing")
                rng = types.get(g.attrs.get("bus_type", ""))
                if rng is not None:
                    lo, hi = sorted(rng)
                    declare(hi - lo + 1)
                    for i in range(lo, hi + 1):
                        bit = f"{g.args[0]}[{i}]"
                        pins.setdefault(bit, LibPin(bit, direction=direction, timing=timing))
                for inner in g.groups:
                    if inner.kind == "pin":
                        declare(len(inner.args))
                        for name in inner.args:
                            p = _lib_pin(name, inner, direction, timing)
                            if name in pins:
                                pins[name].direction = p.direction
                                pins[name].timing = p.timing
                            else:
                                pins[name] = p
        cells[c.args[0]] = pins
    return cells


def _lib_pin(name: str, g: _Group, direction: str, timing: int) -> LibPin:
    return LibPin(
        name,
        direction=g.attrs.get("direction", direction),
        timing=timing + sum(1 for x in g.groups if x.kind == "timing"),
    )


# Verilog


@dataclass
class VerilogPort:
    name: str
    direction: str = ""  # input, output, inout; "" when the module never declares it
    width: int | None = None  # None for a scalar; 0 when the range is not a literal


_DIRECTIONS = ("input", "output", "inout")
_TYPE_WORDS = {
    "wire",
    "reg",
    "logic",
    "wreal",
    "real",
    "var",
    "signed",
    "unsigned",
    "tri",
    "tri0",
    "tri1",
    "supply0",
    "supply1",
    "wand",
    "wor",
    "bit",
    "integer",
    "interconnect",
}


_VERILOG_SPECIAL = re.compile(r'[/("]')


def _verilog_clean(text: str) -> str:
    """Drop comments, attributes and preprocessor lines; keep both sides of an ifdef."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif text.startswith("(*", i) and not text.startswith("(*)", i):
            j = text.find("*)", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append('""')
            i = j + 1
        else:
            # Copy up to the next character that can start a comment, an attribute or
            # a string, rather than one character at a time.
            m = _VERILOG_SPECIAL.search(text, i + 1)
            j = n if m is None else m.start()
            out.append(text[i:j])
            i = j
    lines = "".join(out).split("\n")
    return "\n".join(" " if ln.lstrip(WHITESPACE).startswith("`") else ln for ln in lines)


def _verilog_tokens(text: str, limits: Limits) -> list[str]:
    toks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in WHITESPACE:
            i += 1
            continue
        if len(toks) >= limits.tokens:
            raise _too_many_tokens("Verilog file", limits)
        if (c.isascii() and c.isalnum()) or c in "_$`'":
            j = i
            while j < n and ((text[j].isascii() and text[j].isalnum()) or text[j] in "_$`'"):
                j += 1
            toks.append(text[i:j])
            i = j
        elif c == "\\":
            j = i
            while j < n and text[j] not in WHITESPACE:
                j += 1
            toks.append(text[i + 1 : j])
            i = j
        else:
            toks.append(c)
            i += 1
    return toks


def _range_width(toks: list[str]) -> int:
    """Width of ``[ msb : lsb ]`` given the tokens between the brackets; 0 if unknown."""
    if len(toks) == 3 and toks[1] == ":":
        msb, lsb = _int(toks[0]), _int(toks[2])
        if (
            msb is not None
            and lsb is not None
            and toks[0][0] not in "+-"
            and toks[2][0] not in "+-"
        ):
            return abs(msb - lsb) + 1
    return 0


def _types_and_range(toks: list[str], k: int, end: int) -> tuple[int, int | None]:
    """Skip net and data type words and one packed range from ``toks[k]``, looking no
    further than ``toks[end - 1]``, the end of the declaration. Returns the index of the
    name and the range's width (None without a range)."""
    width: int | None = None
    while k < end:
        if toks[k] in _TYPE_WORDS:
            k += 1
        elif toks[k] == "[" and width is None:
            try:
                j = toks.index("]", k, end)
            except ValueError:
                j = end
            width = _range_width(toks[k + 1 : j])
            k = j + 1
        else:
            break
    return k, width


def _split(toks: list[str]) -> list[list[str]]:
    """Split tokens at top-level commas (outside brackets and parentheses)."""
    items: list[list[str]] = [[]]
    depth = 0
    for t in toks:
        # Whole tokens: a lone backslash gives an empty one, and "" is in every string.
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            depth -= 1
        if depth == 0 and t == ",":
            items.append([])
        else:
            items[-1].append(t)
    return [x for x in items if x]


def _skip_parens(toks: list[str], i: int) -> int:
    """The index just after the parenthesis that closes ``toks[i]``."""
    if i >= len(toks) or toks[i] != "(":
        return i
    depth = 0
    while i < len(toks):
        depth += toks[i] == "("
        depth -= toks[i] == ")"
        i += 1
        if depth == 0:
            break
    return i


def verilog_ports(text: str, module: str, limits: Limits = LIMITS) -> list[VerilogPort] | None:
    """The ports of ``module`` in declaration order, or None if the module is absent.
    Raises :class:`LimitError` when the file has too many tokens or the module too many
    ports."""
    toks = _verilog_tokens(_verilog_clean(text), limits)
    start = next(
        (
            k
            for k in range(len(toks) - 1)
            if toks[k] in ("module", "macromodule") and toks[k + 1] == module
        ),
        None,
    )
    if start is None:
        return None
    i = start + 2
    if i < len(toks) and toks[i] == "#":
        i = _skip_parens(toks, i + 1)
    ports: list[VerilogPort] = []
    ansi = False
    if i < len(toks) and toks[i] == "(":
        end = _skip_parens(toks, i)
        items = _split(toks[i + 1 : end - 1])
        i = end
        ansi = bool(items) and items[0][0] in _DIRECTIONS
        direction, width = "", None
        for item in items:
            if ansi:
                k = 0
                if item[0] in _DIRECTIONS:
                    direction = item[0]
                    k, width = _types_and_range(item, 1, len(item))
                else:
                    # A port without a direction keyword continues the previous one.
                    k, own = _types_and_range(item, 0, len(item))
                    width = own if own is not None else width
                if k < len(item):
                    ports.append(VerilogPort(item[k], direction, width))
            elif item[0] == "." and len(item) > 1:
                ports.append(VerilogPort(item[1]))
            else:
                ports.append(VerilogPort(item[0]))
    if len(ports) > limits.pins:
        raise LimitError(f"module {quote(module)} has more than {limits.pins} ports")
    if ansi:
        return ports
    by_name = {p.name: p for p in ports}
    while i < len(toks) and toks[i] != "endmodule":
        t = toks[i]
        if t in ("function", "task"):
            # Their input and output declarations are not the module's.
            while i < len(toks) and toks[i] != "end" + t:
                i += 1
        elif t in _DIRECTIONS:
            j = i
            while j < len(toks) and toks[j] != ";":
                j += 1
            k, width = _types_and_range(toks, i + 1, j)
            for item in _split(toks[k:j]):
                if item[0] in by_name:
                    by_name[item[0]].direction = t
                    by_name[item[0]].width = width
            i = j
        i += 1
    return ports


# SPICE


@dataclass
class SpiceSubckt:
    name: str
    ports: list[str]
    pininfo: dict[str, str]  # lower-cased port -> I, O or B


def spice_subckt(text: str, name: str, limits: Limits = LIMITS) -> SpiceSubckt | None:
    """``.subckt name`` with its ports and ``*.PININFO`` directions, or None if absent.
    Raises :class:`LimitError` when the file has too many lines or the subcircuit too
    many ports."""
    count = text.count("\n") + text.count("\r") - text.count("\r\n") + 1
    if count > limits.tokens:
        raise LimitError(f"the SPICE file has more than {limits.tokens} lines")
    lines: list[str] = []
    # The pieces of the line being continued: joined once, since appending to the
    # same string again and again copies it every time.
    parts: list[str] | None = None
    for raw in _lines(text):
        s = raw.strip(WHITESPACE)
        if s.startswith("+") and parts is not None:
            parts.append(s[1:])
            continue
        if parts is not None:
            lines.append(" ".join(parts))
        parts = [s]
    if parts is not None:
        lines.append(" ".join(parts))
    for k, line in enumerate(lines):
        ws = words(_spice_code(line))
        if (
            len(ws) < 2
            or ascii_lower(ws[0]) != ".subckt"
            or ascii_lower(ws[1]) != ascii_lower(name)
        ):
            continue
        ports: list[str] = []
        for w in ws[2:]:
            if ascii_lower(w) == "params:" or "=" in w:
                break
            ports.append(w)
        if len(ports) > limits.pins:
            raise LimitError(f".subckt {quote(ws[1])} has more than {limits.pins} ports")
        info: dict[str, str] = {}
        for later in lines[k + 1 :]:
            if ascii_lower(later).startswith(".ends"):
                break
            if ascii_lower(later).startswith("*.pininfo"):
                for item in words(later)[1:]:
                    pin, _, d = item.rpartition(":")
                    if pin:
                        info[ascii_lower(pin)] = ascii_upper(d)
        return SpiceSubckt(ws[1], ports, info)
    return None


def spice_subckts(text: str, limits: Limits = LIMITS) -> list[str]:
    """The names of every ``.subckt`` of a SPICE file, in file order. Raises
    :class:`LimitError` when the file has too many lines."""
    count = text.count("\n") + text.count("\r") - text.count("\r\n") + 1
    if count > limits.tokens:
        raise LimitError(f"the SPICE file has more than {limits.tokens} lines")
    out = []
    for raw in _lines(text):
        ws = words(_spice_code(raw.strip(WHITESPACE)))
        if len(ws) >= 2 and ascii_lower(ws[0]) == ".subckt":
            out.append(ws[1])
    return out


def _spice_code(line: str) -> str:
    if line.startswith("*"):
        return ""
    for mark in (" $", "\t$", ";"):
        k = line.find(mark)
        if k >= 0:
            line = line[:k]
    return line
