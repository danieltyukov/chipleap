"""Clock and reset domain crossings, checked on the netlist Yosys elaborates.

No open-source CDC tool fits this flow, so ChipLeap checks the structural rules
commercial tools apply first. Yosys elaborates the project (flattened, processes turned
into flip-flops and gates, enables folded into the flip-flops, memories kept whole, then
mapped to single-bit gates) and writes it as JSON; this module reads it.

Clock domains: each flip-flop's clock pin is traced back through buffers, inverters and
gating to the ports it comes from. A declared clock port is the domain; a clock that comes
from a flip-flop (a divided clock) or an undeclared port is a domain of its own, with a
warning. Data inputs belong to the first clock unless ``cdc.input_domains`` names another;
``cdc.async_inputs`` are domains of their own. Resets the project declares synchronous
belong to the first clock, asynchronous ones to their own domain, and resets listed in
``cdc.reset_synchronized`` to every domain.

Each flip-flop's data, enable and synchronous reset inputs are traced back to the state
elements and inputs they depend on. A source in another domain is a crossing, and its
structure decides what it is:

* ``synchronizer``: the destination samples the source directly and feeds only another
  flip-flop of its domain, which may feed another (two flops or more).
* ``logic_before_synchronizer`` (error): the same, with gates between, which can glitch.
* ``gray_synchronizer`` or ``multibit_synchronizer`` (error): several bits of one register
  each through a synchronizer. Accepted when the register is gray coded (each bit the XOR
  of two neighbouring bits of one signal, the top one a copy); otherwise the bits can be
  captured in different cycles.
* ``handshake``: the destination captures through its enable, or through a multiplexer that
  otherwise keeps its value, and the enable or select depends on a synchronizer's output.
* ``fifo_memory``: the source is a memory written in one domain and read with an address
  from the reading domain only, as in an asynchronous FIFO, whose pointers the rules above
  check.
* ``unsynchronized`` (error): anything else.

Resets: an asynchronous reset pin is traced to its ports. A reset synchronizer (flops reset
by the port, the first with a constant input, the last resetting others) is recognized; a
flip-flop reset straight from a port that is not in ``cdc.reset_synchronized`` gets a
``reset_release`` warning, since the reset is released asynchronously to its clock. A
crossing into a flip-flop whose asynchronous reset does not include every reset of its
source is a ``reset_crossing`` (error): asserting that reset alone changes the source
while the destination keeps sampling. Destinations without a reset are not checked.

Not checked: reconvergence of separately synchronized signals, pulse widths across clock
ratios and glitch-free clock multiplexers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl import confine
from chipleap.rtl import quote as q
from chipleap.rtl.project import Project
from chipleap.rtl.synth import prepare_verilog, read_commands
from chipleap.verify.result import StepResult
from chipleap.verify.tools import yosys

# Pins of Yosys's single-bit flip-flops and latches that are not data.
ASYNC_PINS = ("R", "S", "L", "AD")
PASS_THROUGH = ("$_BUF_", "$_NOT_")
_INDEX_RE = re.compile(r"^(.*)\[(\d+)\]$")


@dataclass
class State:
    """A flip-flop or latch (one bit), a memory, or a clocked memory read port."""

    name: str  # the cell
    kind: str  # flop | latch | memory | readport
    clock: list[int]  # the clock (or latch enable) bits
    data: dict[str, list[int]]  # input pins that are data: D, E, synchronous R ...
    asyncs: dict[str, list[int]]  # asynchronous set, reset and load pins
    out: list[int]  # what it drives
    label: str = ""  # its register name, for people
    domain: str = ""
    resets: frozenset[str] = frozenset()  # the reset ports that reach its async pins
    reset_direct: frozenset[str] = frozenset()  # those that reach it without a synchronizer
    memory: str = ""  # for a read port: the memory it reads
    enable: list[int] = field(default_factory=list)


class Netlist:
    """The top module of a Yosys JSON netlist: who drives each bit, who reads it, names."""

    def __init__(self, module: dict[str, Any]) -> None:
        self.cells: dict[str, dict[str, Any]] = module.get("cells", {})
        self.ports: dict[str, dict[str, Any]] = module.get("ports", {})
        self.driver: dict[int, tuple[str, str]] = {}  # bit -> (cell or "", pin or port)
        self.loads: dict[int, list[tuple[str, str]]] = defaultdict(list)
        self.names: dict[int, list[str]] = defaultdict(list)
        self.hidden: dict[int, list[str]] = defaultdict(list)
        port_names = set(self.ports)
        for name, p in self.ports.items():
            for i, b in enumerate(p["bits"]):
                if isinstance(b, int) and p["direction"] in ("input", "inout"):
                    self.driver[b] = ("", f"{name}[{i}]" if len(p["bits"]) > 1 else name)
        for cname, c in self.cells.items():
            dirs = c.get("port_directions", {})
            for pin, bits in c.get("connections", {}).items():
                for b in bits:
                    if not isinstance(b, int):
                        continue
                    if dirs.get(pin) == "output":
                        self.driver[b] = (cname, pin)
                    else:
                        self.loads[b].append((cname, pin))
        for name, n in module.get("netnames", {}).items():
            many = len(n["bits"]) > 1
            for i, b in enumerate(n["bits"]):
                if isinstance(b, int):
                    label = f"{name}[{i}]" if many else name
                    (self.hidden if n.get("hide_name") else self.names)[b].append(label)
        # A register is named after itself, not after the port it drives.
        for labels in self.names.values():
            labels.sort(key=lambda s: (_base(s) in port_names, s.count("."), s))

    def label(self, bit: int | str) -> str:
        if not isinstance(bit, int):
            return f"constant {bit}"
        if self.names.get(bit):
            return self.names[bit][0]
        return self.hidden[bit][0] if self.hidden.get(bit) else f"net {bit}"

    def input_port(self, bit: int) -> str | None:
        d = self.driver.get(bit)
        return _base(d[1]) if d and d[0] == "" else None


def _base(label: str) -> str:
    m = _INDEX_RE.match(label)
    return m.group(1) if m else label


def _index(label: str) -> int:
    m = _INDEX_RE.match(label)
    return int(m.group(2)) if m else 0


def _param(value: Any) -> int:
    if isinstance(value, int):
        return value
    s = str(value)
    return int(s, 2) if set(s) <= {"0", "1"} and s else 0


def _states(net: Netlist) -> dict[str, State]:
    """Every state element, keyed by cell name."""
    out: dict[str, State] = {}
    for name, c in net.cells.items():
        t, conn = c["type"], c.get("connections", {})
        if t.startswith(("$_DFF", "$_SDFF", "$_ALDFF", "$_DLATCH", "$_SR_", "$_FF_")):
            latch = t.startswith("$_DLATCH")
            sync = t.startswith("$_SDFF")
            clock = conn.get("E" if latch else "C", [])
            data = {p: b for p, b in conn.items() if p in ("D", "E") and not (latch and p == "E")}
            if sync and "R" in conn:
                data["R"] = conn["R"]
            asyncs = {p: b for p, b in conn.items() if p in ASYNC_PINS and not (sync and p == "R")}
            s = State(name, "latch" if latch else "flop", clock, data, asyncs, conn.get("Q", []))
            s.enable = conn.get("E", []) if not latch else []
            s.label = net.label(s.out[0]) if s.out else name
            out[name] = s
        elif t.startswith("$mem"):
            mem = State(name, "memory", conn.get("WR_CLK", []), {}, {}, [])
            mem.data = {p: conn.get(p, []) for p in ("WR_ADDR", "WR_DATA", "WR_EN")}
            mem.label = str(c.get("parameters", {}).get("MEMID", name)).lstrip("\\")
            out[name] = mem
            ports = _param(c.get("parameters", {}).get("RD_PORTS", 0))
            clocked = str(c.get("parameters", {}).get("RD_CLK_ENABLE", ""))[::-1]
            data_bits = conn.get("RD_DATA", [])
            width = len(data_bits) // ports if ports else 0
            for i in range(ports):
                if i < len(clocked) and clocked[i] == "1":
                    rp = State(
                        f"{name}#rd{i}",
                        "readport",
                        [conn["RD_CLK"][i]],
                        {"RD_ADDR": _slice(conn.get("RD_ADDR", []), ports, i)},
                        {},
                        data_bits[i * width : (i + 1) * width],
                    )
                    rp.memory = name
                    rp.label = net.label(rp.out[0]) if rp.out else rp.name
                    out[rp.name] = rp
    return out


def _slice(bits: list[Any], ports: int, i: int) -> list[Any]:
    n = len(bits) // ports if ports else 0
    return bits[i * n : (i + 1) * n]


@dataclass
class Graph:
    net: Netlist
    states: dict[str, State]
    by_out: dict[int, str]  # output bit -> state element
    reads: dict[int, tuple[str, list[int]]]  # async memory read bit -> (memory, address bits)
    memo: dict[int, frozenset[str]] = field(default_factory=dict)

    def sources(self, bit: Any) -> frozenset[str]:
        """The state elements (by name) and input ports ("in:<port>") whose values reach
        ``bit`` through combinational logic. Iterative, so deep logic cannot exhaust the
        stack; a combinational loop contributes nothing more once it closes."""
        if not isinstance(bit, int):
            return frozenset()
        busy: set[int] = set()
        stack: list[tuple[int, bool]] = [(bit, False)]
        while stack:
            b, done = stack.pop()
            if b in self.memo:
                continue
            leaf, deps = self._step(b)
            if not done:
                busy.add(b)
                stack.append((b, True))
                stack += [(x, False) for x in deps if x not in self.memo and x not in busy]
                continue
            found = set(leaf)
            for x in deps:
                found |= self.memo.get(x, frozenset())
            self.memo[b] = frozenset(found)
            busy.discard(b)
        return self.memo[bit]

    def _step(self, bit: int) -> tuple[set[str], list[int]]:
        """What ``bit`` is (a state element's output, an input, a memory read) and the bits
        it depends on combinationally."""
        if bit in self.by_out:
            return {self.by_out[bit]}, []
        if bit in self.reads:
            mem, addr = self.reads[bit]
            return {mem}, [a for a in addr if isinstance(a, int)]
        d = self.net.driver.get(bit)
        if d is None:
            return set(), []
        if d[0] == "":
            return {f"in:{_base(d[1])}"}, []
        c = self.net.cells[d[0]]
        deps = [
            b
            for pin, bits in c.get("connections", {}).items()
            if c.get("port_directions", {}).get(pin) == "input"
            for b in bits
            if isinstance(b, int)
        ]
        return set(), deps

    def _step_label(self, bit: int, first: bool) -> str:
        """A net on a path: its name, or for a net only Yosys named, the gate driving it."""
        if first or self.net.names.get(bit):
            return self.net.label(bit)
        d = self.net.driver.get(bit)
        kind = self.net.cells[d[0]]["type"].strip("$_") if d and d[0] else "net"
        return f"{kind} gate"

    def through(self, bit: Any) -> Any:
        """``bit`` followed back through buffers."""
        while isinstance(bit, int):
            d = self.net.driver.get(bit)
            if not d or not d[0] or self.net.cells[d[0]]["type"] != "$_BUF_":
                return bit
            bit = self.net.cells[d[0]]["connections"]["A"][0]
        return bit

    def path(self, src: str, bit: int) -> list[str]:
        """Nets from the source to ``bit``, shortest first (breadth first, backwards)."""
        target = set(self.states[src].out) if src in self.states else set()
        prev: dict[int, int] = {}
        queue, seen = [bit], {bit}
        while queue:
            b = queue.pop(0)
            port = self.net.input_port(b)
            if b in target or (port is not None and src == f"in:{port}"):
                chain = [b]
                while chain[-1] in prev:
                    chain.append(prev[chain[-1]])
                return [self._step_label(x, k == 0) for k, x in enumerate(chain)]
            d = self.net.driver.get(b)
            if d is None or not d[0] or self.by_out.get(b):
                continue
            c = self.net.cells[d[0]]
            for pin, bits in c.get("connections", {}).items():
                if c.get("port_directions", {}).get(pin) == "input":
                    for x in bits:
                        if isinstance(x, int) and x not in seen:
                            seen.add(x)
                            prev[x] = b
                            queue.append(x)
        return []


def build(module: dict[str, Any]) -> Graph:
    net = Netlist(module)
    states = _states(net)
    by_out = {b: s.name for s in states.values() for b in s.out if isinstance(b, int)}
    reads: dict[int, tuple[str, list[int]]] = {}
    for name, c in net.cells.items():
        if not c["type"].startswith("$mem"):
            continue
        p = c.get("parameters", {})
        ports = _param(p.get("RD_PORTS", 0))
        clocked = str(p.get("RD_CLK_ENABLE", ""))[::-1]
        data = c["connections"].get("RD_DATA", [])
        width = len(data) // ports if ports else 0
        for i in range(ports):
            if not (i < len(clocked) and clocked[i] == "1"):
                addr = _slice(c["connections"].get("RD_ADDR", []), ports, i)
                for b in data[i * width : (i + 1) * width]:
                    if isinstance(b, int):
                        reads[b] = (name, addr)
    return Graph(net, states, by_out, reads)


# Domains


def _clock_domain(g: Graph, bits: list[int], clocks: list[str]) -> tuple[str, str]:
    """A clock pin's domain, and a warning ("" when there is none)."""
    srcs: set[str] = set()
    for b in bits:
        srcs |= g.sources(b)
    ports = sorted(s[3:] for s in srcs if s.startswith("in:"))
    declared = [p for p in ports if p in clocks]
    flops = sorted(s for s in srcs if not s.startswith("in:"))
    if len(declared) == 1:
        return declared[0], ""
    if len(declared) > 1:
        return "+".join(declared), "clock_mux"
    if flops:
        return g.states[flops[0]].label, "generated_clock"
    if ports:
        return ports[0], "undeclared_clock"
    return "constant", "constant_clock"


def _input_domain(port: str, project: Project) -> str:
    cfg = project.cdc
    first = project.clocks[0].port if project.clocks else "none"
    resets = {r.port: r for r in project.resets}
    if port in cfg.async_inputs:
        return f"async:{port}"
    if port in cfg.input_domains:
        return cfg.input_domains[port]
    if port in cfg.reset_synchronized:
        return "*"
    if port in resets:
        return first if resets[port].sync else f"async:{port}"
    if port in {c.port for c in project.clocks}:
        return f"clock:{port}"
    return first


@dataclass
class Finding:
    rule: str
    severity: str  # error | warning
    source: str
    dest: str
    from_domain: str
    to_domain: str
    message: str
    path: list[str] = field(default_factory=list)
    bits: int = 1
    waived: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rule": self.rule,
            "severity": self.severity,
            "from": self.source,
            "to": self.dest,
            "from_domain": self.from_domain,
            "to_domain": self.to_domain,
            "bits": self.bits,
            "message": self.message,
            "path": self.path,
        }
        if self.waived:
            d["waived"] = self.waived
        return d


class Checker:
    def __init__(self, g: Graph, project: Project) -> None:
        self.g = g
        self.project = project
        self.clocks = [c.port for c in project.clocks]
        self.findings: list[Finding] = []
        self.accepted: list[dict[str, Any]] = []
        for s in g.states.values():
            if s.kind == "memory" or s.clock:
                dom, warn = _clock_domain(g, s.clock, self.clocks)
                s.domain = dom
                if warn and s.kind != "memory":
                    self._clock_warning(s, warn)

    def _clock_warning(self, s: State, rule: str) -> None:
        text = {
            "generated_clock": "is clocked by a flip-flop's output, a domain of its own",
            "undeclared_clock": "is clocked from a port the project does not list as a clock",
            "clock_mux": "is clocked from more than one clock",
            "constant_clock": "has a constant clock and never changes",
        }[rule]
        self.findings.append(
            Finding(rule, "warning", s.domain, s.label, s.domain, s.domain, f"{s.label} {text}")
        )

    def domain(self, source: str) -> str:
        if source.startswith("in:"):
            return _input_domain(source[3:], self.project)
        return self.g.states[source].domain

    def label(self, source: str) -> str:
        return source[3:] if source.startswith("in:") else self.g.states[source].label

    def run(self) -> None:
        crossings = self._crossings()
        chains = self._synchronizers(crossings)
        self._classify(crossings, chains)
        self._resets()

    def _crossings(self) -> list[tuple[str, State, str, int]]:
        """(source, destination, pin, bit) for every source of another domain."""
        out = []
        for s in self.g.states.values():
            if s.kind == "memory" and not s.domain:
                continue
            for pin, bits in s.data.items():
                for b in bits:
                    for src in sorted(self.g.sources(b)):
                        d = self.domain(src)
                        if d in ("*", s.domain) or d.startswith("clock:"):
                            continue
                        if s.kind == "readport" and src == s.memory:
                            continue  # handled with the read address below
                        out.append((src, s, pin, b))
            if s.kind == "readport":
                mem = self.g.states[s.memory]
                if mem.domain != s.domain:
                    out.append((mem.name, s, "RD_DATA", s.out[0] if s.out else 0))
        return out

    def _single_flop_load(self, s: State) -> State | None:
        """The one flip-flop ``s`` feeds, directly at its D input, if that is all it feeds."""
        loads = [(c, p) for b in s.out if isinstance(b, int) for c, p in self._loads(b)]
        if len(loads) != 1:
            return None
        cell, pin = loads[0]
        t = self.g.states.get(cell)
        if t is None or pin != "D" or t.kind != "flop" or t.enable or t.domain != s.domain:
            return None
        return t

    def _loads(self, bit: int) -> list[tuple[str, str]]:
        """Loads of ``bit``, through buffers."""
        out = []
        for c, p in self.g.net.loads.get(bit, []):
            if self.g.net.cells[c]["type"] == "$_BUF_":
                for b in self.g.net.cells[c]["connections"]["Y"]:
                    out += self._loads(b)
            else:
                out.append((c, p))
        return out

    def _synchronizers(
        self, crossings: list[tuple[str, State, str, int]]
    ) -> dict[str, list[State]]:
        """First flops that start a chain of two or more, with the chain."""
        chains: dict[str, list[State]] = {}
        for _, dst, pin, _ in crossings:
            if dst.name in chains or dst.kind != "flop" or pin != "D" or dst.enable:
                continue
            chain = [dst]
            nxt = self._single_flop_load(dst)
            while nxt is not None and nxt not in chain:
                chain.append(nxt)
                nxt = self._single_flop_load(nxt)
            if len(chain) >= 2:
                chains[dst.name] = chain
        return chains

    def _classify(
        self, crossings: list[tuple[str, State, str, int]], chains: dict[str, list[State]]
    ) -> None:
        sync_outputs = {s.name: chain[0] for chain in chains.values() for s in chain[1:]}
        groups: dict[tuple[str, str, str, str], list[tuple[str, State, str, int]]] = defaultdict(
            list
        )
        for src, dst, pin, bit in crossings:
            rule = self._rule(src, dst, pin, bit, chains, sync_outputs)
            groups[(rule, _base(self.label(src)), _base(dst.label), self.domain(src))].append(
                (src, dst, pin, bit)
            )
        # A register synchronized bit by bit: gray coded or a multi-bit error.
        for (rule, src_name, dst_name, from_dom), items in sorted(groups.items()):
            dst = items[0][1]
            to_dom = dst.domain
            bits = len({i[0] for i in items})
            if rule == "synchronizer":
                if bits > 1 and not self._gray([i[0] for i in items]):
                    self._error(
                        "multibit_synchronizer",
                        items,
                        f"{src_name} ({from_dom}) reaches {dst_name} ({to_dom}) as {bits} bits "
                        "each through its own synchronizer: the bits can be captured in "
                        "different cycles; gray code it, or hand it over with a handshake",
                    )
                    continue
                chain = chains[dst.name]
                self.accepted.append(
                    {
                        "rule": "gray_synchronizer" if bits > 1 else "synchronizer",
                        "from": src_name,
                        "to": dst_name,
                        "from_domain": from_dom,
                        "to_domain": to_dom,
                        "bits": bits,
                        "stages": len(chain),
                    }
                )
            elif rule == "handshake":
                qual = self._qualifier(dst, sync_outputs)
                self.accepted.append(
                    {
                        "rule": "handshake",
                        "from": src_name,
                        "to": dst_name,
                        "from_domain": from_dom,
                        "to_domain": to_dom,
                        "bits": len({i[1].name for i in items}),
                        "qualifier": _base(qual.label) if qual else "",
                    }
                )
            elif rule == "fifo_memory":
                self.accepted.append(
                    {
                        "rule": "fifo_memory",
                        "from": src_name,
                        "to": dst_name,
                        "from_domain": from_dom,
                        "to_domain": to_dom,
                        "bits": len({i[1].name for i in items}),
                    }
                )
            elif rule == "logic_before_synchronizer":
                self._error(
                    rule,
                    items,
                    f"{src_name} ({from_dom}) reaches the synchronizer {dst_name} ({to_dom}) "
                    "through logic, which can glitch and be captured; register it in "
                    f"{from_dom} first",
                )
            else:
                self._error(
                    "unsynchronized",
                    items,
                    f"{src_name} ({from_dom}) reaches {dst_name} ({to_dom}) without a synchronizer",
                )

    def _rule(
        self,
        src: str,
        dst: State,
        pin: str,
        bit: int,
        chains: dict[str, list[State]],
        sync_outputs: dict[str, State],
    ) -> str:
        if dst.name in chains:
            direct = self.g.through(bit)
            if pin == "D" and src in self.g.states and direct in self.g.states[src].out:
                return "synchronizer"
            if pin == "D" and src.startswith("in:") and self.g.net.input_port(direct) == src[3:]:
                return "synchronizer"
            return "logic_before_synchronizer"
        if self._qualifier(dst, sync_outputs) is not None:
            return "handshake"
        mem = self.g.states.get(src)
        if mem is not None and mem.kind == "memory" and self._read_in(dst, mem):
            return "fifo_memory"
        return "unsynchronized"

    def _qualifier(self, dst: State, sync_outputs: dict[str, State]) -> State | None:
        """A synchronizer output that gates the destination's capture: through its enable,
        or through a multiplexer select whose other input is the destination itself."""
        gates: list[int] = list(dst.enable)
        own = set(dst.out)
        for b in dst.data.get("D", []):
            gates += self._hold_selects(b, own)
        for b in gates:
            for s in sorted(self.g.sources(b)):
                if s in sync_outputs and self.g.states[s].domain == dst.domain:
                    return self.g.states[s]
        return None

    def _hold_selects(self, bit: Any, own: set[int], depth: int = 0) -> list[int]:
        """Select inputs of multiplexers on the way to ``bit`` that choose between the
        destination's own value and new data."""
        bit = self.g.through(bit)
        if not isinstance(bit, int) or depth > 4:
            return []
        d = self.g.net.driver.get(bit)
        if d is None or not d[0] or bit in self.g.by_out:
            return []
        c = self.g.net.cells[d[0]]
        if c["type"] != "$_MUX_":
            return []
        a, b, s = (c["connections"][p][0] for p in ("A", "B", "S"))
        if self.g.through(a) in own or self.g.through(b) in own:
            return [s]
        return self._hold_selects(a, own, depth + 1) + self._hold_selects(b, own, depth + 1)

    def _read_in(self, dst: State, mem: State) -> bool:
        """Whether ``dst`` reads ``mem`` with an address that comes only from its domain."""
        if dst.kind == "readport" and dst.memory == mem.name:
            addr = dst.data.get("RD_ADDR", [])
        else:
            addr = []
            for b, (m, a) in self.g.reads.items():
                if m == mem.name and any(b in self._cone_bits(x) for x in dst.data.get("D", [])):
                    addr += a
        doms = {self.domain(s) for b in addr for s in self.g.sources(b)}
        return bool(addr) and doms <= {dst.domain, "*"}

    def _cone_bits(self, bit: Any) -> set[int]:
        """Every bit on the combinational way to ``bit``."""
        seen: set[int] = set()
        todo = [bit]
        while todo:
            b = todo.pop()
            if not isinstance(b, int) or b in seen:
                continue
            seen.add(b)
            if b in self.g.by_out or b in self.g.reads:
                continue
            d = self.g.net.driver.get(b)
            if d is not None and d[0]:
                c = self.g.net.cells[d[0]]
                for pin, bits in c.get("connections", {}).items():
                    if c.get("port_directions", {}).get(pin) == "input":
                        todo += bits
        return seen

    def _gray(self, sources: list[str]) -> bool:
        """Whether the register these flops form is gray coded: bit i is the XOR of bits
        i and i+1 of one signal, the top bit a copy of its top bit."""
        regs = [self.g.states.get(s) for s in sources]
        flops = [r for r in regs if r is not None and r.kind == "flop"]
        if len(flops) != len(regs):
            return False
        by_index = {_index(r.label): r for r in flops}
        top = max(by_index)
        if sorted(by_index) != list(range(top + 1)):
            return False
        signals: set[str] | None = None
        for i, r in by_index.items():
            d = self.g.through(r.data.get("D", [None])[0])
            # The top bit is a copy: its input is that bit of the signal, whatever drives it.
            copy = isinstance(d, int) and any(_index(n) == top for n in self._all_names(d))
            ins = [d] if i == top and copy else self._xor_inputs(d)
            if ins is None:
                return False
            want = [i, i + 1] if i < top else [i]
            if len(ins) != len(want):
                return False
            # Each input must be bit want[k] of some signal; collect the signals that fit.
            fits: set[str] | None = None
            for k, x in enumerate(ins):
                names = {_base(n) for n in self._all_names(x) if _index(n) in want}
                fits = names if fits is None else fits
                if len(ins) == 2:
                    other = {_base(n) for n in self._all_names(ins[1 - k]) if _index(n) in want}
                    fits &= other
            signals = fits if signals is None else signals & (fits or set())
            if not signals:
                return False
        return True

    def _all_names(self, bit: int) -> list[str]:
        return [*self.g.net.names.get(bit, []), *self.g.net.hidden.get(bit, [])]

    def _xor_inputs(self, bit: Any) -> list[int] | None:
        """The non-constant inputs of the XOR that drives ``bit``; ``[bit]`` when it is not
        driven by an XOR (a copy)."""
        if not isinstance(bit, int):
            return None
        d = self.g.net.driver.get(bit)
        if d is None or not d[0] or bit in self.g.by_out:
            return [bit]
        c = self.g.net.cells[d[0]]
        if c["type"] not in ("$_XOR_", "$_XNOR_"):
            return [bit]
        ins = [self.g.through(c["connections"][p][0]) for p in ("A", "B")]
        live = [x for x in ins if isinstance(x, int)]
        # A copy that is still an XOR with a constant, or an XOR of two bits.
        return live if live else [bit]

    def _error(self, rule: str, items: list[tuple[str, State, str, int]], message: str) -> None:
        src, dst, _, bit = items[0]
        path = [f"{p}" for p in self.g.path(src, bit)] if isinstance(bit, int) else []
        if path:
            path[0] = f"{path[0]} ({self.domain(src)})"
            path.append(f"{dst.label} ({dst.domain})")
        self.findings.append(
            Finding(
                rule,
                "error",
                _base(self.label(src)),
                _base(dst.label),
                self.domain(src),
                dst.domain,
                message,
                path,
                len({i[0] for i in items}),
            )
        )

    # Resets

    def _reset_sources(self, s: State) -> tuple[set[str], set[str]]:
        """The reset ports that reach ``s``'s asynchronous pins, and those that reach them
        without a reset synchronizer."""
        ports: set[str] = set()
        direct: set[str] = set()
        for bits in s.asyncs.values():
            for b in bits:
                for src in self.g.sources(b):
                    if src.startswith("in:"):
                        ports.add(src[3:])
                        direct.add(src[3:])
                    elif src in self._reset_syncs:
                        ports.add(self._reset_syncs[src])
        return ports, direct

    def _resets(self) -> None:
        self._reset_syncs: dict[str, str] = {}  # last flop of a reset synchronizer -> port
        members: set[str] = set()
        for s in self.g.states.values():
            chain = self._reset_chain(s)
            if chain:
                port, flops = chain
                self._reset_syncs[flops[-1].name] = port
                members |= {f.name for f in flops}
                self.accepted.append(
                    {
                        "rule": "reset_synchronizer",
                        "from": port,
                        "to": _base(flops[-1].label),
                        "from_domain": f"async:{port}",
                        "to_domain": flops[-1].domain,
                        "stages": len(flops),
                    }
                )
        released: dict[tuple[str, str], int] = defaultdict(int)
        for s in self.g.states.values():
            if s.kind not in ("flop", "latch") or not s.asyncs:
                continue
            ports, direct = self._reset_sources(s)
            s.resets = frozenset(ports)
            s.reset_direct = frozenset(direct)
            if s.name not in members:
                for p in direct - set(self.project.cdc.reset_synchronized):
                    released[(p, s.domain)] += 1
        for (port, dom), n in sorted(released.items()):
            self.findings.append(
                Finding(
                    "reset_release",
                    "warning",
                    port,
                    dom,
                    f"async:{port}",
                    dom,
                    f"{n} flip-flop{'s' if n > 1 else ''} of {dom} {'are' if n > 1 else 'is'} "
                    f"reset straight from "
                    f"{port}, so the reset is released asynchronously to {dom}; add a reset "
                    "synchronizer, or list the reset in cdc.reset_synchronized if it is "
                    "synchronized outside",
                )
            )
        groups: dict[tuple[str, str], list[tuple[State, State, int]]] = defaultdict(list)
        for dst in self.g.states.values():
            if dst.kind != "flop" or not dst.resets:
                continue
            for bits in dst.data.values():
                for b in bits:
                    for src_name in self.g.sources(b):
                        src = self.g.states.get(src_name)
                        if src is None or not src.resets or src.resets <= dst.resets:
                            continue
                        groups[(_base(src.label), _base(dst.label))].append((src, dst, b))
        for (sname, dname), items in sorted(groups.items()):
            src, dst, bit = items[0]
            extra = ", ".join(sorted(src.resets - dst.resets))
            path = self.g.path(src.name, bit)
            self.findings.append(
                Finding(
                    "reset_crossing",
                    "error",
                    sname,
                    dname,
                    src.domain,
                    dst.domain,
                    f"{sname} is reset by {extra} but {dname} is not: asserting {extra} alone "
                    f"changes {sname} while {dname} keeps sampling it",
                    path,
                    len({i[0].name for i in items}),
                )
            )

    def _reset_chain(self, last: State) -> tuple[str, list[State]] | None:
        """A reset synchronizer ending at ``last``: flops reset from one port, the first
        with a constant input, each feeding the next, and ``last`` resetting others."""
        if last.kind != "flop" or not last.asyncs:
            return None
        used_as_reset = any(
            p in ASYNC_PINS
            for b in last.out
            if isinstance(b, int)
            for c, p in self._loads(b)
            if c in self.g.states
        )
        if not used_as_reset:
            return None
        chain = [last]
        cur = last
        while True:
            d = self.g.through(cur.data.get("D", [None])[0])
            if not isinstance(d, int):
                break
            prev = self.g.states.get(self.g.by_out.get(d, ""))
            if prev is None or prev.kind != "flop" or prev in chain:
                return None
            chain.insert(0, prev)
            cur = prev
        ports = set()
        for f in chain:
            for bits in f.asyncs.values():
                for b in bits:
                    ports |= {s[3:] for s in self.g.sources(b) if s.startswith("in:")}
        if len(chain) < 2 or len(ports) != 1:
            return None
        return ports.pop(), chain


def _waive(findings: Iterable[Finding], project: Project) -> None:
    for f in findings:
        for w in project.cdc.waive:
            if _match(w.source, f.source) and _match(w.dest, f.dest):
                f.waived = w.reason
                break


def _match(pattern: str, name: str) -> bool:
    rx = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(rx, name) is not None


def netlist(project: Project, workdir: Path) -> dict[str, Any]:
    """The project's top module as Yosys elaborates it, mapped to single-bit gates."""
    tool = yosys()
    if tool.path is None:
        raise RuntimeError("Yosys is not installed")
    files, raw = prepare_verilog(project, workdir)
    out = workdir / "netlist.json"
    script = [
        *read_commands(project, files, raw),
        "proc",
        "flatten",
        # Enables folded into the flip-flops; opt_merge is left out so that two registers
        # with the same input keep their own names.
        "opt_expr",
        "opt_dff",
        "opt_clean",
        "memory -nomap",
        "opt_clean",
        "techmap",
        "opt_expr",
        "opt_clean",
        f"write_json {q.yosys(out)}",
    ]
    ys = workdir / "netlist.ys"
    ys.write_text("\n".join(script) + "\n")
    timeout = float(os.environ.get("CHIPLEAP_CDC_TIMEOUT", "900"))
    cmd = [tool.path, "-q", "-l", "netlist.log", ys.name]
    with confine.confined(cmd, [*project.roots, workdir]) as (argv, env):
        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            log, _ = proc.communicate(timeout=timeout)
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    if proc.returncode != 0 or not out.is_file():
        errors = [ln for ln in (log or "").splitlines() if "ERROR" in ln]
        raise RuntimeError(f"Yosys could not elaborate the design: {(errors or ['no output'])[0]}")
    data = json.loads(out.read_text())
    return dict(data["modules"][project.top])


def check(project: Project) -> dict[str, Any]:
    """Domains, crossings, accepted structures and findings, as data."""
    workdir = (project.build_dir / "cdc").resolve()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    g = build(netlist(project, workdir))
    c = Checker(g, project)
    c.run()
    _waive(c.findings, project)
    flops = [s for s in g.states.values() if s.kind in ("flop", "latch")]
    domains: dict[str, int] = defaultdict(int)
    for s in flops:
        domains[s.domain] += 1
    findings = sorted(c.findings, key=lambda f: (f.severity != "error", f.rule, f.source, f.dest))
    live = [f for f in findings if not f.waived]
    crossing_rules = ("unsynchronized", "logic_before_synchronizer", "multibit_synchronizer")
    return {
        "tool": "Yosys netlist, ChipLeap checker",
        "flops": len(flops),
        "domains": dict(sorted(domains.items())),
        "crossings": sum(a["rule"] != "reset_synchronizer" for a in c.accepted)
        + sum(f.rule in crossing_rules for f in findings),
        "errors": sum(f.severity == "error" for f in live),
        "warnings": sum(f.severity == "warning" for f in live),
        "waived": sum(bool(f.waived) for f in findings),
        "findings": [f.to_dict() for f in findings],
        "accepted": sorted(c.accepted, key=lambda a: (a["rule"], a["from"], a["to"])),
    }


def cdc(project: Project) -> StepResult:
    """The CDC and RDC step: fails on any error that is not waived."""
    if not yosys().found:
        return StepResult("cdc", "skipped", "Yosys is not installed")
    data = check(project)
    errors = [f for f in data["findings"] if f["severity"] == "error" and not f.get("waived")]
    doms = ", ".join(f"{d} ({n})" for d, n in data["domains"].items())
    ok = [a for a in data["accepted"] if a["rule"] != "reset_synchronizer"]
    if errors:
        first = errors[0]
        more = f" and {len(errors) - 1} more" if len(errors) > 1 else ""
        return StepResult("cdc", "fail", f"{first['message']}{more}", data)
    note = f"; {data['warnings']} warnings" if data["warnings"] else ""
    note += f"; {data['waived']} waived" if data["waived"] else ""
    what = f"{len(ok)} crossings, each synchronized" if ok else "no crossing between domains"
    return StepResult("cdc", "pass", f"domains {doms}: {what}{note}", data)
