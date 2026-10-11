"""The result of one verification step, shared by generated bundles and RTL projects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepResult:
    name: str
    status: str  # pass | fail | skipped
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
