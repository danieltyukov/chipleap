"""The accelerator's buffers as SRAM macros: which builds can have them, their banks, and
the generated top. The simulation of it is in test_rtl.py."""

from __future__ import annotations

import pytest

from chipleap.gen import banks
from chipleap.hw.design import Accel
from chipleap.macro.sram import Sram

SMOLLM2 = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)


def sram(words: int, bits: int, height: float) -> Sram:
    return Sram(f"RM_IHPSG13_1P_{words}x{bits}_c2_bm_bist", words, bits, 784.48, height, True)


PDK = [sram(64, 64, 64.36), sram(256, 64, 118.78), sram(512, 64, 191.34), sram(256, 32, 118.78)]


@pytest.mark.parametrize(
    ("accel", "reason"),
    [
        (Accel("ws", 4, 4, 8, ports=1), "reads its buffers element by element"),
        (Accel("os", 4, 4, 8, ports=1), "reads its buffers element by element"),
        (Accel("gemv", 4, 8, 8, ports=2), "2 DMA read ports"),
        (Accel("gemv", 4, 2, 8, ports=1), "2 bytes is no bank word"),
        (Accel("gemv", 4, 32, 8, ports=1), "32 bytes is no bank word"),
        (Accel("gemv", 256, 16, 8, ports=1, wbuf_kib=1), "less than a word"),
    ],
)
def test_only_builds_that_keep_their_schedule_take_macros(accel, reason):
    assert reason in (banks.unsupported(accel) or "")
    with pytest.raises(ValueError, match="cannot be SRAM macros"):
        banks.plan(accel, PDK)


def test_the_smollm2_accelerator_takes_four_256_word_macros():
    assert banks.unsupported(SMOLLM2) is None
    plan = banks.plan(SMOLLM2, PDK)
    # 1 KiB a slot: 128 words of 64 bits, one 256-word macro each.
    assert (plan.nab, plan.nwb, plan.bank_bits, plan.aba, plan.wba) == (2, 2, 64, 7, 7)
    assert plan.abuf.macro.cell == plan.wbuf.macro.cell == "RM_IHPSG13_1P_256x64_c2_bm_bist"
    assert plan.instances() == [
        ("abuf_s0_m0", "RM_IHPSG13_1P_256x64_c2_bm_bist"),
        ("abuf_s1_m0", "RM_IHPSG13_1P_256x64_c2_bm_bist"),
        ("wbuf_s0_l0_m0", "RM_IHPSG13_1P_256x64_c2_bm_bist"),
        ("wbuf_s1_l0_m0", "RM_IHPSG13_1P_256x64_c2_bm_bist"),
    ]
    assert plan.area_um2 == pytest.approx(4 * 784.48 * 118.78)
    assert [s.cell for s in plan.cells()] == ["RM_IHPSG13_1P_256x64_c2_bm_bist"]


def test_weight_banks_are_one_a_slot_and_lane():
    plan = banks.plan(Accel("gemv", 4, 4, 8, ports=1, abuf_kib=2, wbuf_kib=2), PDK)
    names = [inst for inst, _ in plan.instances()]
    assert names[:2] == ["abuf_s0_m0", "abuf_s1_m0"]
    assert names[2:] == [f"wbuf_s{s}_l{lane}_m0" for s in (0, 1) for lane in range(4)]
    # 2 KiB of weights, two slots, four lanes: 64 words of 32 bits a lane.
    assert (plan.wbuf.words, plan.wbuf.bits, plan.wba) == (64, 32, 6)


def test_the_top_ties_the_macros_to_the_core():
    plan = banks.plan(SMOLLM2, PDK)
    text = banks.top_sv(plan)
    assert text.startswith("// Written by ChipLeap")
    assert f"module {banks.TOP} #(\n    parameter EXT_DIRECT = 0," in text
    assert "    .SRAM_BUF(1)" in text and "    .DOUBLE_BUF(1)," in text
    assert "  RM_IHPSG13_1P_256x64_c2_bm_bist wbuf_s1_l0_m0 (" in text
    # The 7-bit bank address on the macro's 8 bits; A_DLY high; the BIST ports tied off.
    assert "      .A_ADDR({{1{1'b0}}, wbuf_s1_l0_addr})," in text
    assert "      .A_DLY(1'b1)," in text and "      .A_BIST_ADDR(8'b0)," in text
    assert "      .A_REN(abuf_s0_re & 1'b1)," in text


def test_a_deep_bank_stacks_macros_and_registers_the_select():
    plan = banks.plan(
        Accel("gemv", 1, 4, 8, ports=1, abuf_kib=4, wbuf_kib=1, double_buffer=False),
        [sram(256, 32, 118.78)],
    )
    assert (plan.abuf.deep, plan.abuf.across, plan.aba) == (4, 1, 10)
    text = banks.top_sv(plan)
    assert "abuf_s0_m3 (" in text
    assert "always @(posedge clk_i) if (abuf_s0_re) abuf_s0_sel_q <= abuf_s0_addr[9:8];" in text
    assert "      .A_WEN(abuf_s0_we & (abuf_s0_addr[9:8] == 2'd3))," in text


def test_a_wide_bank_puts_macros_side_by_side():
    plan = banks.plan(Accel("gemv", 2, 16, 8, ports=1), [sram(512, 64, 191.34)])
    assert (plan.abuf.across, plan.bank_bits) == (2, 128)
    text = banks.top_sv(plan)
    assert "      .A_DIN(bank_wdata[127:64])," in text
    assert "      .A_DOUT(abuf_s0_q0[127:64])," in text
