"""Macro packages (macro/v1): the hand-off format between ChipLeap and aispice.

A package is one hardened block: GDS, LEF, liberty, Verilog model, SPICE netlist,
measured specs and evidence, described by ``macro.toml``. docs/macro-package.md is
the spec. aispice implements the same format and rules in Rust.

Reading and checking::

    from chipleap.macro import check_package, load_manifest
    report = check_package("tiny_ota.macro", pdk="ihp-sg13g2")
    manifest = load_manifest("tiny_ota.macro")

Writing: build a :class:`Manifest`, write ``manifest.to_toml()`` to macro.toml next to
the views, add evidence with :func:`make_evidence` and :func:`write_evidence`, then
check the result with :func:`check_package`.
"""

from chipleap.macro.check import Finding, Report, check_package, parse_pdk_option, required_files
from chipleap.macro.evidence import make_evidence, write_evidence
from chipleap.macro.gds import GdsError, GdsLibrary, parse_gds, read_gds
from chipleap.macro.manifest import (
    SCHEMA_VERSION,
    Corner,
    Domain,
    MacroError,
    Manifest,
    Pdk,
    Pin,
    Producer,
    load_manifest,
)
from chipleap.macro.schema import schema, schema_text

__all__ = [
    "SCHEMA_VERSION",
    "Corner",
    "Domain",
    "Finding",
    "GdsError",
    "GdsLibrary",
    "MacroError",
    "Manifest",
    "Pdk",
    "Pin",
    "Producer",
    "Report",
    "check_package",
    "load_manifest",
    "make_evidence",
    "parse_gds",
    "parse_pdk_option",
    "read_gds",
    "required_files",
    "schema",
    "schema_text",
    "write_evidence",
]
