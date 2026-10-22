"""The generated configurations against X-HEEP's own generator (needs a checkout)."""

import pytest

from chipleap.gen.xheep import pad_config, xheep_config
from chipleap.hw import Accel, Design
from chipleap.hw.design import Peripherals
from chipleap.verify import xheep

pytestmark = [
    pytest.mark.xheep,
    pytest.mark.skipif(xheep.find_checkout() is None, reason="no X-HEEP checkout"),
]


@pytest.mark.parametrize(
    "design",
    [
        Design(bus="onetoM", accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)),
        Design(bus="NtoM", banks=2, il_banks=2, accel=Accel("ws", 4, 4, 8, ports=2)),
        Design(bus="onetoM", accel=None),
    ],
    ids=["onetoM", "NtoM-interleaved", "no-accel"],
)
def test_xheep_accepts_the_configuration(tmp_path, design):
    cfg = tmp_path / "cfg.py"
    cfg.write_text(xheep_config(design))
    checkout = xheep.find_checkout()
    assert checkout is not None
    r = xheep.validate(cfg, checkout)
    assert r.ok, r.log


@pytest.mark.parametrize(
    "peripherals",
    [
        Peripherals(uart=False, gpio=0, timers=2, spi_slave=False, dma_channels=1),
        Peripherals(
            gpio=24, spi_hosts=2, i2c=True, i2s=True, pdm2pcm=True, dma_channels=8, dma_ports=4
        ),
        Peripherals(uart=False, gpio=8, spi_hosts=1, spi_slave=False, dma_channels=2, dma_ports=2),
    ],
    ids=["minimal", "everything", "no-plic"],
)
def test_xheep_renders_the_peripherals_a_spec_asks_for(tmp_path, peripherals):
    """X-HEEP validates and renders every peripheral set, with ChipLeap's pad configuration
    where the design has no debug SPI slave; the MCU then has no SPI slave ports."""
    design = Design(
        bus="onetoM",
        peripherals=peripherals,
        accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
    )
    cfg = tmp_path / "cfg.py"
    cfg.write_text(xheep_config(design))
    pads = None
    if (text := pad_config(design)) is not None:
        pads = tmp_path / "pads.py"
        pads.write_text(text)
    checkout = xheep.find_checkout()
    assert checkout is not None
    v = xheep.validate(cfg, checkout, pads)
    assert v.ok, v.log
    r = xheep.render(cfg, checkout, keep=tmp_path / "out", pads=pads)
    assert r.ok, r.log
    mcu = (tmp_path / "out/hw/core-v-mini-mcu/core_v_mini_mcu.sv").read_text()
    assert ("input logic spi_slave_sck_i" in mcu) == peripherals.spi_slave
