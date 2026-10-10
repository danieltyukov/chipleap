"""External memory interfaces.

Bandwidth is the interface's peak transfer rate times a sustained-efficiency factor for
long sequential bursts, which is how an accelerator streams weights. Energy per bit
covers the memory device and both ends of the interface; it comes from datasheet active
currents for the serial memories and from published LPDDR figures, and is accurate to
perhaps a factor of 1.5. PHY and controller areas are coarse and are reported separately
so they can be read with that in mind.

``phy_max_node_nm`` is the oldest (largest) process node with a published PHY for the
interface: commercial LPDDR4 PHYs reach down to 40 nm (Dolphin, TSMC 40 nm G), LPDDR5 PHYs
to about 22 nm, and no LPDDR PHY exists for any open PDK. Serial memories (QSPI, octal
PSRAM, HyperRAM) need only digital I/O, so they work on any node at whatever rate its pads
manage: about 155 MT/s on IHP SG13G2, which matches the 124 MB/s HyperBus measured on
ETH Zurich's Basilisk chip.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExternalMemory:
    key: str
    name: str
    mts: int  # mega-transfers per second on the interface
    width_bits: int
    efficiency: float  # sustained fraction of peak for long sequential reads
    capacity_mib: int  # largest common single device
    pj_per_bit: float
    phy_mm2_28nm: float  # PHY and controller area at 28 nm
    phy_kge: float  # digital controller logic, scales with the node like any logic
    read_only: bool = False
    pins: int = 0
    min_mts: int = 1  # slowest rate the interface can usefully run at
    phy_max_node_nm: int = 0  # largest node with a known PHY for it; 0 means plain digital I/O
    note: str = ""

    @property
    def peak_gbps(self) -> float:
        """Peak bandwidth in GB/s (10^9 bytes per second)."""
        return self.mts * self.width_bits / 8 / 1000

    @property
    def sustained_bps(self) -> float:
        return self.peak_gbps * 1e9 * self.efficiency

    def sustained_bps_at(self, io_mts: int) -> float:
        """Sustained bandwidth when the I/O runs at most ``io_mts`` (serial memories can
        be clocked down to what the pads support; DRAM PHYs have a floor, ``min_mts``)."""
        return min(self.mts, io_mts) * self.width_bits / 8 * 1e6 * self.efficiency

    def runs_at(self, io_mts: int) -> bool:
        return min(self.mts, io_mts) >= self.min_mts

    def phy_exists(self, node_nm: int) -> bool:
        return self.phy_max_node_nm == 0 or node_nm <= self.phy_max_node_nm

    def available(self, node_nm: int, io_mts: int) -> bool:
        """A PHY exists at this node and the I/O can run the interface fast enough."""
        return self.phy_exists(node_nm) and self.runs_at(io_mts)

    def phy_mm2(self, node_nm: int) -> float:
        # Analog PHY area shrinks much less than logic: assume linear in feature size.
        return self.phy_mm2_28nm * (node_nm / 28)


MEMORIES: dict[str, ExternalMemory] = {
    m.key: m
    for m in (
        ExternalMemory(
            "qspi-flash",
            "Quad SPI NOR flash",
            mts=133,
            width_bits=4,
            efficiency=0.85,
            capacity_mib=128,
            pj_per_bit=60.0,
            phy_mm2_28nm=0.0,
            phy_kge=8.0,
            read_only=True,
            pins=6,
            note="Read only: holds weights, cannot hold a KV cache.",
        ),
        ExternalMemory(
            "octal-psram",
            "Octal DDR PSRAM",
            mts=400,
            width_bits=8,
            efficiency=0.8,
            capacity_mib=64,
            pj_per_bit=25.0,
            phy_mm2_28nm=0.0,
            phy_kge=15.0,
            pins=11,
        ),
        ExternalMemory(
            "hyperram",
            "HyperRAM (HyperBus x8)",
            mts=400,
            width_bits=8,
            efficiency=0.8,
            capacity_mib=64,
            pj_per_bit=25.0,
            phy_mm2_28nm=0.0,
            phy_kge=15.0,
            pins=12,
        ),
        ExternalMemory(
            "lpddr2-x32",
            "LPDDR2-800 x32",
            mts=800,
            width_bits=32,
            efficiency=0.7,
            capacity_mib=1024,
            pj_per_bit=15.0,
            phy_mm2_28nm=0.9,
            phy_kge=60.0,
            pins=60,
            min_mts=400,
            phy_max_node_nm=90,
        ),
        ExternalMemory(
            "lpddr4-x16",
            "LPDDR4-3200 x16",
            mts=3200,
            width_bits=16,
            efficiency=0.7,
            capacity_mib=2048,
            pj_per_bit=6.0,
            phy_mm2_28nm=1.2,
            phy_kge=80.0,
            pins=40,
            min_mts=1600,
            phy_max_node_nm=40,
        ),
        ExternalMemory(
            "lpddr4x-x32",
            "LPDDR4X-4266 x32",
            mts=4266,
            width_bits=32,
            efficiency=0.7,
            capacity_mib=4096,
            pj_per_bit=5.0,
            phy_mm2_28nm=2.0,
            phy_kge=100.0,
            pins=70,
            min_mts=1600,
            phy_max_node_nm=40,
        ),
        ExternalMemory(
            "lpddr5-x16",
            "LPDDR5-6400 x16",
            mts=6400,
            width_bits=16,
            efficiency=0.7,
            capacity_mib=2048,
            pj_per_bit=4.0,
            phy_mm2_28nm=1.4,
            phy_kge=100.0,
            pins=45,
            min_mts=3200,
            phy_max_node_nm=22,
        ),
    )
}

NONE_KEY = "none"


def get_memory(key: str) -> ExternalMemory | None:
    if key == NONE_KEY:
        return None
    try:
        return MEMORIES[key]
    except KeyError:
        raise KeyError(
            f"unknown external memory {key!r}. Known: none, {', '.join(MEMORIES)}"
        ) from None
