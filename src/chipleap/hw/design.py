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


GPIO_PINS = (0, 8, 24, 32)
TIMERS = (2, 4)
DMA_PORTS = (1, 2, 4)
MAX_DMA_CHANNELS = 16


@dataclass(frozen=True)
class Peripherals:
    """X-HEEP's peripherals that a configuration may leave out, and its DMA.

    Every X-HEEP ChipLeap configures has X-HEEP's always-on peripherals (SoC control, boot
    ROM, the flash's SPI host and controller, power manager, timers 0 and 1, fast
    interrupt controller, the external peripheral port, pad control), the DMA and the
    debug module with its JTAG port. The rest is here, as the spec asks for it
    (``chipleap.spec``, its ``peripherals`` section), with these defaults where it does not
    say: what ChipLeap configured before a spec could say, X-HEEP's default without the
    SPI hosts, the I2C and the I2S. The platform interrupt controller (PLIC) is there when
    a peripheral that raises its interrupts through it is (``plic``).
    """

    uart: bool = True
    gpio: int = 32  # pins: 8 on the always-on GPIO, 24 on X-HEEP's other GPIO, 32 on both
    timers: int = 4  # 2: the always-on timer alone; 4: with the second timer block
    spi_hosts: int = 0  # 0, 1 (X-HEEP's SPI host) or 2 (and SPI2)
    i2c: bool = False
    i2s: bool = False
    pdm2pcm: bool = False
    spi_slave: bool = True  # the debug module's SPI slave, which reads and writes memory
    dma_channels: int = 2
    # The DMA's master ports on the bus, each a read, a write and an address port.
    dma_ports: int = 1

    @property
    def plic(self) -> bool:
        """The UART, the GPIO beyond the always-on eight, the I2C, SPI2 and the I2S raise
        their interrupts through the PLIC; the SPI host, the DMA, the timers and the
        always-on GPIO through the fast interrupt controller."""
        return self.uart or self.user_gpio or self.i2c or self.spi_hosts > 1 or self.i2s

    @property
    def ao_gpio(self) -> bool:
        """X-HEEP's always-on GPIO, pins 0 to 7."""
        return self.gpio in (8, 32)

    @property
    def user_gpio(self) -> bool:
        """X-HEEP's other GPIO, pins 8 to 31."""
        return self.gpio in (24, 32)

    def validate(self) -> None:
        if self.gpio not in GPIO_PINS:
            raise DesignError("peripherals.gpio must be 0, 8 (always-on), 24 or 32 pins")
        if self.timers not in TIMERS:
            raise DesignError("peripherals.timers must be 2 (the always-on timer) or 4")
        if self.spi_hosts not in (0, 1, 2):
            raise DesignError("peripherals.spi_hosts must be 0, 1 or 2")
        for name in ("uart", "i2c", "i2s", "pdm2pcm", "spi_slave"):
            if not isinstance(getattr(self, name), bool):
                raise DesignError(f"peripherals.{name} must be true or false")
        if self.dma_ports not in DMA_PORTS:
            raise DesignError("peripherals.dma_ports must be 1, 2 or 4")
        ch = self.dma_channels
        if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= MAX_DMA_CHANNELS:
            raise DesignError(f"peripherals.dma_channels must be 1 to {MAX_DMA_CHANNELS}")
        if ch % self.dma_ports:
            raise DesignError("peripherals.dma_channels must be a multiple of dma_ports")

    @staticmethod
    def from_dict(data: Any) -> Peripherals:
        if data is None:
            return Peripherals()
        if isinstance(data, Peripherals):
            return data
        if not isinstance(data, dict):
            raise DesignError("peripherals must be a mapping")
        bad = set(data) - set(Peripherals.__dataclass_fields__)
        if bad:
            raise DesignError(f"unknown peripherals fields: {', '.join(sorted(bad))}")
        p = Peripherals(**data)
        p.validate()
        return p

    def missing_from(self, have: Peripherals) -> list[str]:
        """What these peripherals, as a spec asks for them, have that ``have`` lacks, in
        words."""
        out = []
        for name, label in (
            ("uart", "a UART"),
            ("i2c", "an I2C"),
            ("i2s", "an I2S"),
            ("pdm2pcm", "a PDM2PCM"),
            ("spi_slave", "the debug SPI slave"),
        ):
            if getattr(self, name) and not getattr(have, name):
                out.append(label)
        for name, label in (
            ("gpio", "GPIO pins"),
            ("timers", "timers"),
            ("spi_hosts", "SPI hosts"),
            ("dma_channels", "DMA channels"),
            ("dma_ports", "DMA master ports"),
        ):
            if getattr(have, name) < getattr(self, name):
                out.append(f"{getattr(self, name)} {label} ({getattr(have, name)} in the design)")
        return out

    def summary(self) -> str:
        """The peripherals beyond the always-on ones, in words."""
        parts = []
        if self.uart:
            parts.append("UART")
        if self.gpio:
            parts.append(f"{self.gpio} GPIO")
        parts.append(f"{self.timers} timers")
        if self.spi_hosts:
            parts.append(f"{self.spi_hosts} SPI host" + ("s" if self.spi_hosts > 1 else ""))
        parts += [
            n for n, on in (("I2C", self.i2c), ("I2S", self.i2s), ("PDM2PCM", self.pdm2pcm)) if on
        ]
        if self.spi_slave:
            parts.append("debug SPI slave")
        ports = f", {self.dma_ports} ports" if self.dma_ports > 1 else ""
        parts.append(f"DMA {self.dma_channels} ch{ports}")
        return ", ".join(parts)


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
    peripherals: Peripherals = field(default_factory=Peripherals)
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
        if not isinstance(self.peripherals, Peripherals):
            raise DesignError("peripherals must be a mapping")
        self.peripherals.validate()
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
        # The default peripherals are left out, so design files written before they could
        # be chosen read the same.
        if self.peripherals == Peripherals():
            d.pop("peripherals")
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
        values["peripherals"] = Peripherals.from_dict(values.get("peripherals"))
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
