"""X-HEEP's peripherals: what a spec asks for, what the generated configuration keeps, what
the SoC's top ties off and what the model charges for each."""

from __future__ import annotations

import ast
import itertools
import re
from dataclasses import asdict, replace

import pytest

from chipleap.explore.engine import baseline_point, explore
from chipleap.explore.score import Point, assess
from chipleap.gen import soc
from chipleap.gen.bundle import write_bundle
from chipleap.gen.xheep import USER, absent_pins, pad_config, user_peripherals, xheep_config
from chipleap.hw import Accel, Design
from chipleap.hw.design import DesignError, Peripherals
from chipleap.hw.tech import get_tech
from chipleap.model import evaluate as ev
from chipleap.spec import SpecError, parse_spec

SMOLLM2 = Design(
    name="smollm2-open-pdk",
    tech="ihp-sg13g2",
    freq_mhz=90.0,
    cpu="cv32e20",
    bus="onetoM",
    banks=1,
    bank_kib=32,
    ext_mem="octal-psram",
    ext_channels=2,
    accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
)
SPEC = {
    "name": "p",
    "workload": {"llm": {"model": "smollm2-135m", "context": 256, "weights": "int4"}},
    "constraints": {"tech": "ihp-sg13g2"},
}
MINIMAL = Peripherals(uart=False, gpio=0, timers=2, spi_slave=False, dma_channels=1)
EVERYTHING = Peripherals(spi_hosts=2, i2c=True, i2s=True, pdm2pcm=True, dma_channels=8, dma_ports=4)


def _added(text: str) -> tuple[list[str], list[str]]:
    """The base and user peripherals a configuration adds, by class."""
    base = re.findall(r"base\.add_peripheral\(\s*(\w+)\(", text)
    user = re.findall(r"user\.add_peripheral\((\w+)\(", text)
    return base, user


def _sets() -> list[Peripherals]:
    """A spread of peripheral sets: every flag on and off, every GPIO and timer count."""
    out = []
    for uart, gpio, timers, hosts, i2c in itertools.product(
        (False, True), (0, 8, 24, 32), (2, 4), (0, 1, 2), (False, True)
    ):
        out.append(
            Peripherals(
                uart=uart,
                gpio=gpio,
                timers=timers,
                spi_hosts=hosts,
                i2c=i2c,
                i2s=hosts == 1,
                pdm2pcm=gpio == 24,
                spi_slave=timers == 4,
            )
        )
    return out


# The default is what ChipLeap configured before a spec could choose (#20's SmolLM2 SoC).


def test_the_default_is_x_heeps_default_without_the_spi_hosts_i2c_and_i2s():
    text = xheep_config(SMOLLM2)
    base, user = _added(text)
    assert user == ["RV_plic", "GPIO", "RV_timer", "UART"]
    assert "GPIO_ao" in base and "DMA" in base
    assert "has_spi_slave=1" in text
    assert "num_channels=2, num_master_ports=1" in text
    assert "num_channels_per_master_port=2" in text
    assert pad_config(SMOLLM2) is None
    # The pins the SoC's top ties off are those it tied before.
    assert set(absent_pins(Peripherals())) == {
        "spi_sck_",
        "spi_cs_",
        "spi_sd_",
        "spi2_",
        "i2c_",
        "i2s_",
        "pdm2pcm_",
        "ddr_",
    }


def test_the_model_keeps_the_smollm2_socs_measured_total():
    """208.4 kGE of X-HEEP's logic and 6.1 kGE of bus, as the trimmed SoC of #20 measured."""
    assert ev.xheep_kge(SMOLLM2) == pytest.approx(208.4, rel=0.005)
    assert ev.bus_kge(SMOLLM2) == pytest.approx(6.1, rel=0.02)


# The configuration keeps exactly what the design has, by one rule.


@pytest.mark.parametrize("p", _sets(), ids=lambda p: p.summary())
def test_the_configuration_keeps_what_the_peripherals_say(p):
    d = replace(SMOLLM2, peripherals=p)
    text = xheep_config(d)
    ast.parse(text)
    base, user = _added(text)
    assert user == user_peripherals(p)
    wanted = {
        "SPI_host": p.spi_hosts >= 1,
        "SPI2": p.spi_hosts >= 2,
        "GPIO": p.gpio in (24, 32),
        "RV_timer": p.timers == 4,
        "UART": p.uart,
        "I2C": p.i2c,
        "I2S": p.i2s,
        "PDM2PCM": p.pdm2pcm,
        # The PLIC is there for the peripherals whose interrupts go through it.
        "RV_plic": p.uart or p.gpio in (24, 32) or p.i2c or p.spi_hosts == 2 or p.i2s,
    }
    assert set(user) == {name for name, on in wanted.items() if on}
    assert ("GPIO_ao" in base) == (p.gpio in (8, 32))
    assert f"has_spi_slave={int(p.spi_slave)}" in text
    assert (pad_config(d) is None) == p.spi_slave
    # Every peripheral left out has its pins tied off in the top, and no other has.
    absent = absent_pins(p)
    for name, _, pins in USER:
        assert all((pre in absent) == (name not in user) for pre in pins), name
    assert ("spi_slave_" in absent) == (not p.spi_slave)


def test_an_imported_class_is_one_the_configuration_uses():
    for p in (MINIMAL, EVERYTHING, Peripherals()):
        text = xheep_config(replace(SMOLLM2, peripherals=p))
        tree = ast.parse(text)
        imported = {
            a.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module in ("peripherals.base_peripherals", "peripherals.user_peripherals")
            for a in node.names
        }
        base, user = _added(text)
        assert imported == set(base) | set(user)


def test_the_dma_gets_its_channels_and_master_ports():
    text = xheep_config(replace(SMOLLM2, peripherals=EVERYTHING))
    assert "num_channels=8, num_master_ports=4" in text
    assert "num_channels_per_master_port=2" in text


def test_the_pad_configuration_drops_only_the_spi_slaves_pins():
    text = pad_config(replace(SMOLLM2, peripherals=MINIMAL))
    assert text is not None
    ast.parse(text)
    assert "configs/pad_cfg.py" in text
    for pin in ("spi_slave_sck", "spi_slave_cs", "spi_slave_miso", "spi_slave_mosi"):
        assert repr(pin) in text


def test_the_top_ties_off_the_pins_of_what_is_left_out():
    mcu = """
module core_v_mini_mcu #(parameter EXT_XBAR_NMASTER = 0) (
    input logic rst_ni,
    input logic clk_i,
    input logic uart_rx_i,
    output logic uart_tx_o,
    input logic spi_sck_i,
    output logic spi_sck_o,
    input logic gpio_3_i,
    output logic gpio_3_o,
    input logic gpio_13_i,
    input logic spi_slave_sck_i,
    input obi_req_t [EXT_XBAR_NMASTER_RND-1:0] ext_xbar_master_req_i,
    output obi_rsp_t [EXT_XBAR_NMASTER_RND-1:0] ext_xbar_master_resp_o,
    output reg_req_t ext_peripheral_slave_req_o,
    input reg_rsp_t ext_peripheral_slave_resp_i,
    input logic intr_ext_peripheral_i
);
endmodule
"""
    ports = soc.mcu_ports(mcu)

    def head(p: Peripherals) -> str:
        top = soc.top_sv(ports, accel_ports=1, absent=absent_pins(p))
        return top[: top.index(");")]

    with_host = head(Peripherals(uart=False, gpio=8, spi_hosts=1))
    assert "spi_sck_o" in with_host and "gpio_3_o" in with_host
    assert "uart_tx_o" not in with_host and "gpio_13_i" not in with_host
    bare = head(MINIMAL)
    for name in ("uart_rx_i", "spi_sck_i", "gpio_3_i", "gpio_13_i", "spi_slave_sck_i"):
        assert name not in bare


# The spec asks; the explorer's designs and the bundle follow.


def test_a_spec_without_the_section_asks_for_the_default():
    s = parse_spec(SPEC)
    assert s.peripherals == Peripherals()
    assert "peripherals" not in s.to_dict()


def test_the_spec_rounds_pins_and_timers_up_to_x_heeps_blocks():
    for asked, got in ((0, 0), (1, 8), (8, 8), (9, 24), (24, 24), (25, 32), (32, 32)):
        assert parse_spec({**SPEC, "peripherals": {"gpio": asked}}).peripherals.gpio == got
    for asked, got in ((0, 2), (2, 2), (3, 4), (4, 4)):
        assert parse_spec({**SPEC, "peripherals": {"timers": asked}}).peripherals.timers == got
    s = parse_spec({**SPEC, "peripherals": {"uart": False, "i2c": True, "dma_channels": 4}})
    assert s.peripherals == Peripherals(uart=False, i2c=True, dma_channels=4)
    assert s.to_dict()["peripherals"] == {"uart": False, "i2c": True, "dma_channels": 4}
    assert parse_spec(s.to_dict()).peripherals == s.peripherals


@pytest.mark.parametrize(
    "section, match",
    [
        ({"usb": True}, "unknown keys usb"),
        ({"uart": "yes"}, "uart must be true or false"),
        ({"gpio": 33}, "gpio must be a whole number from 0 to 32"),
        ({"spi_hosts": 3}, "spi_hosts"),
        ({"dma_channels": 0}, "dma_channels"),
        ({"dma_ports": 3}, "dma_ports must be 1, 2 or 4"),
        ({"dma_channels": 3, "dma_ports": 2}, "multiple of dma_ports"),
        ({"dma_channels": 1, "dma_ports": 2}, "multiple of dma_ports"),
        ("uart", "peripherals must be a mapping"),
    ],
)
def test_the_spec_says_where_a_peripherals_entry_is_wrong(section, match):
    with pytest.raises(SpecError, match=match):
        parse_spec({**SPEC, "peripherals": section})


def test_the_start_design_takes_the_specs_peripherals():
    s = parse_spec({**SPEC, "peripherals": {"uart": False}, "start": {"banks": 1}})
    assert s.start is not None and s.start.peripherals == Peripherals(uart=False)
    with pytest.raises(SpecError, match="peripherals come from the spec"):
        parse_spec({**SPEC, "start": {"peripherals": {"uart": False}}})


def test_every_explored_design_has_the_specs_peripherals():
    asked = Peripherals(uart=False, gpio=8, spi_hosts=1, i2c=True, dma_channels=4)
    spec = parse_spec({**SPEC, "peripherals": {k: v for k, v in asdict(asked).items()}})
    assert baseline_point(spec).design.peripherals == asked
    r = explore(spec, samples=60, seed=5)
    seen = [r.baseline, r.best, *r.pareto, *r.cloud]
    assert all(c.point.design.peripherals == asked for c in seen)


def test_a_design_without_what_the_spec_asks_for_is_infeasible():
    spec = parse_spec({**SPEC, "peripherals": {"i2c": True, "gpio": 20}})
    d = replace(SMOLLM2, peripherals=Peripherals(gpio=8))
    e = ev.evaluate(spec.workload(), d)
    a = assess(spec, Point(d, "int4"), e)
    assert any("an I2C" in v for v in a.violations)
    assert any("24 GPIO pins (8 in the design)" in v for v in a.violations)
    ok = replace(d, peripherals=spec.peripherals)
    assert not any("leaves out" in v for v in assess(spec, Point(ok, "int4"), e).violations)


def test_the_bundle_carries_the_peripherals_and_the_pad_configuration(tmp_path):
    d = replace(SMOLLM2, peripherals=MINIMAL)
    files = {f.relative_to(tmp_path).as_posix() for f in write_bundle(tmp_path, d)}
    assert f"xheep/{d.name}_pads.py" in files
    assert "PADS_CFG=" in (tmp_path / "README.md").read_text()
    from chipleap.gen.bundle import load_design

    loaded, _ = load_design(tmp_path / "design.yaml")
    assert loaded.peripherals == MINIMAL
    files = {
        f.relative_to(tmp_path / "b").as_posix() for f in write_bundle(tmp_path / "b", SMOLLM2)
    }
    assert not any(f.endswith("_pads.py") for f in files)


# Designs read and check their peripherals.


def test_a_design_without_peripherals_gets_the_default_and_round_trips():
    data = SMOLLM2.to_dict()
    assert "peripherals" not in data  # the default is left out
    assert Design.from_dict(data).peripherals == Peripherals()
    data["peripherals"] = {"uart": False}
    assert Design.from_dict(data).peripherals == Peripherals(uart=False)
    d = replace(SMOLLM2, peripherals=EVERYTHING)
    assert Design.from_dict(d.to_dict()) == d
    with pytest.raises(DesignError, match="unknown peripherals fields: usb"):
        Design.from_dict({**data, "peripherals": {"usb": 1}})
    with pytest.raises(DesignError, match="gpio"):
        Design.from_dict({**data, "peripherals": {"gpio": 16}})


# The model charges each block once, and only when the design has it.


def test_each_block_costs_what_the_model_says_and_the_parts_add_up():
    ihp = get_tech("ihp-sg13g2")
    for p in (MINIMAL, Peripherals(), EVERYTHING, *_sets()[::7]):
        d = replace(SMOLLM2, peripherals=p)
        parts = ev.soc_logic_mm2(d, ihp)
        area = ev.area_breakdown(d, ihp, ev.get_cpu(d.cpu), None)
        assert sum(parts.values()) == pytest.approx(area["soc logic"])
        assert ("UART" in parts) == p.uart
        assert ("PLIC" in parts) == p.plic
        assert ("debug SPI slave" in parts) == p.spi_slave
        assert ("SPI2" in parts) == (p.spi_hosts == 2)


def test_leaving_a_peripheral_out_saves_its_block_and_its_share_of_the_bus():
    base = ev.xheep_kge(SMOLLM2)
    no_uart = ev.xheep_kge(replace(SMOLLM2, peripherals=Peripherals(uart=False)))
    _, per_peripheral = ev.XHEEP_PERIPH_BUS_KGE
    assert base - no_uart == pytest.approx(ev.XHEEP_BLOCK_KGE["uart"] + per_peripheral)
    host = ev.xheep_kge(replace(SMOLLM2, peripherals=Peripherals(spi_hosts=1)))
    assert host - base == pytest.approx(ev.XHEEP_BLOCK_KGE["spi_host"] + per_peripheral)
    assert (
        ev.xheep_kge(replace(SMOLLM2, peripherals=MINIMAL))
        < base
        < ev.xheep_kge(replace(SMOLLM2, peripherals=EVERYTHING))
    )


def test_dma_ports_are_bus_masters():
    one = ev.bus_masters(SMOLLM2)
    four = ev.bus_masters(replace(SMOLLM2, peripherals=Peripherals(dma_channels=4, dma_ports=4)))
    assert four - one == 9  # a read, a write and an address port each
    assert ev.bus_kge(replace(SMOLLM2, peripherals=Peripherals(dma_ports=2))) > ev.bus_kge(SMOLLM2)


def test_the_evaluation_reports_the_soc_logic_block_by_block():
    from chipleap.report.data import evaluation_dict
    from chipleap.workload.llm import LLMWorkloadSpec, build_llm

    w = build_llm(LLMWorkloadSpec("smollm2-135m", context=256, weights="int4"))
    e = ev.evaluate(w, SMOLLM2)
    assert sum(e.soc_logic_mm2.values()) == pytest.approx(e.area_mm2["soc logic"])
    data = evaluation_dict(e, ops=False)
    assert set(data["soc_logic_mm2"]) == set(e.soc_logic_mm2)
