"""Design for test: scan insertion with OpenROAD, ATPG with Fault, and a simulation that
shows the chains and the patterns work.

1. D1's synthesis maps the design to IHP SG13G2's cells (``build/dft/synth``).
2. OpenROAD replaces each flip-flop with its scan equivalent (``scan_replace``) and
   stitches the chains (``execute_dft_plan``, ``insert_dft`` in older builds), adding
   ``scan_enable_0``, ``scan_in_N`` and ``scan_out_N``. ChipLeap traces every chain
   through the written netlist from its scan input to its scan output; that order, not
   the report's, is what the patterns use.
3. Fault cuts the flip-flops out of the pre-scan netlist, so each one's output becomes an
   input and its D an output, ``nl2bench`` converts the cut netlist, Quaigh generates
   test patterns and Fault simulates every stuck-at fault against them: its coverage is
   the step's number. Hard macros stay off the chains: Fault cuts them too, then
   ``seal_macros`` takes them out of the test, so a fault seen only at a macro's inputs
   is not detected and no pattern sets a macro's outputs, which hold at 0 (in silicon, a
   test-mode gate on them). A macro is tested through its own BIST port, not by scan.
4. ChipLeap lays each pattern onto the chains and writes ``patterns.json`` and a STIL
   file, then simulates the scan netlist with the cell models on Icarus 13: a flush
   pattern through every chain, and the first few ATPG patterns shifted in, captured and
   shifted out against Fault's expected values.

Names from the netlist reach the testbench as Verilog identifiers (escaped when they
are not plain), and the pattern data as files the testbench reads with ``$readmemb``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.netlist import (
    Chain,
    Module,
    NetlistError,
    Nets,
    parse_netlist,
    read_netlist,
    trace_chain,
)
from chipleap.rtl.openroad import (
    Job,
    OpenroadError,
    OpenroadUnavailable,
    PdkViews,
    failure,
    macro_views,
    pdk_views,
    read_json,
    read_limited,
    read_limited_bytes,
    routed_run,
    routed_views,
)
from chipleap.rtl.project import DftConfig, Project
from chipleap.rtl.provenance import Sealed, SealError, copy_file, open_sealed, record, unseal
from chipleap.rtl.synth import SynthError, synthesize
from chipleap.verify import launch
from chipleap.verify.result import StepResult
from chipleap.verify.tools import (
    clean_env,
    fault,
    fault_env,
    gl_iverilog,
    gl_vvp,
    nl2bench,
    quaigh,
    untrusted,
)

# IHP SG13G2's cells as the scan steps see them.
SCAN_CELL_RE = re.compile(r"^sg13g2_sdf")
FLOP_CELL_RE = re.compile(r"^sg13g2_(s?df|dl[hl])")
SCAN_IN_PIN, SCAN_ENABLE_PIN, SCAN_OUT_PINS = "SCD", "SCE", ("Q",)
# Buffers place and route puts on a chain: followed when tracing a routed netlist.
BUFFERS = {"sg13g2_buf_": ("A", "X"), "sg13g2_dlygate4sd": ("A", "X")}
# What Fault's cut removes: the flip-flops, with their data, output and clock pins.
TIE_HI = "sg13g2_tiehi/L_HI"  # IHP SG13G2's tie cells, as LibreLane's synthesis names them
TIE_LO = "sg13g2_tielo/L_LO"
FAULT_SCL = {
    "dffMatches": [
        {"name": "sg13g2_dfrbpq_*", "d": "D", "q": "Q", "clk": "CLK"},
        {"name": "sg13g2_dfrbp_*", "d": "D", "q": "Q", "clk": "CLK"},
    ],
    "muxInfo": {
        "name": "sg13g2_mux2_1",
        "a": "A1",
        "b": "A0",
        "s": "S",
        "y": "X",
        "invertedOutput": False,
    },
}
SCAN_PORT_RE = re.compile(r"^scan_(enable|in|out)_\d+$")
FAULT_TIMEOUT_S = 3600.0
# The manifest of a scan insertion's two netlists, in its work directory.
SCAN_MANIFEST = "chipleap-scan.manifest.json"
MISMATCHES_SHOWN = 20


class DftError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanResult:
    netlist: Path  # the scan-inserted netlist
    prescan: Path  # the synthesized netlist it came from, as OpenROAD was given it
    chains: tuple[Chain, ...]
    clocks: dict[str, str]  # chain -> clock port
    scan_enable: str
    flops: int
    unscannable: tuple[str, ...]
    report: dict[str, list[str]]  # OpenROAD's plan: chain -> cells
    # The scan netlist's top module, parsed from the bytes that were hashed and written.
    top: Module | None = None
    # The manifest (SCAN_MANIFEST) recording both netlists; later uses copy them through it.
    sealed: Sealed | None = None


@dataclass
class AtpgResult:
    coverage_pct: float
    faults: int
    detected: int
    patterns: int
    generator_coverage_pct: float | None
    vectors: Path  # Fault's test vector file
    version: str = ""
    # The vector file as read once, for the patterns: the file is not read again.
    data: dict[str, Any] = field(default_factory=dict)
    # The hard macros' ports taken out of the test: inputs not observed, outputs tied to 0.
    unobserved: list[str] = field(default_factory=list)
    tied: list[str] = field(default_factory=list)


@dataclass
class Pattern:
    load: dict[str, str]  # chain -> bits, scan-in cell first
    inputs: dict[str, str]  # port bit -> 0/1
    outputs: dict[str, str]  # port bit -> expected 0/1
    unload: dict[str, str]  # chain -> expected bits, scan-in cell first


@dataclass
class SimResult:
    ok: bool
    flush_ok: bool
    patterns: int
    mismatches: list[str] = field(default_factory=list)
    errors: int = 0
    version: str = ""


def _ident(name: str) -> str:
    """A Verilog identifier for ``name``, escaped when it is not a plain one."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        return name
    if not name or any(c.isspace() for c in name):
        raise DftError(f"{name!r} cannot be a Verilog identifier")
    return f"\\{name} "


def _bit_ref(bit: str) -> str:
    """A reference to one bit: ``name`` or ``name[3]``, escaped as needed."""
    m = re.fullmatch(r"(.*)\[(\d+)\]", bit)
    if m:
        return f"{_ident(m.group(1))}[{m.group(2)}]"
    return _ident(bit)


# Scan insertion.


def scan_script(project: Project, files: dict[str, Any], netlist: str, cfg: DftConfig) -> str:
    """The OpenROAD script that inserts the scan chains. Every value from the project is
    a quoted word; the netlist and libraries are ChipLeap's staged copies."""
    macro = files.get("macros", {"lefs": [], "liberty": {}})
    lines = [
        f"read_lef {q.tcl(files['tech_lef'])}",
        *(f"read_lef {q.tcl(f)}" for f in files["cell_lefs"]),
        *(f"read_lef {q.tcl(f)}" for f in macro["lefs"]),
        f"read_liberty {q.tcl(files['liberty']['typ'])}",
        *(f"read_liberty {q.tcl(f)}" for f in macro["liberty"].get("typ", [])),
        f"read_verilog {q.tcl(netlist)}",
        f"link_design {q.tcl(project.top)}",
    ]
    for c in project.clocks:
        lines.append(
            f"create_clock -name {q.tcl(c.port)} -period {q.number(float(c.period_ns))} "
            f"[get_ports {q.tcl(c.port)}]"
        )
    opts = (
        f"-max_length {q.number(int(cfg.max_chain_length))} -clock_mixing {q.tcl(cfg.clock_mixing)}"
    )
    if cfg.max_chains:
        opts += f" -max_chains {q.number(int(cfg.max_chains))}"
    lines += [
        f"set_dft_config {opts}",
        "scan_replace",
        'puts "CHIPLEAP-DFT-PLAN"',
        # The command names changed in 2025; take whichever this OpenROAD has.
        "if {[llength [info commands report_dft_plan]]} {",
        "    report_dft_plan -verbose",
        "} else {",
        "    preview_dft -verbose",
        "}",
        'puts "CHIPLEAP-DFT-END"',
        "if {[llength [info commands execute_dft_plan]]} {",
        "    execute_dft_plan",
        "} else {",
        "    insert_dft",
        "}",
        # A hard macro's pins tied to a constant (an SRAM's A_DLY and BIST port) are
        # constants in the netlist, which OpenROAD writes as nets (one_, zero_) it never
        # declares; tie cells drive them instead, as LibreLane's own synthesis does.
        *(
            [
                f"insert_tiecells {q.tcl(TIE_HI)} -prefix TIE_ONE_",
                f"insert_tiecells {q.tcl(TIE_LO)} -prefix TIE_ZERO_",
            ]
            if project.macros
            else []
        ),
        f"write_verilog {q.tcl(project.top + '.scan.v')}",
    ]
    return "\n".join(lines) + "\n"


def parse_plan(log: str) -> dict[str, list[str]]:
    """OpenROAD's DFT plan: each chain's cells as the report lists them."""
    start = log.find("CHIPLEAP-DFT-PLAN")
    end = log.find("CHIPLEAP-DFT-END", start)
    if start < 0:
        return {}
    text = log[start : end if end > 0 else len(log)]
    chains: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        m = re.match(r"\s*Scan chain '([^']+)' has \d+ cells", line)
        if m:
            current = chains.setdefault(m.group(1), [])
            continue
        m = re.match(r"\s+(\S+)(?:\s+\(.*\))?\s*$", line)
        if current is not None and m and not line.strip().startswith(("*", "Number", "Clock")):
            current.append(m.group(1))
    return chains


def _clock_of(nets: Nets, top: Module, cell: str, clocks: list[str]) -> str:
    inst = nets.by_name.get(cell)
    if inst is None or not inst.pins.get("CLK"):
        return ""
    for port in clocks:
        if nets.same(inst.pins["CLK"][0], port):
            return port
    return ""


def chains_of(
    top: Module, clocks: list[str], through: dict[str, tuple[str, str]] | None = None
) -> tuple[list[Chain], dict[str, str]]:
    """Every scan chain of a netlist, traced from ``scan_in_N`` to ``scan_out_N``."""
    nets = Nets(top)
    ins = sorted(
        (p for p in top.ports if re.fullmatch(r"scan_in_\d+", p)), key=lambda p: int(p[8:])
    )
    chains, clock_of = [], {}
    outputs = top.port_bits("output")
    for port in ins:
        k = port[len("scan_in_") :]
        name = f"chain_{k}"
        # A chain ends on scan_out_N, or on a functional output its last flip-flop drives.
        out = f"scan_out_{k}"
        try:
            chain = trace_chain(
                nets,
                name,
                port,
                out if out in top.ports else outputs,
                si_pin=SCAN_IN_PIN,
                q_pins=SCAN_OUT_PINS,
                through=through,
            )
        except NetlistError as exc:
            raise DftError(str(exc)) from exc
        chains.append(chain)
        clock_of[name] = _clock_of(nets, top, chain.cells[0], clocks) if chain.cells else ""
    return chains, clock_of


def insert_scan(project: Project, workdir: Path | None = None) -> ScanResult:
    """Synthesize the project and insert its scan chains. Both netlists are recorded in
    ``SCAN_MANIFEST`` with the hashes of the bytes OpenROAD was given and the bytes it
    wrote, and every later use (ATPG, the chain simulation, place and route) copies them
    through it."""
    cfg = project.dft or DftConfig()
    work = project.work_dir(workdir or "dft")
    # First, so a run that stops halfway leaves no manifest for the netlists it replaces.
    unseal(work, SCAN_MANIFEST)
    views = pdk_views(project)
    try:
        synth = synthesize(project, work / "synth")
    except SynthError as exc:
        raise DftError(f"synthesis failed: {exc}") from exc
    job = Job(work, "scan")
    files = views.stage_all(job)
    # Scan insertion leaves the hard macros as they are: no macro has a scan equivalent.
    boxes = macro_views(project, work=builddir.work_dir(work, "macro_views"))
    files["macros"] = boxes.stage_all(job)
    netlist = job.stage(synth.netlist)
    # From here on the synthesized netlist is the staged copy: its hash was taken of the
    # bytes copied, and it is read only with that hash checked.
    prescan = job.dir / netlist
    pre_digest = job.digests[netlist]
    pre_text = _read_checked(prescan, pre_digest)
    pre = parse_netlist(pre_text).top(project.top)
    taken = [p for p in pre.ports if SCAN_PORT_RE.match(p)]
    if taken:
        raise DftError(
            f"the design already has a port named {taken[0]}, which scan insertion would add"
        )
    log, code = job.run(scan_script(project, files, netlist, cfg))
    written = job.output(f"{project.top}.scan.v")
    if code != 0 or not written.is_file():
        raise failure("OpenROAD scan insertion", log, code)
    # Read once: these bytes are hashed, written beside the step's other results (where
    # place and route and the report find them) and parsed.
    data = read_limited_bytes(written)
    out = builddir.write_bytes(work, written.name, data)
    top = parse_netlist(data.decode(errors="replace")).top(project.top)
    sealed = record(
        work,
        SCAN_MANIFEST,
        "scan",
        {
            prescan.relative_to(work).as_posix(): pre_digest,
            out.name: hashlib.sha256(data).hexdigest(),
        },
    )
    chains, clocks = chains_of(top, [c.port for c in project.clocks])
    flops = [i for i in top.instances if FLOP_CELL_RE.match(i.cell)]
    on_chain = {c for ch in chains for c in ch.cells}
    unscannable = tuple(sorted(i.name for i in flops if i.name not in on_chain))
    enables = sorted(p for p in top.ports if re.fullmatch(r"scan_enable_\d+", p))
    plan = parse_plan(log)
    for ch in chains:
        if ch.name in plan and plan[ch.name] != list(ch.cells):
            raise DftError(
                f"{ch.name}: OpenROAD's report and the netlist disagree on the order of its cells"
            )
    return ScanResult(
        netlist=out,
        prescan=prescan,
        chains=tuple(chains),
        clocks=clocks,
        scan_enable=enables[0] if enables else "",
        flops=len(flops),
        unscannable=unscannable,
        report=plan,
        top=top,
        sealed=sealed,
    )


def _read_checked(path: Path, digest: str) -> str:
    """The text of ``path``, read once and refused unless those bytes hash to ``digest``."""
    data = read_limited_bytes(path)
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
        raise DftError(f"{path.name} changed after ChipLeap copied it")
    return data.decode(errors="replace")


def scan_sealed(scan: ScanResult) -> Sealed:
    """The manifest of ``scan``'s netlists as it is on disk now: it must be there, verify,
    and be the one this insertion wrote (not an earlier or later run's)."""
    if scan.sealed is None:
        raise DftError("the scan netlists were not recorded by ChipLeap")
    try:
        sealed = open_sealed(scan.sealed.root, SCAN_MANIFEST, "scan")
    except SealError as exc:
        raise DftError(f"the scan netlists cannot be used: {exc}") from None
    if not hmac.compare_digest(sealed.mac, scan.sealed.mac):
        raise DftError(
            "the scan netlists were made again after this step inserted them; run it again"
        )
    return sealed


# ATPG with Fault.


_CUT_PORT_RE = re.compile(r"^\s*(input|output)\s+(\[[^\]]*\]\s*)?(\\\S+)\s*;\s*$", re.M)


def seal_macros(cut: str, instances: list[str]) -> tuple[str, list[str], list[str]]:
    """Fault's cut netlist with the hard macros' ports taken out of the test: a macro's
    inputs (``\\<inst>.<pin>.d`` outputs, as Fault cuts it) become plain wires, so no
    fault counts as detected there, and its outputs (``\\<inst>.<pin>`` inputs) become
    wires tied to 0, so no pattern sets them. Returns the netlist and the names of both.
    Scan chains cannot load or unload a macro, and in silicon only a test-mode gate on a
    macro's outputs makes them known: the coverage this gives assumes that gate."""
    prefixes = tuple(f"\\{i}." for i in instances)
    unobserved: list[str] = []
    tied: list[str] = []
    ties: list[str] = []

    def decl(m: re.Match[str]) -> str:
        kind, rng, name = m.group(1), m.group(2) or "", m.group(3)
        if not name.startswith(prefixes):
            return m.group(0)
        if kind == "output" and name.endswith(".d"):
            unobserved.append(name)
        elif kind == "input":
            tied.append(name)
            ties.append(f"  assign {name} = 0;")
        else:
            raise DftError(f"Fault cut a macro port it should not have: {name}")
        return f"  wire {rng}{name} ;"

    text = _CUT_PORT_RE.sub(decl, cut)
    gone = set(unobserved) | set(tied)
    head = re.search(r"\bmodule\s+\S+\s*\((.*?)\);", text, re.S)
    if head is None:
        raise DftError("Fault's cut netlist has no module header")
    ports = [p.strip() for p in head.group(1).split(",")]
    kept = ",\n  ".join(p for p in ports if p and p not in gone)
    text = text[: head.start(1)] + "\n  " + kept + "\n" + text[head.end(1) :]
    end = text.rfind("endmodule")
    if end < 0:
        raise DftError("Fault's cut netlist has no endmodule")
    text = text[:end] + "\n".join(ties) + ("\n" if ties else "") + text[end:]
    return text, unobserved, tied


def _bypass(project: Project) -> list[str]:
    """Fault's options for the clocks and resets, held inactive while it simulates."""
    args: list[str] = []
    clocks = [c.port for c in project.clocks]
    if clocks:
        args += ["--clock", clocks[0]]
        args += [f"--bypassing={c}" for c in clocks[1:]]
    else:
        args += ["--clock", "__chipleap_no_clock"]
    resets = list(project.resets)
    if resets:
        args += ["--reset", resets[0].port]
        if resets[0].active_low:
            args.append("--activeLow")
        args += [f"--bypassing={r.port}={1 if r.active_low else 0}" for r in resets[1:]]
    else:
        args += ["--reset", "__chipleap_no_reset"]
    return args


def _stage(src: Path, work: Path, name: str | None = None, sealed: Sealed | None = None) -> str:
    """Copy ``src`` into ``work`` (a directory the step just emptied) for a tool that runs
    there; with ``sealed``, ``src`` must be a file it recorded, unchanged."""
    dest = work / (name or src.name)
    try:
        if sealed is not None:
            sealed.copy(src, dest)
        else:
            copy_file(src.resolve(), dest)
    except SealError as exc:
        raise DftError(str(exc)) from None
    return dest.name


def _run(cmd: list[str], work: Path, log: str, env: dict[str, str]) -> tuple[int, str]:
    limit = float(os.environ.get("CHIPLEAP_FAULT_TIMEOUT", str(FAULT_TIMEOUT_S)))
    try:
        code = launch.run(cmd, work, work / log, limit, env=env)
    except launch.TimedOut as exc:
        raise DftError(f"{Path(cmd[0]).name} timed out after {exc.seconds:g} s") from None
    except launch.Busy as exc:
        raise DftError(str(exc)) from None
    return code, read_limited(work / log)


def run_atpg(project: Project, scan: ScanResult, views: PdkViews, workdir: Path) -> AtpgResult:
    """Cut, convert, generate and fault-simulate with Fault and Quaigh."""
    tool, conv, gen = fault(), nl2bench(), quaigh()
    for t, why in ((tool, "Fault"), (conv, "nl2bench (Fault's)"), (gen, "Quaigh")):
        if not t.found or t.path is None:
            raise OpenroadUnavailable(f"{why} is not installed")
    if not gl_iverilog().found:
        raise OpenroadUnavailable("Fault simulates with Icarus 13 or newer, not installed")
    assert tool.path is not None and conv.path is not None
    sealed = scan_sealed(scan)
    work = builddir.work_dir(workdir, "atpg", empty=True)
    top = project.top
    _stage(scan.prescan, work, f"{top}.nl.v", sealed)
    builddir.write_text(work, "scl.yml", yaml.safe_dump(FAULT_SCL, sort_keys=False))
    env = fault_env(tool.path)
    bypass = _bypass(project)
    # The hard macros are cut like flip-flops (Fault's black boxes), then taken out of
    # the test (seal_macros).
    from chipleap.rtl import macros

    stubs = [_stage(b, work) for b in macros.stubs(project)]
    boxes = (
        ["--blackbox", ",".join(Path(b).stem for b in stubs), "--blackbox-models", ",".join(stubs)]
        if stubs
        else []
    )
    code, log = _run(
        [tool.path, "cut", "-s", "scl.yml", *boxes, *bypass, "-o", f"{top}.cut.v", f"{top}.nl.v"],
        work,
        "cut.log",
        env,
    )
    if code != 0 or not (work / f"{top}.cut.v").is_file():
        raise DftError(f"fault cut failed (exit {code}): {_last(log)}")
    unobserved: list[str] = []
    tied: list[str] = []
    if stubs:
        cut = read_limited(work / f"{top}.cut.v")
        text, unobserved, tied = seal_macros(cut, [m.flat for m in project.macros])
        builddir.write_text(work, f"{top}.cut.v", text)
    lib = _stage(views.lib("typ"), work)
    code, log = _run(
        [conv.path, "-o", f"{top}.bench", "-l", lib, f"{top}.cut.v"], work, "bench.log", env
    )
    if code != 0 or not (work / f"{top}.bench").is_file():
        raise DftError(f"nl2bench failed (exit {code}): {_last(log)}")
    models = [_stage(m, work) for m in views.models]
    cmd = [
        tool.path,
        "atpg",
        "-g",
        "Quaigh",
        "-b",
        f"{top}.bench",
        "-c",
        models[-1],
        *(a for m in models[:-1] for a in ("-I", m)),
        *bypass,
        "--rng-seed",
        f"{project.tests.seed & 0xFFFFFFFFFFFFFFFF:016X}",
        "-o",
        f"{top}.tv.json",
        "--output-covered",
        f"{top}.cov.yml",
        f"{top}.cut.v",
    ]
    code, log = _run(cmd, work, "atpg.log", env)
    # Fault's per-thread scratch directories; a symlink among them is only unlinked.
    for junk in work.glob("thr0x*"):
        if junk.is_symlink():
            with suppress(OSError):
                junk.unlink()
        else:
            shutil.rmtree(junk, ignore_errors=True)
    # Fault compacts the vectors after simulating them, but not the same way every run
    # (25 or 26 of the UART's 39), so ChipLeap keeps Quaigh's set, which is the same every
    # run and has the same coverage.
    vectors = work / f"{top}.raw_tv.json"
    if not vectors.is_file():
        vectors = work / f"{top}.tv.json"
    if code != 0 or not vectors.is_file():
        raise DftError(f"fault atpg failed (exit {code}): {_last(log)}")
    result = parse_atpg(log, read_limited(work / f"{top}.cov.yml"), vectors, tool.version)
    result.unobserved, result.tied = unobserved, tied
    return result


def _last(log: str) -> str:
    lines = [ln for ln in log.strip().splitlines() if ln.strip() and "sorry:" not in ln]
    return lines[-1] if lines else "no output"


def parse_atpg(log: str, coverage_yaml: str, vectors: Path, version: str = "") -> AtpgResult:
    """Fault's coverage from its YAML, the pattern count from its vectors, and Quaigh's
    own figure from the log."""
    try:
        cov = yaml.safe_load(coverage_yaml) or {}
    except yaml.YAMLError as exc:
        raise DftError(f"Fault's coverage file is not valid YAML: {exc}") from None
    if not isinstance(cov, dict):
        raise DftError("Fault's coverage file is not a mapping")

    def count(key: str) -> int:
        v = cov.get(key)
        return len(v) if isinstance(v, list) else 0

    # Every fault site can be stuck at 0 or at 1. (The uncovered lists also name sites
    # the covered lists hold, so they are not counted.)
    detected = count("sa0Covered") + count("sa1Covered")
    faults = 2 * count("faultPoints")
    # PyYAML reads Swift's "1e+0" as a string, not a number.
    try:
        pct = float(str(cov.get("ratio"))) * 100
    except ValueError:
        pct = 0.0
    if not faults:
        m = re.search(r"Simulations concluded: Coverage ([\d.]+)%", log)
        pct = float(m.group(1)) if m else pct
    gen = re.findall(r"detecting \d+/\d+ faults \(([\d.]+)% coverage\)", log)
    data = read_json(vectors)
    if not isinstance(data, dict):
        raise DftError("Fault's vectors file is not a mapping")
    patterns = len(data.get("coverageList", []))
    return AtpgResult(
        coverage_pct=round(pct, 2),
        faults=faults,
        detected=detected,
        patterns=patterns,
        generator_coverage_pct=float(gen[-1]) if gen else None,
        vectors=vectors,
        version=version,
        data=data,
    )


def _bits_of(entry: dict[str, Any], value: int) -> dict[str, str]:
    """A Fault port value as bits: bit i of the value is the port's bit at offset i from
    its least significant end."""
    name = str(entry["name"]).lstrip("\\").strip()
    lo, hi = sorted((int(entry.get("from", 0)), int(entry.get("to", 0))))
    if hi == lo == 0 and "from" not in entry:
        return {name: str(value & 1)}
    if hi == lo:
        return {name if lo == 0 else f"{name}[{lo}]": str(value & 1)}
    return {f"{name}[{lo + i}]": str((value >> i) & 1) for i in range(hi - lo + 1)}


def map_patterns(data: dict[str, Any], scan: ScanResult, top: Module) -> list[Pattern]:
    """Fault's vectors laid onto the scan chains: each cut flip-flop's input value is what
    the chain loads into it, its D output what the chain captures."""
    inputs = data.get("inputs") or []
    outputs = data.get("outputs") or []
    vectors = data.get("coverageList") or []
    if (
        not isinstance(inputs, list)
        or not isinstance(outputs, list)
        or not isinstance(vectors, list)
    ):
        raise DftError("Fault's vectors file does not have inputs, outputs and coverageList")
    cells = {c for ch in scan.chains for c in ch.cells}
    scalar = {p for p in top.ports if p not in top.ranges}
    out: list[Pattern] = []
    # In a stable order: Fault lists them in the order its threads finish.
    for v in sorted(vectors, key=lambda v: json.dumps(v.get("vector"), sort_keys=True)):
        values = v.get("vector") or []
        if len(values) != len(inputs):
            raise DftError("a Fault vector does not have one value per input")
        state: dict[str, str] = {}
        pis: dict[str, str] = {}
        for entry, (_, value) in zip(inputs, values, strict=True):
            name = str(entry["name"]).lstrip("\\").strip()
            if name in cells:
                state[name] = str(int(value) & 1)
            else:
                width_one = name in scalar
                bits = {name: str(int(value) & 1)} if width_one else _bits_of(entry, int(value))
                pis.update(bits)
        golden = int(str(v.get("goldenOutput", "0")), 16)
        k = 0
        captured: dict[str, str] = {}
        pos: dict[str, str] = {}
        for entry in outputs:
            name = str(entry["name"]).lstrip("\\").strip()
            width = abs(int(entry.get("to", 0)) - int(entry.get("from", 0))) + 1
            for i in range(width):
                bit = str((golden >> k) & 1)
                k += 1
                if name.endswith(".d") and name[:-2] in cells:
                    captured[name[:-2]] = bit
                elif name in scalar:
                    pos[name] = bit
                else:
                    pos[f"{name}[{i}]"] = bit
        missing = [c for c in cells if c not in state or c not in captured]
        if missing:
            raise DftError(f"Fault's vectors do not cover flip-flop {missing[0]}")
        out.append(
            Pattern(
                load={ch.name: "".join(state[c] for c in ch.cells) for ch in scan.chains},
                inputs=pis,
                outputs=pos,
                unload={ch.name: "".join(captured[c] for c in ch.cells) for ch in scan.chains},
            )
        )
    return out


# Pattern files.


def patterns_json(
    scan: ScanResult, patterns: list[Pattern], atpg: AtpgResult | None
) -> dict[str, Any]:
    return {
        "format": "chipleap.patterns/v1",
        "scan_enable": scan.scan_enable,
        "chains": [
            {
                "name": ch.name,
                "scan_in": ch.scan_in,
                "scan_out": ch.scan_out,
                "clock": scan.clocks.get(ch.name, ""),
                "cells": list(ch.cells),
            }
            for ch in scan.chains
        ],
        "coverage_pct": atpg.coverage_pct if atpg else None,
        "patterns": [
            {"load": p.load, "inputs": p.inputs, "outputs": p.outputs, "unload": p.unload}
            for p in patterns
        ],
    }


def stil(project: Project, scan: ScanResult, patterns: list[Pattern], top: Module) -> str:
    """The patterns as STIL (IEEE 1450): signals, scan structures, a load-unload and a
    capture procedure, and one pattern per vector. Chains are loaded scan-in cell first."""
    clocks = [c.port for c in project.clocks]
    pis = sorted({b for p in patterns for b in p.inputs})
    pos = sorted({b for p in patterns for b in p.outputs})
    period = float(project.fastest_clock.period_ns) if project.fastest_clock else 10.0

    def sig(bit: str) -> str:
        return '"' + bit.replace('"', "") + '"'

    lines = [
        "STIL 1.0;",
        "Header {",
        f'    Title "{project.name} stuck-at patterns";',
        '    Source "ChipLeap: OpenROAD scan insertion, Fault with Quaigh";',
        "}",
        "Signals {",
        *(f"    {sig(c)} In;" for c in clocks),
        *(f"    {sig(r.port)} In;" for r in project.resets),
        f"    {sig(scan.scan_enable)} In;",
        *(f"    {sig(ch.scan_in)} In {{ ScanIn; }}" for ch in scan.chains),
        *(f"    {sig(ch.scan_out)} Out {{ ScanOut; }}" for ch in scan.chains),
        *(f"    {sig(b)} In;" for b in pis),
        *(f"    {sig(b)} Out;" for b in pos),
        "}",
        "SignalGroups {",
        f"    _pi = '{'+'.join(sig(b) for b in pis) or sig(scan.scan_enable)}';",
        *([f"    _po = '{'+'.join(sig(b) for b in pos)}';"] if pos else []),
        "}",
        "ScanStructures {",
    ]
    for ch in scan.chains:
        lines += [
            f"    ScanChain {sig(ch.name)} {{",
            f"        ScanLength {len(ch.cells)};",
            f"        ScanIn {sig(ch.scan_in)};",
            f"        ScanOut {sig(ch.scan_out)};",
            f"        ScanEnable {sig(scan.scan_enable)};",
            f"        ScanMasterClock {sig(scan.clocks.get(ch.name) or (clocks[0] if clocks else 'clk'))};",
            "        ScanCells " + " ".join(sig(c) for c in ch.cells) + ";",
            "    }",
        ]
    lines += [
        "}",
        "Timing {",
        '    WaveformTable "_default_" {',
        f"        Period '{period:g}ns';",
        "        Waveforms {",
        *(f"            {sig(c)} {{ 01 {{ '0ns' D/U; '{period / 2:g}ns' D; }} }}" for c in clocks),
        "            _pi { 01 { '0ns' D/U; } }",
        *(
            ["            _po { LHX { '0ns' X; '" + f"{period * 0.9:g}" + "ns' L/H/X; } }"]
            if pos
            else []
        ),
        "        }",
        "    }",
        "}",
        'PatternBurst "_burst_" { PatList { "_patterns_"; } }',
        'PatternExec { PatternBurst "_burst_"; }',
        "Procedures {",
        '    "load_unload" {',
        '        W "_default_";',
        f"        V {{ {sig(scan.scan_enable)} = 1; }}",
        "        Shift { V { "
        + " ".join(f"{sig(ch.scan_in)} = #; {sig(ch.scan_out)} = #;" for ch in scan.chains)
        + " "
        + " ".join(f"{sig(c)} = P;" for c in clocks)
        + " } }",
        "    }",
        '    "capture" {',
        '        W "_default_";',
        f"        V {{ {sig(scan.scan_enable)} = 0; _pi = #; {'_po = #; ' if pos else ''}}}",
        "        V { " + " ".join(f"{sig(c)} = P;" for c in clocks) + " }",
        "    }",
        "}",
        'Pattern "_patterns_" {',
        '    W "_default_";',
    ]
    for n, p in enumerate(patterns):
        # A chain's bits in STIL's shift order: the cell nearest scan-out first.
        load = " ".join(f"{sig(ch.name)} = {p.load[ch.name][::-1]};" for ch in scan.chains)
        lines.append(f'    "pattern {n}": Call "load_unload" {{ {load} }}')
        pi = "".join(p.inputs.get(b, "0") for b in pis)
        po = "".join({"0": "L", "1": "H"}[p.outputs[b]] if b in p.outputs else "X" for b in pos)
        lines.append(f'    Call "capture" {{ _pi = {pi or "0"}; {f"_po = {po}; " if pos else ""}}}')
        unload = " ".join(
            f"{sig(ch.name)} = {''.join({'0': 'L', '1': 'H'}[b] for b in p.unload[ch.name][::-1])};"
            for ch in scan.chains
        )
        lines.append(f'    Call "load_unload" {{ {unload} }}')
    lines.append("}")
    return "\n".join(lines) + "\n"


# Simulation of the chains and patterns.


@dataclass(frozen=True)
class _Bench:
    pi: list[str]  # input port bits the patterns drive
    po: list[str]  # output port bits they check
    chains: list[Chain]
    shift: int  # cycles to load or unload the longest chain


def _bench(scan: ScanResult, top: Module, clocks: list[str], resets: list[str]) -> _Bench:
    special = set(clocks) | set(resets) | {scan.scan_enable}
    special |= {ch.scan_in for ch in scan.chains}
    pi = [
        b
        for p in top.ports
        if top.directions.get(p) == "input" and p not in special
        for b in top.bits(p)
    ]
    # Dedicated scan outputs only: a chain may also end on a functional output, which
    # stays an output the patterns check.
    outs = {ch.scan_out for ch in scan.chains if SCAN_PORT_RE.match(ch.scan_out)}
    po = [
        b
        for p in top.ports
        if top.directions.get(p) == "output" and p not in outs
        for b in top.bits(p)
    ]
    shift = max((len(ch.cells) for ch in scan.chains), default=0)
    return _Bench(pi, po, list(scan.chains), shift)


def write_bench_data(
    bench: _Bench, patterns: list[Pattern], work: Path, flush_extra: int = 8
) -> tuple[int, int]:
    """The testbench's data files: per shift cycle the scan-in bits, the expected scan-out
    bits and a mask of which to check, for the flush pattern and for each ATPG pattern;
    per pattern the inputs and the expected outputs with their mask. Returns the number of
    flush cycles and of shift cycles per pattern."""
    n = len(bench.chains)
    t_flush = bench.shift + flush_extra
    flush_in, flush_out, flush_mask = [], [], []
    seq = [(t // 2) % 2 for t in range(t_flush)]  # 0011 repeated
    for t in range(t_flush):
        sin, sout, smask = ["0"] * n, ["0"] * n, ["0"] * n
        for c, ch in enumerate(bench.chains):
            sin[c] = str(seq[t])
            if t >= len(ch.cells):
                sout[c], smask[c] = str(seq[t - len(ch.cells)]), "1"
        # Chain 0 is the least significant bit of each word.
        flush_in.append("".join(reversed(sin)))
        flush_out.append("".join(reversed(sout)))
        flush_mask.append("".join(reversed(smask)))
    shift_in, shift_out, shift_mask, pi, po, po_mask = [], [], [], [], [], []
    for p in patterns:
        for t in range(bench.shift):
            sin, sout, smask = ["0"] * n, ["0"] * n, ["0"] * n
            for c, ch in enumerate(bench.chains):
                length = len(ch.cells)
                start = bench.shift - length
                if t >= start:
                    # Shift in the cell nearest scan-out first.
                    sin[c] = p.load[ch.name][length - 1 - (t - start)]
                if t < length:
                    # Unload: scan-out shows the cell nearest it first.
                    sout[c], smask[c] = p.unload[ch.name][length - 1 - t], "1"
            shift_in.append("".join(reversed(sin)))
            shift_out.append("".join(reversed(sout)))
            shift_mask.append("".join(reversed(smask)))
        pi.append("".join(p.inputs.get(b, "0") for b in reversed(bench.pi)) or "0")
        po.append("".join(p.outputs.get(b, "0") for b in reversed(bench.po)) or "0")
        po_mask.append("".join("1" if b in p.outputs else "0" for b in reversed(bench.po)) or "0")
    files = {
        "flush_in": flush_in,
        "flush_out": flush_out,
        "flush_mask": flush_mask,
        "shift_in": shift_in or ["0" * max(n, 1)],
        "shift_out": shift_out or ["0" * max(n, 1)],
        "shift_mask": shift_mask or ["0" * max(n, 1)],
        "pi": pi or ["0"],
        "po": po or ["0"],
        "po_mask": po_mask or ["0"],
    }
    for name, rows in files.items():
        builddir.write_text(work, f"{name}.mem", "\n".join(rows) + "\n")
    return t_flush, bench.shift


def testbench(
    project: Project,
    top: Module,
    scan: ScanResult,
    bench: _Bench,
    t_flush: int,
    n_patterns: int,
    tied: tuple[str, ...] = (),
) -> str:
    """A Verilog testbench that shifts the flush pattern and then each ATPG pattern through
    the chains of the scan netlist, capturing once per pattern. ``tied`` are the hard
    macros' outputs ATPG took as 0 (``seal_macros``), held at 0 here too."""
    clocks = [c.port for c in project.clocks]
    resets = {r.port: r for r in project.resets}
    n = max(len(bench.chains), 1)
    npi, npo = max(len(bench.pi), 1), max(len(bench.po), 1)
    shift = max(bench.shift, 1)
    lines = [
        "`timescale 1ns/1ps",
        "module chipleap_scan_tb;",
        f"  reg [{n - 1}:0] flush_in [0:{t_flush - 1}];",
        f"  reg [{n - 1}:0] flush_out [0:{t_flush - 1}];",
        f"  reg [{n - 1}:0] flush_mask [0:{t_flush - 1}];",
        f"  reg [{n - 1}:0] shift_in [0:{max(n_patterns, 1) * shift - 1}];",
        f"  reg [{n - 1}:0] shift_out [0:{max(n_patterns, 1) * shift - 1}];",
        f"  reg [{n - 1}:0] shift_mask [0:{max(n_patterns, 1) * shift - 1}];",
        f"  reg [{npi - 1}:0] pi [0:{max(n_patterns, 1) - 1}];",
        f"  reg [{npo - 1}:0] po [0:{max(n_patterns, 1) - 1}];",
        f"  reg [{npo - 1}:0] po_mask [0:{max(n_patterns, 1) - 1}];",
        "  reg clk;",
        "  reg se;",
        f"  reg [{n - 1}:0] sin;",
        f"  wire [{n - 1}:0] sout;",
        f"  reg [{npi - 1}:0] pi_cur;",
        f"  wire [{npo - 1}:0] po_now;",
        "  integer errors, t, p, c, shown;",
    ]
    conns = []
    dedicated = {
        ch.scan_out: c for c, ch in enumerate(bench.chains) if SCAN_PORT_RE.match(ch.scan_out)
    }
    for c, ch in enumerate(bench.chains):
        if ch.scan_out not in dedicated:
            lines.append(f"  assign sout[{c}] = po_now[{bench.po.index(ch.scan_out)}];")
    for port in top.ports:
        d = top.directions.get(port)
        bits = top.bits(port)
        if port in clocks:
            expr = "clk"
        elif port in resets:
            expr = "1'b1" if resets[port].active_low else "1'b0"
        elif port == scan.scan_enable:
            expr = "se"
        elif d == "input" and port in {ch.scan_in for ch in bench.chains}:
            expr = f"sin[{[ch.scan_in for ch in bench.chains].index(port)}]"
        elif d == "output" and port in dedicated:
            expr = f"sout[{dedicated[port]}]"
        elif d == "input":
            expr = "{" + ", ".join(f"pi_cur[{bench.pi.index(b)}]" for b in bits) + "}"
        elif d == "output":
            expr = "{" + ", ".join(f"po_now[{bench.po.index(b)}]" for b in bits) + "}"
        else:
            expr = ""
        conns.append(f"    .{_ident(port)}({expr})")
    if not bench.pi:
        lines.append("  initial pi_cur = 1'b0;")
    if not bench.po:
        lines.append("  assign po_now = 1'b0;")
    lines += [f"  {_ident(project.top)} dut (", ",\n".join(conns), "  );"]
    for name in tied:
        inst, _, pin = name.lstrip("\\").rpartition(".")
        lines.append(f"  initial force dut.{_ident(inst)}.{_ident(pin)} = 0;")
    lines += [
        "  task tick; begin #10 clk = 1'b1; #10 clk = 1'b0; end endtask",
        "  initial begin",
        '    $readmemb("flush_in.mem", flush_in);',
        '    $readmemb("flush_out.mem", flush_out);',
        '    $readmemb("flush_mask.mem", flush_mask);',
        '    $readmemb("shift_in.mem", shift_in);',
        '    $readmemb("shift_out.mem", shift_out);',
        '    $readmemb("shift_mask.mem", shift_mask);',
        '    $readmemb("pi.mem", pi);',
        '    $readmemb("po.mem", po);',
        '    $readmemb("po_mask.mem", po_mask);',
        "    errors = 0; shown = 0; clk = 1'b0; se = 1'b1; sin = 0; pi_cur = 0;",
        "    #5;",
        f"    for (t = 0; t < {t_flush}; t = t + 1) begin",
        "      sin = flush_in[t]; #1;",
        f"      for (c = 0; c < {n}; c = c + 1)",
        "        if (flush_mask[t][c] && sout[c] !== flush_out[t][c]) begin",
        "          errors = errors + 1;",
        f"          if (shown < {MISMATCHES_SHOWN}) begin",
        '            $display("CHIPLEAP-SCAN flush chain=%0d cycle=%0d got=%b want=%b", c, t, sout[c], flush_out[t][c]);',
        "            shown = shown + 1;",
        "          end",
        "        end",
        "      tick;",
        "    end",
        '    $display("CHIPLEAP-SCAN flush-done errors=%0d", errors);',
        f"    for (p = 0; p < {n_patterns}; p = p + 1) begin",
        "      se = 1'b1;",
        f"      for (t = 0; t < {shift}; t = t + 1) begin sin = shift_in[p * {shift} + t]; #1; tick; end",
        "      pi_cur = pi[p]; se = 1'b0; #2;",
        f"      for (c = 0; c < {npo}; c = c + 1)",
        "        if (po_mask[p][c] && po_now[c] !== po[p][c]) begin",
        "          errors = errors + 1;",
        f"          if (shown < {MISMATCHES_SHOWN}) begin",
        '            $display("CHIPLEAP-SCAN output pattern=%0d bit=%0d got=%b want=%b", p, c, po_now[c], po[p][c]);',
        "            shown = shown + 1;",
        "          end",
        "        end",
        "      tick;",
        "      se = 1'b1;",
        f"      for (t = 0; t < {shift}; t = t + 1) begin",
        "        #1;",
        f"        for (c = 0; c < {n}; c = c + 1)",
        f"          if (shift_mask[p * {shift} + t][c] && sout[c] !== shift_out[p * {shift} + t][c]) begin",
        "            errors = errors + 1;",
        f"            if (shown < {MISMATCHES_SHOWN}) begin",
        f'              $display("CHIPLEAP-SCAN unload pattern=%0d chain=%0d cycle=%0d got=%b want=%b", p, c, t, sout[c], shift_out[p * {shift} + t][c]);',
        "              shown = shown + 1;",
        "            end",
        "          end",
        "        tick;",
        "      end",
        "    end",
        '    $display("CHIPLEAP-SCAN done patterns=%0d errors=%0d", p, errors);',
        "    $finish;",
        "  end",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


def parse_sim(log: str) -> tuple[bool, int, int, list[str], bool]:
    """(finished, patterns, errors, mismatches, flush ok) from the testbench's output."""
    mism = [
        ln.strip()[len("CHIPLEAP-SCAN ") :]
        for ln in log.splitlines()
        if re.match(r"\s*CHIPLEAP-SCAN (flush|output|unload) ", ln)
    ]
    flush = re.search(r"CHIPLEAP-SCAN flush-done errors=(\d+)", log)
    done = re.search(r"CHIPLEAP-SCAN done patterns=(\d+) errors=(\d+)", log)
    if not done:
        return False, 0, 0, mism, bool(flush and flush.group(1) == "0")
    return True, int(done.group(1)), int(done.group(2)), mism, bool(flush and flush.group(1) == "0")


def simulate_patterns(
    project: Project,
    scan: ScanResult,
    patterns: list[Pattern],
    views: PdkViews,
    workdir: Path,
    netlist: Path | None = None,
    name: str = "sim",
    sealed: Sealed | None = None,
    tied: tuple[str, ...] = (),
) -> SimResult:
    """Run the flush pattern and ``patterns`` through the chains of ``netlist`` (the scan
    netlist by default) on Icarus 13 with the cell models. Either netlist is simulated
    only as it was recorded: the scan netlist through the insertion's manifest, a routed
    one through its run's, ``sealed``. The copy is checked as it is made, and the testbench
    is built from the copy."""
    tool = gl_iverilog()
    vvp = gl_vvp(tool)
    if not tool.found or tool.path is None or vvp is None:
        raise OpenroadUnavailable("Icarus 13 or newer is not installed (CHIPLEAP_GL_IVERILOG)")
    if netlist is not None and sealed is None:
        raise DftError(f"{netlist.name} was not recorded by ChipLeap")
    source = sealed if netlist is not None else scan_sealed(scan)
    work = builddir.work_dir(workdir, name, empty=True)
    dut = _stage(netlist or scan.netlist, work, sealed=source)
    models = [_stage(m, work) for m in views.models]
    # The hard macros as their black boxes: the patterns hold their outputs (``tied``).
    from chipleap.rtl import macros

    models += [_stage(b, work) for b in macros.stubs(project)]
    top = read_netlist([work / dut]).top(project.top)
    clocks = [c.port for c in project.clocks]
    bench = _bench(scan, top, clocks, [r.port for r in project.resets])
    t_flush, _ = write_bench_data(bench, patterns, work)
    builddir.write_text(
        work, "tb.v", testbench(project, top, scan, bench, t_flush, len(patterns), tied)
    )
    env = clean_env()
    compile_cmd = [
        tool.path,
        "-g2012",
        "-o",
        "tb.vvp",
        "-s",
        "chipleap_scan_tb",
        "tb.v",
        dut,
        *models,
    ]
    code, log = _run(compile_cmd, work, "compile.log", env)
    if code != 0:
        errs = [ln for ln in log.splitlines() if "error" in ln.lower() and "sorry" not in ln]
        raise DftError(
            f"Icarus could not compile the scan testbench: {errs[0] if errs else _last(log)}"
        )
    code, log = _run([vvp, "-n", "tb.vvp"], work, "sim.log", env)
    finished, n, errors, mism, flush_ok = parse_sim(log)
    if not finished:
        raise DftError(f"the scan simulation did not finish (exit {code}): {_last(log)}")
    return SimResult(
        ok=errors == 0 and n == len(patterns),
        flush_ok=flush_ok,
        patterns=n,
        mismatches=mism[:MISMATCHES_SHOWN],
        errors=errors,
        version=tool.version,
    )


# The step.


def _sim_data(sim: SimResult | None) -> dict[str, Any] | None:
    if sim is None:
        return None
    return {
        "simulator": f"Icarus {sim.version}",
        "flush": "pass" if sim.flush_ok else "fail",
        "patterns": sim.patterns,
        "errors": sim.errors,
        "mismatches": sim.mismatches,
    }


def dft_data(
    project: Project,
    scan: ScanResult,
    atpg: AtpgResult | None,
    sim: SimResult | None,
    routed: SimResult | None,
    notes: list[str],
) -> dict[str, Any]:
    return {
        "tool": "OpenROAD",
        "chains": [
            {
                "name": ch.name,
                "length": len(ch.cells),
                "scan_in": ch.scan_in,
                "scan_out": ch.scan_out,
                "clock": scan.clocks.get(ch.name, ""),
                "first": ch.cells[0] if ch.cells else "",
                "last": ch.cells[-1] if ch.cells else "",
            }
            for ch in scan.chains
        ],
        "scan_enable": scan.scan_enable,
        "flops": scan.flops,
        "scanned": sum(len(ch.cells) for ch in scan.chains),
        "unscannable": list(scan.unscannable),
        "atpg": None
        if atpg is None
        else {
            "tool": f"Fault {atpg.version} with Quaigh".replace("  ", " "),
            "coverage_pct": atpg.coverage_pct,
            "faults": atpg.faults,
            "detected": atpg.detected,
            "patterns": atpg.patterns,
            "generator_coverage_pct": atpg.generator_coverage_pct,
            **(
                {
                    "macro_inputs_not_observed": len(atpg.unobserved),
                    "macro_outputs_held_at_0": len(atpg.tied),
                }
                if project.macros
                else {}
            ),
        },
        "simulation": _sim_data(sim),
        "simulation_routed": _sim_data(routed),
        "scan_netlist": project.rel(scan.netlist),
        "patterns_json": project.rel(project.build_dir / "dft" / "patterns.json"),
        "patterns_stil": project.rel(project.build_dir / "dft" / "patterns.stil"),
        "notes": notes,
    }


def _macro_outputs(project: Project) -> tuple[str, ...]:
    """Every output of every hard macro, as Fault names a cut macro's outputs
    (``\\<instance>.<pin>``): what the chain simulation holds at 0 without ATPG."""
    from chipleap.rtl import macros
    from chipleap.rtl.cdc import macro_boxes

    boxes = macro_boxes(project)
    out: list[str] = []
    for m in project.macros:
        cell = m.cell if m.package is None else macros.manifest(project, m.package).name
        out += [f"\\{m.flat}.{pin}" for pin in boxes[cell].outputs]
    return tuple(out)


def run_dft(
    project: Project,
) -> tuple[ScanResult, AtpgResult | None, SimResult | None, SimResult | None, list[str]]:
    cfg = project.dft or DftConfig()
    work = project.work_dir("dft")
    # Pattern files of an earlier run are not this run's.
    for stale in ("patterns.json", "patterns.stil"):
        with suppress(FileNotFoundError):
            (work / stale).unlink()
    scan = insert_scan(project, work)
    views = pdk_views(project)
    notes: list[str] = []
    atpg: AtpgResult | None = None
    patterns: list[Pattern] = []
    if cfg.atpg and not scan.unscannable:
        try:
            atpg = run_atpg(project, scan, views, work)
        except OpenroadUnavailable as exc:
            notes.append(f"ATPG skipped: {exc}")
        if atpg is not None:
            # The netlist and the vectors as they were read and checked, not read again.
            top = scan.top or read_netlist([scan.netlist]).top(project.top)
            patterns = map_patterns(atpg.data, scan, top)
            builddir.write_text(
                work, "patterns.json", json.dumps(patterns_json(scan, patterns, atpg), indent=1)
            )
            builddir.write_text(work, "patterns.stil", stil(project, scan, patterns, top))
    elif scan.unscannable:
        notes.append(
            f"ATPG skipped: {len(scan.unscannable)} flip-flops or latches are not on a chain "
            f"({', '.join(scan.unscannable[:3])})"
        )
    else:
        notes.append("ATPG off (dft.atpg is false)")
    sim: SimResult | None = None
    routed: SimResult | None = None
    try:
        tied = tuple(atpg.tied) if atpg else _macro_outputs(project)
        sim = simulate_patterns(project, scan, patterns[: cfg.patterns], views, work, tied=tied)
    except OpenroadUnavailable as exc:
        notes.append(f"chain simulation skipped: {exc}")
        return scan, atpg, sim, routed, notes
    try:
        routed = simulate_routed(project, scan, patterns[: cfg.patterns], work, tied)
    except OpenroadError as exc:
        # The routed check is a second look when there is a routed design; one ChipLeap
        # did not record here is not read, and the scan netlist's result stands.
        notes.append(f"the routed netlist was not simulated: {str(exc).splitlines()[0]}")
    return scan, atpg, sim, routed, notes


def simulate_routed(
    project: Project,
    scan: ScanResult,
    patterns: list[Pattern],
    work: Path,
    tied: tuple[str, ...] = (),
) -> SimResult | None:
    """The same chains and patterns on the routed netlist, when the routed design has the
    scan chains: place and route adds buffers to them but must keep their order."""
    run = routed_run(project)
    if run is None:
        return None
    views = routed_views(project, run)
    top = parse_netlist(views.read_text(views.netlist)).top(project.top)
    if not any(SCAN_PORT_RE.match(p) for p in top.ports):
        return None
    chains, _ = chains_of(top, [c.port for c in project.clocks], BUFFERS)
    if [c.cells for c in chains] != [c.cells for c in scan.chains]:
        raise DftError("the routed netlist's scan chains are not the scan netlist's")
    return simulate_patterns(
        project,
        scan,
        patterns,
        pdk_views(project, run),
        work,
        views.netlist,
        "sim-routed",
        views.sealed,
        tied,
    )


def dft(project: Project) -> StepResult:
    """The DFT step: scan chains inserted and traced, ATPG coverage, and the chains and
    patterns simulated on the scan netlist."""
    if project.dft is None:
        return StepResult("dft", "skipped", "no dft section in the project")
    try:
        with untrusted(*project.roots):
            scan, atpg, sim, routed, notes = run_dft(project)
    except OpenroadUnavailable as exc:
        return StepResult("dft", "skipped", str(exc))
    except (DftError, OpenroadError, NetlistError, builddir.BuildDirError) as exc:
        return StepResult("dft", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    cfg = project.dft
    data = dft_data(project, scan, atpg, sim, routed, notes)
    lengths = sorted({len(ch.cells) for ch in scan.chains})
    of = f"{lengths[0]}" if len(lengths) == 1 else f"{lengths[0]} to {lengths[-1]}"
    chains = (
        f"{len(scan.chains)} scan chain{'s' if len(scan.chains) != 1 else ''} of {of} flip-flops"
    )
    parts = [f"{chains} ({data['scanned']} of {scan.flops} scanned)"]
    failed = bool(scan.unscannable)
    if atpg is not None:
        parts.append(
            f"Fault with Quaigh: {atpg.coverage_pct:.1f}% stuck-at coverage "
            f"({atpg.detected} of {atpg.faults} faults) with {atpg.patterns} patterns"
        )
        if project.macros:
            n = len(project.macros)
            parts[-1] += (
                f", with the {n} hard macro{'s' if n != 1 else ''} out of the test (inputs not "
                "observed, outputs held at 0 as a test-mode gate would hold them; a macro is "
                "tested through its own BIST port)"
            )
        if cfg.min_coverage is not None and atpg.coverage_pct < cfg.min_coverage:
            failed = True
            parts[-1] += f", below the {cfg.min_coverage:g}% asked for"
    for result, where in ((sim, "the scan netlist"), (routed, "the routed netlist")):
        if result is None:
            continue
        if result.ok and result.flush_ok:
            n = result.patterns
            parts.append(
                f"flush and {n} pattern{'s' if n != 1 else ''} pass on {where} "
                f"(Icarus {result.version})"
            )
        else:
            failed = True
            first = result.mismatches[0] if result.mismatches else "no result"
            parts.append(f"the simulation on {where} fails: {result.errors} mismatches ({first})")
    parts += notes
    return StepResult("dft", "fail" if failed else "pass", "; ".join(parts), data)
