"""The macro/v1 validator: every rule of docs/macro-package.md, in the documented order.

aispice runs the same rules in Rust (``aispice_core::macropkg::check``), with the same
rule ids and messages; tests/fixtures/macro/expected.json pins both.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from chipleap.macro.gds import (
    GdsCycleError,
    GdsError,
    GdsLibrary,
    parse_gds,
    structure_identities,
)
from chipleap.macro.limits import (
    LIMITS,
    FileTooLarge,
    LimitError,
    Limits,
    json_depth,
    toml_depth,
    too_large,
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
class PdkLibs:
    """The cells of a PDK's libraries (the LEF macros, CDL subcircuits and GDS cells of
    every library under ``libs.ref``, read by ``chipleap.macro.layer.pdk_libs``): every
    name, compared in ASCII lower case, and the identity of each cell of the GDS a chip
    merges the standard cells from, by its exact name
    (``chipleap.macro.gds.structure_identities``)."""

    names: frozenset[str] = frozenset()
    std_cells: Mapping[str, str] = field(default_factory=dict)


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


# Reading the package: only what is in it. A symbolic link could lead to a sibling
# directory (another package's evidence, say) or anywhere on the machine, and a FIFO
# would block the read, so neither is followed or opened.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _kind(root: Path, rel: str) -> str:
    """What the package holds at ``rel``, its last part not followed: "file", "dir",
    "link", "other" or "" for nothing."""
    try:
        mode = os.lstat(root / rel).st_mode
    except (OSError, ValueError):
        return ""
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "file"
    return "dir" if stat.S_ISDIR(mode) else "other"


def _open(root: Path, rel: str) -> BinaryIO:
    """Package file ``rel`` (``name`` or ``evidence/name``) opened for reading, without
    following a symbolic link in any part of it, and only if it is a regular file."""
    *dirs, name = rel.split("/")
    fd = os.open(root, os.O_RDONLY | _DIRECTORY)
    try:
        for d in dirs:
            try:
                nfd = os.open(d, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise OSError(exc.errno, f"{d} is a symbolic link") from None
                raise
            os.close(fd)
            fd = nfd
        try:
            ffd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OSError(exc.errno, "it is a symbolic link") from None
            raise
    finally:
        os.close(fd)
    f = os.fdopen(ffd, "rb")
    if not stat.S_ISREG(os.fstat(ffd).st_mode):
        f.close()
        raise OSError(errno.EINVAL, "it is not a regular file")
    return f


def _read(root: Path, rel: str, limit: int) -> bytes:
    """A package file's bytes. Raises :class:`FileTooLarge` past ``limit`` and
    ``OSError`` when it cannot be read."""
    with _open(root, rel) as f:
        size = os.fstat(f.fileno()).st_size
        if size > limit:
            raise FileTooLarge(too_large(size, limit))
        data = f.read(limit + 1)
    if len(data) > limit:  # it grew after the stat
        raise FileTooLarge(too_large(len(data), limit))
    return data


def _sha256(root: Path, rel: str, limit: int) -> str:
    """A package file's SHA-256, read in chunks, within ``limit``."""
    h = hashlib.sha256()
    with _open(root, rel) as f:
        size = os.fstat(f.fileno()).st_size
        if size > limit:
            raise FileTooLarge(too_large(size, limit))
        read = 0
        while chunk := f.read(1 << 20):
            read += len(chunk)
            if read > limit:
                raise FileTooLarge(too_large(read, limit))  # it grew after the stat
            h.update(chunk)
    return h.hexdigest()


class _Checker:
    def __init__(
        self, root: Path, report: Report, limits: Limits, pdk_libs: PdkLibs | None = None
    ) -> None:
        self.root = root
        self.report = report
        self.limits = limits
        self.pdk_libs = pdk_libs  # the PDK's libraries, names in lower case, if given
        # The cells a chip would merge, seen while the views are read: (file, kind, name,
        # and for a GDS cell named exactly like a standard cell of the PDK's, its verdict
        # and the cell it places that is not the PDK's own, from std_cells).
        self.cells: list[tuple[str, str, str, str, str]] = []
        self.too_large: set[str] = set()  # files reported as too large, once each
        self.unreadable: set[str] = set()  # files reported as unreadable, once each
        self.digests: dict[str, str | None] = {}  # package files hashed for evidence
        self.hash_limit_reported = False

    def add(self, rule: str, file: str, message: str, severity: str = "error") -> None:
        if len(self.report.findings) < self.limits.findings:
            self.report.findings.append(Finding(rule, severity, file, message))
        elif severity == "error":
            self.report.unlisted_errors += 1
        else:
            self.report.unlisted_warnings += 1

    def present(self, rel: str) -> bool:
        """Whether the package holds ``rel`` as anything but a directory: a symbolic
        link is there, and reading it is refused."""
        return _kind(self.root, rel) not in ("", "dir")

    def read(self, rel: str, limit: int) -> bytes | None:
        """A package file's bytes, or None after reporting why it cannot be read."""
        try:
            return _read(self.root, rel, limit)
        except FileTooLarge as exc:
            self.report_too_large(rel, str(exc))
        except OSError as exc:
            self.report_unreadable(rel, _unreadable(exc))
        return None

    def report_unreadable(self, rel: str, message: str) -> None:
        if rel not in self.unreadable:
            self.unreadable.add(rel)
            self.add("file.unreadable", rel, message)

    def report_too_large(self, rel: str, message: str) -> None:
        if rel not in self.too_large:
            self.too_large.add(rel)
            self.add("file.too_large", rel, message)

    # macro.toml

    def manifest(self) -> Manifest | None:
        if not self.present(MANIFEST):
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

    def std_cells(self, m: Manifest, lib: GdsLibrary, data: bytes) -> dict[str, tuple[str, str]]:
        """For each GDS cell named exactly like a standard cell of the PDK's libraries:
        ("own", "") when it is the PDK's own, the two identical and every cell it places
        the PDK's own too; ("differs", "") when it is not identical; ("places", child)
        when a cell it places is not the PDK's own. The top cell is never one."""
        std = self.pdk_libs.std_cells if self.pdk_libs is not None else {}
        if not any(c in std for c in lib.cells if c != m.name):
            return {}
        ids: dict[str, str] = {}
        for raw, identity in structure_identities(data):
            name = raw.rstrip(b"\0").decode("latin-1")
            ids[name] = "" if name in ids else identity
        out: dict[str, tuple[str, str]] = {}

        def verdict(c: str) -> tuple[str, str]:
            if c not in out:
                if c == m.name or c not in std:
                    out[c] = ("not", "")
                elif not ids.get(c) or ids[c] != std[c]:
                    out[c] = ("differs", "")
                else:
                    child = next((r for r in lib.children(c) if verdict(r)[0] != "own"), None)
                    out[c] = ("own", "") if child is None else ("places", child)
            return out[c]

        return {c: verdict(c) for c in lib.cells if c != m.name and c in std}

    def required_files(self, m: Manifest) -> None:
        for f in required_files(m):
            if not self.present(f):
                self.add("file.missing", f, "required file is missing")

    # GDS

    def gds(self, m: Manifest, lef: LefMacro | None) -> None:
        f = m.file("gds")
        if not self.present(f):
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
        verdicts = self.std_cells(m, lib, data)
        self.cells += [
            (f, "GDS cell", c, *verdicts.get(c, ("", ""))) for c in lib.cells if c != m.name
        ]
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
        if not self.present(f):
            return None, None, []
        try:
            macros = parse_lef(_text(_read(self.root, f, self.limits.view_bytes)), self.limits)
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
        if not self.present(f):
            return
        if problem is not None:
            rule, message = problem
            if rule == "file.too_large":
                self.report_too_large(f, message)
            elif rule == "file.unreadable":
                self.report_unreadable(f, message)
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
        if not self.present(f):
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
        if not self.present(f):
            return
        data = self.read(f, self.limits.view_bytes)
        if data is None:
            return
        try:
            ports = verilog_ports(_text(data), m.name, self.limits)
        except LimitError as exc:
            self.add("verilog.limit", f, str(exc))
            return
        except ViewError as exc:
            self.add("verilog.duplicate", f, str(exc))
            return
        if ports is None:
            self.add("verilog.module", f, f"no module {quote(m.name)}")
            return
        # A port declared twice (on both sides of an ifdef, say): a tool reads one of
        # the declarations, and which one depends on its defines.
        count: dict[str, int] = {}
        for port in ports:
            count[port.name] = count.get(port.name, 0) + max(port.declared, 1)
        twice = sorted(n for n, k in count.items() if k > 1)
        for name in twice:
            self.add("verilog.duplicate", f, f"port {quote(name)} is declared more than once")
        if twice:
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
        if not self.present(f):
            return
        data = self.read(f, self.limits.view_bytes)
        if data is None:
            return
        try:
            sub = spice_subckt(_text(data), m.name, self.limits)
            self.cells += [
                (f, "subcircuit", c, "", "")
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
        if not self.present(f):
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

    def summaries(self) -> list[str] | None:
        """Names of the summaries in evidence/, sorted; None when there are too many.
        Names that are not UTF-8 are skipped, as aispice cannot name them either. Any
        ``*.json`` but a directory is a summary: one that is a symbolic link is listed,
        and reported when it is read. ``evidence`` itself must be a directory, not a
        link to one."""
        kind = _kind(self.root, "evidence")
        if kind in ("link", "other"):
            what = "a symbolic link" if kind == "link" else "not a directory"
            self.report_unreadable("evidence", f"cannot read the folder: it is {what}")
        if kind != "dir":
            return []
        found: list[str] = []
        with os.scandir(self.root / "evidence") as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or entry.is_dir(follow_symlinks=False):
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
        if base in self.too_large or base in self.unreadable:
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
            digest = _sha256(self.root, base, self.limits.gds_bytes)
        except FileTooLarge as exc:
            self.report_too_large(base, str(exc))
        except OSError as exc:
            self.report_unreadable(base, _unreadable(exc))
        self.digests[base] = digest
        return digest

    def evidence(self, m: Manifest) -> None:
        files = self.summaries()
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
                if base in ("", ".", "..") or not self.present(base):
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
    libraries, not one of their cells. The one exception is a GDS cell that is the PDK's
    own standard cell (``_Checker.std_cells``), which only the libraries can show."""
    prefixes = PDK_CELL_PREFIXES.get(m.pdk.name, ())
    libs = c.pdk_libs
    pdk = quote(m.pdk.name)
    for f, what, name, verdict, placed in [(MANIFEST, "name", m.name, "", ""), *c.cells]:
        low = ascii_lower(name)
        hit = next((p for p in prefixes if low.startswith(p)), None)
        if verdict == "own" or (hit is None and (libs is None or low not in libs.names)):
            continue
        if verdict == "differs":
            message = f"{what} {quote(name)} differs from the cell of that name in PDK {pdk}"
        elif verdict == "places":
            message = f"{what} {quote(name)} places {quote(placed)}, which is not the PDK's own"
        elif hit is None:
            message = f"{what} {quote(name)} is a cell of PDK {pdk}"
        elif what == "GDS cell" and libs is None:
            message = (
                f"{what} {quote(name)} begins with {quote(hit)}, as the cells of PDK {pdk} "
                "do, and without the PDK's libraries cannot be shown to be the PDK's own"
            )
        else:
            message = f"{what} {quote(name)} begins with {quote(hit)}, as the cells of PDK {pdk} do"
        c.add("pdk.cell_name", f, message)


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
    pdk_libs: PdkLibs | None = None,
) -> Report:
    """Check a macro package directory against macro/v1.

    ``pdk`` is the placing chip's PDK, ``NAME`` or ``NAME@COMMIT``. ``pdk_libs`` are the
    cells of the PDK's libraries, when the caller has them
    (``chipleap.macro.layer.pdk_libs``). Raises ``FileNotFoundError`` when ``path`` is
    not a directory and ``ValueError`` when ``pdk`` is malformed. ``limits`` exist for
    tests; the spec fixes their values.
    """
    expected = parse_pdk_option(pdk) if pdk is not None else None
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")
    report = Report(str(path))
    if pdk_libs is not None:
        pdk_libs = PdkLibs(frozenset(ascii_lower(n) for n in pdk_libs.names), pdk_libs.std_cells)
    c = _Checker(root, report, limits, pdk_libs)
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
