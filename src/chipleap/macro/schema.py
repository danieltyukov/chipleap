"""The macro/v1 JSON schema and a small validator for the part of JSON Schema it uses.

A full JSON Schema library would word its errors its own way. aispice validates the
same documents in Rust, and both tools must report the same messages, so both carry
this interpreter for the keywords the schema uses, applied in one fixed order:
``$ref``, ``type`` (stop on failure), ``const``, ``enum``, ``minLength``,
``maxLength``, ``pattern``, ``minimum``, ``exclusiveMinimum``, ``required``,
``properties`` (by sorted key), ``additionalProperties`` (by sorted key),
``minProperties``, ``minItems``, ``items``, then ``if``/``then``/``else``. Other
keywords are annotations. A test checks the interpreter against the jsonschema
package on the fixtures.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Any

from chipleap.macro.text import describe, fmt_g, join, plural, quote

SCHEMA_FILE = "macro-v1.schema.json"

_TYPE_NAMES = {
    "object": "a table",
    "array": "an array",
    "string": "a string",
    "number": "a number",
    "integer": "an integer",
    "boolean": "true or false",
}


def schema_text() -> str:
    return resources.files("chipleap.macro").joinpath(SCHEMA_FILE).read_text(encoding="utf-8")


@cache
def schema() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(schema_text())
    return data


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _type_ok(kind: str, v: Any) -> bool:
    if kind == "object":
        return isinstance(v, dict)
    if kind == "array":
        return isinstance(v, list)
    if kind == "string":
        return isinstance(v, str)
    if kind == "boolean":
        return isinstance(v, bool)
    if kind == "number":
        return _is_number(v)
    if kind == "integer":
        return _is_number(v) and float(v).is_integer()
    return False


def _matches(pattern: str, s: str) -> bool:
    m = re.search(pattern, s)
    # Python's $ also matches before a final newline; Rust's regex does not. Every
    # pattern in the schema is anchored at the end, so require the match to reach it.
    return m is not None and (not pattern.endswith("$") or m.end() == len(s))


def _resolve(ref: str) -> Any:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise ValueError(f"unsupported $ref {ref}")
    return schema()["$defs"][ref[len(prefix) :]]


def _label(path: str) -> str:
    return path or "the document"


def validate(node: Any, value: Any, path: str = "") -> list[str]:
    """Problems with ``value`` against schema ``node``, as messages, in a fixed order."""
    errs: list[str] = []
    if node is True:
        return errs
    if node is False:
        return [f"{_label(path)} is not allowed here"]
    if "$ref" in node:
        errs += validate(_resolve(node["$ref"]), value, path)
    kind = node.get("type")
    if kind is not None and not _type_ok(kind, value):
        errs.append(f"{_label(path)} must be {_TYPE_NAMES[kind]}")
        return errs
    if "const" in node and value != node["const"]:
        errs.append(f"{_label(path)} must be {describe(node['const'])}")
    if "enum" in node and value not in node["enum"]:
        allowed = ", ".join(str(x) for x in node["enum"])
        errs.append(f"{_label(path)} is {describe(value)}, expected one of {allowed}")
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            n = node["minLength"]
            errs.append(f"{_label(path)} must have at least {plural(n, 'character')}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            n = node["maxLength"]
            errs.append(f"{_label(path)} must have at most {plural(n, 'character')}")
        if "pattern" in node and not _matches(node["pattern"], value):
            errs.append(f"{_label(path)} is {quote(value)}, which does not match {node['pattern']}")
    if _is_number(value):
        if "minimum" in node and value < node["minimum"]:
            errs.append(f"{_label(path)} must be at least {fmt_g(node['minimum'])}")
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            errs.append(f"{_label(path)} must be greater than {fmt_g(node['exclusiveMinimum'])}")
    if isinstance(value, dict):
        for key in node.get("required", []):
            if key not in value:
                errs.append(f"{join(path, key)} is missing")
        props = node.get("properties", {})
        for key in sorted(props):
            if key in value:
                errs += validate(props[key], value[key], join(path, key))
        extra = node.get("additionalProperties", True)
        for key in sorted(value):
            if key in props:
                continue
            if extra is False:
                errs.append(f"{join(path, key)} is not a known field")
            elif extra is not True:
                errs += validate(extra, value[key], join(path, key))
        if "minProperties" in node and len(value) < node["minProperties"]:
            n = node["minProperties"]
            errs.append(f"{_label(path)} must have at least {plural(n, 'entry', 'entries')}")
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errs.append(f"{_label(path)} must have at least {plural(node['minItems'], 'item')}")
        if "items" in node:
            for i, item in enumerate(value):
                errs += validate(node["items"], item, f"{path}[{i}]")
    if "if" in node:
        branch = "then" if not validate(node["if"], value, path) else "else"
        if branch in node:
            errs += validate(node[branch], value, path)
    return errs


def validate_def(name: str, value: Any) -> list[str]:
    """Validate against ``#/$defs/<name>``; the root schema when ``name`` is empty."""
    return validate(schema()["$defs"][name] if name else schema(), value)
