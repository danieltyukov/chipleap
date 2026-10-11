"""A reader for the structural Verilog that Yosys and OpenROAD write.

The implementation steps need the netlist itself, not the RTL: which flip-flops a scan
chain links and in what order, which ports a scan insertion added, and which signals
cross between power domains. Those netlists use a small part of Verilog: module headers,
port and wire declarations, ``assign`` between nets, and cell instances with named
connections. Escaped identifiers (``\\u_rx.data_q ``), buses, part selects, constants and
concatenations appear in connections; attributes, compiler directives, parameters and
defparams are skipped.

Every connection is reduced to single bits named ``name`` or ``name[i]``, so two
references to the same wire compare equal however they were written.

A netlist may come from a project's ``build/`` directory, so the reader is bounded in
everything it builds (``Limits``): bytes across the files read, modules, instances, net
bits, the width of any one bus or constant (``[0:2**31]`` is refused before a bit is
made), and when the hierarchy is walked, its depth, the instances under the top, and
modules that contain themselves. Tokens are read as they are needed, every traversal is
a loop over an explicit stack, and every loop ends at the end of the input.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    """What one netlist may hold. The defaults are far above the designs the
    implementation steps run on (the UART has 346 cells) and far below what would
    exhaust memory."""

    bytes: int = 64 << 20  # across every file read
    modules: int = 10_000
    instances: int = 1_000_000  # in all modules, an instance array counting its width
    bits: int = 8_000_000  # net bits declared and connected
    bus_width: int = 1 << 16  # bits in one declaration, select, constant or connection
    depth: int = 64  # levels of hierarchy below the top
    flat: int = 1_000_000  # instances in the flattened hierarchy
    chain: int = 100_000  # cells on one scan chain
    digits: int = 80  # characters in one number


LIMITS = Limits()

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


@dataclass
class _Budget:
    """What the netlists read so far have used of ``limits``."""

    limits: Limits
    bytes: int = 0
    modules: int = 0
    instances: int = 0
    bits: int = 0

    def add(self, what: str, n: int = 1) -> None:
        total = getattr(self, what) + n
        setattr(self, what, total)
        cap = getattr(self.limits, what)
        if total > cap:
            noun = {"bytes": "bytes", "modules": "modules", "instances": "instances"}
            raise NetlistError(
                f"the netlist has more than {cap} {noun.get(what, 'net bits')}"
                if what != "bytes"
                else f"the netlist is larger than {cap >> 20} MB"
            )


class _Parser:
    """A recursive-descent parser without recursion over the input: every construct it
    reads is flat, and concatenations nest with an explicit stack."""

    def __init__(self, text: str, budget: _Budget) -> None:
        budget.add("bytes", len(text.encode()))
        self.budget = budget
        self.limits = budget.limits
        self.toks = _tokens(text)
        self.cur: tuple[str, str] | None = next(self.toks, None)

    def peek(self) -> str:
        return self.cur[1] if self.cur is not None else ""

    def kind(self) -> str:
        return self.cur[0] if self.cur is not None else "eof"

    def take(self) -> str:
        if self.cur is None:
            raise NetlistError("the netlist ends in the middle of a statement")
        value = self.cur[1]
        self.cur = next(self.toks, None)
        return value

    def expect(self, value: str) -> None:
        got = self.take()
        if got != value:
            raise NetlistError(f"expected {value!r}, found {got!r}")

    def skip_to(self, end: str) -> None:
        """Skip past the next ``end`` at nesting depth zero."""
        depth = 0
        while self.cur is not None:
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
        if len(t) > self.limits.digits:
            raise NetlistError(f"the number {t[:20]}... is too long")
        return int(t)

    def width(self, hi: int, lo: int, what: str) -> int:
        n = abs(hi - lo) + 1
        if n > self.limits.bus_width:
            raise NetlistError(f"{what} is {n} bits wide, more than {self.limits.bus_width}")
        return n

    def range_(self, what: str, bus: bool = True) -> tuple[int, int] | None:
        """A ``[msb:lsb]``, if one is next; a bus no wider than the limit."""
        if self.peek() != "[":
            return None
        self.take()
        msb = self.number()
        self.expect(":")
        lsb = self.number()
        self.expect("]")
        if bus:
            self.width(msb, lsb, what)
        return msb, lsb

    def parse(self, out: Netlist) -> None:
        while self.cur is not None:
            if self.take() == "module":
                mod = self.module()
                if mod.name in out.modules:
                    raise NetlistError(f"module {mod.name} is defined twice")
                self.budget.add("modules")
                out.modules[mod.name] = mod

    def declare(self, mod: Module, name: str, rng: tuple[int, int] | None) -> None:
        if rng is not None:
            mod.ranges[name] = rng
        self.budget.add("bits", self.width(*rng, name) if rng else 1)

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
                if self.peek() == "[":
                    rng = self.range_(f"a port of {mod.name}")
                    continue
                t = self.take()
                if t in ("input", "output", "inout"):
                    direction, rng = t, None
                elif t in DECLARATIONS or t in (",", "signed"):
                    continue
                else:
                    mod.ports.append(t)
                    if direction:
                        mod.directions[t] = direction
                        self.declare(mod, t, rng)
            self.take()
        self.expect(";")
        while True:
            if self.cur is None:
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
                rng = self.range_(f"a declaration in {mod.name}")
            else:
                break
        while True:
            name = self.take()
            if kind in ("input", "output", "inout"):
                mod.directions[name] = kind
                if name not in mod.ports:
                    mod.ports.append(name)
            self.declare(mod, name, rng)
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
        # An instance array counts as many instances as it is wide.
        rng = self.range_(f"instance array {name}", bus=False)
        self.budget.add("instances", abs(rng[0] - rng[1]) + 1 if rng else 1)
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
        """The bits of a connection, MSB first. Concatenations nest on an explicit stack."""
        stack: list[list[str]] = []
        while True:
            if self.peek() == "{":
                self.take()
                stack.append([])
                if len(stack) > self.limits.depth:
                    raise NetlistError("concatenations nest too deeply")
                continue
            bits = self.operand(mod)
            while True:
                if not stack:
                    return bits
                stack[-1] += bits
                if len(stack[-1]) > self.limits.bus_width:
                    raise NetlistError(
                        f"a concatenation is wider than {self.limits.bus_width} bits"
                    )
                t = self.take()
                if t == ",":
                    break
                if t != "}":
                    raise NetlistError(f"unexpected {t!r} in a concatenation")
                bits = stack.pop()

    def operand(self, mod: Module) -> list[str]:
        if self.kind() == "number":
            out = _constant(self.take(), self.limits)
        else:
            name = self.take()
            if self.peek() != "[":
                out = mod.bits(name)
            else:
                self.take()
                hi = self.number()
                lo = hi
                if self.peek() == ":":
                    self.take()
                    lo = self.number()
                self.expect("]")
                self.width(hi, lo, f"a select of {name}")
                step = -1 if hi >= lo else 1
                out = [f"{name}[{i}]" for i in range(hi, lo + step, step)]
        self.budget.add("bits", len(out))
        return out


def _constant(text: str, limits: Limits = LIMITS) -> list[str]:
    if len(text) > limits.digits:
        raise NetlistError(f"the constant {text[:20]}... is too long")
    if "'" not in text:
        return [f"1'b{b}" for b in format(int(text) & 0xFFFFFFFF, "032b")]
    width_text, _, rest = text.partition("'")
    rest = rest.lstrip("sS")
    base, digits = rest[0].lower(), rest[1:].replace("_", "").lower()
    width = int(width_text) if width_text else 32
    if not 0 < width <= limits.bus_width:
        raise NetlistError(f"the constant {text} is {width} bits wide")
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


def parse_netlist(text: str, limits: Limits = LIMITS) -> Netlist:
    """The modules of a structural Verilog netlist."""
    out = Netlist()
    _Parser(text, _Budget(limits)).parse(out)
    return out


def read_netlist(paths: Sequence[Path], limits: Limits = LIMITS) -> Netlist:
    """The modules of the netlist files ``paths``, read with one budget: the byte limit
    holds across them, and each is refused before it is read when it would exceed it."""
    budget = _Budget(limits)
    out = Netlist()
    for path in paths:
        size = path.stat().st_size
        if budget.bytes + size > limits.bytes:
            raise NetlistError(f"the netlist is larger than {limits.bytes >> 20} MB")
        with path.open("rb") as fh:
            text = fh.read(limits.bytes - budget.bytes + 1).decode(errors="replace")
        _Parser(text, budget).parse(out)
    return out


def hierarchy(nl: Netlist, top: str, limits: Limits = LIMITS) -> Iterator[tuple[str, Module]]:
    """Every module in the hierarchy under ``top`` as (instance path, module), the top as
    ``""``, depth first. A loop over an explicit stack; refuses a module that contains
    itself, directly or through others, a hierarchy deeper than ``limits.depth``, and more
    than ``limits.flat`` instances under the top in all."""
    root = nl.top(top)
    todo: list[tuple[str, Module, tuple[str, ...]]] = [("", root, (root.name,))]
    count = 0
    while todo:
        path, mod, ancestors = todo.pop()
        yield path, mod
        count += len(mod.instances)
        if count > limits.flat:
            raise NetlistError(f"the hierarchy under {top} holds more than {limits.flat} instances")
        prefix = f"{path}/" if path else ""
        for inst in reversed(mod.instances):
            child = nl.modules.get(inst.cell)
            if child is None:
                continue
            if child.name in ancestors:
                loop = " > ".join([*ancestors[ancestors.index(child.name) :], child.name])
                raise NetlistError(f"module {child.name} contains itself ({loop})")
            if len(ancestors) > limits.depth:
                raise NetlistError(f"the hierarchy under {top} is deeper than {limits.depth}")
            todo.append((prefix + inst.name, child, (*ancestors, child.name)))


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
    scan_out: str | Iterable[str],
    *,
    si_pin: str = "SCD",
    q_pins: Iterable[str] = ("Q",),
    through: dict[str, tuple[str, str]] | None = None,
    limit: int = LIMITS.chain,
) -> Chain:
    """The flip-flops a chain links, from ``scan_in`` to its scan output: each cell's scan
    input is reached from the previous cell's output. ``scan_out`` is the output bit, or
    the output bits to look for one in: scan insertion may end a chain on a functional
    output the last flip-flop already drives. Buffers (cells whose name starts with a key
    of ``through``, mapped to their input and output pins) are followed, as place and
    route inserts them, including trees of them. Raises NetlistError when the chain
    forks, breaks, loops or reaches no output."""
    through = through or {}
    q_pins = tuple(q_pins)
    outs = [scan_out] if isinstance(scan_out, str) else list(scan_out)
    targets = {nets.find(o): o for o in outs}
    cells: list[str] = []
    seen: set[str] = set()
    bit = scan_in
    for _ in range(limit):
        found, reached = _reach(nets, bit, targets, si_pin, through)
        after = cells[-1] if cells else scan_in
        if len(found) > 1:
            raise NetlistError(
                f"{name}: the chain forks after {after} ({', '.join(sorted(found)[:3])})"
            )
        if not found:
            if reached:
                # The first in the order given: a dedicated scan output, or the first port bit.
                out = next(o for o in outs if nets.find(o) in reached)
                return Chain(name, scan_in, out, tuple(cells))
            raise NetlistError(f"{name}: the chain breaks after {after}")
        inst = nets.by_name[next(iter(found))]
        if inst.name in seen:
            raise NetlistError(f"{name}: the chain loops at {inst.name}")
        seen.add(inst.name)
        cells.append(inst.name)
        q_bits = [inst.pins[q][0] for q in q_pins if inst.pins.get(q)]
        if not q_bits:
            raise NetlistError(f"{name}: {inst.name} has no {'/'.join(q_pins)} connected")
        bit = q_bits[0]
    raise NetlistError(f"{name}: longer than {limit} cells")


def _reach(
    nets: Nets,
    bit: str,
    targets: dict[str, str],
    si_pin: str,
    through: dict[str, tuple[str, str]],
) -> tuple[set[str], set[str]]:
    """The cells whose scan input ``bit`` reaches through buffers, and the target nets it
    reaches."""
    found: set[str] = set()
    reached: set[str] = set()
    todo = [bit]
    visited: set[str] = set()
    while todo:
        b = nets.find(todo.pop())
        if b in visited:
            continue
        visited.add(b)
        if b in targets:
            reached.add(b)
        for inst, pin in nets.on(b):
            if pin == si_pin:
                found.add(inst.name)
                continue
            for prefix, (in_pin, out_pin) in through.items():
                if inst.cell.startswith(prefix) and pin == in_pin and inst.pins.get(out_pin):
                    todo.append(inst.pins[out_pin][0])
    return found, reached
