"""A reader for the structural Verilog that Yosys and OpenROAD write.

The implementation steps need the netlist itself, not the RTL: which flip-flops a scan
chain links and in what order, which ports a scan insertion added, and which signals
cross between power domains. Those netlists use a small part of Verilog: module headers,
port and wire declarations, ``assign`` between nets, and cell instances with named
connections. Escaped identifiers (``\\u_rx.data_q ``), buses, part selects, constants and
concatenations appear in connections; attributes, compiler directives, parameters and
defparams are skipped.

Every connection is reduced to single bits named ``name`` or ``name[i]``, so two
references to the same wire compare equal however they were written. The text is read
once, with a size limit, and every loop ends at the end of the input.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

MAX_BYTES = 64 << 20

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?(?:\*/|\Z))
  | (?P<attr>\(\*.*?(?:\*\)|\Z))
  | (?P<directive>`[^\n]*)
  | (?P<escaped>\\\S+)
  | (?P<number>\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+|\d+)
  | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<punct>[()\[\]{},;.:=#])
  | (?P<other>.)
    """,
    re.S | re.X,
)
DECLARATIONS = {"input", "output", "inout", "wire", "reg", "tri", "supply0", "supply1", "logic"}
SKIPPED = {"parameter", "localparam", "defparam", "specparam", "genvar"}


class NetlistError(ValueError):
    pass


@dataclass
class Instance:
    cell: str
    name: str
    pins: dict[str, list[str]] = field(default_factory=dict)  # pin -> bits, MSB first


@dataclass
class Module:
    name: str
    ports: list[str] = field(default_factory=list)
    directions: dict[str, str] = field(default_factory=dict)  # port -> input|output|inout
    ranges: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (msb, lsb)
    instances: list[Instance] = field(default_factory=list)
    assigns: list[tuple[list[str], list[str]]] = field(default_factory=list)

    def bits(self, name: str) -> list[str]:
        """Every bit of a declared net or port, MSB first."""
        if name not in self.ranges:
            return [name]
        msb, lsb = self.ranges[name]
        step = -1 if msb >= lsb else 1
        return [f"{name}[{i}]" for i in range(msb, lsb + step, step)]

    def port_bits(self, direction: str) -> list[str]:
        return [b for p in self.ports if self.directions.get(p) == direction for b in self.bits(p)]


@dataclass
class Netlist:
    modules: dict[str, Module] = field(default_factory=dict)

    def top(self, name: str) -> Module:
        if name not in self.modules:
            raise NetlistError(f"module {name} is not in the netlist")
        return self.modules[name]


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup or "other"
        if kind in ("ws", "comment", "attr", "directive"):
            continue
        value = m.group()
        if kind == "escaped":
            yield "ident", value[1:]
        else:
            yield kind, value


class _Parser:
    def __init__(self, text: str) -> None:
        if len(text) > MAX_BYTES:
            raise NetlistError(f"the netlist is larger than {MAX_BYTES >> 20} MB")
        self.toks = list(_tokens(text))
        self.i = 0

    def peek(self, ahead: int = 0) -> str:
        j = self.i + ahead
        return self.toks[j][1] if j < len(self.toks) else ""

    def kind(self) -> str:
        return self.toks[self.i][0] if self.i < len(self.toks) else "eof"

    def take(self) -> str:
        if self.i >= len(self.toks):
            raise NetlistError("the netlist ends in the middle of a statement")
        value = self.toks[self.i][1]
        self.i += 1
        return value

    def expect(self, value: str) -> None:
        got = self.take()
        if got != value:
            raise NetlistError(f"expected {value!r}, found {got!r}")

    def skip_to(self, end: str) -> None:
        """Skip past the next ``end`` at nesting depth zero."""
        depth = 0
        while self.i < len(self.toks):
            t = self.take()
            if t in "([{":
                depth += 1
            elif t in ")]}":
                depth -= 1
            elif t == end and depth <= 0:
                return
        raise NetlistError(f"expected {end!r} before the end of the netlist")

    def number(self) -> int:
        t = self.take()
        if not t.isdigit():
            raise NetlistError(f"expected a number, found {t!r}")
        return int(t)

    def range_(self) -> tuple[int, int] | None:
        if self.peek() != "[":
            return None
        self.take()
        msb = self.number()
        self.expect(":")
        lsb = self.number()
        self.expect("]")
        return msb, lsb

    def parse(self) -> Netlist:
        out = Netlist()
        while self.i < len(self.toks):
            if self.take() == "module":
                mod = self.module()
                out.modules[mod.name] = mod
        return out

    def module(self) -> Module:
        mod = Module(self.take())
        if self.peek() == "#":
            self.take()
            self.skip_paren()
        if self.peek() == "(":
            self.take()
            direction = ""
            rng: tuple[int, int] | None = None
            while self.peek() != ")":
                t = self.take()
                if t in ("input", "output", "inout"):
                    direction, rng = t, None
                elif t in DECLARATIONS:
                    continue
                elif t == "[":
                    self.i -= 1
                    rng = self.range_()
                elif t == ",":
                    continue
                else:
                    mod.ports.append(t)
                    if direction:
                        mod.directions[t] = direction
                        if rng is not None:
                            mod.ranges[t] = rng
            self.take()
        self.expect(";")
        while True:
            if self.i >= len(self.toks):
                raise NetlistError(f"module {mod.name} has no endmodule")
            t = self.peek()
            if t == "endmodule":
                self.take()
                return mod
            if t in DECLARATIONS:
                self.declaration(mod)
            elif t in SKIPPED:
                self.skip_to(";")
            elif t == "assign":
                self.take()
                while True:
                    lhs = self.expr(mod)
                    self.expect("=")
                    rhs = self.expr(mod)
                    mod.assigns.append((lhs, rhs))
                    if self.take() == ";":
                        break
            elif self.kind() == "ident":
                self.instance(mod)
            else:
                raise NetlistError(f"module {mod.name}: unexpected {t!r}")

    def skip_paren(self) -> None:
        self.expect("(")
        depth = 1
        while depth:
            t = self.take()
            depth += {"(": 1, ")": -1}.get(t, 0)

    def declaration(self, mod: Module) -> None:
        kind = self.take()
        rng: tuple[int, int] | None = None
        while True:
            t = self.peek()
            if t in ("signed", "wire", "reg", "logic"):
                self.take()
            elif t == "[":
                rng = self.range_()
            else:
                break
        while True:
            name = self.take()
            if kind in ("input", "output", "inout"):
                mod.directions[name] = kind
                if name not in mod.ports:
                    mod.ports.append(name)
            if rng is not None:
                mod.ranges[name] = rng
            if self.peek() == "=":  # wire x = y;
                self.take()
                rhs = self.expr(mod)
                mod.assigns.append((mod.bits(name), rhs))
            t = self.take()
            if t == ";":
                return
            if t != ",":
                raise NetlistError(f"module {mod.name}: unexpected {t!r} in a declaration")

    def instance(self, mod: Module) -> None:
        cell = self.take()
        if self.peek() == "#":
            self.take()
            self.skip_paren()
        name = self.take()
        if self.peek() == "[":
            self.range_()
        inst = Instance(cell, name)
        self.expect("(")
        index = 0
        while self.peek() != ")":
            if self.peek() == ",":
                self.take()
                continue
            if self.peek() == ".":
                self.take()
                pin = self.take()
                self.expect("(")
                inst.pins[pin] = [] if self.peek() == ")" else self.expr(mod)
                self.expect(")")
            else:
                inst.pins[str(index)] = self.expr(mod)
            index += 1
        self.take()
        self.expect(";")
        mod.instances.append(inst)

    def expr(self, mod: Module) -> list[str]:
        """The bits of a connection, MSB first."""
        if self.peek() == "{":
            self.take()
            bits: list[str] = []
            while True:
                bits += self.expr(mod)
                t = self.take()
                if t == "}":
                    return bits
                if t != ",":
                    raise NetlistError(f"unexpected {t!r} in a concatenation")
        if self.kind() == "number":
            return _constant(self.take())
        name = self.take()
        if self.peek() != "[":
            return mod.bits(name)
        self.take()
        hi = self.number()
        lo = hi
        if self.peek() == ":":
            self.take()
            lo = self.number()
        self.expect("]")
        step = -1 if hi >= lo else 1
        return [f"{name}[{i}]" for i in range(hi, lo + step, step)]


def _constant(text: str) -> list[str]:
    if "'" not in text:
        return [f"1'b{b}" for b in format(int(text), "032b")]
    width_text, _, rest = text.partition("'")
    rest = rest.lstrip("sS")
    base, digits = rest[0].lower(), rest[1:].replace("_", "").lower()
    width = int(width_text) if width_text else 32
    if base == "b":
        bits = digits
    elif base == "h" and not set(digits) & set("xz?"):
        bits = format(int(digits, 16), "b")
    elif base == "o" and not set(digits) & set("xz?"):
        bits = format(int(digits, 8), "b")
    elif base == "d" and digits.isdigit():
        bits = format(int(digits), "b")
    else:
        bits = "x" * width
    bits = bits.replace("?", "z").rjust(width, "0")[-width:]
    return [f"1'b{b}" for b in bits]


def parse_netlist(text: str) -> Netlist:
    """The modules of a structural Verilog netlist."""
    return _Parser(text).parse()


class Nets:
    """The connectivity of one module, with assignments merged: each bit belongs to a
    net, and each net knows the instance pins on it."""

    def __init__(self, mod: Module) -> None:
        self.parent: dict[str, str] = {}
        for lhs, rhs in mod.assigns:
            # Verilog lines the two sides up from the least significant bit.
            for a, b in zip(reversed(lhs), reversed(rhs), strict=False):
                self._union(a, b)
        self.pins: dict[str, list[tuple[Instance, str]]] = {}
        for inst in mod.instances:
            for pin, bits in inst.pins.items():
                for b in bits:
                    self.pins.setdefault(self.find(b), []).append((inst, pin))
        self.by_name = {inst.name: inst for inst in mod.instances}

    def find(self, bit: str) -> str:
        root = bit
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        # Path halving keeps later lookups short.
        while self.parent.get(bit, bit) != root:
            self.parent[bit], bit = root, self.parent[bit]
        return root

    def _union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def same(self, a: str, b: str) -> bool:
        return self.find(a) == self.find(b)

    def on(self, bit: str) -> list[tuple[Instance, str]]:
        return self.pins.get(self.find(bit), [])


@dataclass(frozen=True)
class Chain:
    name: str
    scan_in: str
    scan_out: str
    cells: tuple[str, ...]  # from scan-in to scan-out


def trace_chain(
    nets: Nets,
    name: str,
    scan_in: str,
    scan_out: str,
    *,
    si_pin: str = "SCD",
    q_pins: Iterable[str] = ("Q",),
    through: dict[str, tuple[str, str]] | None = None,
    limit: int = 1_000_000,
) -> Chain:
    """The flip-flops a chain links, from ``scan_in`` to ``scan_out``: each cell's scan
    input is reached from the previous cell's output. Buffers (cells whose name starts
    with a key of ``through``, mapped to their input and output pins) are followed, as
    place and route inserts them, including trees of them. Raises NetlistError when the
    chain forks, breaks or loops."""
    through = through or {}
    q_pins = tuple(q_pins)
    cells: list[str] = []
    seen: set[str] = set()
    bit = scan_in
    for _ in range(limit):
        found, at_out = _reach(nets, bit, scan_out, si_pin, through)
        after = cells[-1] if cells else scan_in
        if len(found) > 1:
            raise NetlistError(
                f"{name}: the chain forks after {after} ({', '.join(sorted(found)[:3])})"
            )
        if not found:
            if at_out:
                return Chain(name, scan_in, scan_out, tuple(cells))
            raise NetlistError(f"{name}: the chain breaks after {after}")
        inst = nets.by_name[next(iter(found))]
        if inst.name in seen:
            raise NetlistError(f"{name}: the chain loops at {inst.name}")
        seen.add(inst.name)
        cells.append(inst.name)
        outs = [inst.pins[q][0] for q in q_pins if inst.pins.get(q)]
        if not outs:
            raise NetlistError(f"{name}: {inst.name} has no {'/'.join(q_pins)} connected")
        bit = outs[0]
    raise NetlistError(f"{name}: longer than {limit} cells")


def _reach(
    nets: Nets, bit: str, scan_out: str, si_pin: str, through: dict[str, tuple[str, str]]
) -> tuple[set[str], bool]:
    """The cells whose scan input ``bit`` reaches through buffers, and whether it reaches
    ``scan_out``."""
    found: set[str] = set()
    at_out = False
    todo = [bit]
    visited: set[str] = set()
    while todo:
        b = nets.find(todo.pop())
        if b in visited:
            continue
        visited.add(b)
        at_out = at_out or nets.same(b, scan_out)
        for inst, pin in nets.on(b):
            if pin == si_pin:
                found.add(inst.name)
                continue
            for prefix, (in_pin, out_pin) in through.items():
                if inst.cell.startswith(prefix) and pin == in_pin and inst.pins.get(out_pin):
                    todo.append(inst.pins[out_pin][0])
    return found, at_out
