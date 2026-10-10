"""Requirement specs: the workload, targets, constraints and objective, read from YAML.

A spec says *what* is needed. Design points say *how*. ``load_spec`` validates the file
and reports every problem with the path of the offending key, so a typo in a spec fails
with a message that says where it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from chipleap.hw.cpu import CPUS
from chipleap.hw.design import Design, DesignError
from chipleap.hw.memory import MEMORIES, NONE_KEY
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.model.evaluate import DEFAULT_FIRMWARE_KIB
from chipleap.workload.graph import GraphError, build_graph
from chipleap.workload.llm import WEIGHT_FORMATS, LLMWorkloadSpec, build_llm, get_arch
from chipleap.workload.ops import Workload

OBJECTIVES = ("energy", "area", "throughput", "edp")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    name: str
    workload_cfg: dict[str, Any]
    throughput: float = 0.0  # minimum units per second
    latency_ms: float = 0.0  # maximum time per step, 0 for none
    area_mm2: float = 0.0  # maximum, 0 for none
    power_mw: float = 0.0
    energy_mj: float = 0.0  # per unit
    techs: tuple[str, ...] = tuple(TECHNOLOGIES)
    memories: tuple[str, ...] = (NONE_KEY, *MEMORIES)
    cpus: tuple[str, ...] = tuple(CPUS)
    max_freq_mhz: float = 0.0
    weight_formats: tuple[str, ...] = ()  # precisions the explorer may choose between
    firmware_kib: float = DEFAULT_FIRMWARE_KIB
    objective: str = "energy"
    start: Design | None = None
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_llm(self) -> bool:
        return "llm" in self.workload_cfg

    def default_weight_format(self) -> str:
        if self.is_llm:
            return str(self.workload_cfg["llm"].get("weights", "int4"))
        return str(self.workload_cfg["graph"].get("weights", "int8"))

    def allowed_weight_formats(self) -> tuple[str, ...]:
        return self.weight_formats or (self.default_weight_format(),)

    def workload(self, weights: str | None = None) -> Workload:
        """Build the workload, optionally with a different weight precision."""
        try:
            if self.is_llm:
                cfg = dict(self.workload_cfg["llm"])
                if weights:
                    cfg["weights"] = weights
                fields = set(LLMWorkloadSpec.__dataclass_fields__)
                bad = set(cfg) - fields
                if bad:
                    raise SpecError(f"workload.llm: unknown keys {', '.join(sorted(bad))}")
                get_arch(str(cfg.get("model", "")))
                return build_llm(LLMWorkloadSpec(**cfg))
            graph = dict(self.workload_cfg["graph"])
            if weights:
                graph["weights"] = weights
            return build_graph(graph)
        except (KeyError, ValueError, TypeError) as exc:
            if isinstance(exc, SpecError):
                raise
            msg = exc.args[0] if exc.args else str(exc)
            raise SpecError(f"workload: {msg}") from exc

    def with_(self, **changes: Any) -> Spec:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "workload": self.workload_cfg}
        targets = {
            k: v for k, v in (("throughput", self.throughput), ("latency_ms", self.latency_ms)) if v
        }
        if targets:
            out["targets"] = targets
        cons: dict[str, Any] = {
            k: v
            for k, v in (
                ("area_mm2", self.area_mm2),
                ("power_mw", self.power_mw),
                ("energy_mj", self.energy_mj),
                ("max_freq_mhz", self.max_freq_mhz),
            )
            if v
        }
        if self.techs != tuple(TECHNOLOGIES):
            cons["tech"] = list(self.techs)
        if self.memories != (NONE_KEY, *MEMORIES):
            cons["ext_mem"] = list(self.memories)
        if self.cpus != tuple(CPUS):
            cons["cpu"] = list(self.cpus)
        if cons:
            out["constraints"] = cons
        if self.weight_formats:
            out["allow"] = {"weights": list(self.weight_formats)}
        out["objective"] = self.objective
        return out


def _as_list(value: Any, where: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise SpecError(f"{where} must be a name or a non-empty list of names")
    out = []
    for v in value:
        if v not in allowed:
            raise SpecError(f"{where}: {v!r} is not one of {', '.join(allowed)}")
        out.append(str(v))
    return tuple(out)


def _number(section: dict[str, Any], key: str, where: str) -> float:
    if key not in section:
        return 0.0
    v = section[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise SpecError(f"{where}.{key} must be a positive number")
    return float(v)


def parse_spec(data: Any, *, name: str = "spec") -> Spec:
    if not isinstance(data, dict):
        raise SpecError("a spec must be a YAML mapping")
    known = {
        "name",
        "description",
        "workload",
        "targets",
        "constraints",
        "allow",
        "objective",
        "start",
    }
    unknown = set(data) - known
    if unknown:
        raise SpecError(f"unknown top-level keys: {', '.join(sorted(unknown))}")

    wl = data.get("workload")
    if not isinstance(wl, dict) or len(wl) != 1 or next(iter(wl)) not in ("llm", "graph"):
        raise SpecError("workload must have exactly one of: llm, graph")
    if not isinstance(next(iter(wl.values())), dict):
        raise SpecError("workload.llm or workload.graph must be a mapping")

    targets = data.get("targets") or {}
    cons = data.get("constraints") or {}
    allow = data.get("allow") or {}
    for sec, label, keys in (
        (targets, "targets", {"throughput", "latency_ms"}),
        (
            cons,
            "constraints",
            {
                "area_mm2",
                "power_mw",
                "energy_mj",
                "tech",
                "ext_mem",
                "cpu",
                "max_freq_mhz",
                "firmware_kib",
            },
        ),
        (allow, "allow", {"weights"}),
    ):
        if not isinstance(sec, dict):
            raise SpecError(f"{label} must be a mapping")
        bad = set(sec) - keys
        if bad:
            raise SpecError(f"{label}: unknown keys {', '.join(sorted(bad))}")

    objective = str(data.get("objective", "energy"))
    if objective not in OBJECTIVES:
        raise SpecError(f"objective must be one of {', '.join(OBJECTIVES)}")

    start = None
    if data.get("start") is not None:
        try:
            base = Design().to_dict()
            base.update(data["start"])
            start = Design.from_dict(base)
        except (DesignError, TypeError) as exc:
            raise SpecError(f"start: {exc}") from exc

    spec = Spec(
        name=str(data.get("name", name)),
        description=str(data.get("description", "")),
        workload_cfg=wl,
        throughput=_number(targets, "throughput", "targets"),
        latency_ms=_number(targets, "latency_ms", "targets"),
        area_mm2=_number(cons, "area_mm2", "constraints"),
        power_mw=_number(cons, "power_mw", "constraints"),
        energy_mj=_number(cons, "energy_mj", "constraints"),
        techs=_as_list(cons["tech"], "constraints.tech", tuple(TECHNOLOGIES))
        if "tech" in cons
        else tuple(TECHNOLOGIES),
        memories=_as_list(cons["ext_mem"], "constraints.ext_mem", (NONE_KEY, *MEMORIES))
        if "ext_mem" in cons
        else (NONE_KEY, *MEMORIES),
        cpus=_as_list(cons["cpu"], "constraints.cpu", tuple(CPUS))
        if "cpu" in cons
        else tuple(CPUS),
        max_freq_mhz=_number(cons, "max_freq_mhz", "constraints"),
        weight_formats=_as_list(allow["weights"], "allow.weights", WEIGHT_FORMATS)
        if "weights" in allow
        else (),
        firmware_kib=_number(cons, "firmware_kib", "constraints") or DEFAULT_FIRMWARE_KIB,
        objective=objective,
        start=start,
    )
    spec.workload()  # surface workload errors now, with their location
    return spec


def load_spec(path: str | Path) -> Spec:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise SpecError(f"{p}: not valid YAML: {exc}") from exc
    except OSError as exc:
        raise SpecError(f"{p}: {exc.strerror}") from exc
    try:
        return parse_spec(data, name=p.stem)
    except (SpecError, GraphError) as exc:
        raise SpecError(f"{p}: {exc}") from exc
