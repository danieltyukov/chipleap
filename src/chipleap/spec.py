"""Requirement specs: the workload, targets, constraints and objective, read from YAML.

A spec says *what* is needed. Design points say *how*. ``load_spec`` validates the file
and reports every problem with the path of the offending key, so a typo in a spec fails
with a message that says where it is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from chipleap.hw.cpu import CPUS
from chipleap.hw.design import (
    DMA_PORTS,
    MAX_DMA_CHANNELS,
    PLATFORMS,
    SPEC_NAME_MAX,
    Design,
    DesignError,
    Peripherals,
    is_safe_name,
    safe_name,
)
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
    # What gets generated: an X-HEEP SoC, or the accelerator alone as AXI4 IP for any host.
    # The explorer never changes it.
    platform: str = "xheep"
    # X-HEEP's peripherals the chip needs besides the workload (the ``peripherals``
    # section), which every design the explorer makes has.
    peripherals: Peripherals = field(default_factory=Peripherals)
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
        if self.platform != "xheep":
            out["platform"] = self.platform
        per = asdict(self.peripherals)
        changed = {k: v for k, v in per.items() if v != asdict(Peripherals())[k]}
        if changed:
            out["peripherals"] = changed
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


# The spec's peripherals section: what each key takes. Pins and timers round up to what
# X-HEEP's blocks give: GPIO in blocks of 8 (always-on) and 24 pins, timers in pairs.
_FLAGS = ("uart", "i2c", "i2s", "pdm2pcm", "spi_slave")
_COUNTS = {
    "gpio": (0, 32),
    "timers": (0, 4),
    "spi_hosts": (0, 2),
    "dma_channels": (1, MAX_DMA_CHANNELS),
    "dma_ports": (1, max(DMA_PORTS)),
}


def parse_peripherals(sec: Any, where: str = "peripherals") -> Peripherals:
    """X-HEEP's peripherals a spec asks for; what it does not name keeps its default
    (``chipleap.hw.design.Peripherals``)."""
    if sec is None:
        return Peripherals()
    if not isinstance(sec, dict):
        raise SpecError(f"{where} must be a mapping")
    bad = set(sec) - set(_FLAGS) - set(_COUNTS)
    if bad:
        raise SpecError(f"{where}: unknown keys {', '.join(sorted(bad))}")
    values: dict[str, Any] = {}
    for key in _FLAGS:
        if key in sec:
            if not isinstance(sec[key], bool):
                raise SpecError(f"{where}.{key} must be true or false")
            values[key] = sec[key]
    for key, (lo, hi) in _COUNTS.items():
        if key in sec:
            v = sec[key]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise SpecError(f"{where}.{key} must be a whole number from {lo} to {hi}")
            values[key] = v
    if "gpio" in values:
        values["gpio"] = next(n for n in (0, 8, 24, 32) if n >= values["gpio"])
    if "timers" in values:
        values["timers"] = 2 if values["timers"] <= 2 else 4
    if values.get("dma_ports", 1) not in DMA_PORTS:
        raise SpecError(f"{where}.dma_ports must be 1, 2 or 4")
    try:
        p = Peripherals(**values)
        p.validate()
    except DesignError as exc:
        raise SpecError(f"{where}: {exc}") from exc
    return p


def _name(given: Any, fallback: str) -> str:
    """The spec name, which becomes the design's name and a file name in its bundle."""
    if given is None:
        return safe_name(fallback, "spec")
    if not is_safe_name(str(given), SPEC_NAME_MAX):
        raise SpecError(
            f"name {given!r} must be 1 to {SPEC_NAME_MAX} letters, digits, '_', '.' or '-', "
            "starting with a letter or digit"
        )
    return str(given)


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
        "platform",
        "peripherals",
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

    platform = str(data.get("platform", "xheep"))
    if platform not in PLATFORMS:
        raise SpecError(f"platform must be one of {', '.join(PLATFORMS)}")

    peripherals = parse_peripherals(data.get("peripherals"))

    start = None
    if data.get("start") is not None:
        if not isinstance(data["start"], dict):
            raise SpecError("start must be a mapping")
        if "peripherals" in data["start"]:
            raise SpecError("start: peripherals come from the spec's peripherals section")
        try:
            base = Design().to_dict()
            base["platform"] = platform
            base.update(data["start"])
            base["peripherals"] = asdict(peripherals)
            start = Design.from_dict(base)
        except (DesignError, TypeError) as exc:
            raise SpecError(f"start: {exc}") from exc
        if start.platform != platform:
            raise SpecError(f"start: platform {start.platform} differs from the spec's {platform}")

    spec = Spec(
        name=_name(data.get("name"), name),
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
        platform=platform,
        peripherals=peripherals,
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
