"""The macro/v1 validator: every rule of docs/macro-package.md, in the documented order.

aispice runs the same rules in Rust (``aispice_core::macropkg::check``), with the same
rule ids and messages; tests/fixtures/macro/expected.json pins both.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.macro.evidence import sha256_file
from chipleap.macro.gds import GdsCycleError, GdsError, GdsLibrary, parse_gds
from chipleap.macro.limits import (
    LIMITS,
    FileTooLarge,
    LimitError,
    Limits,
    json_depth,
    read_capped,
    toml_depth,
)
from chipleap.macro.manifest import (
    MANIFEST,
    OPENADA_SCHEMA,
    SCHEMA_VERSION,
    Manifest,
    Pin,
    parse_toml,
)
from chipleap.macro.schema import validate_def
from chipleap.macro.text import fmt_g, names, plural, quote
from chipleap.macro.views import (
    LefMacro,
    ViewError,
    ascii_lower,
    parse_lef,
    parse_liberty,
    spice_subckt,
    spice_subckts,
    verilog_ports,
)

REPORT_SCHEMA = "macro.check/v1"
REQUIRED_EVIDENCE = ("drc", "lvs")
# Half a database unit of 1 nm: sizes printed to three decimals still compare equal.
TOLERANCE_UM = 0.0005
_PDK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PDK_COMMIT = re.compile(r"[0-9a-f]{7,40}")
# The name spaces of each PDK's own cells, lower case: a chip merges layouts and netlists
# by cell name, so a macro's cell named in one could replace a cell of the PDK.
PDK_CELL_PREFIXES = {"ihp-sg13g2": ("sg13g2", "rm_ihpsg13")}


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # "error" or "warning"
    file: str  # relative to the package, "." for the package itself
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "message": self.message,
        }

    def to_text(self) -> str:
        where = "" if self.file == "." else f"{self.file}: "
        return f"{self.severity:<7} {self.rule} {where}{self.message}"


@dataclass
class Report:
    package: str
    name: str | None = None
    kind: str | None = None
    findings: list[Finding] = field(default_factory=list)
    # Findings past Limits.findings are counted, not listed; a check.limit warning
    # at the end of the list says how many.
    unlisted_errors: int = 0
    unlisted_warnings: int = 0

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings) + self.unlisted_errors

    @property
    def warnings(self) -> int:
        return sum(f.severity == "warning" for f in self.findings) + self.unlisted_warnings

    @property
    def passed(self) -> bool:
        return self.errors == 0

    def rules(self) -> list[str]:
        return [f.rule for f in self.findings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "package": self.package,
            "name": self.name,
            "kind": self.kind,
            "pass": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_text(self) -> str:
        lines = [f.to_text() for f in self.findings]
        verdict = "pass" if self.passed else "fail"
        lines.append(
            f"{self.package}: {verdict}, {plural(self.errors, 'error')}, "
            f"{plural(self.warnings, 'warning')}"
        )
        return "\n".join(lines)


def parse_pdk_option(text: str) -> tuple[str, str | None]:
    """``ihp-sg13g2`` or ``ihp-sg13g2@22f2a25`` as (name, commit)."""
    name, _, commit = text.partition("@")
    if not _PDK_NAME.fullmatch(name):
        raise ValueError(f"--pdk {text!r}: give NAME or NAME@COMMIT, e.g. ihp-sg13g2@22f2a25")
    if commit and not _PDK_COMMIT.fullmatch(commit):
        raise ValueError(f"--pdk {text!r}: the commit must be 7 to 40 lowercase hex digits")
    return name, commit or None


def commits_match(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _no_constant(name: str) -> None:
    # NaN and Infinity are not JSON; Python accepts them, aispice's serde_json does not.
    raise ValueError(f"{name} is not a JSON value")


def _json_int(s: str) -> int | float:
    # serde_json keeps an integer that fits in 64 bits and reads others as a float.
    if s == "-0":
        return -0.0
    if len(s.lstrip("-")) <= 20:
        v = int(s)
        if -(1 << 63) <= v < (1 << 64):
            return v
    return _json_float(s)


def _json_float(s: str) -> float:
    x = float(s)
    if math.isinf(x):
        raise ValueError("number out of range")
    return x


def _loads_json(text: str) -> Any:
    return json.loads(
        text, parse_constant=_no_constant, parse_int=_json_int, parse_float=_json_float
    )


def _nesting(limit: int, what: str = "tables and arrays") -> str:
    return f"{what} nest more than {limit} deep"


def _sum_text(err: Exception) -> str:
    return str(err).splitlines()[0] if str(err) else type(err).__name__


def _unreadable(err: OSError) -> str:
    # The reason alone: str(err) would put this machine's absolute path in the report.
    return f"cannot read the file: {err.strerror or _sum_text(err)}"


class _Checker:
    def __init__(
        self, root: Path, report: Report, limits: Limits, pdk_cells: frozenset[str] = frozenset()
    ) -> None:
        self.root = root
        self.report = report
        self.limits = limits
        self.pdk_cells = pdk_cells  # lower case: the cells of the PDK's libraries, if given
        # The cells a chip would merge, seen while the views are read: (file, kind, name).
        self.cells: list[tuple[str, str, str]] = []
        self.too_large: set[str] = set()  # files reported as too large, once each
        self.digests: dict[str, str | None] = {}  # package files hashed for evidence
        self.hash_limit_reported = False

    def add(self, rule: str, file: str, message: str, severity: str = "error") -> None:
        if len(self.report.findings) < self.limits.findings:
            self.report.findings.append(Finding(rule, severity, file, message))
        elif severity == "error":
            self.report.unlisted_errors += 1
        else:
            self.report.unlisted_warnings += 1

    def read(self, rel: str, limit: int) -> bytes | None:
        """A package file's bytes, or None after reporting why it cannot be read."""
        try:
            return read_capped(self.root / rel, limit)
        except FileTooLarge as exc:
            self.report_too_large(rel, str(exc))
        except OSError as exc:
            self.add("file.unreadable", rel, _unreadable(exc))
        return None

    def report_too_large(self, rel: str, message: str) -> None:
        if rel not in self.too_large:
            self.too_large.add(rel)
            self.add("file.too_large", rel, message)

    # macro.toml

    def manifest(self) -> Manifest | None:
        path = self.root / MANIFEST
        if not path.is_file():
            self.add("file.missing", MANIFEST, "required file is missing")
            return None
        raw = self.read(MANIFEST, self.limits.manifest_bytes)
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.add("manifest.syntax", MANIFEST, f"not valid TOML: {_sum_text(exc)}")
            return None
        if toml_depth(text) > self.limits.nesting:
            self.add("manifest.limit", MANIFEST, _nesting(self.limits.nesting))
            return None
        try:
            data = parse_toml(text)
        except ValueError as exc:
            self.add("manifest.syntax", MANIFEST, f"not valid TOML: {_sum_text(exc)}")
            return None
        version = data.get("schema")
        if isinstance(version, str) and version.startswith("macro/") and version != SCHEMA_VERSION:
            self.add(
                "manifest.version",
                MANIFEST,
                f"schema is {quote(version)}; this validator reads {SCHEMA_VERSION}",
            )
            return None
        errors = validate_def("", data)
        for e in errors:
            self.add("manifest.schema", MANIFEST, e)
        if errors:
            return None
        m = Manifest.from_dict(data)
        # Every view check walks every pin bit, so a huge bus would stall them all.
        too_big = False
        if m.pin_bits() > self.limits.pins:
            self.add("manifest.limit", MANIFEST, f"the pins have more than {self.limits.pins} bits")
            too_big = True
        if len(m.corners) > self.limits.corners:
            self.add("manifest.limit", MANIFEST, f"more than {self.limits.corners} corners")
            too_big = True
        self.report.name, self.report.kind = m.name, m.kind
        return None if too_big else m

    def semantics(self, m: Manifest) -> None:
        seen: set[str] = set()
        for p in m.pins:
            if p.name in seen:
                self.add(
                    "manifest.duplicate_pin", MANIFEST, f"pin {quote(p.name)} is declared twice"
                )
            seen.add(p.name)
        # Lookups by name: a long pin or domain list must not cost its square.
        by_name: dict[str, Pin] = {}
        for p in m.pins:
            by_name.setdefault(p.name, p)
        listed: dict[str, set[str]] = {"power": set(), "ground": set()}
        domains: set[str] = set()
        for d in m.domains:
            if d.name in domains:
                self.add("manifest.domain", MANIFEST, f"domain {quote(d.name)} is declared twice")
            domains.add(d.name)
            listed["power"].update(d.power)
            listed["ground"].update(d.ground)
            for role, pin_names in (("power", d.power), ("ground", d.ground)):
                for n in pin_names:
                    pin = by_name.get(n)
                    if pin is None:
                        self.add(
                            "manifest.domain",
                            MANIFEST,
                            f"domain {quote(d.name)} lists {quote(n)} as {role}, "
                            "but there is no such pin",
                        )
                    elif pin.kind != role:
                        self.add(
                            "manifest.domain",
                            MANIFEST,
                            f"domain {quote(d.name)} lists {quote(n)} as {role}, "
                            f"but its kind is {pin.kind}",
                        )
        for p in m.pins:
            if p.is_supply:
                if p.name not in listed[p.kind]:
                    self.add(
                        "manifest.domain", MANIFEST, f"{p.kind} pin {quote(p.name)} is in no domain"
                    )
            elif p.domain not in domains:
                self.add(
                    "manifest.domain",
                    MANIFEST,
                    f"pin {quote(p.name)} names domain {quote(p.domain)}, which is not declared",
                )
        corners: set[str] = set()
        for c in m.corners:
            if c.name in corners:
                self.add("manifest.corner", MANIFEST, f"corner {quote(c.name)} is declared twice")
            corners.add(c.name)
            for k in sorted(c.voltages):
                if k not in domains:
                    self.add(
                        "manifest.corner",
                        MANIFEST,
                        f"corner {quote(c.name)} sets the voltage of domain {quote(k)}, "
                        "which is not declared",
                    )

    def dirname(self, m: Manifest) -> None:
        actual = self.root.resolve().name
        if actual != f"{m.name}.macro":
            self.add(
                "package.dirname",
                ".",
                f"the directory is {quote(actual)}; name it {quote(m.name + '.macro')}",
                "warning",
            )

    def required_files(self, m: Manifest) -> None:
        for f in required_files(m):
            if not (self.root / f).is_file():
                self.add("file.missing", f, "required file is missing")

    # GDS

    def gds(self, m: Manifest, lef: LefMacro | None) -> None:
        f = m.file("gds")
        if not (self.root / f).is_file():
            return
        data = self.read(f, self.limits.gds_bytes)
        if data is None:
            return
        try:
            lib = parse_gds(data, self.limits)
        except GdsCycleError as exc:
            self.add("gds.cycle", f, str(exc))
            return
        except LimitError as exc:
            self.add("gds.limit", f, str(exc))
            return
        except GdsError as exc:
            self.add("gds.parse", f, f"cannot read the GDS: {_sum_text(exc)}")
            return
        self.cells += [(f, "GDS cell", c) for c in lib.cells if c != m.name]
        tops = lib.top_cells()
        if m.name not in tops:
            if m.name in lib.cells:
                self.add("gds.top_cell", f, f"cell {quote(m.name)} is placed inside another cell")
            else:
                self.add(
                    "gds.top_cell",
                    f,
                    f"no top cell is named {quote(m.name)}; top cells: {names(tops)}",
                )
            return
        for t in tops:
            if t != m.name:
                self.add(
                    "gds.extra_top_cell",
                    f,
                    f"top cell {quote(t)} would be merged into the chip with the macro",
                    "warning",
                )
        self._gds_bbox(m, lib, f, lef)
        labels = set(lib.labels(m.name))
        for p in m.pins:
            for bit in p.bits():
                if bit not in labels:
                    self.add(
                        "gds.label",
                        f,
                        f"pin {quote(bit)} has no text label in the top cell",
                        "warning",
                    )

    def _gds_bbox(self, m: Manifest, lib: GdsLibrary, f: str, lef: LefMacro | None) -> None:
        try:
            box = lib.bbox(m.name)
        except LimitError as exc:
            self.add("gds.limit", f, str(exc))
            return
        except GdsError as exc:
            self.add("gds.parse", f, f"cannot read the GDS: {_sum_text(exc)}")
            return
        if box is None:
            self.add("gds.bbox", f, "the top cell has no geometry")
            return
        ox, oy = lef.origin if lef is not None else (0.0, 0.0)
        bx0, by0 = -ox, -oy
        bx1, by1 = bx0 + m.width_um, by0 + m.height_um
        x0, y0, x1, y1 = (v + 0.0 for v in box)
        t = TOLERANCE_UM
        if x0 < bx0 - t or y0 < by0 - t or x1 > bx1 + t or y1 > by1 + t:
            self.add(
                "gds.bbox",
                f,
                f"the top cell spans ({fmt_g(x0)}, {fmt_g(y0)}) to ({fmt_g(x1)}, {fmt_g(y1)}) um, "
                f"outside the {fmt_g(m.width_um)} x {fmt_g(m.height_um)} um boundary",
            )

    # LEF

    def read_lef(self, m: Manifest) -> tuple[LefMacro | None, tuple[str, str] | None, list[str]]:
        """(macro, (rule, message) of a problem, macro names), reporting nothing: the GDS
        check needs the LEF origin, and the LEF's own findings come after the GDS's."""
        f = m.file("lef")
        path = self.root / f
        if not path.is_file():
            return None, None, []
        try:
            macros = parse_lef(_text(read_capped(path, self.limits.view_bytes)), self.limits)
        except FileTooLarge as exc:
            return None, ("file.too_large", str(exc)), []
        except OSError as exc:
            return None, ("file.unreadable", _unreadable(exc)), []
        except LimitError as exc:
            return None, ("lef.limit", str(exc)), []
        except ViewError as exc:
            return None, ("lef.parse", f"cannot read the LEF: {_sum_text(exc)}"), []
        return macros.get(m.name), None, sorted(macros)

    def lef(
        self,
        m: Manifest,
        lef: LefMacro | None,
        problem: tuple[str, str] | None,
        macro_names: list[str],
    ) -> None:
        f = m.file("lef")
        if not (self.root / f).is_file():
            return
        if problem is not None:
            rule, message = problem
            if rule == "file.too_large":
                self.report_too_large(f, message)
            else:
                self.add(rule, f, message)
            return
        if lef is None:
            self.add(
                "lef.macro",
                f,
                f"no MACRO {quote(m.name)}; macros in the file: {names(macro_names)}",
            )
            return
        cls = lef.cls.split(" ")[0]
        if cls != "BLOCK":
            what = f"CLASS is {quote(lef.cls)}" if lef.cls else "the macro has no CLASS"
            self.add("lef.class", f, f"{what}, expected BLOCK", "warning")
        if lef.size is None:
            self.add("lef.size", f, "the macro has no SIZE")
        elif (
            abs(lef.size[0] - m.width_um) > TOLERANCE_UM
            or abs(lef.size[1] - m.height_um) > TOLERANCE_UM
        ):
            self.add(
                "lef.size",
                f,
                f"SIZE is {fmt_g(lef.size[0])} x {fmt_g(lef.size[1])} um, macro.toml says "
                f"{fmt_g(m.width_um)} x {fmt_g(m.height_um)} um",
            )
        bits: set[str] = set()
        for p in m.pins:
            for bit in p.bits():
                bits.add(bit)
                lp = lef.pins.get(bit)
                if lp is None:
                    self.add("lef.missing_pin", f, f"pin {quote(bit)} from macro.toml is missing")
                    continue
                use = lp.use or "SIGNAL"
                if p.is_supply:
                    want = p.kind.upper()
                    if use != want:
                        self.add(
                            "lef.use", f, f"pin {quote(bit)} is USE {use}, expected USE {want}"
                        )
                else:
                    want_dir = p.direction.upper()
                    if lp.direction != want_dir:
                        got = f"DIRECTION {lp.direction}" if lp.direction else "no DIRECTION"
                        self.add(
                            "lef.direction",
                            f,
                            f"pin {quote(bit)} has {got}, macro.toml says {p.direction}",
                        )
                    if use in ("POWER", "GROUND"):
                        self.add(
                            "lef.use", f, f"pin {quote(bit)} is USE {use}, but its kind is {p.kind}"
                        )
                    elif use != p.kind.upper():
                        self.add(
                            "lef.use",
                            f,
                            f"pin {quote(bit)} is USE {use}, expected USE {p.kind.upper()}",
                            "warning",
                        )
                if lp.shapes == 0:
                    self.add("lef.port", f, f"pin {quote(bit)} has no PORT shapes")
                if p.layer and p.layer not in lp.layers:
                    self.add(
                        "lef.pin_layer", f, f"pin {quote(bit)} has no shape on {quote(p.layer)}"
                    )
        for name in sorted(set(lef.pins) - bits):
            self.add("lef.extra_pin", f, f"pin {quote(name)} is not in macro.toml")

    # Liberty

    def liberty(self, m: Manifest, f: str) -> None:
        if not (self.root / f).is_file():
            return
        data = self.read(f, self.limits.liberty_bytes)
        if data is None:
            return
        try:
            cells = parse_liberty(_text(data), self.limits, cell=m.name)
        except LimitError as exc:
            self.add("lib.limit", f, str(exc))
            return
        except ViewError as exc:
            self.add("lib.parse", f, f"cannot read the liberty file: {_sum_text(exc)}")
            return
        pins = cells.get(m.name)
        if pins is None:
            self.add("lib.cell", f, f"no cell {quote(m.name)}")
            return
        bits: set[str] = set()
        for p in m.pins:
            for bit in p.bits():
                bits.add(bit)
                lp = pins.get(bit)
                if lp is None:
                    self.add("lib.missing_pin", f, f"pin {quote(bit)} from macro.toml is missing")
                elif p.is_supply:
                    if not lp.pg:
                        self.add("lib.pg_pin", f, f"pin {quote(bit)} must be a pg_pin")
                    elif not lp.pg_type.endswith("_" + p.kind):
                        got = f"pg_type {quote(lp.pg_type)}" if lp.pg_type else "no pg_type"
                        self.add(
                            "lib.pg_pin",
                            f,
                            f"pg_pin {quote(bit)} has {got}, expected a *_{p.kind} type",
                        )
                elif lp.pg:
                    self.add(
                        "lib.pg_pin", f, f"pin {quote(bit)} is a pg_pin, but its kind is {p.kind}"
                    )
                else:
                    if lp.direction != p.direction:
                        got = f"direction {quote(lp.direction)}" if lp.direction else "no direction"
                        self.add(
                            "lib.direction",
                            f,
                            f"pin {quote(bit)} has {got}, macro.toml says {p.direction}",
                        )
                    if (
                        m.kind == "digital"
                        and p.direction == "output"
                        and p.kind in ("signal", "clock")
                        and lp.timing == 0
                    ):
                        self.add("lib.timing", f, f"output pin {quote(bit)} has no timing arc")
        for name in sorted(set(pins) - bits):
            self.add("lib.extra_pin", f, f"pin {quote(name)} is not in macro.toml")

    # Verilog

    def verilog(self, m: Manifest) -> None:
        f = m.file("v")
        if not (self.root / f).is_file():
            return
        data = self.read(f, self.limits.view_bytes)
        if data is None:
            return
        try:
            ports = verilog_ports(_text(data), m.name, self.limits)
        except LimitError as exc:
            self.add("verilog.limit", f, str(exc))
            return
        if ports is None:
            self.add("verilog.module", f, f"no module {quote(m.name)}")
            return
        by_name = {vp.name: vp for vp in ports}
        for p in m.pins:
            vp = by_name.get(p.name)
            if vp is None:
                if not p.is_supply:
                    self.add(
                        "verilog.missing_pin", f, f"port {quote(p.name)} from macro.toml is missing"
                    )
                continue
            if not vp.direction:
                self.add(
                    "verilog.direction",
                    f,
                    f"port {quote(p.name)} is never declared input, output or inout",
                )
            elif p.is_supply:
                if vp.direction not in ("inout", "input"):
                    self.add(
                        "verilog.direction",
                        f,
                        f"{p.kind} port {quote(p.name)} is {vp.direction}, expected inout or input",
                    )
            elif vp.direction != p.direction:
                self.add(
                    "verilog.direction",
                    f,
                    f"port {quote(p.name)} is {vp.direction}, macro.toml says {p.direction}",
                )
            if p.is_supply or vp.width == 0:
                continue
            if p.width is None and vp.width is not None:
                self.add(
                    "verilog.width",
                    f,
                    f"port {quote(p.name)} is a bus of width {vp.width}, macro.toml says a scalar",
                )
            elif p.width is not None and vp.width is None:
                self.add(
                    "verilog.width",
                    f,
                    f"port {quote(p.name)} is a scalar, macro.toml says a bus of width {p.width}",
                )
            elif p.width is not None and vp.width != p.width:
                self.add(
                    "verilog.width",
                    f,
                    f"port {quote(p.name)} has width {vp.width}, macro.toml says {p.width}",
                )
        names = {p.name for p in m.pins}
        for name in sorted(set(by_name) - names):
            self.add("verilog.extra_pin", f, f"port {quote(name)} is not in macro.toml")

    # SPICE

    def spice(self, m: Manifest) -> None:
        f = m.file("spice")
        if not (self.root / f).is_file():
            return
        data = self.read(f, self.limits.view_bytes)
        if data is None:
            return
        try:
            sub = spice_subckt(_text(data), m.name, self.limits)
            self.cells += [
                (f, "subcircuit", c)
                for c in spice_subckts(_text(data), self.limits)
                if ascii_lower(c) != ascii_lower(m.name)
            ]
        except LimitError as exc:
            self.add("spice.limit", f, str(exc))
            return
        if sub is None:
            self.add("spice.subckt", f, f"no .subckt {quote(m.name)}")
            return
        ports = {ascii_lower(p) for p in sub.ports}
        letters = {"input": "I", "output": "O", "inout": "B"}
        bits: set[str] = set()
        for p in m.pins:
            for bit in p.bits():
                bits.add(ascii_lower(bit))
                if ascii_lower(bit) not in ports:
                    self.add(
                        "spice.missing_pin", f, f"port {quote(bit)} from macro.toml is missing"
                    )
                    continue
                got = sub.pininfo.get(ascii_lower(bit))
                if not p.is_supply and got is not None and got != letters[p.direction]:
                    self.add(
                        "spice.direction",
                        f,
                        f"*.PININFO gives {quote(bit)} direction {got}, "
                        f"macro.toml says {p.direction}",
                    )
        for name in sorted({p for p in sub.ports if ascii_lower(p) not in bits}):
            self.add("spice.extra_pin", f, f"port {quote(name)} is not in macro.toml")

    # Spec table

    def specs(self, m: Manifest) -> None:
        f = m.file("specs.toml")
        if not (self.root / f).is_file():
            return
        raw = self.read(f, self.limits.specs_bytes)
        if raw is None:
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.add("specs.parse", f, f"not valid TOML: {_sum_text(exc)}")
            return
        if toml_depth(text) > self.limits.nesting:
            self.add("specs.limit", f, _nesting(self.limits.nesting))
            return
        try:
            data = parse_toml(text)
        except ValueError as exc:
            self.add("specs.parse", f, f"not valid TOML: {_sum_text(exc)}")
            return
        errors = validate_def("specs_file", data)
        for e in errors:
            self.add("specs.schema", f, e)
        if errors:
            return
        corners = [c.name for c in m.corners]
        seen: set[str] = set()
        for s in data["specs"]:
            name = s["name"]
            if name in seen:
                self.add("specs.duplicate", f, f"spec {quote(name)} is declared twice")
            seen.add(name)
        for s in data["specs"]:
            name, measured = s["name"], s["measured"]
            for c in corners:
                if c not in measured:
                    self.add(
                        "specs.corner",
                        f,
                        f"spec {quote(name)} has no value at corner {quote(c)}",
                        "warning",
                    )
            unknown = sorted(k for k in measured if k not in corners)
            for k in unknown:
                self.add(
                    "specs.corner",
                    f,
                    f"spec {quote(name)} has a value at corner {quote(k)}, "
                    "which macro.toml does not declare",
                )
            unit = f" {s['unit']}" if s["unit"] else ""
            for k in [c for c in corners if c in measured] + unknown:
                v = measured[k]
                if "min" in s and v < s["min"]:
                    self.add(
                        "specs.fail",
                        f,
                        f"spec {quote(name)} at corner {quote(k)} is {fmt_g(v)}{unit}, "
                        f"below the minimum {fmt_g(s['min'])}{unit}",
                    )
                if "max" in s and v > s["max"]:
                    self.add(
                        "specs.fail",
                        f,
                        f"spec {quote(name)} at corner {quote(k)} is {fmt_g(v)}{unit}, "
                        f"above the maximum {fmt_g(s['max'])}{unit}",
                    )

    # Evidence

    def summaries(self, folder: Path) -> list[str] | None:
        """Names of the summaries in evidence/, sorted; None when there are too many.
        Names that are not UTF-8 are skipped, as aispice cannot name them either."""
        if not folder.is_dir():
            return []
        found: list[str] = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
                try:
                    os.fsencode(entry.name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                found.append(entry.name)
                if len(found) > self.limits.evidence_files:
                    return None
        return sorted(found)

    def digest(self, f: str, base: str) -> str | None:
        """The SHA-256 of package file ``base``, hashed once per check. A file already
        found too large for its own check is not read here either."""
        if base in self.digests:
            return self.digests[base]
        if base in self.too_large:
            return None
        if len(self.digests) >= self.limits.hashed_files:
            if not self.hash_limit_reported:
                self.hash_limit_reported = True
                self.add(
                    "evidence.limit",
                    f,
                    f"the summaries name more than {self.limits.hashed_files} package files "
                    "as inputs",
                )
            return None
        digest: str | None = None
        try:
            digest = sha256_file(self.root / base, self.limits.gds_bytes)
        except FileTooLarge as exc:
            self.report_too_large(base, str(exc))
        except OSError as exc:
            self.add("file.unreadable", base, _unreadable(exc))
        self.digests[base] = digest
        return digest

    def evidence(self, m: Manifest) -> None:
        files = self.summaries(self.root / "evidence")
        if files is None:
            self.add(
                "evidence.limit",
                "evidence",
                f"more than {self.limits.evidence_files} summaries",
            )
            return
        seen: set[str] = set()
        for name in files:
            f = f"evidence/{name}"
            raw = self.read(f, self.limits.evidence_bytes)
            if raw is None:
                continue
            try:
                text = raw.decode("utf-8")
                if json_depth(text) > self.limits.nesting:
                    self.add(
                        "evidence.limit", f, _nesting(self.limits.nesting, "objects and arrays")
                    )
                    continue
                doc = _loads_json(text)
            except ValueError as exc:
                self.add("evidence.parse", f, f"not valid JSON: {_sum_text(exc)}")
                continue
            openada = isinstance(doc, dict) and doc.get("schema") == OPENADA_SCHEMA
            errors = validate_def("openada_result" if openada else "evidence", doc)
            for e in errors:
                self.add("evidence.schema", f, e)
            if errors:
                continue
            op, status = doc["operation"], doc["engineering"]["status"]
            seen.add(op)
            if op in REQUIRED_EVIDENCE and status != "pass":
                self.add("evidence.status", f, f"{op} status is {quote(status)}, expected pass")
            elif status not in ("pass", "not_applicable"):
                self.add(
                    "evidence.status",
                    f,
                    f"{op} status is {quote(status)}, expected pass or not_applicable",
                )
            for item in doc.get("inputs", []):
                path_in, digest = item.get("path"), item.get("sha256")
                if not isinstance(path_in, str) or not isinstance(digest, str):
                    continue
                base = path_in.replace("\\", "/").rsplit("/", 1)[-1]
                if base in ("", ".", "..") or not (self.root / base).is_file():
                    continue
                actual = self.digest(f, base)
                if actual is not None and actual != digest:
                    self.add(
                        "evidence.stale",
                        f,
                        f"input {quote(base)} has sha256 {digest[:12]}, "
                        f"the package file has {actual[:12]}",
                    )
            if not openada:
                pdk = doc["pdk"]
                if pdk["name"] != m.pdk.name:
                    self.add(
                        "pdk.mismatch",
                        f,
                        f"ran on PDK {quote(pdk['name'])}, macro.toml says {quote(m.pdk.name)}",
                    )
                elif not commits_match(pdk["commit"], m.pdk.commit):
                    self.add(
                        "pdk.mismatch",
                        f,
                        f"ran on PDK commit {quote(pdk['commit'])}, "
                        f"macro.toml says {quote(m.pdk.commit)}",
                    )
        for op in REQUIRED_EVIDENCE:
            if op not in seen:
                self.add("evidence.missing", "evidence", f"no summary for {op}")

    def expected_pdk(self, m: Manifest, name: str, commit: str | None) -> None:
        if m.pdk.name != name:
            self.add(
                "pdk.expected",
                MANIFEST,
                f"the package is built for {quote(m.pdk.name)}, expected {quote(name)}",
            )
        elif commit is not None and not commits_match(commit, m.pdk.commit):
            self.add(
                "pdk.expected",
                MANIFEST,
                f"the package is built on PDK commit {quote(m.pdk.commit)}, "
                f"expected {quote(commit)}",
                "warning",
            )


def _cell_names(c: _Checker, m: Manifest) -> None:
    """``pdk.cell_name``: the package's name and every cell the chip would merge, ASCII
    case folded, outside its PDK's cell name spaces and, when the validator has the PDK's
    libraries, not one of their cells."""
    prefixes = PDK_CELL_PREFIXES.get(m.pdk.name, ())
    for f, what, name in [(MANIFEST, "name", m.name), *c.cells]:
        low = ascii_lower(name)
        hit = next((p for p in prefixes if low.startswith(p)), None)
        if hit is not None:
            c.add(
                "pdk.cell_name",
                f,
                f"{what} {quote(name)} begins with {quote(hit)}, as the cells of PDK "
                f"{quote(m.pdk.name)} do",
            )
        elif low in c.pdk_cells:
            c.add(
                "pdk.cell_name",
                f,
                f"{what} {quote(name)} is a cell of PDK {quote(m.pdk.name)}",
            )


def required_files(m: Manifest) -> list[str]:
    """Files the package must hold beside macro.toml and evidence/, in check order."""
    files = [m.file(ext) for ext in ("gds", "lef", "lib", "v", "spice")]
    if m.kind == "analog":
        files.append(m.file("specs.toml"))
    for c in m.corners:
        if c.liberty and c.liberty not in files:
            files.append(c.liberty)
    return files


def check_package(
    path: str | Path,
    pdk: str | None = None,
    limits: Limits = LIMITS,
    pdk_cells: Iterable[str] = (),
) -> Report:
    """Check a macro package directory against macro/v1.

    ``pdk`` is the placing chip's PDK, ``NAME`` or ``NAME@COMMIT``. ``pdk_cells`` are the
    names of the cells of the PDK's libraries, when the caller has them
    (``chipleap.macro.layer.pdk_cell_names``). Raises ``FileNotFoundError`` when ``path``
    is not a directory and ``ValueError`` when ``pdk`` is malformed. ``limits`` exist for
    tests; the spec fixes their values.
    """
    expected = parse_pdk_option(pdk) if pdk is not None else None
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")
    report = Report(str(path))
    c = _Checker(root, report, limits, frozenset(ascii_lower(n) for n in pdk_cells))
    _check(c, expected)
    unlisted = report.unlisted_errors + report.unlisted_warnings
    if unlisted:
        report.findings.append(
            Finding(
                "check.limit",
                "warning",
                ".",
                f"{plural(unlisted, 'more finding')} not listed: "
                f"{plural(report.unlisted_errors, 'error')}, "
                f"{plural(report.unlisted_warnings, 'warning')}",
            )
        )
    return report


def _check(c: _Checker, expected: tuple[str, str | None] | None) -> None:
    m = c.manifest()
    if m is None:
        return
    c.semantics(m)
    c.dirname(m)
    c.required_files(m)
    lef, lef_problem, lef_names = c.read_lef(m)
    c.gds(m, lef)
    c.lef(m, lef, lef_problem, lef_names)
    libs = [m.file("lib")] + [
        x.liberty for x in m.corners if x.liberty and x.liberty != m.file("lib")
    ]
    for f in dict.fromkeys(libs):
        c.liberty(m, f)
    c.verilog(m)
    c.spice(m)
    c.specs(m)
    c.evidence(m)
    if expected is not None:
        c.expected_pdk(m, *expected)
    _cell_names(c, m)
