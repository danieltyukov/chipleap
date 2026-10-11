# MCP server

`chipleap mcp` runs ChipLeap as a [Model Context Protocol](https://modelcontextprotocol.io) server on stdio. The tools answer architecture questions (what to build and why), so an assistant can run the whole loop from a requirement written in plain words.

## Connect a client

Claude Code, available in every project:

```sh
claude mcp add chipleap -s user -- chipleap mcp
```

Claude Desktop (`claude_desktop_config.json`) or Cursor (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "chipleap": { "command": "chipleap", "args": ["mcp"] }
  }
}
```

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "chipleap": { "type": "stdio", "command": "chipleap", "args": ["mcp"] }
  }
}
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.chipleap]
command = "chipleap"
args = ["mcp"]
```

If a desktop app cannot find `chipleap` (common with pyenv, nvm-style shims or uv tool installs), use its absolute path: `which chipleap`.

## Tools

| Tool | Input | Returns |
|---|---|---|
| `required_resources` | spec (YAML), optional throughput | bytes per step, sustained bandwidth, MAC rate, capacity, CPU work, and which memories (how many channels) and nodes can deliver it |
| `explore_designs` | spec, samples, seed, max_steps | recommended design (also as YAML), its metrics and per-operator breakdown, the decision log, the Pareto front |
| `explain_bottleneck` | spec, design (YAML), weights, limit | the diagnosis, and every single change with its throughput, energy, area, power, bottleneck and feasibility, best first |
| `evaluate_design` | spec, design, weights | metrics, time shares, area and energy breakdowns, placement, violations, top operators |
| `describe_workload` | spec | MACs, weights and KV read per step, resident storage, the matrix multiplies |
| `generate_soc` | spec, design, out_dir | writes the bundle; returns the file list |
| `verify_soc` | out_dir, steps, fetch_xheep | consistency, lint, simulation, synthesis and X-HEEP results |
| `check_macro` | path, optional pdk (`NAME` or `NAME@COMMIT`) | checks a macro package ([macro/v1](macro-package.md)) before a chip places it: every finding with its rule id, severity and file |
| `list_models`, `catalog`, `check_tools` | none | presets, technology and memory data with sources, installed tools |

For RTL the user wrote (a project with a `project.yaml`, see [projects.md](projects.md)):

| Tool | Input | Returns |
|---|---|---|
| `describe_project` | project (file or directory) | the project with every default filled in; errors name the key |
| `lint_rtl` | project | Verilator and slang diagnostics: tool, severity, file, line, column, code, message |
| `simulate_rtl` | project, tests (a regular expression), simulator | every cocotb test's status; for a failure its message, file and line; compile errors as diagnostics |
| `coverage_rtl` | project, limit | line, branch and toggle coverage per file and the points the tests never reached |
| `synthesize_rtl` | project, tool | cell area, cells, flip-flops, critical path, Fmax estimate, Yosys warnings |
| `verify_rtl` | project, steps, sim_tool, synth_tool, pnr_tool | every step's result; writes `build/verify/report.html` |
| `dft_rtl` | project | scan chains (length, ports, clock), flip-flops scanned, Fault's stuck-at coverage and pattern count, the flush and pattern simulation on the scan and routed netlists, the pattern files |
| `power_rtl` | project, corners | per corner and group, power from the tests' activity on the gate-level netlist and the vectorless figure; pins annotated; the gate-level test results |
| `ir_drop_rtl` | project, corner | worst and average drop per supply net, the worst cells, the map |
| `upf_rtl` | project | power domains, their cells and areas, whether OpenROAD placed them inside, the signals between domains, the UPF OpenROAD wrote back |
| `timing_paths_rtl` | project, corner, check, count | the routed design's worst setup or hold paths, each stage's pin, instance, cell and delay |
| `eco_rtl` | project, changes | each change and what it did, timing and checks before and after the new signoff |

`simulate_rtl`, `coverage_rtl`, `power_rtl` and `verify_rtl` run the project's cocotb tests and golden model, which are code, so connect them only to projects you trust; their descriptions tell the assistant so. `describe_project`, `lint_rtl` and `synthesize_rtl` with Yosys run none of the project's code, and a project cannot make them show other files: its paths must stay inside its directory, and on Linux with Landlock the lint and synthesis tools can read only the project, the system and their own files ([projects.md](projects.md#files-outside-the-project)). No tool argument can widen that: directories outside come only from `CHIPLEAP_ALLOW_PATHS` in the environment you start the server with. None of this sandboxes the project's own code.

The implementation tools stop their OpenROAD and LibreLane runs when a call is cancelled or the server shuts down. `eco_rtl` takes changes as `resize:INSTANCE=CELL`, `buffer:INSTANCE/PIN=CELL` and `repair:setup` or `repair:hold`, with an optional `@MARGIN_NS`, always applied as a whole list to the original routed design; the `fix_timing` prompt has an assistant read `timing_paths_rtl`, try one change at a time and keep only what helps while DRC, LVS and antenna stay clean.

The `chipleap://spec-reference` resource documents the spec and design formats, and `chipleap://project-reference` the project file. The `design_chip` prompt walks an assistant through the loop: write a spec, check the requirements, explore, explain, then generate and verify with the user's agreement, keeping estimates and measurements apart. The `fix_rtl` prompt has it make a project lint clean and pass its tests, fixing the RTL from the diagnostics and never editing a test only to make it pass.

## Run logs and replay

```sh
chipleap mcp --log-dir ~/.chipleap/runs
chipleap replay ~/.chipleap/runs/chipleap-20261010-120000.jsonl
```

With `--log-dir`, every tool call is appended to a JSONL file with its arguments, the ChipLeap version and a SHA-256 of the result. `chipleap replay` re-runs the calls that have no side effects and do not read files (all but `generate_soc`, `verify_soc`, `check_tools` and `check_macro`) and reports whether each result is identical. The tools are deterministic for the same inputs (the explorer takes a seed), so a design session can be audited or reproduced without the assistant that produced it.

The RTL project tools are replayed too, all but `verify_rtl` (which can run place and route) and the tools that read the routed design in `build/` (`power_rtl`, `ir_drop_rtl`, `timing_paths_rtl`, `eco_rtl`), whose inputs the digest does not cover. Their results hold no absolute paths, times or host names, and cocotb runs with the project's seed. Their log entries also carry a SHA-256 over every file the project names (sources, includes, tests, golden model, waivers, constraints) and the versions of Verilator, slang, Icarus, Yosys, sv2v and cocotb (and of OpenROAD, Fault, Quaigh and the gate-level Icarus for `dft_rtl` and `upf_rtl`), so when a replayed result differs, `chipleap replay` says whether the files or the tools changed since the call was made.

## Example prompts

- What memory bandwidth does Llama 3.2 1B need for ten tokens per second, and which memories can deliver it?
- Design the smallest chip on the IHP open PDK that runs SmolLM2 135M at two tokens per second.
- This design is memory bound. What is the cheapest change that fixes it, and what does it cost in area?
- Compare int4 and int8 weights for Qwen3 0.6B prefill on 28 nm.
- Generate the SoC for the recommended design into ./out and verify it.
- Make the RTL project in ./uart lint clean and pass its tests, then tell me its area on IHP SG13G2.
- Insert scan chains into ./uart and tell me the fault coverage.
- The routed design in ./uart misses timing at the slow corner. Fix it with ECO changes, without placing it again.
