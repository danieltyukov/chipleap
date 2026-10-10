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
from chipleap.hw.design import Design, DesignError
from chipleap.hw.memory import MEMORIES
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.model.bounds import requirements
from chipleap.model.evaluate import evaluate
from chipleap.report.data import evaluation_dict, explore_dict, requirements_dict
from chipleap.report.text import format_evaluation, format_explore, format_requirements, num, table
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


def cmd_explore(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
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
            print(
                f"\nWrote {args.out}/ (report.html, DATASHEET.md, xheep/, rtl/, sw/, tb/). "
                f"Next: chipleap verify {args.out}"
            )
    return 0 if r.best.assessment.feasible else 2


def cmd_generate(args: argparse.Namespace) -> int:
    design, weights = load_design(args.design)
    spec = load_spec(args.spec) if args.spec else None
    cand = _evaluate(spec, design, weights) if spec else None
    if args.name:
        design = design.with_(name=args.name)
    files = write_bundle(Path(args.out), design, weights=weights, spec=spec, candidate=cand)
    print(f"Wrote {len(files)} files to {args.out}/")
    return 0


def _print_steps(results: list[Any]) -> int:
    failed = 0
    for r in results:
        print(f"{r.name:6s} {r.status.upper():8s} {r.summary}")
        failed += r.status == "fail"
    return 1 if failed else 0


def cmd_verify(args: argparse.Namespace) -> int:
    from chipleap.verify.remote import Remote
    from chipleap.verify.run import verify_bundle

    steps = tuple(s.strip() for s in args.steps.split(",") if s.strip())
    remote = Remote.from_env(getattr(args, "remote", None), getattr(args, "remote_setup", None))
    results = verify_bundle(
        Path(args.bundle),
        steps=steps,
        xheep_dir=Path(args.xheep) if args.xheep else None,
        fetch_xheep=args.fetch_xheep,
        cases=args.cases,
        seed=args.seed,
        sim_tool=getattr(args, "sim_tool", "verilator"),
        synth_tool=getattr(args, "synth_tool", "yosys"),
        synth_tech=getattr(args, "synth_tech", "ihp-sg13g2"),
        remote=remote,
    )
    code = _print_steps(results)
    print(f"\nWrote {args.bundle}/verify/VERIFY.md and verify.json")
    return code


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
    from chipleap.verify.tools import all_tools, ihp_liberty
    from chipleap.verify.xheep import find_checkout

    rows = [[t.name, "yes" if t.found else "no", t.version, t.purpose] for t in all_tools()]
    import shutil

    sv2v = shutil.which("sv2v")
    rows.append(["sv2v", "yes" if sv2v else "no", "", "SystemVerilog front end for older Yosys"])
    lib = ihp_liberty()
    rows.append(
        [
            "IHP SG13G2",
            "yes" if lib else "no",
            "",
            str(lib) if lib else "open PDK for synthesis (set CHIPLEAP_LIBERTY)",
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
    return 0


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
    dest.write_text(text)
    print(f"Wrote {dest}. Next: chipleap explore {dest} -o out")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from chipleap.mcp_server import main as mcp_main

    mcp_main(log_dir=args.log_dir)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from chipleap.mcp_server import replay

    ok, total = replay(Path(args.log))
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
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("verify", help="lint, simulate, synthesize and X-HEEP-check a bundle")
    s.add_argument("bundle")
    s.add_argument("--steps", default="consistency,lint,sim,synth,xheep")
    s.add_argument("--xheep", help="X-HEEP checkout (default: CHIPLEAP_XHEEP_DIR or the cache)")
    s.add_argument("--fetch-xheep", action="store_true", help="clone the tested X-HEEP commit")
    s.add_argument("--cases", type=int, default=0, help="limit the number of simulation cases")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument(
        "--sim-tool",
        default="verilator",
        choices=["verilator", "xcelium", "vcs", "questa", "verilator-sv"],
        help="simulator: Verilator (default), Cadence Xcelium, Synopsys VCS, Siemens Questa",
    )
    s.add_argument(
        "--synth-tool",
        default="yosys",
        choices=["yosys", "genus", "dc"],
        help="synthesis: Yosys (default), Cadence Genus or Synopsys Design Compiler",
    )
    s.add_argument(
        "--synth-tech",
        default="ihp-sg13g2",
        help="technology whose liberty Genus or Design Compiler maps to (chipleap tech add)",
    )
    s.add_argument("--remote", help="run the simulator and synthesis on this ssh host")
    s.add_argument(
        "--remote-setup",
        help="shell snippet that puts the tools on the path there (CHIPLEAP_REMOTE_SETUP)",
    )
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("doctor", help="which tools ChipLeap can use here or on a server")
    s.add_argument("--remote", help="also list the commercial tools on this ssh host")
    s.add_argument("--remote-setup", help="shell snippet that puts the tools on the path there")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("tech", help="technologies of your own, measured from liberty files")
    tsub = s.add_subparsers(dest="tech_cmd", required=True)
    t = tsub.add_parser("add", help="measure a PDK's liberty files and add it as a technology")
    t.add_argument("name", help="key for the technology, for example mylib40")
    t.add_argument("--liberty", required=True, help="standard-cell liberty (typical corner)")
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
