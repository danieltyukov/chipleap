"""cocotb tests for the scratchpad: random reads and writes with byte enables, checked
against a dictionary of words."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

CLOCK_NS = 20
WORDS = 256


async def start(dut):
    cocotb.start_soon(Clock(dut.clk_i, CLOCK_NS, unit="ns").start())
    dut.rst_ni.value = 0
    dut.req_i.value = 0
    dut.we_i.value = 0
    dut.addr_i.value = 0
    dut.wdata_i.value = 0
    dut.be_i.value = 0
    await ClockCycles(dut.clk_i, 2)
    dut.rst_ni.value = 1


async def write(dut, addr, data, be=0xF):
    await FallingEdge(dut.clk_i)
    dut.req_i.value, dut.we_i.value = 1, 1
    dut.addr_i.value, dut.wdata_i.value, dut.be_i.value = addr, data, be


async def read(dut, addr):
    await FallingEdge(dut.clk_i)
    dut.req_i.value, dut.we_i.value, dut.addr_i.value = 1, 0, addr
    await FallingEdge(dut.clk_i)
    dut.req_i.value = 0
    assert dut.rvalid_o.value == 1, "no rvalid the cycle after a read"
    return int(dut.rdata_o.value)


def merge(old, data, be):
    mask = sum(0xFF << (8 * b) for b in range(4) if be >> b & 1)
    return (old & ~mask) | (data & mask)


@cocotb.test()
async def random_access(dut):
    await start(dut)
    rng = random.Random(1)
    mem = {}
    # Every word written once, so no read sees a word the macro has not been given.
    for addr in range(WORDS):
        mem[addr] = rng.getrandbits(32)
        await write(dut, addr, mem[addr])
    for _ in range(400):
        addr = rng.randrange(WORDS)
        if rng.random() < 0.5:
            data, be = rng.getrandbits(32), rng.randrange(1, 16)
            mem[addr] = merge(mem[addr], data, be)
            await write(dut, addr, data, be)
        else:
            got = await read(dut, addr)
            assert got == mem[addr], f"word {addr}: read {got:#010x}, expected {mem[addr]:#010x}"


@cocotb.test()
async def read_holds(dut):
    await start(dut)
    await write(dut, 3, 0x12345678)
    assert await read(dut, 3) == 0x12345678
    # Writes do not change the word a read left on rdata_o.
    await write(dut, 4, 0xDEADBEEF)
    await FallingEdge(dut.clk_i)
    dut.req_i.value = 0
    await ClockCycles(dut.clk_i, 3)
    assert int(dut.rdata_o.value) == 0x12345678
