"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from chipleap import __version__
from chipleap.explore.engine import Candidate, explore
from chipleap.explore.score import Point, assess
from chipleap.gen.bundle import load_design, write_bundle
from chipleap.hw.cpu import CPUS
from chipleap.hw.design import PLATFORMS, Design, DesignError
from chipleap.hw.memory import MEMORIES
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.model.bounds import requirements
from chipleap.model.evaluate import evaluate
from chipleap.report.data import evaluation_dict, explore_dict, requirements_dict
from chipleap.report.text import format_evaluation, format_explore, format_requirements, num, table
from chipleap.rtl import builddir
from chipleap.spec import Spec, SpecError, load_spec, parse_spec
from chipleap.workload.llm import PRESETS
from chipleap.workload.ops import Workload


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _spec_from_args(args: argparse.Namespace) -> Spec:
    if getattr(args, "spec", None):
        return load_spec(args.spec)
    if getattr(args, "model", None):
        llm: dict[str, Any] = {"model": args.model}
        for key in ("phase", "context", "prompt", "batch", "weights", "kv"):
            v = getattr(args, key, None)
            if v is not None:
                llm[key] = v
        return parse_spec({"name": args.model, "workload": {"llm": llm}})
    raise SpecError("give a spec file or --model")


def _workload_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec", nargs="?", help="spec file (YAML)")
    p.add_argument("-m", "--model", help="LLM preset instead of a spec, e.g. llama-3.2-1b")
    p.add_argument("--phase", choices=("decode", "prefill"))
    p.add_argument("--context", type=int)
    p.add_argument("--prompt", type=int)
    p.add_argument("--batch", type=int)
    p.add_argument("--weights", choices=("int8", "int4", "int2"))
    p.add_argument("--kv", choices=("int8", "int4"))


def cmd_models(args: argparse.Namespace) -> int:
    rows = [
        [
            k,
            a.name,
            f"{a.params / 1e9:.3f}",
            str(a.layers),
            str(a.hidden),
            f"{a.heads}/{a.kv_heads}",
            str(a.vocab),
        ]
        for k, a in PRESETS.items()
    ]
    if args.json:
        _print_json(
            [
                {
                    "key": k,
                    "name": a.name,
                    "params": a.params,
                    "layers": a.layers,
                    "hidden": a.hidden,
                    "heads": a.heads,
                    "kv_heads": a.kv_heads,
                    "head_dim": a.head_dim,
                    "ffn": a.ffn,
                    "vocab": a.vocab,
                    "tied_embeddings": a.tied_embeddings,
                    "source": a.source,
                }
                for k, a in PRESETS.items()
            ]
        )
        return 0
    print(
        table(
            ["key", "model", "params (B)", "layers", "hidden", "heads/kv", "vocab"],
            rows,
            right=(2, 3, 4, 6),
        )
    )
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    if args.json:
        _print_json(
            {
                "technologies": {k: t.__dict__ for k, t in TECHNOLOGIES.items()},
                "memories": {k: m.__dict__ for k, m in MEMORIES.items()},
                "cpus": {
                    k: {
                        "name": c.name,
                        "kge": c.kge,
                        "cycles_per_mac": c.cycles_per_mac,
                        "note": c.note,
                    }
                    for k, c in CPUS.items()
                },
            }
        )
        return 0
    print("Technologies")
    print(
        table(
            ["key", "name", "MHz max", "I/O MT/s", "int8 MAC pJ", "SRAM pJ/B", "source"],
            [
                [
                    k,
                    t.name,
                    f"{t.max_freq_mhz:g}",
                    str(t.max_io_mts),
                    f"{t.mac_pj_8x8:.3g}",
                    f"{t.sram_pj_per_byte:.3g}",
                    "measured" if t.measured else "scaled",
                ]
                for k, t in TECHNOLOGIES.items()
            ],
            right=(2, 3, 4, 5),
        )
    )
    print("\nExternal memories")
    print(
        table(
            ["key", "name", "GB/s peak", "capacity MiB", "pJ/bit", "pins"],
            [
                [
                    k,
                    m.name,
                    f"{m.peak_gbps:.3g}",
                    str(m.capacity_mib),
                    f"{m.pj_per_bit:g}",
                    str(m.pins),
                ]
                for k, m in MEMORIES.items()
            ],
            right=(2, 3, 4, 5),
        )
    )
    print("\nCPUs")
    print(
        table(
            ["key", "name", "kGE", "cycles/MAC int8", "note"],
            [
                [k, c.name, f"{c.kge:g}", f"{c.cycles_per_mac[8]:g}", c.note]
                for k, c in CPUS.items()
            ],
            right=(2, 3),
        )
    )
    return 0


def _workload_summary(w: Workload) -> list[str]:
    mm = w.matmuls
    return [
        w.description,
        f"Parameters: {w.params / 1e9:.4g} B" if w.params else "",
        f"Per step: {w.macs / 1e9:.4g} GMAC, {w.weight_bytes_read / 1e6:.4g} MB of weights "
        f"and {w.kv_bytes_read / 1e6:.4g} MB of KV cache read, {len(mm)} matrix multiplies "
        f"({sum(op.count for op in mm)} jobs).",
        f"Resident: {w.storage.weights / 2**20:.4g} MiB weights, {w.storage.kv / 2**20:.4g} MiB "
        f"KV cache, {w.storage.activations / 1024:.4g} KiB activation scratch.",
        f"Arithmetic intensity: {w.macs / max(1.0, w.weight_bytes_read + w.kv_bytes_read):.3g} "
        "MAC per byte of weights and KV read.",
    ]


def cmd_workload(args: argparse.Namespace) -> int:
    spec = _spec_from_args(args)
    w = spec.workload()
    if args.json:
        _print_json(
            {
                "name": w.name,
                "description": w.description,
                "params": w.params,
                "macs": w.macs,
                "weight_bytes_read": w.weight_bytes_read,
                "kv_bytes_read": w.kv_bytes_read,
                "storage": w.storage.__dict__,
                "ops": [op.__dict__ | {"type": type(op).__name__} for op in w.ops],
            }
        )
        return 0
    print("\n".join(ln for ln in _workload_summary(w) if ln))
    rows = []
    for op in sorted(w.matmuls, key=lambda o: -o.macs):
        rows.append(
            [
                op.name,
                f"{op.m}x{op.k}x{op.n}",
                str(op.count),
                num(op.macs / 1e6),
                num(op.w_bytes / 1e6),
                op.w_kind.value,
            ]
        )
    print()
    print(table(["matmul", "M x K x N", "count", "MMAC", "W MB", "W from"], rows, right=(2, 3, 4)))
    return 0


def cmd_require(args: argparse.Namespace) -> int:
    spec = _spec_from_args(args)
    target = args.throughput or spec.throughput
    if not target:
        raise SpecError("give --throughput or a spec with targets.throughput")
    req = requirements(spec.workload(), target)
    if args.json:
        _print_json(requirements_dict(req))
    else:
        print(format_requirements(req))
    return 0


def _evaluate(spec: Spec, design: Design, weights: str | None) -> Candidate:
    wf = weights or spec.default_weight_format()
    ev = evaluate(spec.workload(wf), design, firmware_kib=spec.firmware_kib)
    point = Point(design, wf)
    return Candidate(point, ev, assess(spec, point, ev), "user")


def cmd_evaluate(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    design, weights = load_design(args.design)
    c = _evaluate(spec, design, args.weights or weights)
    if args.json:
        d = evaluation_dict(c.evaluation)
        d["meets_spec"] = c.assessment.meets
        d["misses"] = list(c.assessment.misses)
        _print_json(d)
    else:
        print(format_evaluation(c.evaluation))
        if c.assessment.misses:
            print("\nAgainst the spec:\n" + "\n".join(f"  {m}" for m in c.assessment.misses))
    return 0 if c.assessment.feasible else 2


def _with_platform(spec: Spec, platform: str | None) -> Spec:
    if not platform:
        return spec
    start = spec.start.with_(platform=platform) if spec.start else None
    return spec.with_(platform=platform, start=start)


def cmd_explore(args: argparse.Namespace) -> int:
    spec = _with_platform(load_spec(args.spec), args.platform)
    r = explore(spec, samples=args.samples, seed=args.seed, max_steps=args.steps)
    if args.out:
        out = Path(args.out)
        write_bundle(
            out, r.best.design, weights=r.best.point.weights, spec=spec, candidate=r.best, result=r
        )
    if args.json:
        _print_json(explore_dict(r))
    else:
        print(format_explore(r, verbose=args.verbose))
        if args.out:
            dirs = "rtl/, sw/, tb/" if spec.platform == "standalone" else "xheep/, rtl/, sw/, tb/"
            print(
                f"\nWrote {args.out}/ (report.html, DATASHEET.md, {dirs}). "
                f"Next: chipleap verify {args.out}"
            )
    return 0 if r.best.assessment.feasible else 2


def cmd_generate(args: argparse.Namespace) -> int:
    design, weights = load_design(args.design)
    spec = load_spec(args.spec) if args.spec else None
    cand = _evaluate(spec, design, weights) if spec else None
    if args.name:
        design = design.with_(name=args.name)
    if args.platform:
        design = design.with_(platform=args.platform)
    files = write_bundle(Path(args.out), design, weights=weights, spec=spec, candidate=cand)
    print(f"Wrote {len(files)} files to {args.out}/")
    return 0


def _print_steps(results: list[Any], width: int = 6) -> int:
    failed = 0
    for r in results:
        print(f"{r.name:{width}s} {r.status.upper():8s} {r.summary}")
        failed += r.status == "fail"
    return 1 if failed else 0


BUNDLE_STEPS = "consistency,lint,sim,synth,xheep"


def _verify_project(args: argparse.Namespace, path: Path) -> int:
    from chipleap.rtl.verify import verify_project
    from chipleap.verify.remote import Remote

    remote = Remote.from_env(getattr(args, "remote", None), getattr(args, "remote_setup", None))
    results = verify_project(
        path,
        steps=args.steps or "",
        sim_tool=getattr(args, "sim_tool", None) or "",
        synth_tool=getattr(args, "synth_tool", None) or "yosys",
        pnr_tool=getattr(args, "pnr_tool", None) or "librelane",
        remote=remote,
        allow=getattr(args, "allow_path", None) or (),
    )
    code = _print_steps(results, width=8)
    out = path.parent / "build" / "verify"
    print(f"\nWrote {out}/VERIFY.md, verify.json and report.html")
    return code


def cmd_verify(args: argparse.Namespace) -> int:
    from chipleap.rtl.project import find_project
    from chipleap.verify.remote import Remote
    from chipleap.verify.run import verify_bundle

    project = find_project(args.bundle)
    if project is not None:
        return _verify_project(args, project)
    steps = tuple(s.strip() for s in (args.steps or BUNDLE_STEPS).split(",") if s.strip())
    if "all" in steps:
        # Place and route takes many minutes, so it runs only when asked for.
        steps = ("consistency", "lint", "sim", "synth", "xheep", "pnr")
    sim_tool = getattr(args, "sim_tool", None) or "verilator"
    if sim_tool in ("icarus", "auto"):
        raise ValueError(
            "the accelerator's testbench runs on verilator, xcelium, vcs, questa or verilator-sv"
        )
    remote = Remote.from_env(getattr(args, "remote", None), getattr(args, "remote_setup", None))
    results = verify_bundle(
        Path(args.bundle),
        steps=steps,
        xheep_dir=Path(args.xheep) if args.xheep else None,
        fetch_xheep=args.fetch_xheep,
        cases=args.cases,
        seed=args.seed,
        sim_tool=sim_tool,
        synth_tool=getattr(args, "synth_tool", None) or "yosys",
        synth_tech=getattr(args, "synth_tech", None) or "ihp-sg13g2",
        remote=remote,
        pnr_tool=getattr(args, "pnr_tool", None) or "librelane",
    )
    code = _print_steps(results)
    print(f"\nWrote {args.bundle}/verify/VERIFY.md and verify.json")
    return code


def _clock_arg(text: str) -> tuple[str, float]:
    port, _, period = text.partition(":")
    try:
        return port, float(period or 10)
    except ValueError:
        raise ValueError(f"--clock {text!r}: give PORT or PORT:PERIOD_NS") from None


def _reset_arg(text: str) -> tuple[str, bool]:
    port, _, active = text.partition(":")
    if active not in ("", "low", "high"):
        raise ValueError(f"--reset {text!r}: give PORT, PORT:low or PORT:high")
    from chipleap.rtl.project import active_low_by_name

    return port, active == "low" if active else active_low_by_name(port)


def cmd_project(args: argparse.Namespace) -> int:
    from chipleap.rtl.project import init_project, load_project

    if args.project_cmd == "init":
        path, notes = init_project(
            args.dir,
            top=args.top,
            sources=args.source,
            filelist=args.filelist,
            clocks=[_clock_arg(c) for c in args.clock],
            resets=[_reset_arg(r) for r in args.reset],
            tech=args.tech,
            tests_dir=args.tests,
            name=args.name,
            force=args.force,
        )
        print(f"Wrote {path}")
        for n in notes:
            print(f"  {n}")
        print(f"Next: chipleap verify {args.dir}")
        return 0
    p = load_project(args.path, args.allow_path)
    if args.json:
        _print_json(p.to_dict())
        return 0
    import yaml

    print(f"{p.file}: a valid project")
    print(yaml.safe_dump(p.to_dict(), sort_keys=False, default_flow_style=None).rstrip())
    return 0


def cmd_chip(args: argparse.Namespace) -> int:
    from chipleap.chip.assemble import plan_chip
    from chipleap.rtl.project import load_project

    plan = plan_chip(load_project(args.path, args.allow_path))
    if args.json:
        _print_json(plan)
        return 0
    die, core, outline = plan["die_um"], plan["core_um"], plan["outline_um"]
    print(
        f"{plan['name']}: die {die[0]:g} x {die[1]:g} um "
        f"({outline[2] - outline[0]:g} x {outline[3] - outline[1]:g} um with the seal ring), "
        f"core {core[0]:g},{core[1]:g} to {core[2]:g},{core[3]:g} um"
    )
    print(
        f"{len(plan['pads'])} pads at a {plan['pad_pitch_um']:g} um pitch, "
        f"{plan['ring_cells']} IO ring cells in all"
    )
    for pad in plan["pads"]:
        what = pad["net"] or pad["kind"]
        cell = pad["cell"] or "(in or out, by its port)"
        x, y = pad["bond_center_um"]
        print(f"  {pad['number']:3d}  {pad['side']:5s}  {what:20s} {cell:32s} bond at {x:g},{y:g}")
    for m in plan["macros"]:
        x, y = m["at_um"]
        print(f"  macro {m['instance']} ({m['cell']}) at {x:g},{y:g} um, {m['orientation']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    code = cmd_explore(args)
    if code != 0 or not args.out:
        return code
    print()
    vargs = argparse.Namespace(
        bundle=args.out,
        steps=args.verify_steps,
        xheep=args.xheep,
        fetch_xheep=args.fetch_xheep,
        cases=0,
        seed=args.seed,
    )
    return cmd_verify(vargs)


def cmd_doctor(args: argparse.Namespace) -> int:
    from chipleap.verify.tools import all_tools, ihp_liberty, liberty_settings_problem
    from chipleap.verify.xheep import find_checkout

    rows = [
        [
            t.name,
            "yes" if t.found else "no",
            t.version,
            t.purpose + (f"; {t.note}" if t.note else ""),
        ]
        for t in all_tools()
    ]
    from chipleap.verify.tools import which

    sv2v = which("sv2v")
    rows.append(["sv2v", "yes" if sv2v else "no", "", "SystemVerilog front end for older Yosys"])
    lib = ihp_liberty()
    rows.append(
        [
            "IHP SG13G2",
            "yes" if lib else "no",
            "",
            str(lib)
            if lib
            else liberty_settings_problem() or "open PDK for synthesis (set CHIPLEAP_LIBERTY)",
        ]
    )
    co = find_checkout()
    rows.append(
        [
            "X-HEEP",
            "yes" if co else "no",
            "",
            str(co) if co else "checkout for config checks (chipleap verify --fetch-xheep)",
        ]
    )
    from chipleap.rtl import confine

    off = f" (off: {confine.ENV}=0)" if confine.abi() and not confine.enabled() else ""
    rows.append(
        [
            "Landlock",
            "yes" if confine.enabled() else "no",
            f"ABI {confine.abi()}" if confine.abi() else "",
            f"keeps a project's lint and synthesis to its own files{off}",
        ]
    )
    from chipleap.verify.remote import Remote, RemoteError
    from chipleap.verify.tools import commercial

    rows += [[t.name, "yes" if t.found else "no", "", t.purpose] for t in commercial()]
    remote = Remote.from_env(getattr(args, "remote", None), getattr(args, "remote_setup", None))
    if remote is not None:
        tools = commercial()
        try:
            found = remote.which([t.name for t in tools])
        except RemoteError as exc:
            print(table(["tool", "found", "version", "used for"], rows))
            print(f"\n{exc}", file=sys.stderr)
            return 1
        rows += [
            [f"{t.name} @ {remote.host}", "yes" if t.name in found else "no", "", t.purpose]
            for t in tools
        ]
    print(table(["tool", "found", "version", "used for"], rows))
    return _formal_selftest()


def _formal_selftest() -> int:
    """Ask the SymbiYosys and EQY ChipLeap would use questions with known answers: a build
    that does not match the Yosys it drives can answer wrong without failing."""
    from chipleap.rtl.selftest import formal_selftest

    checks = formal_selftest()
    if not checks:
        return 0
    print("\nFormal tools self-test (questions with known answers):")
    for c in checks:
        print(f"  {'pass' if c.ok else 'FAIL'}  {c.message}")
    if all(c.ok for c in checks):
        return 0
    print(
        "\nchipleap: the formal and equivalence steps would give wrong answers with these "
        "tools; fix them before trusting a proof",
        file=sys.stderr,
    )
    return 1


def cmd_tech(args: argparse.Namespace) -> int:
    from chipleap.hw import techlib
    from chipleap.hw.tech import user_tech_dir

    if args.tech_cmd == "list":
        rows = [
            [
                k,
                t.name,
                f"{t.node_nm}",
                "yours" if t.user else ("measured" if t.measured else "scaled"),
                t.host or "",
            ]
            for k, t in TECHNOLOGIES.items()
        ]
        print(table(["key", "name", "nm", "source", "files on"], rows))
        print(f"\nYour technologies live in {user_tech_dir() or '(turned off)'}")
        return 0
    if args.tech_cmd == "remove":
        print(f"Removed {techlib.remove(args.name)}")
        return 0
    tech, basis = techlib.measure(
        args.name,
        args.liberty,
        args.node,
        sram=args.sram_liberty,
        vdd=args.vdd,
        max_mhz=args.max_mhz,
        max_io_mts=args.max_io_mts,
        lef=tuple(args.lef),
        db=args.db,
        host=args.remote,
        name=args.display_name,
        liberty_slow=args.liberty_slow,
    )
    path = techlib.save(tech, force=args.force)
    print(f"Added technology {tech.key!r} ({tech.node_nm} nm, {tech.vdd:g} V) in {path}")
    for what, how in basis.items():
        print(f"  {what}: {how}")
    print(
        f"Explore with it: constraints: {{tech: [{tech.key}]}} in a spec, or "
        f"chipleap verify DIR --synth-tool genus --synth-tech {tech.key}"
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.path)
    if dest.exists() and not args.force:
        print(f"{dest} exists; use --force to overwrite", file=sys.stderr)
        return 1
    text = resources.files("chipleap").joinpath("data").joinpath("example-spec.yaml").read_text()
    # Never through a symlink: --force must not overwrite what one points at.
    builddir.write_text(dest.parent, dest.name, text)
    print(f"Wrote {dest}. Next: chipleap explore {dest} -o out")
    return 0


def _terminal_safe(text: str) -> str:
    """Neutralise control and bidirectional characters, keeping newlines and tabs:
    macro check messages quote names and tokens from files someone else wrote."""
    bidi = {"\u061c", "\u200e", "\u200f", *(chr(c) for c in range(0x202A, 0x202F))}
    bidi |= {chr(c) for c in range(0x2066, 0x206A)}
    return "".join(
        c if c in "\n\t" or not (ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 or c in bidi) else "\ufffd"
        for c in text
    )


def cmd_macro(args: argparse.Namespace) -> int:
    from chipleap.macro import check_package, schema_text

    if args.macro_cmd == "schema":
        print(schema_text(), end="")
        return 0
    cells: frozenset[str] = frozenset()
    if args.pdk_libs:
        from chipleap.macro.layer import MacroImportError, pdk_cell_names

        try:
            cells = pdk_cell_names(Path(args.pdk_libs).expanduser())
        except (MacroImportError, OSError) as exc:
            print(f"chipleap: --pdk-libs: {exc}", file=sys.stderr)
            return 2
    try:
        report = check_package(args.dir, pdk=args.pdk, pdk_cells=cells)
    except FileNotFoundError as exc:
        print(f"chipleap: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(report.to_dict())
    else:
        print(_terminal_safe(report.to_text()))
    failed = not report.passed or (args.strict and report.warnings > 0)
    return 1 if failed else 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from chipleap.mcp_server import main as mcp_main

    mcp_main(log_dir=args.log_dir)
    return 0


def cmd_eco(args: argparse.Namespace) -> int:
    from chipleap.rtl.eco import eco, timing_paths
    from chipleap.rtl.openroad import OpenroadError
    from chipleap.rtl.project import EcoChange, load_project

    project = load_project(args.project, args.allow_path)
    if args.paths:
        kind = "hold" if args.hold else "setup"
        try:
            paths, run = timing_paths(project, args.corner, kind, args.count)
        except OpenroadError as exc:
            raise ValueError(str(exc).splitlines()[0]) from exc
        print(f"Worst {kind} paths at the {args.corner} corner of {run}:")
        for p in paths:
            print(f"\n{p.slack_ns:+.3f} ns  {p.startpoint} -> {p.endpoint} ({p.group})")
            for st in p.stages:
                if st.instance:
                    print(f"    {st.delay_ns:7.3f} {st.time_ns:8.3f}  {st.pin} ({st.cell})")
        return 0
    changes = [
        *(EcoChange.parse(f"resize:{c}") for c in args.resize),
        *(EcoChange.parse(f"buffer:{c}") for c in args.buffer),
        *(EcoChange.parse(f"repair:{c}") for c in args.repair),
    ]
    r = eco(project, changes or None)
    print(f"eco      {r.status.upper():8s} {r.summary}")
    for c in r.data.get("changes", []):
        kind = next(k for k in ("resize", "buffer", "repair") if k in c)
        print(f"  {kind} {c[kind]} {c.get('cell', '')} {c.get('result', '')}".rstrip())
    if r.status == "pass" and changes:
        print("Add these to the project's eco section to keep them in its flow.")
    return 1 if r.status == "fail" else 0


def cmd_replay(args: argparse.Namespace) -> int:
    from chipleap.mcp_server import replay_calls

    calls = replay_calls(Path(args.log))
    total = len(calls)
    ok = sum(c["outcome"] == "same" for c in calls)
    for c in calls:
        if c["outcome"] != "same":
            print(f"  {c['tool']}({json.dumps(c['args'], sort_keys=True)}): {c['outcome']}")
    print(f"Replayed {total} calls: {ok} reproduced exactly, {total - ok} differ")
    return 0 if ok == total else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chipleap",
        description="Architecture exploration from workload to RISC-V SoC.",
    )
    p.add_argument("--version", action="version", version=f"chipleap {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("models", help="list the LLM presets")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("catalog", help="technologies, external memories and CPUs")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_catalog)

    s = sub.add_parser("workload", help="what a workload needs per step")
    _workload_args(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_workload)

    s = sub.add_parser("require", help="minimum bandwidth, compute and capacity for a target")
    _workload_args(s)
    s.add_argument("-t", "--throughput", type=float, help="tokens (or units) per second")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_require)

    s = sub.add_parser("evaluate", help="evaluate one design against a spec")
    s.add_argument("spec")
    s.add_argument("design", help="design.yaml or a bundle directory")
    s.add_argument("--weights", choices=("int8", "int4", "int2"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_evaluate)

    for name, helptext in (
        ("explore", "search the design space and explain the result"),
        ("run", "explore, write the bundle and verify it"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("spec")
        s.add_argument("-o", "--out", required=(name == "run"), help="write a bundle here")
        s.add_argument("--samples", type=int, default=2000)
        s.add_argument("--seed", type=int, default=0)
        s.add_argument("--steps", type=int, default=40, help="most agent steps per phase")
        s.add_argument("--json", action="store_true")
        s.add_argument(
            "--platform",
            choices=PLATFORMS,
            help="override the spec: xheep (an X-HEEP SoC) or standalone (AXI4 IP, any host)",
        )
        s.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="show the diagnosis and alternatives at every step",
        )
        if name == "run":
            s.add_argument("--verify-steps", default="consistency,lint,sim,synth,xheep")
            s.add_argument("--xheep", help="X-HEEP checkout")
            s.add_argument("--fetch-xheep", action="store_true")
            s.set_defaults(func=cmd_run)
        else:
            s.set_defaults(func=cmd_explore)

    s = sub.add_parser(
        "generate", help="write the bundle (X-HEEP config, RTL, driver) for a design"
    )
    s.add_argument("design", help="design.yaml or a bundle directory")
    s.add_argument("-o", "--out", required=True)
    s.add_argument("--spec", help="spec, to include predictions in the datasheet")
    s.add_argument("--name", help="rename the design")
    s.add_argument(
        "--platform",
        choices=PLATFORMS,
        help="xheep (X-HEEP SoC) or standalone (the accelerator as AXI4 IP for any host)",
    )
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser(
        "verify", help="check a generated bundle or an RTL project (lint, simulate, synthesize)"
    )
    s.add_argument("bundle", help="a bundle directory, or an RTL project (project.yaml)")
    s.add_argument(
        "--steps",
        default=None,
        help="comma-separated steps; for a bundle consistency,lint,sim,synth,xheep, for a "
        "project lint,sim,coverage,formal,cdc,synth,equiv; pnr (place and route), gls "
        "(gate-level simulation of its netlist), the steps after it (eco, dft, power, "
        "ir_drop, upf) and chip (the whole chip with its pad ring and macros) are opt-in, and "
        "all runs every step",
    )
    s.add_argument("--xheep", help="X-HEEP checkout (default: CHIPLEAP_XHEEP_DIR or the cache)")
    s.add_argument("--fetch-xheep", action="store_true", help="clone the tested X-HEEP commit")
    s.add_argument("--cases", type=int, default=0, help="limit the number of simulation cases")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument(
        "--sim-tool",
        default=None,
        choices=["verilator", "icarus", "auto", "xcelium", "vcs", "questa", "verilator-sv"],
        help="simulator: Verilator (a bundle's default), Icarus (RTL projects), Cadence "
        "Xcelium, Synopsys VCS, Siemens Questa; a project's default is its own",
    )
    s.add_argument(
        "--synth-tool",
        default=None,
        choices=["yosys", "genus", "dc"],
        help="synthesis: Yosys (default), Cadence Genus or Synopsys Design Compiler",
    )
    s.add_argument(
        "--synth-tech",
        default=None,
        help="technology whose liberty Genus or Design Compiler maps a bundle to (chipleap "
        "tech add); a project uses its own",
    )
    s.add_argument(
        "--pnr-tool",
        default=None,
        choices=["librelane", "innovus"],
        help="place and route: LibreLane (default) or Cadence Genus and Innovus",
    )
    s.add_argument(
        "--allow-path",
        action="append",
        default=[],
        metavar="DIR",
        help="let an RTL project use files from this directory outside it (repeatable; "
        "also CHIPLEAP_ALLOW_PATHS)",
    )
    s.add_argument("--remote", help="run the simulator and synthesis on this ssh host")
    s.add_argument(
        "--remote-setup",
        help="shell snippet that puts the tools on the path there (CHIPLEAP_REMOTE_SETUP)",
    )
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("project", help="describe your own RTL as a project (project.yaml)")
    psub = s.add_subparsers(dest="project_cmd", required=True)
    t = psub.add_parser("init", help="write project.yaml for the RTL in a directory")
    t.add_argument("dir", nargs="?", default=".")
    t.add_argument("--top", help="top module (default: the one no source instantiates)")
    t.add_argument("--source", action="append", default=[], help="source file or glob (repeatable)")
    t.add_argument("--filelist", help="a .f filelist instead of --source")
    t.add_argument(
        "--clock", action="append", default=[], help="clock as PORT or PORT:PERIOD_NS (repeatable)"
    )
    t.add_argument(
        "--reset", action="append", default=[], help="reset as PORT, PORT:low or PORT:high"
    )
    t.add_argument("--tech", default="ihp-sg13g2", help="technology key (chipleap tech list)")
    t.add_argument("--tests", help="directory with the cocotb tests (default: test/ or tests/)")
    t.add_argument("--name", help="project name (default: the top module)")
    t.add_argument("--force", action="store_true", help="replace an existing project.yaml")
    t.set_defaults(func=cmd_project)
    t = psub.add_parser("check", help="read a project and show it with every default filled in")
    t.add_argument("path", nargs="?", default=".")
    t.add_argument(
        "--allow-path",
        action="append",
        default=[],
        metavar="DIR",
        help="let the project use files from this directory outside it (repeatable)",
    )
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_project)

    s = sub.add_parser(
        "eco", help="change a routed RTL project (resize, buffer, repair) and sign it off again"
    )
    s.add_argument("project", help="the project (project.yaml or its directory)")
    s.add_argument("--resize", action="append", default=[], metavar="INST=CELL", help="swap a cell")
    s.add_argument(
        "--buffer",
        action="append",
        default=[],
        metavar="INST/PIN=CELL",
        help="put a buffer after a driver's output or before a sink's input",
    )
    s.add_argument(
        "--repair",
        action="append",
        default=[],
        metavar="setup|hold[@NS]",
        help="repair timing again after global routing, with a margin",
    )
    s.add_argument("--paths", action="store_true", help="list the worst paths instead")
    s.add_argument("--corner", default="slow", choices=["typ", "slow", "fast"])
    s.add_argument("--hold", action="store_true", help="with --paths: hold paths")
    s.add_argument("--count", type=int, default=5, help="with --paths: paths per group")
    s.add_argument(
        "--allow-path",
        action="append",
        default=[],
        metavar="DIR",
        help="let the project use files from this directory outside it",
    )
    s.set_defaults(func=cmd_eco)

    s = sub.add_parser("chip", help="a project's chip: pad ring, macros and floorplan")
    csub = s.add_subparsers(dest="chip_cmd", required=True)
    t = csub.add_parser("plan", help="show the chip's floorplan without running a tool")
    t.add_argument("path", nargs="?", default=".")
    t.add_argument(
        "--allow-path",
        action="append",
        default=[],
        metavar="DIR",
        help="let the project use files from this directory outside it (repeatable)",
    )
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_chip)

    s = sub.add_parser("doctor", help="which tools ChipLeap can use here or on a server")
    s.add_argument("--remote", help="also list the commercial tools on this ssh host")
    s.add_argument("--remote-setup", help="shell snippet that puts the tools on the path there")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("tech", help="technologies of your own, measured from liberty files")
    tsub = s.add_subparsers(dest="tech_cmd", required=True)
    t = tsub.add_parser("add", help="measure a PDK's liberty files and add it as a technology")
    t.add_argument("name", help="key for the technology, for example mylib40")
    t.add_argument("--liberty", required=True, help="standard-cell liberty (typical corner)")
    t.add_argument(
        "--liberty-slow", default="", help="slow-corner liberty, for place-and-route timing"
    )
    t.add_argument("--node", type=int, required=True, help="process node in nm")
    t.add_argument(
        "--sram-liberty", action="append", default=[], help="SRAM macro .lib file or directory"
    )
    t.add_argument("--vdd", type=float, default=0.0, help="supply (default: the library's)")
    t.add_argument("--max-mhz", type=float, default=0.0, help="practical clock")
    t.add_argument("--max-io-mts", type=int, default=0, help="fastest external memory rate")
    t.add_argument("--lef", action="append", default=[], help="technology LEF, then cell LEFs")
    t.add_argument("--db", default="", help="compiled .db for Design Compiler")
    t.add_argument("--remote", default="", help="the files are on this ssh host")
    t.add_argument("--display-name", default="", help="longer name for reports")
    t.add_argument("--force", action="store_true", help="replace an existing file")
    t.set_defaults(func=cmd_tech)
    t = tsub.add_parser("list", help="list technologies, built in and your own")
    t.set_defaults(func=cmd_tech)
    t = tsub.add_parser("remove", help="remove a technology of your own")
    t.add_argument("name")
    t.set_defaults(func=cmd_tech)

    s = sub.add_parser("init", help="write an example spec")
    s.add_argument("path", nargs="?", default="chipleap.yaml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("macro", help="macro packages shared with aispice (macro/v1)")
    msub = s.add_subparsers(dest="macro_cmd", required=True)
    m = msub.add_parser(
        "check", help="check a macro package against macro/v1 (exit code 1 when it fails)"
    )
    m.add_argument("dir", help="the package directory, e.g. tiny_ota.macro")
    m.add_argument(
        "--pdk", help="the PDK of the chip that places it: NAME or NAME@COMMIT, e.g. ihp-sg13g2"
    )
    m.add_argument(
        "--pdk-libs",
        metavar="DIR",
        help="the PDK's directory (ihp-sg13g2): also refuse cells named like its libraries' cells",
    )
    m.add_argument("--strict", action="store_true", help="also fail on warnings")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_macro)
    m = msub.add_parser("schema", help="print the JSON schema of macro.toml")
    m.set_defaults(func=cmd_macro)

    s = sub.add_parser("mcp", help="run the MCP server on stdio")
    s.add_argument("--log-dir", help="append every tool call to a JSONL run log here")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("replay", help="re-run the tool calls in an MCP run log and compare")
    s.add_argument("log")
    s.set_defaults(func=cmd_replay)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
        return code
    except (SpecError, DesignError, KeyError, ValueError) as exc:
        msg = exc.args[0] if exc.args else str(exc)
        print(f"chipleap: {msg}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
