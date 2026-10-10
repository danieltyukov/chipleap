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

The `chipleap://spec-reference` resource documents the spec and design formats, and the `design_chip` prompt walks an assistant through the loop: write a spec, check the requirements, explore, explain, then generate and verify with the user's agreement, keeping estimates and measurements apart.

## Run logs and replay

```sh
chipleap mcp --log-dir ~/.chipleap/runs
chipleap replay ~/.chipleap/runs/chipleap-20261010-120000.jsonl
```

With `--log-dir`, every tool call is appended to a JSONL file with its arguments, the ChipLeap version and a SHA-256 of the result. `chipleap replay` re-runs the calls that have no side effects and reports whether each result is identical. The tools are deterministic for the same inputs (the explorer takes a seed), so a design session can be audited or reproduced without the assistant that produced it.

## Example prompts

- What memory bandwidth does Llama 3.2 1B need for ten tokens per second, and which memories can deliver it?
- Design the smallest chip on the IHP open PDK that runs SmolLM2 135M at two tokens per second.
- This design is memory bound. What is the cheapest change that fixes it, and what does it cost in area?
- Compare int4 and int8 weights for Qwen3 0.6B prefill on 28 nm.
- Generate the SoC for the recommended design into ./out and verify it.
