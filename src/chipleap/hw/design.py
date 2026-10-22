"""A design point: a host system plus an optional accelerator.

Every field maps onto something the generator emits or the model assumes. On the
``xheep`` platform the CPU, bus and memory banks go into an X-HEEP configuration and the
accelerator plugs into it. On the ``standalone`` platform the generated hardware is the
accelerator alone, with AXI4 interfaces for any host (Arm, other RISC-V cores, FPGA
processing systems); the CPU, bus, banks and external memory then describe the host
system the model assumes around it. The accelerator fields become RTL parameters either
way.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from chipleap.hw.cpu import CPUS
from chipleap.hw.memory import MEMORIES, NONE_KEY
from chipleap.hw.tech import TECHNOLOGIES

DATAFLOWS = ("gemv", "ws", "os")
DATAFLOW_NAMES = {
    "gemv": "weight-streaming GEMV (lanes x vector)",
    "ws": "weight-stationary systolic array",
    "os": "output-stationary systolic array",
}
BUS_TYPES = ("onetoM", "NtoM")
PLATFORMS = ("xheep", "standalone")
PLATFORM_NAMES = {
    "xheep": "X-HEEP SoC (RISC-V)",
    "standalone": "standalone accelerator IP with AXI4 interfaces, for any host",
}
IL_BANK_COUNTS = (0, 2, 4, 8)
BUFFERS = ("auto", "flops")
MAX_BANKS = 16


class DesignError(ValueError):
    pass


# Names become file names in a bundle and appear in generated RTL, C and YAML, so they are
# restricted to characters that are safe in all of them. Spec names are shorter so that
# derived design names ("<spec>-baseline") still fit.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SPEC_NAME_MAX = 64
DESIGN_NAME_MAX = 96


def is_safe_name(name: str, limit: int = DESIGN_NAME_MAX) -> bool:
    return len(name) <= limit and NAME_RE.fullmatch(name) is not None


def safe_name(text: str, fallback: str = "design") -> str:
    """``text`` with every unsafe character replaced, for names derived from file names."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-.")[:SPEC_NAME_MAX]
    return s if s and is_safe_name(s) else fallback


def _pow2(x: int) -> bool:
    return x > 0 and x & (x - 1) == 0


@dataclass(frozen=True)
class Accel:
    """The generated accelerator: a register interface and 32-bit read and write master
    ports (OBI towards X-HEEP, or AXI4 through the standalone wrapper).

    ``rows`` and ``cols`` mean different things per dataflow:

    * ``gemv``: ``rows`` output lanes, each a ``cols``-wide dot product per cycle.
    * ``ws``: a ``rows`` x ``cols`` systolic array holding weights; rows run along K.
    * ``os``: a ``rows`` x ``cols`` systolic array holding outputs; rows run along M.
    """

    dataflow: str = "gemv"
    rows: int = 8
    cols: int = 8
    w_bits: int = 8  # widest weight the PEs accept; narrower weights are unpacked
    ports: int = 2  # 32-bit OBI read ports
    abuf_kib: int = 8
    wbuf_kib: int = 4
    double_buffer: bool = True
    # What the activation and weight buffers are in a layout: "auto" puts them in the PDK's
    # SRAM macros where the build can take them (chipleap.gen.banks), "flops" keeps them
    # in flip-flops however small the build.
    buffers: str = "auto"

    @property
    def macs(self) -> int:
        return self.rows * self.cols

    def validate(self) -> None:
        if self.dataflow not in DATAFLOWS:
            raise DesignError(f"accel.dataflow must be one of {', '.join(DATAFLOWS)}")
        for name in ("rows", "cols"):
            v = getattr(self, name)
            if not _pow2(v) or v > 256:
                raise DesignError(f"accel.{name} must be a power of two from 1 to 256")
        if self.w_bits not in (2, 4, 8):
            raise DesignError("accel.w_bits must be 2, 4 or 8")
        if not _pow2(self.ports) or self.ports > 16:
            raise DesignError("accel.ports must be 1, 2, 4, 8 or 16")
        for name in ("abuf_kib", "wbuf_kib"):
            v = getattr(self, name)
            if not _pow2(v) or v > 256:
                raise DesignError(f"accel.{name} must be a power of two from 1 to 256")
        if self.buffers not in BUFFERS:
            raise DesignError(f"accel.buffers must be one of {', '.join(BUFFERS)}")


@dataclass(frozen=True)
class Design:
    name: str = "design"
    tech: str = "ihp-sg13g2"
    freq_mhz: float = 100.0
    cpu: str = "cv32e20"
    bus: str = "onetoM"
    banks: int = 2
    bank_kib: int = 32
    il_banks: int = 0
    il_bank_kib: int = 16
    ext_mem: str = NONE_KEY
    ext_channels: int = 1
    accel: Accel | None = None
    platform: str = "xheep"
    notes: tuple[str, ...] = field(default=(), compare=False)

    @property
    def sram_kib(self) -> int:
        return self.banks * self.bank_kib + self.il_banks * self.il_bank_kib

    def validate(self) -> None:
        if not is_safe_name(self.name):
            raise DesignError(
                f"name {self.name!r} must be 1 to {DESIGN_NAME_MAX} letters, digits, '_', '.' or '-', "
                "starting with a letter or digit"
            )
        if self.platform not in PLATFORMS:
            raise DesignError(f"platform must be one of {', '.join(PLATFORMS)}")
        if self.tech not in TECHNOLOGIES:
            raise DesignError(f"tech must be one of {', '.join(TECHNOLOGIES)}")
        if self.cpu not in CPUS:
            raise DesignError(f"cpu must be one of {', '.join(CPUS)}")
        if self.bus not in BUS_TYPES:
            raise DesignError(f"bus must be one of {', '.join(BUS_TYPES)}")
        if not 1 <= self.banks <= MAX_BANKS:
            raise DesignError(f"banks must be between 1 and {MAX_BANKS}")
        if self.il_banks not in IL_BANK_COUNTS:
            raise DesignError("il_banks must be 0, 2, 4 or 8")
        if self.banks + self.il_banks > MAX_BANKS:
            raise DesignError(f"X-HEEP supports at most {MAX_BANKS} memory banks in total")
        if self.il_banks and self.bus != "NtoM":
            raise DesignError("interleaved banks need the NtoM bus")
        for name in ("bank_kib", "il_bank_kib"):
            v = getattr(self, name)
            if not _pow2(v) or not 1 <= v <= 256:
                raise DesignError(f"{name} must be a power of two from 1 to 256 KiB")
        if self.ext_mem != NONE_KEY and self.ext_mem not in MEMORIES:
            raise DesignError(f"ext_mem must be none or one of {', '.join(MEMORIES)}")
        if self.ext_channels not in (1, 2, 4):
            raise DesignError("ext_channels must be 1, 2 or 4")
        if self.freq_mhz <= 0:
            raise DesignError("freq_mhz must be positive")
        if self.accel is not None:
            self.accel.validate()
            if self.accel.ports > 1 and self.bus != "NtoM":
                raise DesignError("an accelerator with several ports needs the NtoM bus")

    def with_(self, **changes: Any) -> Design:
        return replace(self, **changes)

    def with_accel(self, **changes: Any) -> Design:
        base = self.accel or Accel()
        return replace(self, accel=replace(base, **changes))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("notes")
        if self.accel is None:
            d["accel"] = None
        elif self.accel.buffers == "auto":
            d["accel"].pop("buffers")  # as every design before the field
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Design:
        if not isinstance(data, dict):
            raise DesignError("a design must be a mapping")
        known = {f for f in Design.__dataclass_fields__ if f != "notes"}
        unknown = set(data) - known
        if unknown:
            raise DesignError(f"unknown design fields: {', '.join(sorted(unknown))}")
        values = dict(data)
        accel = values.pop("accel", None)
        if accel is not None:
            if not isinstance(accel, dict):
                raise DesignError("accel must be a mapping or null")
            akeys = set(Accel.__dataclass_fields__)
            bad = set(accel) - akeys
            if bad:
                raise DesignError(f"unknown accel fields: {', '.join(sorted(bad))}")
            values["accel"] = Accel(**accel)
        design = Design(**values)
        design.validate()
        return design

    def summary(self) -> str:
        parts = [
            f"{self.cpu} @ {self.freq_mhz:g} MHz",
            f"{self.bus} bus",
            f"{self.banks}x{self.bank_kib} KiB"
            + (f" + {self.il_banks}x{self.il_bank_kib} KiB interleaved" if self.il_banks else ""),
        ]
        if self.accel:
            a = self.accel
            parts.append(
                f"{a.dataflow} {a.rows}x{a.cols} int{a.w_bits}, {a.ports} port"
                + ("s" if a.ports > 1 else "")
                + (", flop buffers" if a.buffers == "flops" else "")
            )
        else:
            parts.append("no accelerator")
        if self.ext_mem != NONE_KEY:
            ch = f"{self.ext_channels}x " if self.ext_channels > 1 else ""
            parts.append(f"{ch}{self.ext_mem}")
        parts.append(self.tech)
        if self.platform == "standalone":
            parts.append("standalone AXI IP")
        return ", ".join(parts)
