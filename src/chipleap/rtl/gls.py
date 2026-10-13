"""Gate-level simulation: the project's cocotb tests on its netlist.

By default the routed netlist of the LibreLane run in ``build/pnr`` (``final/nl``) runs
with the technology's cell models and the SDF of the same run at one corner
(``final/sdf/<corner>``, the typical one unless ``gate_sim.corner`` names another), so the
tests see the delays of the routed design. ``gate_sim.netlist: synth`` runs ChipLeap's
synthesized netlist with zero delay instead, which needs no routed run.

IHP's cell models need Icarus 13: Icarus 12 elaborates them, but every flip-flop stays X
and every test fails the same way. ``scripts/install_iverilog13.sh`` installs it beside the
system one and ``CHIPLEAP_IVERILOG`` selects it; this step checks the version.

The netlist is copied into ``build/gls`` with ``initial $sdf_annotate("<sdf>");`` added to
its top module, so the design under test is still the netlist's top with the same ports,
and the cell models and the SDF are copied there too. Icarus runs with ``-gspecify`` and
``-T`` for the minimum, typical or maximum delays. The tests run with ``GATES=yes`` in the
environment (the convention of Tiny Tapeout's gate-level tests, for a test that must skip
what only RTL has) and with ``CHIPLEAP_PARAMETERS``, as every simulation does, since a
netlist has no parameters left to read.

A project's SRAM macros run on the PDK's own models (and a package on its Verilog model):
with an SDF, the timing models, whose clock to output and the macros' other paths the SDF
sets (Icarus ignores their timing checks); without one, the functional models.

The tests are the project's code, so this step runs them as the simulation step does: only
for projects you trust.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.openroad import OpenroadError, read_recorded, recorded_files, routed_run
from chipleap.rtl.project import Project, runs_tools
from chipleap.rtl.sim import SimError, SimUnavailable, run_tests, sim_data, summarize
from chipleap.rtl.synth import SynthError, liberty_for, synthesize
from chipleap.verify.result import StepResult
from chipleap.verify.tools import Tool, iverilog

DEFAULT_CORNER = "nom_typ_1p20V_25C"
INSTALL_HINT = "scripts/install_iverilog13.sh, then CHIPLEAP_IVERILOG"


class GlsUnavailable(RuntimeError):
    pass


# A delay's condition on a line of its own; a timing check's (``(HOLD (COND A_W_ACCESS
# (posedge A_DIN[0])) ...``, as an SRAM's liberty gives) is followed by its port on the
# same line, in parentheses, and is already in a form Icarus reads.
_COND_RE = re.compile(r"\(COND ([^\n()][^\n()]*)\n")


def normalize_sdf(text: str) -> str:
    """OpenSTA writes conditions as ``(COND A0 == 1'b1 & A1 == 1'b0``; Icarus reads only
    the parenthesized form the cell models use, ``(COND (A0 == 1'b1 && A1 == 1'b0)``, and
    stops reading the file at the first one it cannot parse, leaving later cells without
    delays. A hard macro's paths and checks name bus bits (``A_DOUT[0]``), which Icarus
    cannot read either (``macro_paths``)."""

    def fix(m: re.Match[str]) -> str:
        expr = re.sub(r"\s\|\s", " || ", re.sub(r"\s&\s", " && ", m.group(1).strip()))
        return f"(COND ({expr})\n"

    return macro_paths(_COND_RE.sub(fix, text))


# A path to one bit of a bus: (IOPATH A_CLK A_DOUT[3] (rise) (fall)).
_BIT_PATH_RE = re.compile(
    r"^(\s*)\(IOPATH (\((?:posedge|negedge) \S+\)|\S+) ([A-Za-z_][A-Za-z0-9_]*)\[\d+\] "
    r"\(([-\d.:]*)\) \(([-\d.:]*)\)\)\s*$"
)


def _slowest(triples: list[str]) -> str:
    """The largest of each field of ``min:typ:max`` triples (an empty field stays empty)."""
    fields = [t.split(":") for t in triples]
    out = []
    for i in range(max(len(f) for f in fields)):
        vals = [float(f[i]) for f in fields if i < len(f) and f[i]]
        out.append(f"{max(vals):.3f}" if vals else "")
    return ":".join(out)


def macro_paths(text: str) -> str:
    """The SDF with each cell's paths to the bits of a bus (a hard macro's clock to output,
    ``A_CLK`` to ``A_DOUT[i]``) folded into one path to the whole bus with the slowest
    bit's delays, which the macro's model's ``specify`` path takes, and its timing checks
    on bus bits left out: Icarus reads neither form and does not check timing."""
    cells = re.split(r"(?=\(CELL\b)", text)
    out = []
    for cell in cells:
        cell = _drop_bus_checks(cell)
        groups: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {}
        lines = cell.split("\n")
        kept: list[str] = []
        for line in lines:
            m = _BIT_PATH_RE.match(line)
            if m is None:
                kept.append(line)
                continue
            key = (m.group(1), m.group(2), m.group(3))
            if key not in groups:
                groups[key] = ([], [])
                kept.append(f"\x00{len(groups) - 1}")
            groups[key][0].append(m.group(4))
            groups[key][1].append(m.group(5))
        folded = [
            f"{ind}(IOPATH {src} {bus} ({_slowest(r)}) ({_slowest(f)}))"
            for (ind, src, bus), (r, f) in groups.items()
        ]
        out.append("\n".join(folded[int(x[1:])] if x.startswith("\x00") else x for x in kept))
    return "".join(out)


def _drop_bus_checks(cell: str) -> str:
    """``cell`` without its TIMINGCHECK blocks that name a bus bit."""
    start = cell.find("(TIMINGCHECK")
    while start >= 0:
        depth, end = 0, start
        for end in range(start, len(cell)):
            depth += {"(": 1, ")": -1}.get(cell[end], 0)
            if depth == 0:
                break
        block = cell[start : end + 1]
        if "[" in block:
            cell = cell[:start] + cell[end + 1 :]
            start = cell.find("(TIMINGCHECK", start)
        else:
            start = cell.find("(TIMINGCHECK", end + 1)
    return cell


def sdf_messages(log: str) -> dict[str, Any]:
    """What Icarus said about the SDF: lines it could not read (the delays after them are
    lost), and interconnects it could not place (Icarus cannot annotate a wire that ends
    at a port of the top module; their delays stay zero)."""
    lines = [ln for ln in log.splitlines() if "SDF ERROR" in ln]
    broken = sorted(
        {ln.split("SDF ERROR: ", 1)[-1] for ln in lines if re.search(r"syntax|malformed", ln, re.I)}
    )
    unplaced = sum("intermodpath" in ln for ln in lines)
    return {"unreadable": broken[:5], "unplaced_interconnects": unplaced}


def icarus_major(tool: Tool) -> int:
    m = re.match(r"(\d+)", tool.version or "")
    return int(m.group(1)) if tool.found and m else 0


def cell_models(project: Project) -> list[Path]:
    """The Verilog models of the technology's cells, found beside its liberty."""
    lib = liberty_for(project.tech)
    if project.tech != "ihp-sg13g2" or lib is None:
        raise GlsUnavailable(
            f"gate-level simulation needs the cell models of {project.tech}; "
            "ChipLeap finds them for IHP SG13G2"
        )
    found = sorted((lib.resolve().parent.parent / "verilog").glob("*.v"))
    if not found:
        raise GlsUnavailable(f"no cell models beside {lib}")
    return found


def annotated(text: str, top: str, sdf: Path) -> str:
    """The netlist with an SDF annotation in the module ``top``."""
    m = re.search(rf"\bmodule\s+{re.escape(top)}\b", text)
    end = re.compile(r"\bendmodule\b").search(text, m.end()) if m else None
    if m is None or end is None:
        raise ValueError(f"no module {top} in the netlist")
    line = f'initial $sdf_annotate("{q.plain(sdf)}");\n'
    return text[: end.start()] + line + text[end.start() :]


# The inputs scan insertion adds (chipleap.rtl.dft), as a netlist declares them.
SCAN_INPUT_RE = re.compile(r"\binput\s+(?:wire\s+)?(scan_(?:enable|in)_\d+)\s*[;,)]")


def scan_inputs(text: str) -> list[str]:
    """The scan inputs a netlist declares, if scan insertion added any."""
    return sorted(set(SCAN_INPUT_RE.findall(text)))


def hold_low(top: str, ports: list[str]) -> str:
    """A second top module that holds ``ports`` of ``top`` at 0 for the whole run. Port
    names come from the netlist and are plain identifiers (``SCAN_INPUT_RE``)."""
    lines = ["module chipleap_gls_aux;", "  initial begin"]
    lines += [f"    force {q.plain(top)}.{q.plain(p)} = 1'b0;" for p in ports]
    lines += ["  end", "endmodule"]
    return "\n".join(lines) + "\n"


def _netlist(project: Project) -> tuple[Path, Path | None, str]:
    """The netlist to simulate, its SDF (None without delays) and its corner."""
    cfg = project.gate_sim
    if cfg.netlist == "synth":
        try:
            return synthesize(project).netlist, None, ""
        except SynthError as exc:
            raise GlsUnavailable(f"synthesis failed: {exc}") from exc
    run = routed_run(project)
    if run is None:
        raise GlsUnavailable(
            "no routed run in build/pnr: run chipleap verify --steps pnr first, or set "
            "gate_sim.netlist: synth"
        )
    corner = cfg.corner or DEFAULT_CORNER
    sdf = run / "final" / "sdf" / corner / f"{project.top}__{corner}.sdf"
    # Only what ChipLeap recorded of the run counts (chipleap.rtl.provenance).
    files = recorded_files(project, run)
    if sdf not in files:
        sdfs = run / "final" / "sdf"
        have = sorted({f.parent.name for f in files if f.parent.parent == sdfs})
        raise GlsUnavailable(f"no SDF for corner {corner}; the run has {', '.join(have) or 'none'}")
    return run / "final" / "nl" / f"{project.top}.nl.v", sdf, corner


def prepare(project: Project, workdir: Path) -> tuple[Project, tuple[str, ...], dict[str, Any]]:
    """The project as the simulator sees it at gate level (cell models and the netlist
    copy as its sources, no parameters), the simulator options, and what was used."""
    netlist, sdf, corner = _netlist(project)
    # The PDK's models, trusted, copied so the run reads its own; like every file here,
    # written without following a symlink (builddir).
    sources = [
        builddir.write_bytes(workdir, f"cells/{m.name}", m.read_bytes())
        for m in cell_models(project)
    ]
    # A routed netlist and its SDF are read as ChipLeap recorded them, hash checked; the
    # synthesized netlist is this step's own.
    read = (lambda p: read_recorded(project, p)) if sdf is not None else project.read
    text = read(netlist).decode(errors="replace")
    args: tuple[str, ...] = ()
    if sdf is not None:
        local_sdf = builddir.write_text(
            workdir, sdf.name, normalize_sdf(read(sdf).decode(errors="replace"))
        )
        text = annotated(text, project.top, local_sdf)
        args = ("-gspecify", "-ginterconnect", f"-T{project.gate_sim.delays}")
    copy = builddir.write_text(workdir, f"{project.top}.gls.v", text)
    extra: list[Path] = []
    # A netlist with scan chains (dft.insert) has scan ports the tests know nothing of; they
    # are held at their functional values, as the power step holds them.
    scan = scan_inputs(text)
    if scan:
        if project.tests.toplevel != project.top:
            raise GlsUnavailable(
                f"the routed netlist has scan ports ({scan[0]}) and the tests' top is "
                f"{project.tests.toplevel}, not {project.top}: drive them low in the tests"
            )
        extra.append(
            builddir.write_text(workdir, "chipleap_gls_aux.v", hold_low(project.top, scan))
        )
        args = (*args, "-s", "chipleap_gls_aux")
    gate = dataclasses.replace(
        project, sources=(*sources, copy, *extra), include_dirs=(), parameters={}
    )
    used: dict[str, Any] = {
        "netlist": project.rel(netlist),
        "sdf": project.rel(sdf) if sdf else None,
        "corner": corner or None,
        "delays": project.gate_sim.delays if sdf else None,
    }
    if project.macros:
        # With an SDF the SRAMs run on the PDK's timing models, whose clock to output the
        # SDF sets; without one, on their functional models.
        used["macro_models"] = "timing" if sdf is not None else "functional"
    return gate, args, used


@runs_tools
def gls(project: Project, test_filter: str = "") -> StepResult:
    """The gate-level step: the project's tests on its netlist."""
    if project.tests.dir is None:
        return StepResult("gls", "skipped", "the project has no tests (add a tests section)")
    tool = iverilog()
    if icarus_major(tool) < 13:
        found = f"Icarus {tool.version}" if tool.found else "no Icarus"
        found += f" ({tool.note})" if tool.note else ""
        return StepResult(
            "gls",
            "skipped",
            f"gate-level simulation of IHP cells needs Icarus 13 or newer, found {found}: "
            f"{INSTALL_HINT}",
        )
    workdir = project.work_dir("gls", empty=True)
    try:
        gate, args, used = prepare(project, workdir)
    except GlsUnavailable as exc:
        return StepResult("gls", "skipped", str(exc))
    except OpenroadError as exc:
        return StepResult("gls", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    env = {"GATES": "yes", "CHIPLEAP_PARAMETERS": json.dumps(project.parameters, sort_keys=True)}
    try:
        run = run_tests(
            gate,
            "icarus",
            test_filter=test_filter,
            workdir=workdir / "sim",
            extra_args=args,
            env=env,
            macro_timing=used["sdf"] is not None,
        )
    except SimUnavailable as exc:
        return StepResult("gls", "skipped", str(exc).splitlines()[0])
    except SimError as exc:
        return StepResult("gls", "fail", str(exc).splitlines()[0], {"error": str(exc), **used})
    status, summary = summarize(run)
    data = {**sim_data(run), **used}
    if used["sdf"]:
        test_log = workdir / "sim" / "test.log"
        msgs = sdf_messages(test_log.read_text(errors="replace") if test_log.is_file() else "")
        data["sdf_messages"] = msgs
        if msgs["unreadable"]:
            first = msgs["unreadable"][0].replace(str(workdir) + "/", "")
            return StepResult(
                "gls",
                "fail",
                f"Icarus could not read the SDF, so delays are missing: {first}",
                data,
            )
    if used["sdf"]:
        what = f"routed netlist with SDF at {used['corner']} ({used['delays']} delays)"
    else:
        what = "synthesized netlist, zero delay"
    return StepResult("gls", status, f"{what}: {summary}", data)
