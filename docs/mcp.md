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

`simulate_rtl`, `coverage_rtl` and `verify_rtl` run the project's cocotb tests and golden model, which are code, so connect them only to projects you trust. A project's paths must stay inside its directory and no tool argument can widen that: directories outside come only from `CHIPLEAP_ALLOW_PATHS` in the environment you start the server with ([projects.md](projects.md#files-outside-the-project)). That stops a project file from showing other files through results and diagnostics; it does not sandbox the project's own code.

The `chipleap://spec-reference` resource documents the spec and design formats, and `chipleap://project-reference` the project file. The `design_chip` prompt walks an assistant through the loop: write a spec, check the requirements, explore, explain, then generate and verify with the user's agreement, keeping estimates and measurements apart. The `fix_rtl` prompt has it make a project lint clean and pass its tests, fixing the RTL from the diagnostics and never editing a test only to make it pass.

## Run logs and replay

```sh
chipleap mcp --log-dir ~/.chipleap/runs
chipleap replay ~/.chipleap/runs/chipleap-20261010-120000.jsonl
```

With `--log-dir`, every tool call is appended to a JSONL file with its arguments, the ChipLeap version and a SHA-256 of the result. `chipleap replay` re-runs the calls that have no side effects and reports whether each result is identical. The tools are deterministic for the same inputs (the explorer takes a seed), so a design session can be audited or reproduced without the assistant that produced it.

The RTL project tools are replayed too (all but `verify_rtl`, which can run place and route). Their results hold no absolute paths, times or host names, and cocotb runs with the project's seed. Their log entries also carry a SHA-256 over every file the project names (sources, includes, tests, golden model, waivers, constraints) and the versions of Verilator, slang, Icarus, Yosys, sv2v and cocotb, so when a replayed result differs, `chipleap replay` says whether the files or the tools changed since the call was made.

## Example prompts

- What memory bandwidth does Llama 3.2 1B need for ten tokens per second, and which memories can deliver it?
- Design the smallest chip on the IHP open PDK that runs SmolLM2 135M at two tokens per second.
- This design is memory bound. What is the cheapest change that fixes it, and what does it cost in area?
- Compare int4 and int8 weights for Qwen3 0.6B prefill on 28 nm.
- Generate the SoC for the recommended design into ./out and verify it.
- Make the RTL project in ./uart lint clean and pass its tests, then tell me its area on IHP SG13G2.
