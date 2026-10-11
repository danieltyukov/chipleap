"""Readers for the text views of a macro: LEF, liberty, Verilog and SPICE.

Each reader extracts what the macro/v1 rules compare (names, directions, sizes, pin
shapes, timing arcs) and nothing more, so it stays small and tolerant of the many
dialects tools write. aispice's ``aispice_core::macropkg`` has the same readers in
Rust; a reader change goes into both.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field


class ViewError(ValueError):
    pass


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


def _lef_tokens(text: str) -> list[str]:
    toks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c == '"':
            j = text.find('"', i + 1)
            j = n if j < 0 else j
            toks.append(text[i + 1 : j])
            i = j + 1
        elif c == ";":
            toks.append(";")
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] != ";":
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
    try:
        return float(s)
    except ValueError:
        raise ViewError(f"expected a number, found {s!r}") from None


def parse_lef(text: str) -> dict[str, LefMacro]:
    """Every MACRO in a LEF file, by name. Raises :class:`ViewError` when it is malformed."""
    toks = _lef_tokens(text)
    macros: dict[str, LefMacro] = {}
    i = 0
    while i < len(toks):
        if toks[i] == "MACRO" and i + 1 < len(toks):
            t = _Tokens(toks, f"MACRO {toks[i + 1]}")
            t.i = i + 2
            m = _lef_macro(t, toks[i + 1])
            macros.setdefault(m.name, m)
            i = t.i
        else:
            i += 1
    return macros


def _lef_macro(t: _Tokens, name: str) -> LefMacro:
    m = LefMacro(name)
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


def _lib_tokens(text: str) -> list[tuple[str, bool, bool]]:
    """(token, starts a line, is a quoted string) for a liberty file."""
    toks: list[tuple[str, bool, bool]] = []
    i, n = 0, len(text)
    line_start = True
    while i < n:
        c = text[i]
        if c == "\n":
            line_start = True
            i += 1
        elif c.isspace():
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
            toks.append((text[i + 1 : j], line_start, True))
            line_start = False
            i = j + 1
        elif c in "(){}:;,":
            toks.append((c, line_start, False))
            line_start = False
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '(){}:;,"':
                if text.startswith("/*", j) or text.startswith("//", j):
                    break
                j += 1
            toks.append((text[i:j], line_start, False))
            line_start = False
            i = j
    return toks


def _lib_parse(toks: list[tuple[str, bool, bool]]) -> list[_Group]:
    pos = 0

    def peek(k: int = 0) -> tuple[str, bool, bool] | None:
        return toks[pos + k] if pos + k < len(toks) else None

    def body(top: bool) -> list[_Group]:
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
                    pos += 1
                    g = _Group(name, args)
                    stack.append(g)
                    g.groups = body(False)
                    stack.pop()
                    groups.append(g)
                elif t is not None and t[0] == ";" and not t[2]:
                    pos += 1
            else:
                raise ViewError(f"expected : or ( after {name}")

    stack: list[_Group] = [_Group("", [])]
    return body(True)


def parse_liberty(text: str) -> dict[str, dict[str, LibPin]]:
    """Pins of every cell in the library group, by cell then pin bit name.

    Bus bits come from the bus's ``bus_type`` and from ``pin`` groups inside the bus.
    Raises :class:`ViewError` on a syntax error.
    """
    groups = _lib_parse(_lib_tokens(text))
    lib = next((g for g in groups if g.kind == "library"), None)
    if lib is None:
        raise ViewError("no library group")
    types: dict[str, tuple[int, int]] = {}
    for g in lib.groups:
        if g.kind == "type" and g.args:
            # A type without integer bit_from and bit_to declares no bits.
            with contextlib.suppress(KeyError, ValueError):
                types[g.args[0]] = (int(g.attrs["bit_from"]), int(g.attrs["bit_to"]))
    cells: dict[str, dict[str, LibPin]] = {}
    for cell in lib.groups:
        if cell.kind != "cell" or not cell.args:
            continue
        pins: dict[str, LibPin] = {}
        for g in cell.groups:
            if g.kind == "pg_pin":
                for name in g.args:
                    pins.setdefault(name, LibPin(name, pg=True, pg_type=g.attrs.get("pg_type", "")))
            elif g.kind == "pin":
                for name in g.args:
                    pins.setdefault(name, _lib_pin(name, g, "", 0))
            elif g.kind == "bus" and g.args:
                direction = g.attrs.get("direction", "")
                timing = sum(1 for x in g.groups if x.kind == "timing")
                rng = types.get(g.attrs.get("bus_type", ""))
                if rng is not None:
                    lo, hi = sorted(rng)
                    for i in range(lo, hi + 1):
                        bit = f"{g.args[0]}[{i}]"
                        pins.setdefault(bit, LibPin(bit, direction=direction, timing=timing))
                for inner in g.groups:
                    if inner.kind == "pin":
                        for name in inner.args:
                            p = _lib_pin(name, inner, direction, timing)
                            if name in pins:
                                pins[name].direction = p.direction
                                pins[name].timing = p.timing
                            else:
                                pins[name] = p
        cells.setdefault(cell.args[0], pins)
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
            out.append(text[i])
            i += 1
    lines = "".join(out).split("\n")
    return "\n".join(" " if ln.lstrip().startswith("`") else ln for ln in lines)


def _verilog_tokens(text: str) -> list[str]:
    toks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c.isalnum() or c in "_$`'":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$`'"):
                j += 1
            toks.append(text[i:j])
            i = j
        elif c == "\\":
            j = i
            while j < n and not text[j].isspace():
                j += 1
            toks.append(text[i + 1 : j])
            i = j
        else:
            toks.append(c)
            i += 1
    return toks


def _range_width(toks: list[str]) -> int:
    """Width of ``[ msb : lsb ]`` given the tokens between the brackets; 0 if unknown."""
    if len(toks) == 3 and toks[1] == ":" and toks[0].isdigit() and toks[2].isdigit():
        return abs(int(toks[0]) - int(toks[2])) + 1
    return 0


def _types_and_range(toks: list[str], k: int) -> tuple[int, int | None]:
    """Skip net and data type words and one packed range from ``toks[k]``.
    Returns the index of the name and the range's width (None without a range)."""
    width: int | None = None
    while k < len(toks):
        if toks[k] in _TYPE_WORDS:
            k += 1
        elif toks[k] == "[" and width is None:
            j = toks.index("]", k) if "]" in toks[k:] else len(toks)
            width = _range_width(toks[k + 1 : j])
            k = j + 1
        else:
            break
    return k, width


def _split(toks: list[str], seps: str = ",") -> list[list[str]]:
    """Split tokens at top-level separators (outside brackets and parentheses)."""
    items: list[list[str]] = [[]]
    depth = 0
    for t in toks:
        if t in "([{":
            depth += 1
        elif t in ")]}":
            depth -= 1
        if depth == 0 and t in seps:
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


def verilog_ports(text: str, module: str) -> list[VerilogPort] | None:
    """The ports of ``module`` in declaration order, or None if the module is absent."""
    toks = _verilog_tokens(_verilog_clean(text))
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
                    k, width = _types_and_range(item, 1)
                else:
                    # A port without a direction keyword continues the previous one.
                    k, own = _types_and_range(item, 0)
                    width = own if own is not None else width
                if k < len(item):
                    ports.append(VerilogPort(item[k], direction, width))
            elif item[0] == "." and len(item) > 1:
                ports.append(VerilogPort(item[1]))
            else:
                ports.append(VerilogPort(item[0]))
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
            k, width = _types_and_range(toks, i + 1)
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


def spice_subckt(text: str, name: str) -> SpiceSubckt | None:
    """``.subckt name`` with its ports and ``*.PININFO`` directions, or None if absent."""
    lines: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("+") and lines:
            lines[-1] += " " + s[1:]
        else:
            lines.append(s)
    for k, line in enumerate(lines):
        words = _spice_code(line).split()
        if len(words) < 2 or words[0].lower() != ".subckt" or words[1].lower() != name.lower():
            continue
        ports: list[str] = []
        for w in words[2:]:
            if w.lower() == "params:" or "=" in w:
                break
            ports.append(w)
        info: dict[str, str] = {}
        for later in lines[k + 1 :]:
            if later.lower().startswith(".ends"):
                break
            if later.upper().startswith("*.PININFO"):
                for item in later.split()[1:]:
                    pin, _, d = item.rpartition(":")
                    if pin:
                        info[pin.lower()] = d.upper()
        return SpiceSubckt(words[1], ports, info)
    return None


def _spice_code(line: str) -> str:
    if line.startswith("*"):
        return ""
    for mark in (" $", "\t$", ";"):
        k = line.find(mark)
        if k >= 0:
            line = line[:k]
    return line
