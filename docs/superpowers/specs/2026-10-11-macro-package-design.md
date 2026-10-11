# Macro package contract (M0): design

Date: 2026-10-11. Status: implemented in chipleap and aispice on `feat/macro-package`. Milestone M0 of the chip design roadmap (`~/workspace/personal/chip-design-roadmap.md`).

This is the design record for both repositories; aispice's `docs/superpowers/specs/2026-10-11-macro-package-design.md` points here. The format itself is specified in `docs/macro-package.md`, which both repositories carry byte for byte.

## Why

ChipLeap covers digital design and aispice analog design. Mixed-signal chips need each to place the other's blocks: digital-on-top chips (ChipLeap, milestone D4) place analog macros from aispice, analog-on-top chips (aispice, A4) place hardened digital blocks from ChipLeap. The roadmap fixes the macro package as the only coupling between the tools, so its format has to be exact, versioned, and checked the same way on both sides. The roadmap's biggest risk for M0 is the two tools drifting apart (risk 5).

## Goals

- One versioned format, `macro/v1`, that A4 and D4 write and read without knowing anything else about the other tool.
- A validator in each tool with the same rules, rule ids and messages, from the CLI and from each tool's MCP server.
- Shared fixtures that pin every finding both validators report, so drift fails CI in either repository.
- Library functions later milestones call to read, write and check packages.

## Non-goals

- Producing real packages. A4 (aispice macro export) and D3 or D4 (ChipLeap hardening) produce them.
- Running DRC, LVS or STA. The package records their results as evidence; the validator checks the records.
- Checking a package against the PDK's own files (layer names in the tech LEF, device models). That needs an installed PDK and belongs to the milestones that run the tools.

## Decisions

**Layout.** As the roadmap proposed: `macro.toml` plus `<name>.gds`, `.lef`, `.lib`, `.v`, `.spice`, a spec table for analog macros, and `evidence/`. One name everywhere (GDS top cell, LEF macro, liberty cell, Verilog module, SPICE subcircuit), so it must be an identifier valid in all five formats.

**`<name>.specs.toml`, not `<name>.specs`.** aispice already uses `<circuit>.specs` for spec definitions: one line per spec, a measurement expression over a testbench's nodes plus limits. A macro package has no testbench, and its consumer needs measured numbers per corner. Reusing the extension for a different format would confuse both people and aispice's `check_specs`, so the table is TOML with its own schema (`#/$defs/specs_file`) and keeps the original definition line as an optional `definition` field.

**Corners in `macro.toml`.** Both the spec table and the liberty files need corners, so they are declared once in the manifest. A corner may name its own liberty file, which D4 needs for slow-corner timing; `<name>.lib` is characterized at the first corner.

**Supply domains list their pins.** `[[domains]]` carries a nominal voltage and arrays of power and ground pins; signal pins name their domain, supply pins do not. A ground pin shared by two domains (common in practice) needs no special case.

**Buses by width.** A pin with `width = N` stands for bits `name[0]` to `name[N-1]`. LEF, liberty and SPICE list bits; Verilog declares a range of the same width. Square brackets are required everywhere.

**Evidence mirrors OpenADA's envelope rather than adopting it.** OpenADA's `openada.result/v0alpha1` requires execution and provenance fields (host, Python version, OpenADA version) that are meaningless for a hand-off and that neither tool produces, and its schema forbids extra fields, so a PDK field could not be added. The macro summary keeps OpenADA's field names for what they share (`schema`, `operation`, `tool`, `engineering.status`, `engineering.summary`, `inputs` with `sha256`), adds `pdk`, and the validator also accepts an unmodified OpenADA result. A converter from one to the other is a field copy.

**Stale evidence is an error.** Each summary lists its inputs with SHA-256 hashes. An input named like a package file must still match it; otherwise the GDS or netlist changed after the check ran.

**One schema file, three documents.** `macro-v1.schema.json` validates `macro.toml` at its root and, under `$defs`, an evidence summary and the spec table. It is the machine-readable contract (editors use it through `#:schema`) and the input of both validators.

**A small schema interpreter in each tool instead of a JSON Schema library.** Libraries word errors their own way and report them in their own order, which would break identical messages. Both tools carry the same roughly 100-line interpreter for the keywords the schema uses, applied in a fixed order with fixed message templates. ChipLeap's tests compare it with the `jsonschema` package on 26 valid and invalid manifests and on every fixture. One known difference is documented in the test: JSON Schema patterns are ECMA-262, where `$` matches only at the end, while the `jsonschema` package uses Python's `re`, which also matches before a final newline. Both of our interpreters follow ECMA-262.

**Readers written for the purpose, no new heavy dependencies.** GDS: a record-stream reader for cell names, references, text labels and bounding boxes through the hierarchy, exact for quarter turns (box of box) and for other angles (transformed corner points). Checked against KLayout on rotated, mirrored, arrayed and nested references and on flush and extended paths. LEF, liberty, Verilog and SPICE: tolerant scanners that extract only names, directions, sizes, pin shapes and timing arcs. ChipLeap adds `tomli` on Python 3.10 only; aispice-core adds `toml`, `regex` and `sha2`, all already in its lock file.

**Identical messages, pinned by fixtures.** `expected.json` lists, for every fixture and option, the exact findings in order. Each repository's tests run its own validator over the shared fixtures and compare. Parser error details are the one exception (each tool's TOML, JSON and GDS errors are worded by its own parser), so those findings pin a message prefix. Numbers in messages use C's `%.6g`, which Python has and aispice implements.

**Drift detection.** Each repository pins the SHA-256 of the spec, the schema and the fixtures' `SHA256SUMS`, and checks every fixture file against `SHA256SUMS`. A one-sided change fails that repository's tests until the hash is updated, which is the moment to copy the change to the other repository. CONTRIBUTING in both says so.

**Strictness.** Every view must declare exactly the manifest's pins, missing and extra alike, because LVS and placement both break on a mismatch. Unknown manifest fields are errors (typos), with `[extra]` for tool-specific data. A different PDK name is an error; a different PDK commit against `--pdk` is a warning, since an update often leaves a block valid.

**Versioning.** `macro/v1` is frozen once released. A change in meaning makes `macro/v2`; validators report an unknown `macro/` version as `manifest.version` instead of a pile of schema errors.

## Rules

The rule table is in `docs/macro-package.md`. Checks run in a fixed order: manifest (stop on syntax, version or schema errors), manifest semantics, directory name, required files, GDS, LEF, each liberty file, Verilog, SPICE, spec table, evidence, expected PDK. Within a view, pins in manifest order and bit by bit, then extra pins sorted by name.

## Interfaces for later milestones

ChipLeap (`chipleap.macro`): `check_package(path, pdk=None) -> Report`, `load_manifest(path) -> Manifest`, `Manifest` (with `Pin`, `Domain`, `Corner`, `Pdk`, `Producer`), `Manifest.to_toml()`, `make_evidence(...)`, `write_evidence(...)`, `required_files(manifest)`, `read_gds(path) -> GdsLibrary`, `schema()`. CLI `chipleap macro check DIR [--pdk NAME[@COMMIT]] [--strict] [--json]` and `chipleap macro schema`; MCP tool `check_macro`.

aispice (`aispice_core::macropkg`): `check_package(dir, &CheckOptions) -> Report`, `Manifest::load(dir)`, `Manifest::from_toml_str`, `Manifest::to_toml()`, `Evidence::new(...)` and `Evidence::write(...)`, `required_files`, `gds::read_gds`, `SCHEMA_JSON`. CLI `aispice macro check DIR [--pdk ...] [--strict] [--json]` and `aispice macro schema`; agent and MCP tool `check_macro`.

## Alternatives considered

- **Adopting OpenADA's envelope as is**: rejected for the reasons above; accepted as an input instead.
- **A JSON Schema library in each tool**: rejected because messages and order would differ.
- **Generated broken variants at test time**: rejected in favour of committed directories, which are easier to read and identical in both repositories without porting the mutation code.
- **A CI job that fetches the other repository's copy**: deferred. It would make one repository's CI fail on the other's unmerged work. The pinned hashes give the same signal at review time.

## Risks and follow-ups

- The readers accept the dialects the fixtures and common tools use. Real exports from Magic, KLayout, OpenROAD and LibreLane (A4, D3) may find gaps; extend the readers and add a fixture per gap, in both repositories.
- Subcell name collisions between macros merged into one chip GDS are not checked. D4 and A4 should prefix subcells with the macro name or uniquify on merge.
- PDK-aware checks (LEF layer names against the tech LEF, GDS layers against the PDK's layer table) need the PDK and come with D4 and A4.
