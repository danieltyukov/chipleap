"""cocotb tests for the UART, against the reference model in model/uart_model.py."""

import json
import os
import random

import cocotb
import uart_model
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

CLOCK_NS = 10


async def start(dut):
    """Clock, reset, idle line. Returns the bit period in clock cycles."""
    cocotb.start_soon(Clock(dut.clk_i, CLOCK_NS, unit="ns").start())
    dut.rst_ni.value = 0
    dut.tx_valid_i.value = 0
    dut.tx_data_i.value = 0
    dut.rx_i.value = 1
    await ClockCycles(dut.clk_i, 3)
    dut.rst_ni.value = 1
    await ClockCycles(dut.clk_i, 2)
    # A gate-level netlist has no parameters left; ChipLeap passes the project's values.
    params = json.loads(os.environ.get("CHIPLEAP_PARAMETERS", "{}"))
    return int(params["CLKS_PER_BIT"]) if "CLKS_PER_BIT" in params else int(dut.CLKS_PER_BIT.value)


async def send(dut, byte):
    """Hand ``byte`` to the transmitter once it is ready."""
    await FallingEdge(dut.clk_i)
    while not dut.tx_ready_o.value:
        await FallingEdge(dut.clk_i)
    dut.tx_data_i.value = byte
    dut.tx_valid_i.value = 1
    await FallingEdge(dut.clk_i)
    dut.tx_valid_i.value = 0


async def capture(dut, period):
    """The next frame on tx_o, sampled in the middle of each bit."""
    while dut.tx_o.value:
        await RisingEdge(dut.clk_i)
    await ClockCycles(dut.clk_i, period // 2)
    bits = []
    for _ in range(10):
        bits.append(int(dut.tx_o.value))
        await ClockCycles(dut.clk_i, period)
    return bits


async def drive(dut, bits, period):
    """Put ``bits`` on rx_i, one per bit period."""
    for b in bits:
        dut.rx_i.value = b
        await ClockCycles(dut.clk_i, period)
    dut.rx_i.value = 1


async def receive(dut, timeout):
    """The next (byte, error) the receiver reports."""
    for _ in range(timeout):
        await RisingEdge(dut.clk_i)
        if dut.rx_valid_o.value:
            return int(dut.rx_data_o.value), int(dut.rx_error_o.value)
    raise AssertionError("the receiver reported no frame")


@cocotb.test()
async def transmits_frames(dut):
    period = await start(dut)
    for byte in [0x00, 0xFF, 0xA5, *random.sample(range(256), 5)]:
        cap = cocotb.start_soon(capture(dut, period))
        await send(dut, byte)
        bits = await cap
        assert bits == uart_model.frame(byte), f"sent {byte:#04x}, line carried {bits}"


@cocotb.test()
async def receives_frames(dut):
    period = await start(dut)
    for byte in [0x00, 0xFF, 0x3C, *random.sample(range(256), 5)]:
        got = cocotb.start_soon(receive(dut, 12 * period))
        await drive(dut, uart_model.frame(byte), period)
        assert await got == (byte, 0), f"drove {byte:#04x}"


@cocotb.test()
async def flags_a_low_stop_bit(dut):
    period = await start(dut)
    bits = uart_model.frame(0x5A)
    bits[-1] = 0
    got = cocotb.start_soon(receive(dut, 12 * period))
    await drive(dut, bits, period)
    assert await got == (0x5A, 1)


@cocotb.test()
async def ignores_a_glitch(dut):
    period = await start(dut)
    dut.rx_i.value = 0
    await ClockCycles(dut.clk_i, max(1, period // 4))
    dut.rx_i.value = 1
    for _ in range(12 * period):
        await RisingEdge(dut.clk_i)
        assert not dut.rx_valid_o.value, "a glitch was taken for a frame"


@cocotb.test()
async def loops_back_back_to_back(dut):
    period = await start(dut)

    async def wire():
        while True:
            await RisingEdge(dut.clk_i)
            dut.rx_i.value = dut.tx_o.value

    cocotb.start_soon(wire())
    sent = [random.randrange(256) for _ in range(6)]

    async def producer():
        for byte in sent:
            await send(dut, byte)

    cocotb.start_soon(producer())
    got = [await receive(dut, 12 * period) for _ in sent]
    assert got == [(b, 0) for b in sent]
