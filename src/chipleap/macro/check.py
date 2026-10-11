"""The macro/v1 validator: every rule of docs/macro-package.md, in the documented order.

aispice runs the same rules in Rust (``aispice_core::macropkg::check``), with the same
rule ids and messages; tests/fixtures/macro/expected.json pins both.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.macro.evidence import sha256_file
from chipleap.macro.gds import GdsError, GdsLibrary, read_gds
from chipleap.macro.manifest import (
    MANIFEST,
    OPENADA_SCHEMA,
    SCHEMA_VERSION,
    Manifest,
)
from chipleap.macro.schema import validate_def
from chipleap.macro.text import fmt_g, plural, quote
from chipleap.macro.views import (
    LefMacro,
    ViewError,
    parse_lef,
    parse_liberty,
    spice_subckt,
    verilog_ports,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

REPORT_SCHEMA = "macro.check/v1"
REQUIRED_EVIDENCE = ("drc", "lvs")
# Half a database unit of 1 nm: sizes printed to three decimals still compare equal.
TOLERANCE_UM = 0.0005
_PDK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PDK_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


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

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(f.severity == "warning" for f in self.findings)

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
    if not _PDK_NAME.match(name) or name != name.strip():
        raise ValueError(f"--pdk {text!r}: give NAME or NAME@COMMIT, e.g. ihp-sg13g2@22f2a25")
    if commit and not _PDK_COMMIT.fullmatch(commit):
        raise ValueError(f"--pdk {text!r}: the commit must be 7 to 40 lowercase hex digits")
    return name, commit or None


def commits_match(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _sum_text(err: Exception) -> str:
    return str(err).splitlines()[0] if str(err) else type(err).__name__


class _Checker:
    def __init__(self, root: Path, report: Report) -> None:
        self.root = root
        self.report = report

    def add(self, rule: str, file: str, message: str, severity: str = "error") -> None:
        self.report.findings.append(Finding(rule, severity, file, message))

    # macro.toml

    def manifest(self) -> Manifest | None:
        path = self.root / MANIFEST
        if not path.is_file():
            self.add("file.missing", MANIFEST, "required file is missing")
            return None
        try:
            data = tomllib.loads(path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
        self.report.name, self.report.kind = m.name, m.kind
        return m

    def semantics(self, m: Manifest) -> None:
        seen: set[str] = set()
        for p in m.pins:
            if p.name in seen:
                self.add(
                    "manifest.duplicate_pin", MANIFEST, f"pin {quote(p.name)} is declared twice"
                )
            seen.add(p.name)
        domains: set[str] = set()
        for d in m.domains:
            if d.name in domains:
                self.add("manifest.domain", MANIFEST, f"domain {quote(d.name)} is declared twice")
            domains.add(d.name)
            for role, names in (("power", d.power), ("ground", d.ground)):
                for n in names:
                    pin = m.pin(n)
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
                listed = any(
                    p.name in (d.power if p.kind == "power" else d.ground) for d in m.domains
                )
                if not listed:
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
        path = self.root / f
        if not path.is_file():
            return
        try:
            lib = read_gds(path)
        except (OSError, GdsError) as exc:
            self.add("gds.parse", f, f"cannot read the GDS: {_sum_text(exc)}")
            return
        tops = lib.top_cells()
        if m.name not in tops:
            if m.name in lib.cells:
                self.add("gds.top_cell", f, f"cell {quote(m.name)} is placed inside another cell")
            else:
                found = ", ".join(quote(t) for t in tops) or "none"
                self.add(
                    "gds.top_cell", f, f"no top cell is named {quote(m.name)}; top cells: {found}"
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

    def read_lef(self, m: Manifest) -> tuple[LefMacro | None, str | None, list[str]]:
        """(macro, parse error, other macro names) without reporting anything."""
        path = self.root / m.file("lef")
        if not path.is_file():
            return None, None, []
        try:
            macros = parse_lef(_read_text(path))
        except ViewError as exc:
            return None, _sum_text(exc), []
        return macros.get(m.name), None, sorted(macros)

    def lef(self, m: Manifest, lef: LefMacro | None, error: str | None, names: list[str]) -> None:
        f = m.file("lef")
        if not (self.root / f).is_file():
            return
        if error is not None:
            self.add("lef.parse", f, f"cannot read the LEF: {error}")
            return
        if lef is None:
            others = ", ".join(quote(n) for n in names) or "none"
            self.add("lef.macro", f, f"no MACRO {quote(m.name)}; macros in the file: {others}")
            return
        cls = lef.cls.split()[0] if lef.cls else ""
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
        path = self.root / f
        if not path.is_file():
            return
        try:
            cells = parse_liberty(_read_text(path))
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
        path = self.root / f
        if not path.is_file():
            return
        ports = verilog_ports(_read_text(path), m.name)
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
        path = self.root / f
        if not path.is_file():
            return
        sub = spice_subckt(_read_text(path), m.name)
        if sub is None:
            self.add("spice.subckt", f, f"no .subckt {quote(m.name)}")
            return
        ports = {p.lower() for p in sub.ports}
        letters = {"input": "I", "output": "O", "inout": "B"}
        bits: set[str] = set()
        for p in m.pins:
            for bit in p.bits():
                bits.add(bit.lower())
                if bit.lower() not in ports:
                    self.add(
                        "spice.missing_pin", f, f"port {quote(bit)} from macro.toml is missing"
                    )
                    continue
                got = sub.pininfo.get(bit.lower())
                if not p.is_supply and got is not None and got != letters[p.direction]:
                    self.add(
                        "spice.direction",
                        f,
                        f"*.PININFO gives {quote(bit)} direction {got}, "
                        f"macro.toml says {p.direction}",
                    )
        for name in sorted({p for p in sub.ports if p.lower() not in bits}):
            self.add("spice.extra_pin", f, f"port {quote(name)} is not in macro.toml")

    # Spec table

    def specs(self, m: Manifest) -> None:
        f = m.file("specs.toml")
        path = self.root / f
        if not path.is_file():
            return
        try:
            data = tomllib.loads(path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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

    def evidence(self, m: Manifest) -> None:
        folder = self.root / "evidence"
        files = sorted(p for p in folder.glob("*.json") if p.is_file()) if folder.is_dir() else []
        seen: set[str] = set()
        for path in files:
            f = f"evidence/{path.name}"
            try:
                doc = json.loads(path.read_bytes().decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
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
                target = self.root / base
                if base in ("", ".", "..") or not target.is_file():
                    continue
                actual = sha256_file(target)
                if actual != digest:
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


def required_files(m: Manifest) -> list[str]:
    """Files the package must hold beside macro.toml and evidence/, in check order."""
    files = [m.file(ext) for ext in ("gds", "lef", "lib", "v", "spice")]
    if m.kind == "analog":
        files.append(m.file("specs.toml"))
    for c in m.corners:
        if c.liberty and c.liberty not in files:
            files.append(c.liberty)
    return files


def check_package(path: str | Path, pdk: str | None = None) -> Report:
    """Check a macro package directory against macro/v1.

    ``pdk`` is the placing chip's PDK, ``NAME`` or ``NAME@COMMIT``. Raises
    ``FileNotFoundError`` when ``path`` is not a directory and ``ValueError`` when
    ``pdk`` is malformed.
    """
    expected = parse_pdk_option(pdk) if pdk is not None else None
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")
    report = Report(str(path))
    c = _Checker(root, report)
    m = c.manifest()
    if m is None:
        return report
    c.semantics(m)
    c.dirname(m)
    c.required_files(m)
    lef, lef_error, lef_names = c.read_lef(m)
    c.gds(m, lef)
    c.lef(m, lef, lef_error, lef_names)
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
    return report
