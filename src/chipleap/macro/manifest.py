"""macro.toml: what a macro package says about its block, and reading and writing it."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.macro.limits import LIMITS, LimitError, Limits, read_capped, toml_depth
from chipleap.macro.schema import validate_def

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

SCHEMA_VERSION = "macro/v1"
SPECS_SCHEMA = "macro.specs/v1"
EVIDENCE_SCHEMA = "macro.evidence/v1"
OPENADA_SCHEMA = "openada.result/v0alpha1"
MANIFEST = "macro.toml"
SUPPLY_KINDS = ("power", "ground")


class MacroError(ValueError):
    """A macro.toml that cannot be read or does not follow the schema."""


@dataclass(frozen=True)
class Pdk:
    name: str
    commit: str


@dataclass(frozen=True)
class Producer:
    tool: str
    version: str
    source: str = ""


@dataclass(frozen=True)
class Domain:
    name: str
    voltage: float
    power: tuple[str, ...]
    ground: tuple[str, ...]


@dataclass(frozen=True)
class Pin:
    name: str
    direction: str
    kind: str
    domain: str = ""
    width: int | None = None
    layer: str = ""
    description: str = ""

    @property
    def is_supply(self) -> bool:
        return self.kind in SUPPLY_KINDS

    def bits(self) -> list[str]:
        """The pin's bit names: ``q[0]``, ``q[1]`` for a bus, the name for a scalar."""
        if self.width is None:
            return [self.name]
        return [f"{self.name}[{i}]" for i in range(self.width)]


@dataclass(frozen=True)
class Corner:
    name: str
    process: str
    temperature: float
    voltages: dict[str, float] = field(default_factory=dict)
    liberty: str = ""


@dataclass
class Manifest:
    """The parsed macro.toml. Build one and call :meth:`to_toml` to write a package."""

    name: str
    kind: str
    pdk: Pdk
    producer: Producer
    width_um: float
    height_um: float
    domains: list[Domain]
    pins: list[Pin]
    corners: list[Corner] = field(default_factory=list)
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def file(self, ext: str) -> str:
        """The package file name for a view: ``file("lef")`` is ``<name>.lef``."""
        return f"{self.name}.{ext}"

    def pin(self, name: str) -> Pin | None:
        return next((p for p in self.pins if p.name == name), None)

    def pin_bits(self) -> int:
        """How many bits the pins have: a bus's width, one for a scalar."""
        return sum(1 if p.width is None else p.width for p in self.pins)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        """Build from parsed TOML. Raises :class:`MacroError` if it breaks the schema."""
        errors = validate_def("", data)
        if errors:
            raise MacroError("macro.toml does not follow macro/v1: " + "; ".join(errors))
        return cls(
            name=data["name"],
            kind=data["kind"],
            description=data.get("description", ""),
            pdk=Pdk(**data["pdk"]),
            producer=Producer(**data["producer"]),
            width_um=float(data["size"]["width_um"]),
            height_um=float(data["size"]["height_um"]),
            domains=[
                Domain(d["name"], float(d["voltage"]), tuple(d["power"]), tuple(d["ground"]))
                for d in data["domains"]
            ],
            pins=[
                Pin(
                    name=p["name"],
                    direction=p["direction"],
                    kind=p["kind"],
                    domain=p.get("domain", ""),
                    width=int(p["width"]) if "width" in p else None,
                    layer=p.get("layer", ""),
                    description=p.get("description", ""),
                )
                for p in data["pins"]
            ],
            corners=[
                Corner(
                    name=c["name"],
                    process=c["process"],
                    temperature=float(c["temperature"]),
                    voltages={k: float(v) for k, v in c.get("voltages", {}).items()},
                    liberty=c.get("liberty", ""),
                )
                for c in data.get("corners", [])
            ],
            extra=dict(data.get("extra", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"schema": SCHEMA_VERSION, "name": self.name, "kind": self.kind}
        if self.description:
            d["description"] = self.description
        d["pdk"] = {"name": self.pdk.name, "commit": self.pdk.commit}
        d["producer"] = {"tool": self.producer.tool, "version": self.producer.version}
        if self.producer.source:
            d["producer"]["source"] = self.producer.source
        d["size"] = {"width_um": self.width_um, "height_um": self.height_um}
        d["domains"] = [
            {"name": x.name, "voltage": x.voltage, "power": list(x.power), "ground": list(x.ground)}
            for x in self.domains
        ]
        pins = []
        for p in self.pins:
            pd: dict[str, Any] = {"name": p.name, "direction": p.direction, "kind": p.kind}
            if p.domain:
                pd["domain"] = p.domain
            if p.width is not None:
                pd["width"] = p.width
            if p.layer:
                pd["layer"] = p.layer
            if p.description:
                pd["description"] = p.description
            pins.append(pd)
        d["pins"] = pins
        if self.corners:
            corners = []
            for c in self.corners:
                cd: dict[str, Any] = {
                    "name": c.name,
                    "process": c.process,
                    "temperature": c.temperature,
                }
                if c.voltages:
                    cd["voltages"] = dict(c.voltages)
                if c.liberty:
                    cd["liberty"] = c.liberty
                corners.append(cd)
            d["corners"] = corners
        if self.extra:
            d["extra"] = self.extra
        return d

    def to_toml(self) -> str:
        """macro.toml text. Raises :class:`MacroError` if the result would break the schema."""
        d = self.to_dict()
        errors = validate_def("", d)
        if errors:
            raise MacroError("; ".join(errors))
        return dump_toml(d)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        # A JSON string with its non-ASCII characters kept is a valid TOML basic string.
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _toml_key(k: str) -> str:
    bare = k and all(c.isascii() and (c.isalnum() or c in "_-") for c in k)
    return k if bare else json.dumps(k, ensure_ascii=False)


def dump_toml(d: dict[str, Any]) -> str:
    """TOML for a mapping of scalars, tables and arrays of tables, in the mapping's order."""
    scalars = [(k, v) for k, v in d.items() if not isinstance(v, dict) and not _is_table_list(v)]
    out = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars]
    for k, v in d.items():
        if isinstance(v, dict):
            out.append("")
            out.append(f"[{_toml_key(k)}]")
            out += [f"{_toml_key(kk)} = {_toml_value(vv)}" for kk, vv in v.items()]
        elif _is_table_list(v):
            for item in v:
                out.append("")
                out.append(f"[[{_toml_key(k)}]]")
                out += [f"{_toml_key(kk)} = {_toml_value(vv)}" for kk, vv in item.items()]
    return "\n".join(out) + "\n"


def _is_table_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def parse_toml(text: str) -> dict[str, Any]:
    """Parse TOML as aispice's toml crate reads it: an integer must fit in 64 bits and a
    float must not overflow, where tomllib would accept both. Raises
    ``tomllib.TOMLDecodeError`` or ``ValueError``."""
    data = tomllib.loads(text, parse_float=_toml_float)
    stack: list[Any] = [data]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, int) and not isinstance(v, bool) and not -(1 << 63) <= v < (1 << 63):
            raise ValueError("integer does not fit in 64 bits")
    return data


def _toml_float(s: str) -> float:
    x = float(s)
    if math.isinf(x) and s.lstrip("+-") != "inf":
        raise ValueError("floating-point number overflowed")
    return x


def read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file. Raises ``tomllib.TOMLDecodeError``, ``UnicodeDecodeError`` or
    ``ValueError``."""
    return parse_toml(path.read_text(encoding="utf-8"))


def load_manifest(path: str | Path, limits: Limits = LIMITS) -> Manifest:
    """Read macro.toml from a package directory (or the file itself)."""
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST
    try:
        text = read_capped(p, limits.manifest_bytes).decode("utf-8")
        if toml_depth(text) > limits.nesting:
            raise LimitError(f"tables and arrays nest more than {limits.nesting} deep")
        data = parse_toml(text)
    except (OSError, ValueError) as exc:
        raise MacroError(f"cannot read {p}: {exc}") from exc
    return Manifest.from_dict(data)
