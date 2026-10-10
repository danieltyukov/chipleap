# Security policy

ChipLeap runs locally. It reads spec and design files, writes bundles, and starts Verilator, a C++ compiler, Yosys, sv2v, git and X-HEEP's Python generator as subprocesses. The MCP server exposes the same operations to an AI assistant.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub security advisories: https://github.com/danieltyukov/chipleap/security/advisories/new. Include the version (`chipleap --version`), the operating system, the steps to reproduce and what an attacker could gain.

## Scope

In scope:

- Spec, design or bundle contents that make ChipLeap write outside the requested output directory, execute commands, or run code other than the tools listed above.
- The MCP tools doing something the user did not ask for, for example through prompt injection in a spec, a design file or a bundle.
- Generated RTL, drivers or configurations that contain content not derived from the design.
- The release pipeline.

Out of scope:

- Vulnerabilities in Verilator, Yosys, sv2v, X-HEEP or the PDK. Report those to their maintainers.
- Running `chipleap verify --fetch-xheep`, which clones X-HEEP from GitHub by design. Point `--xheep` at a checkout you trust.
