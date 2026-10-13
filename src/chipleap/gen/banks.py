"""The accelerator's buffers as IHP's SRAM macros: which builds can have them, the banks
and macros they take, and the top ChipLeap generates for them (``chipleap_accel_sram``):
the core with ``SRAM_BUF`` and every macro beside it, under fixed instance names a
layout can place.

The core reads a bank in the cycle it would read its flop array, and the bank's word a
cycle later takes the place of the operand registers (``chipleap_accel.sv``), so the
schedule ``chipleap.model.accel`` predicts holds cycle for cycle. That needs a word a
cycle from each bank: the gemv core, whose chunk of COLS bytes is one aligned bank word,
with a bank per weight lane, and one DMA port, whose single write a cycle a single-port
macro takes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from chipleap.gen.rtl import rtl_params
from chipleap.hw.design import Accel
from chipleap.macro.sram import Bank, Sram, choose, ports

TOP = "chipleap_accel_sram"


def unsupported(accel: Accel) -> str | None:
    """Why ``accel``'s buffers cannot be SRAM macros, or None when they can."""
    if accel.dataflow != "gemv":
        return f"the {accel.dataflow} dataflow reads its buffers element by element"
    if accel.ports != 1:
        return f"{accel.ports} DMA read ports write more than one word a cycle"
    if accel.cols not in (4, 8, 16):
        return f"a chunk of {accel.cols} bytes is no bank word (4, 8 or 16 bytes)"
    p = rtl_params(accel)
    wpm = accel.cols // 4
    slots = 2 if accel.double_buffer else 1
    if p.wbuf_words // slots // accel.rows < wpm:
        return f"{accel.rows} weight lanes leave less than a word of the weight buffer each"
    return None


@dataclass(frozen=True)
class Banks:
    """The banks of a build: ``nab`` activation banks (one a slot) and ``nwb`` weight
    banks (one a slot and lane), each of ``bank_bits``-bit words."""

    accel: Accel
    abuf: Bank
    wbuf: Bank
    nab: int
    nwb: int
    bank_bits: int
    aba: int  # address bits of an activation bank
    wba: int  # of a weight bank

    def instances(self) -> list[tuple[str, str]]:
        """Every macro as (instance, cell), activation banks first."""
        out = []
        for s in range(self.nab):
            out += [(f"abuf_s{s}_m{k}", self.abuf.macro.cell) for k in range(self.abuf.count)]
        for b in range(self.nwb):
            s, lane = divmod(b, self.accel.rows)
            out += [
                (f"wbuf_s{s}_l{lane}_m{k}", self.wbuf.macro.cell) for k in range(self.wbuf.count)
            ]
        return out

    @property
    def area_um2(self) -> float:
        return self.nab * self.abuf.area_um2 + self.nwb * self.wbuf.area_um2

    def cells(self) -> list[Sram]:
        return sorted({self.abuf.macro, self.wbuf.macro}, key=lambda s: s.cell)


def _bits(words: int) -> int:
    return max(1, (words - 1).bit_length())


def plan(accel: Accel, srams: Iterable[Sram]) -> Banks:
    """The banks and macros of ``accel``'s buffers, from the macros ``srams`` offers."""
    reason = unsupported(accel)
    if reason is not None:
        raise ValueError(f"the buffers cannot be SRAM macros: {reason}")
    srams = list(srams)
    p = rtl_params(accel)
    nab = 2 if accel.double_buffer else 1
    nwb = nab * accel.rows
    wpm = accel.cols // 4
    a_words = p.abuf_words // nab // wpm
    w_words = p.wbuf_words // nwb // wpm
    bits = 32 * wpm
    return Banks(
        accel,
        choose(srams, a_words, bits),
        choose(srams, w_words, bits),
        nab,
        nwb,
        bits,
        _bits(a_words),
        _bits(w_words),
    )


def bank_macros(
    lines: list[str], prefix: str, bank: Bank, aba: int, bits: int, sig: tuple[str, ...]
) -> None:
    """The macros of one bank, ``prefix_m<k>``: ``deep`` stacked on the address's top bits,
    the read's select registered for the output, ``across`` side by side on the word."""
    m = bank.macro
    ma = m.addr_bits
    sel_bits = aba - ma if bank.deep > 1 else 0
    mb = m.bits
    re, we, addr = f"{prefix}_re", f"{prefix}_we", f"{prefix}_addr"
    lines += [
        f"  wire {re} = {sig[0]};",
        f"  wire {we} = {sig[1]};",
        f"  wire [{aba - 1}:0] {addr} = {sig[2]};",
    ]
    if sel_bits:
        lines += [
            f"  reg [{sel_bits - 1}:0] {prefix}_sel_q;",
            f"  always @(posedge clk_i) if ({re}) {prefix}_sel_q <= {addr}[{aba - 1}:{ma}];",
        ]
        maddr = f"{addr}[{ma - 1}:0]"
    elif ma > aba:
        maddr = f"{{{{{ma - aba}{{1'b0}}}}, {addr}}}"
    else:
        maddr = addr
    for d in range(bank.deep):
        hit = f"({addr}[{aba - 1}:{ma}] == {sel_bits}'d{d})" if sel_bits else "1'b1"
        lines.append(f"  wire [{bank.across * mb - 1}:0] {prefix}_q{d};")
        for a in range(bank.across):
            k = d * bank.across + a
            lo, hi = a * mb, min((a + 1) * mb, bits)
            pad = mb - (hi - lo)

            def slice_(name: str, lo: int = lo, hi: int = hi, pad: int = pad) -> str:
                s = f"{name}[{hi - 1}:{lo}]"
                return f"{{{pad}'b0, {s}}}" if pad else s

            conns = {
                "A_CLK": "clk_i",
                "A_MEN": f"({re} | {we}) & {hit}",
                "A_WEN": f"{we} & {hit}",
                "A_REN": f"{re} & {hit}",
                "A_ADDR": maddr,
                "A_DIN": slice_("bank_wdata"),
                "A_DLY": "1'b1",
                "A_DOUT": f"{prefix}_q{d}[{(a + 1) * mb - 1}:{a * mb}]",
                "A_BM": slice_("bank_wmask"),
            }
            body = []
            for name, _, width in ports(m):
                tie = f"{max(width, 1)}'b0"  # the BIST port set
                body.append(f"      .{name}({conns.get(name, tie)})")
            lines += [f"  {m.cell} {prefix}_m{k} (", ",\n".join(body), "  );"]
    if sel_bits:
        lines.append(f"  reg [{bits - 1}:0] {prefix}_rd;")
        lines.append("  always @(*) begin")
        lines.append(f"    {prefix}_rd = {prefix}_q0[{bits - 1}:0];")
        for d in range(1, bank.deep):
            lines.append(
                f"    if ({prefix}_sel_q == {sel_bits}'d{d}) {prefix}_rd = {prefix}_q{d}[{bits - 1}:0];"
            )
        lines += ["  end", f"  assign {sig[3]} = {prefix}_rd;"]
    else:
        lines.append(f"  assign {sig[3]} = {prefix}_q0[{bits - 1}:0];")


def top_sv(banks: Banks) -> str:
    """``chipleap_accel_sram``: the ports of ``chipleap_accel`` for this build, the core
    with ``SRAM_BUF`` and the macros of every bank."""
    p = rtl_params(banks.accel)
    b, nab, nwb, aba, wba = banks.bank_bits, banks.nab, banks.nwb, banks.aba, banks.wba
    lines = [
        "// Written by ChipLeap: the accelerator with its buffers in IHP's SRAM macros.",
        f"module {TOP} (",
        "    input wire clk_i,",
        "    input wire rst_ni,",
        "    input wire reg_valid_i,",
        "    input wire reg_write_i,",
        "    input wire [31:0] reg_addr_i,",
        "    input wire [31:0] reg_wdata_i,",
        "    input wire [3:0] reg_wstrb_i,",
        "    output wire [31:0] reg_rdata_o,",
        "    output wire reg_error_o,",
        "    output wire reg_ready_o,",
        "    output wire [0:0] rd_req_o,",
        "    input wire [0:0] rd_gnt_i,",
        "    output wire [31:0] rd_addr_o,",
        "    input wire [0:0] rd_rvalid_i,",
        "    input wire [31:0] rd_rdata_i,",
        "    output wire wr_req_o,",
        "    input wire wr_gnt_i,",
        "    output wire [31:0] wr_addr_o,",
        "    output wire [31:0] wr_wdata_o,",
        "    output wire [3:0] wr_be_o,",
        "    input wire wr_rvalid_i,",
        "    output wire irq_o",
        ");",
        f"  wire [{nab - 1}:0] abank_re, abank_we;",
        f"  wire [{nab * aba - 1}:0] abank_addr;",
        f"  wire [{nab * b - 1}:0] abank_rdata;",
        f"  wire [{nwb - 1}:0] wbank_re, wbank_we;",
        f"  wire [{nwb * wba - 1}:0] wbank_addr;",
        f"  wire [{nwb * b - 1}:0] wbank_rdata;",
        f"  wire [{b - 1}:0] bank_wdata, bank_wmask;",
        "  chipleap_accel_core #(",
        *(
            f"    .{name}({value}),"
            for name, value in (
                ("DATAFLOW", p.dataflow),
                ("ROWS", p.rows),
                ("COLS", p.cols),
                ("WBITS", p.wbits),
                ("PORTS", p.ports),
                ("ABUF_WORDS", p.abuf_words),
                ("WBUF_WORDS", p.wbuf_words),
                ("DOUBLE_BUF", p.double_buf),
                ("ACC_ROWS", p.acc_rows),
            )
        ),
        "    .SRAM_BUF(1)",
        "  ) u_core (",
        *(
            f"      .{n}({n}),"
            for n in [
                "clk_i",
                "rst_ni",
                "reg_valid_i",
                "reg_write_i",
                "reg_addr_i",
                "reg_wdata_i",
                "reg_wstrb_i",
                "reg_rdata_o",
                "reg_error_o",
                "reg_ready_o",
                "rd_req_o",
                "rd_gnt_i",
                "rd_addr_o",
                "rd_rvalid_i",
                "rd_rdata_i",
                "wr_req_o",
                "wr_gnt_i",
                "wr_addr_o",
                "wr_wdata_o",
                "wr_be_o",
                "wr_rvalid_i",
            ]
        ),
        *(
            f"      .{n}_o({n}),"
            for n in [
                "abank_re",
                "abank_we",
                "abank_addr",
                "wbank_re",
                "wbank_we",
                "wbank_addr",
                "bank_wdata",
                "bank_wmask",
            ]
        ),
        "      .abank_rdata_i(abank_rdata),",
        "      .wbank_rdata_i(wbank_rdata),",
        "      .irq_o(irq_o)",
        "  );",
    ]
    for s in range(nab):
        sig = (
            f"abank_re[{s}]",
            f"abank_we[{s}]",
            f"abank_addr[{(s + 1) * aba - 1}:{s * aba}]",
            f"abank_rdata[{(s + 1) * b - 1}:{s * b}]",
        )
        bank_macros(lines, f"abuf_s{s}", banks.abuf, aba, b, sig)
    for k in range(nwb):
        s, lane = divmod(k, banks.accel.rows)
        sig = (
            f"wbank_re[{k}]",
            f"wbank_we[{k}]",
            f"wbank_addr[{(k + 1) * wba - 1}:{k * wba}]",
            f"wbank_rdata[{(k + 1) * b - 1}:{k * b}]",
        )
        bank_macros(lines, f"wbuf_s{s}_l{lane}", banks.wbuf, wba, b, sig)
    lines.append("endmodule")
    return "\n".join(lines) + "\n"
