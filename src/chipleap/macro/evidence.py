"""Writing evidence summaries (macro.evidence/v1) into a package's evidence/ folder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePath
from typing import Any

from chipleap.macro.limits import FileTooLarge, too_large
from chipleap.macro.manifest import EVIDENCE_SCHEMA, MacroError, Pdk
from chipleap.macro.schema import validate_def
from chipleap.rtl import builddir

STATUSES = ("pass", "fail", "unknown", "not_applicable")


def sha256_file(path: str | Path, limit: int | None = None) -> str:
    """The file's SHA-256, read in chunks. With ``limit``, raises
    :class:`~chipleap.macro.limits.FileTooLarge` for a larger file before reading it."""
    p = Path(path)
    size = p.stat().st_size
    if limit is not None and size > limit:
        raise FileTooLarge(too_large(size, limit))
    h = hashlib.sha256()
    read = 0
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            read += len(chunk)
            if limit is not None and read > limit:
                raise FileTooLarge(too_large(read, limit))  # it grew after the stat
            h.update(chunk)
    return h.hexdigest()


def _file_ref(path: Path, package: Path, sha256: str) -> dict[str, str]:
    try:
        rel = path.resolve().relative_to(package.resolve()).as_posix()
    except ValueError:
        # Outside the package (a rule deck, say): the name is enough to say what it was.
        rel = path.name
    return {"path": rel, "sha256": sha256}


def make_evidence(
    package: str | Path,
    operation: str,
    status: str,
    summary: str,
    tool: tuple[str, str],
    pdk: Pdk,
    inputs: list[str | Path] | None = None,
    artifacts: list[str | Path] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An evidence summary for a check that ran on files of ``package``.

    ``tool`` is (name, version). Inputs and artifacts are hashed now, so validate the
    package after any later change to them: the validator reports stale evidence.
    """
    root = Path(package)
    doc: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "operation": operation,
        "tool": {"name": tool[0], "version": tool[1]},
        "pdk": {"name": pdk.name, "commit": pdk.commit},
        "engineering": {"status": status, "summary": summary},
        "inputs": [_file_ref(Path(p), root, sha256_file(Path(p))) for p in inputs or []],
    }
    if artifacts:
        doc["artifacts"] = [_file_ref(Path(p), root, sha256_file(Path(p))) for p in artifacts]
    if data:
        doc["data"] = data
    errors = validate_def("evidence", doc)
    if errors:
        raise MacroError("; ".join(errors))
    return doc


def write_evidence(package: str | Path, doc: dict[str, Any], name: str | None = None) -> Path:
    """Write a summary to ``evidence/<name or operation>.json`` and return its path. A
    package may come from someone else, so neither ``evidence/`` nor the summary is
    written through a symlink (``chipleap.rtl.builddir``)."""
    rel = PurePath("evidence", f"{name or doc['operation']}.json")
    return builddir.write_text(Path(package), rel, json.dumps(doc, indent=2) + "\n")
