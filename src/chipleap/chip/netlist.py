"""The chip's netlist and the files around it: which net every pad and macro pin is on,
the chip top in Verilog, its timing constraints, its pins and its placement.

The core's ports come from Yosys (``core_ports``): directions and widths after the
project's parameters. Then ``bind`` decides every net. A signal pad is the input or
output pad of one bit of a core port; an analog pad puts a net on its core-side pin;
a macro pin joins a core port bit, an analog pad's net or a net other macro pins name.
Every core input must be driven by exactly one input pad or macro output, inout ports
are refused, and a macro input must be connected.

Names in the chip top: a core port bit ``tx_data_i[3]`` is the net ``tx_data_i_3_core``
between its pad and the core; the pad's bond side is the chip's port ``pad_tx_data_i_3``;
an analog or shared net keeps the name the chip section gives it. Every name is checked
for collisions and Verilog keywords before anything is written.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.chip.config import ChipConfig, port_bit
from chipleap.chip.floorplan import Floorplan, Placed
from chipleap.macro.layer import MacroView
from chipleap.rtl import builddir, confine
from chipleap.rtl import quote as q
from chipleap.rtl.project import IDENT_RE, Project, runs_tools
from chipleap.rtl.synth import DEFAULT_PERIOD_NS, prepare_verilog, read_commands
from chipleap.verify.tools import yosys

PORT_BITS_MAX = 100_000
PORTS_JSON_MAX = 64 << 20
GRID_NM = 5
# The chip's port for a digital pad: a square on Metal3 this far outside the IO cell's
# bond side, in the cell's own coordinates. On top of the cell's pin it broke OpenRCX
# (ODB-0420); here the router joins it to the pin.
PAD_PIN_RECT = (39.0, -2.5, 41.0, -0.5)
VERILOG_KEYWORDS = frozenset(
    [
        "always",
        "and",
        "assign",
        "automatic",
        "begin",
        "buf",
        "bufif0",
        "bufif1",
        "case",
        "casex",
        "casez",
        "cell",
        "cmos",
        "config",
        "deassign",
        "default",
        "defparam",
        "design",
        "disable",
        "edge",
        "else",
        "end",
        "endcase",
        "endconfig",
        "endfunction",
        "endgenerate",
        "endmodule",
        "endprimitive",
        "endspecify",
        "endtable",
        "endtask",
        "event",
        "for",
        "force",
        "forever",
        "fork",
        "function",
        "generate",
        "genvar",
        "highz0",
        "highz1",
        "if",
        "ifnone",
        "incdir",
        "include",
        "initial",
        "inout",
        "input",
        "instance",
        "integer",
        "join",
        "large",
        "liblist",
        "library",
        "localparam",
        "macromodule",
        "medium",
        "module",
        "nand",
        "negedge",
        "nmos",
        "nor",
        "noshowcancelled",
        "not",
        "notif0",
        "notif1",
        "or",
        "output",
        "parameter",
        "pmos",
        "posedge",
        "primitive",
        "pull0",
        "pull1",
        "pulldown",
        "pullup",
        "pulsestyle_ondetect",
        "pulsestyle_onevent",
        "rcmos",
        "real",
        "realtime",
        "reg",
        "release",
        "repeat",
        "rnmos",
        "rpmos",
        "rtran",
        "rtranif0",
        "rtranif1",
        "scalared",
        "showcancelled",
        "signed",
        "small",
        "specify",
        "specparam",
        "strong0",
        "strong1",
        "supply0",
        "supply1",
        "table",
        "task",
        "time",
        "tran",
        "tranif0",
        "tranif1",
        "tri",
        "tri0",
        "tri1",
        "triand",
        "trior",
        "trireg",
        "unsigned",
        "use",
        "uwire",
        "vectored",
        "wait",
        "wand",
        "weak0",
        "weak1",
        "while",
        "wire",
        "wor",
        "xnor",
        "xor",
        "logic",
        "bit",
        "byte",
        "int",
        "shortint",
        "longint",
    ]
)


SUPPLY_NETS = frozenset({"vdd", "vss", "iovdd", "iovss"})


class NetlistError(ValueError):
    pass


@dataclass(frozen=True)
class Port:
    name: str
    direction: str  # input, output, inout
    width: int
    offset: int = 0
    upto: bool = False

    def indices(self) -> list[int]:
        """The bit indices in declaration order, left to right: [7:0] gives 7 to 0."""
        lo, hi = self.offset, self.offset + self.width - 1
        return list(range(lo, hi + 1)) if self.upto else list(range(hi, lo - 1, -1))

    def bits(self) -> list[str]:
        if self.width == 1 and not self.upto and self.offset == 0:
            return [self.name]
        return [f"{self.name}[{i}]" for i in self.indices()]


def parse_ports(data: Any, top: str) -> dict[str, Port]:
    """The ports of ``top`` in Yosys's JSON."""
    try:
        raw = data["modules"][top]["ports"]
    except (KeyError, TypeError):
        raise NetlistError(f"Yosys's JSON has no ports for {top}") from None
    if not isinstance(raw, dict):
        raise NetlistError("Yosys's JSON is not what ChipLeap expects")
    out: dict[str, Port] = {}
    total = 0
    for name, p in raw.items():
        if not isinstance(name, str) or not IDENT_RE.fullmatch(name) or not isinstance(p, dict):
            raise NetlistError(f"port {str(name)[:40]!r} of {top} is not a plain name")
        bits = p.get("bits")
        direction = p.get("direction")
        offset = p.get("offset", 0)
        upto = p.get("upto", 0)
        if (
            not isinstance(bits, list)
            or not bits
            or direction not in ("input", "output", "inout")
            or not isinstance(offset, int)
            or abs(offset) > PORT_BITS_MAX
            or upto not in (0, 1)
        ):
            raise NetlistError(f"port {name} of {top} is not what ChipLeap expects")
        total += len(bits)
        if total > PORT_BITS_MAX:
            raise NetlistError(f"{top} has more than {PORT_BITS_MAX} port bits")
        out[name] = Port(name, direction, len(bits), offset, bool(upto))
    return out


@runs_tools
def core_ports(project: Project, workdir: Path) -> dict[str, Port]:
    """The ports of the project's top as Yosys elaborates it with the project's
    parameters. The top is made a black box before it is written, so the JSON holds its
    ports and nothing else."""
    tool = yosys()
    if tool.path is None:
        raise RuntimeError(tool.missing("Yosys"))
    files, raw = prepare_verilog(project, workdir)
    out = workdir / "core_ports.json"
    top = q.plain(project.top)
    script = [
        *read_commands(project, files, raw),
        # Every module of the core, for the check that no macro shares a name with one.
        "tee -q -o core_modules.txt ls",
        f"blackbox {top}",
        f"hierarchy -top {top}",
        f"write_json {q.yosys(out)}",
    ]
    ys = builddir.write_text(workdir, "core_ports.ys", "\n".join(script) + "\n")
    cmd = [tool.path, "-q", "-l", "core_ports.log", ys.name]
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
            log, _ = proc.communicate(
                timeout=float(os.environ.get("CHIPLEAP_SYNTH_TIMEOUT", "1800"))
            )
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    if proc.returncode != 0 or not out.is_file():
        errors = [ln for ln in (log or "").splitlines() if "ERROR" in ln]
        raise RuntimeError(f"Yosys could not elaborate the design: {(errors or ['no output'])[0]}")
    if out.stat().st_size > PORTS_JSON_MAX:
        raise NetlistError("Yosys's port list is larger than expected")
    return parse_ports(json.loads(out.read_text()), project.top)


def core_modules(workdir: Path) -> list[str]:
    """The core's modules that ``core_ports`` listed, as the source names them."""
    from chipleap.chip.merge import yosys_modules

    path = workdir / "core_modules.txt"
    if not path.is_file() or path.stat().st_size > PORTS_JSON_MAX:
        raise NetlistError("Yosys did not list the core's modules")
    return yosys_modules(path.read_text(errors="replace"))


# Binding


def flat(bit: str) -> str:
    """``tx_data_i[3]`` as one identifier: ``tx_data_i_3``."""
    return bit.replace("[", "_").replace("]", "")


@dataclass
class Binding:
    """Every net of the chip top."""

    ports: dict[str, Port]
    core_nets: dict[str, str]  # core port bit -> net
    directions: dict[str, str]  # signal pad's bit -> in or out
    io: dict[str, dict[str, str]]  # pad instance -> IO cell pin -> net
    chip_ports: dict[str, str]  # chip port -> pad instance
    macros: dict[str, dict[str, str]]  # macro instance -> pin -> net
    clocks: dict[str, str]  # clock port -> its pad instance
    warnings: list[str] = field(default_factory=list)

    def nets(self) -> list[str]:
        """Every net of the chip top except its ports, sorted."""
        seen = set(self.core_nets.values())
        for conns in (*self.io.values(), *self.macros.values()):
            seen.update(conns.values())
        return sorted(seen - set(self.chip_ports))


def _core_bit(net: str, ports: Mapping[str, Port]) -> str | None:
    """The core port bit ``net`` names, written as ``Port.bits`` writes it; None when it
    names none. A one-bit port answers to ``x`` and ``x[offset]``."""
    pb = port_bit(net)
    if pb is None or pb[0] not in ports:
        return None
    p = ports[pb[0]]
    name, index = pb
    if index is None:
        return net if p.bits() == [name] else None
    bits = p.bits()
    if bits == [name] and index == p.offset:
        return name
    return net if net in bits else None


def _own_net(
    net: str, core_nets: Mapping[str, str], chip_ports: Mapping[str, str], what: str
) -> None:
    """Refuse a net the chip section names that is also one ChipLeap names itself, or a
    supply's."""
    if net.casefold() in SUPPLY_NETS:
        raise NetlistError(f"{what} {net}: {net} is a supply's name")
    for bit, core in core_nets.items():
        if net == core:
            raise NetlistError(f"{what} {net}: {net} is the net of core port bit {bit}")
    if net in chip_ports:
        raise NetlistError(f"{what} {net}: {net} is the chip's port for a pad")


def _expand_connect(
    instance: str, connect: Mapping[str, str], view: MacroView, ports: Mapping[str, Port]
) -> dict[str, str]:
    """``connect`` bit by bit. A key is a pin, a bit of a bus pin (``q[3]``) or a whole
    bus (``q``); a whole bus connects to a core port of its width, bit 0 to the port's
    least significant bit, or to a net of its own, ``NAME_0`` and up."""
    out: dict[str, str] = {}
    for key, value in connect.items():
        pin = view.pin(key)
        if pin is not None:
            if pin.is_supply:
                raise NetlistError(
                    f"macro {instance}: {key} is a supply pin; ChipLeap connects it to "
                    f"{'VDD' if pin.kind == 'power' else 'VSS'} itself"
                )
            bits = {key: value}
        elif bus := view.bus(key):
            port = ports.get(value)
            if port is not None:
                if port.width != len(bus):
                    raise NetlistError(
                        f"macro {instance}: bus {key} has {len(bus)} bits and core port "
                        f"{value} {port.width}"
                    )
                lsb_first = list(reversed(port.bits()))
                bits = {p.name: lsb_first[i] for i, p in enumerate(bus)}
            elif IDENT_RE.fullmatch(value):
                bits = {p.name: f"{value}_{i}" for i, p in enumerate(bus)}
            else:
                raise NetlistError(
                    f"macro {instance}: bus {key} connects to a core port of its width or a "
                    f"net name, not {value}"
                )
        else:
            have = ", ".join(sorted({x.name.split("[")[0] for x in view.pins if not x.is_supply}))
            raise NetlistError(
                f"macro {instance}: connect names {key}, which {view.cell} does not have "
                f"(its pins: {have})"
            )
        for bit, net in bits.items():
            if bit in out:
                raise NetlistError(f"macro {instance}: pin {bit} is connected twice")
            out[bit] = net
    return out


def _base(bit: str) -> str:
    return bit.split("[", 1)[0]


def bind(
    config: ChipConfig,
    ports: Mapping[str, Port],
    views: Mapping[str, MacroView],
    cells: Mapping[str, str],
    clocks: Iterable[str] = (),
    top: str = "",
) -> Binding:
    """The nets of the chip: ``ports`` are the core's, ``views`` the macros' views by
    cell, ``cells`` each macro instance's cell, ``clocks`` the project's clock ports and
    ``top`` the core's module."""
    for p in ports.values():
        if p.direction == "inout":
            raise NetlistError(
                f"core port {p.name} is inout; ChipLeap's pads are inputs, outputs or analog"
            )
    core_nets = {bit: f"{flat(bit)}_core" for p in ports.values() for bit in p.bits()}
    seen: dict[str, str] = {}
    for port_bit_name, core_net in core_nets.items():
        if core_net in seen:
            raise NetlistError(
                f"core port bits {seen[core_net]} and {port_bit_name} would share the net "
                f"{core_net} and the pad port pad_{flat(port_bit_name)}; rename one of the ports"
            )
        seen[core_net] = port_bit_name
    drivers: dict[str, list[str]] = {}
    users: dict[str, list[str]] = {}
    io: dict[str, dict[str, str]] = {}
    chip_ports: dict[str, str] = {}
    directions: dict[str, str] = {}
    on_pad: dict[str, str] = {}  # core bit -> pad instance
    analog: set[str] = set()
    known = ", ".join(ports) or "none"
    for side, slot, pad in config.all_pads():
        inst = f"pad_{side}_{slot}"
        where = f"chip.pads.{side}[{slot}]"
        if pad.kind == "signal":
            bit = _core_bit(pad.net, ports)
            if bit is None:
                raise NetlistError(
                    f"{where}: {pad.net} is not a port bit of the core; its ports: {known}"
                )
            port = ports[_base(bit)]
            net = core_nets[bit]
            chip_port = f"pad_{flat(bit)}"
            if port.direction == "input":
                io[inst] = {"p2c": net, "pad": chip_port}
                directions[pad.net] = "in"
                drivers.setdefault(net, []).append(f"pad {inst}")
            else:
                io[inst] = {"c2p": net, "pad": chip_port}
                directions[pad.net] = "out"
            chip_ports[chip_port] = inst
            on_pad[bit] = inst
        elif pad.kind == "analog":
            _own_net(pad.net, core_nets, chip_ports, f"{where}: analog net")
            # The pin the core does not use gets a net of its own, so LVS still finds
            # the pin as a port of its own.
            used, spare = ("padres", "pad") if pad.resistor else ("pad", "padres")
            io[inst] = {used: pad.net, spare: f"{pad.net}_{spare}"}
            analog.add(pad.net)
            users.setdefault(pad.net, []).append(f"pad {inst}")
    macros: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    free: set[str] = set()  # nets only macro pins name
    unconnected: set[str] = set()  # nets of macro pins left unconnected
    for m in config.macros:
        view = views[cells[m.instance]]
        wanted = _expand_connect(m.instance, m.connect, view, ports)
        conns: dict[str, str] = {}
        for mp in view.pins:
            if mp.is_supply:
                continue
            given = wanted.get(mp.name)
            if given is None:
                if mp.direction == "input":
                    raise NetlistError(
                        f"macro {m.instance}: input {mp.name} is not connected; add it to connect"
                    )
                warnings.append(f"macro {m.instance}: {mp.direction} {mp.name} is not connected")
                conns[mp.name] = f"{m.instance}_{flat(mp.name)}"
                if conns[mp.name] in unconnected:
                    raise NetlistError(
                        f"macro {m.instance}: {mp.name} left open would share the net "
                        f"{conns[mp.name]} with another open pin; rename the instance"
                    )
                unconnected.add(conns[mp.name])
                continue
            bit = _core_bit(given, ports)
            named = port_bit(given)
            if bit is not None:
                net = core_nets[bit]
            elif named is not None and named[0] in ports:
                raise NetlistError(
                    f"macro {m.instance}: {given} is not a bit of core port {named[0]}"
                )
            elif not IDENT_RE.fullmatch(given):
                raise NetlistError(
                    f"macro {m.instance}: {given} names no core port; a net of its own "
                    "must be a plain name"
                )
            else:
                net = given
                if net not in analog:
                    _own_net(net, core_nets, chip_ports, f"macro {m.instance}: net")
                    free.add(net)
            conns[mp.name] = net
            if mp.direction == "output":
                drivers.setdefault(net, []).append(f"macro {m.instance}.{mp.name}")
            else:
                users.setdefault(net, []).append(f"macro {m.instance}.{mp.name}")
        macros[m.instance] = conns

    for p in ports.values():
        for bit in p.bits():
            net = core_nets[bit]
            n = drivers.get(net, [])
            if p.direction == "input" and not n:
                raise NetlistError(
                    f"core input {bit} is not driven: put it on a pad or connect a macro "
                    "output to it"
                )
            if p.direction == "output" and n:
                raise NetlistError(f"core output {bit} is also driven by {', '.join(n)}")
            if p.direction == "output" and bit not in on_pad and not users.get(net):
                warnings.append(f"core output {bit} is on no pad and drives no macro")
    for net, n in drivers.items():
        if len(n) > 1:
            raise NetlistError(f"net {net} has {len(n)} drivers: {', '.join(n)}")
    taken = unconnected & (free | analog | set(core_nets.values()) | set(chip_ports))
    if taken:
        raise NetlistError(
            f"net {sorted(taken)[0]} is both an unconnected macro pin's net and a net the chip "
            "section names; rename the net"
        )
    for net in sorted(free):
        who = [*drivers.get(net, []), *users.get(net, [])]
        if len(who) < 2:
            warnings.append(f"net {net} reaches only {who[0]}")
    clock_pads: dict[str, str] = {}
    for c in clocks:
        bit = _core_bit(c, ports)
        if bit is None or bit not in on_pad or directions.get(c) != "in":
            raise NetlistError(f"clock {c} must be on an input pad of the chip")
        clock_pads[c] = on_pad[bit]
    b = Binding(dict(ports), core_nets, directions, io, chip_ports, macros, clock_pads, warnings)
    _check_names(b, config, {views[c].cell for c in cells.values()}, top)
    return b


def _check_names(b: Binding, config: ChipConfig, cells: set[str], top: str) -> None:
    """Every name the chip top declares is a Verilog identifier, no keyword, and names
    one thing; a macro's cell is a module name of its own."""
    owners: dict[str, str] = {}
    instances = {*b.io, *(m.instance for m in config.macros), "u_core"}
    for kind, names in (
        ("port", b.chip_ports),
        ("net", b.nets()),
        ("instance", instances),
    ):
        for n in names:
            if not IDENT_RE.fullmatch(n) or n in VERILOG_KEYWORDS:
                raise NetlistError(f"{n!r} cannot be a {kind} name in the chip's Verilog")
            if n in owners and owners[n] != kind:
                raise NetlistError(f"{n} names both a {owners[n]} and a {kind} of the chip")
            if kind == "port" and n in b.nets():
                raise NetlistError(f"{n} names both a port and a net of the chip")
            owners[n] = kind
    if config.name in owners or config.name in VERILOG_KEYWORDS:
        raise NetlistError(f"chip.name {config.name} is also a name inside the chip")
    for cell in sorted(cells):
        if not IDENT_RE.fullmatch(cell) or cell in VERILOG_KEYWORDS:
            raise NetlistError(f"macro cell {cell!r} cannot be a module name in Verilog")
        if cell in (config.name, top):
            raise NetlistError(f"macro cell {cell} has the name of the chip or of its core")


# Files


def chip_top(
    config: ChipConfig,
    b: Binding,
    plan: Floorplan,
    top: str,
    parameters: Mapping[str, int],
) -> str:
    """The chip top in Verilog: the ring's cells kept as they are, the core, the macros."""
    lines = [
        f"// {config.name}: written by ChipLeap from the chip section of project.yaml.",
        f"module {config.name} (",
        ",\n".join(f"  {p}" for p in sorted(b.chip_ports)) or "",
        ");",
    ]
    lines += [f"  inout {p};" for p in sorted(b.chip_ports)]
    lines += [f"  wire {n};" for n in b.nets()]
    for cell in plan.ring:
        conns = b.io.get(cell.inst, {})
        pins = ", ".join(f".{pin}({net})" for pin, net in sorted(conns.items()))
        lines.append(f"  (* keep *) {cell.cell} {cell.inst} ({pins});")
    params = ", ".join(f".{k}({int(v)})" for k, v in parameters.items())
    core = []
    for p in b.ports.values():
        nets = [b.core_nets[bit] for bit in p.bits()]
        core.append(f".{p.name}({nets[0] if len(nets) == 1 else '{' + ', '.join(nets) + '}'})")
    lines.append(f"  {top} {'#(' + params + ') ' if params else ''}u_core (")
    lines.append(",\n".join(f"    {c}" for c in core))
    lines.append("  );")
    for m in plan.macros:
        lines.append(f"  {m.cell} {m.inst} ({_macro_pins(b.macros.get(m.inst, {}))});")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _macro_pins(conns: Mapping[str, str]) -> str:
    """A macro's named connections; a bus's bits (``q[0]`` to ``q[3]``) go together as
    one concatenation, most significant first, as its liberty declares it."""
    groups: dict[str, dict[int | None, str]] = {}
    for pin, net in conns.items():
        pb = port_bit(pin)
        if pb is None:
            raise NetlistError(f"macro pin {pin} is not a name or a bit")
        groups.setdefault(pb[0], {})[pb[1]] = net
    out = []
    for name, bits in sorted(groups.items()):
        if list(bits) == [None]:
            out.append(f".{name}({bits[None]})")
            continue
        if None in bits:
            raise NetlistError(f"macro pin {name} is both a bus and a single bit")
        order = sorted((i for i in bits if i is not None), reverse=True)
        if order != list(range(len(order) - 1, -1, -1)):
            raise NetlistError(f"macro bus {name} does not have every bit from 0 up")
        out.append(f".{name}({{{', '.join(bits[i] for i in order)}}})")
    return ", ".join(out)


def sdc(b: Binding, clocks: Mapping[str, float]) -> str:
    """Timing constraints: each clock on its input pad's core-side pin, input delays on
    the other input pads' and output delays on the output pads' core-side pins, as a
    fraction of the first clock's period (LibreLane's IO_DELAY_CONSTRAINT)."""
    lines = ["# Written by ChipLeap from the chip section of project.yaml."]
    if b.clocks:
        for port, inst in b.clocks.items():
            lines.append(
                f"create_clock -name {q.plain(flat(port))} -period {clocks[port]:g} "
                f"[get_pins {q.plain(inst)}/p2c]"
            )
        ref, period = flat(next(iter(b.clocks))), clocks[next(iter(b.clocks))]
    else:
        ref, period = "__VIRTUAL_CLK__", DEFAULT_PERIOD_NS
        lines.append(f"create_clock -name {ref} -period {period:g}")
    clock_pads = set(b.clocks.values())
    ins = sorted(i for i, c in b.io.items() if "p2c" in c and i not in clock_pads)
    outs = sorted(i for i, c in b.io.items() if "c2p" in c)
    lines.append(f"set io_delay [expr {period:g} * $::env(IO_DELAY_CONSTRAINT) / 100]")
    if ins:
        pins = " ".join(f"{q.plain(i)}/p2c" for i in ins)
        lines.append(f"set_input_delay $io_delay -clock [get_clocks {ref}] [get_pins {{{pins}}}]")
    if outs:
        pins = " ".join(f"{q.plain(i)}/c2p" for i in outs)
        lines.append(f"set_output_delay $io_delay -clock [get_clocks {ref}] [get_pins {{{pins}}}]")
    lines += [
        "set_clock_uncertainty $::env(CLOCK_UNCERTAINTY_CONSTRAINT) [all_clocks]",
        "set_clock_transition $::env(CLOCK_TRANSITION_CONSTRAINT) [all_clocks]",
        "if { [info exists ::env(OPENLANE_SDC_IDEAL_CLOCKS)] && $::env(OPENLANE_SDC_IDEAL_CLOCKS) } {",
        "    unset_propagated_clock [all_clocks]",
        "} else {",
        "    set_propagated_clock [all_clocks]",
        "}",
    ]
    return "\n".join(lines) + "\n"


def transform(
    cell: Placed, rect: tuple[float, float, float, float], size: tuple[float, float]
) -> tuple[float, float, float, float]:
    """A rect in a cell's own coordinates (``size`` is the cell's width and height) in die
    coordinates, for the cell placed as ``cell`` (DEF orientations)."""
    w, h = size
    pts = []
    for px, py in ((rect[0], rect[1]), (rect[2], rect[3])):
        if cell.orient == "N":
            qx, qy = px, py
        elif cell.orient == "S":
            qx, qy = w - px, h - py
        elif cell.orient == "E":
            qx, qy = py, w - px
        elif cell.orient == "W":
            qx, qy = h - py, px
        elif cell.orient == "FN":
            qx, qy = w - px, py
        elif cell.orient == "FS":
            qx, qy = px, h - py
        else:
            raise NetlistError(f"orientation {cell.orient} is not handled")
        pts.append((cell.x + qx, cell.y + qy))
    (ax, ay), (bx, by) = pts
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def pins_def(config: ChipConfig, b: Binding, plan: Floorplan, pad_size: tuple[float, float]) -> str:
    """The DEF that LibreLane takes the chip's pins from (FP_DEF_TEMPLATE): each digital
    pad's port, a square on Metal3 outside its IO cell's bond side, on the grid."""
    cells = {c.inst: c for c in plan.ring}
    w, h = plan.die
    lines = [
        "VERSION 5.8 ;",
        'DIVIDERCHAR "/" ;',
        'BUSBITCHARS "[]" ;',
        f"DESIGN {config.name} ;",
        "UNITS DISTANCE MICRONS 1000 ;",
        f"DIEAREA ( 0 0 ) ( {round(w * 1000)} {round(h * 1000)} ) ;",
        f"PINS {len(b.chip_ports)} ;",
    ]
    for port, inst in sorted(b.chip_ports.items()):
        x0, y0, x1, y1 = transform(cells[inst], PAD_PIN_RECT, pad_size)
        a = -(-round(x0 * 1000) // GRID_NM) * GRID_NM
        c = -(-round(y0 * 1000) // GRID_NM) * GRID_NM
        e = round(x1 * 1000) // GRID_NM * GRID_NM
        f = round(y1 * 1000) // GRID_NM * GRID_NM
        lines += [
            f"- {port} + NET {port} + DIRECTION INOUT + USE SIGNAL",
            f"  + PORT + LAYER Metal3 ( 0 0 ) ( {e - a} {f - c} ) + FIXED ( {a} {c} ) N ;",
        ]
    lines += ["END PINS", "END DESIGN"]
    return "\n".join(lines) + "\n"


def placement(plan: Floorplan) -> str:
    """LibreLane's MACRO_PLACEMENT_CFG: every ring cell and macro, fixed."""
    return "".join(f"{c.inst} {c.x:.3f} {c.y:.3f} {c.orient}\n" for c in (*plan.ring, *plan.macros))
