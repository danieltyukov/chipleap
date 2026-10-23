"""The whole SoC as one block: the top that wires the accelerator into X-HEEP, X-HEEP's
banks in IHP's macros, the clock cells, and the sources the step hands LibreLane. The
layout itself runs with CHIPLEAP_RUN_SOC=1 (hours)."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipleap.gen import banks, soc
from chipleap.hw.design import Accel
from chipleap.hw.tech import get_tech
from chipleap.macro.sram import IHP_1P
from chipleap.model.evaluate import bank_mm2
from chipleap.verify import soc as soc_step

MCU = """
// a comment, with a port in it: input logic fake_i,
module core_v_mini_mcu #(
    parameter EXT_XBAR_NMASTER = 0,
    parameter type obi_req_t = xheep_obi_pkg::xheep_obi_req_t
) (
    input logic rst_ni,
    input logic clk_i,
    input logic uart_rx_i,
    output logic uart_tx_o,
    input logic spi_flash_sck_i,
    input logic spi_sck_i,
    output logic spi_sck_o,
    output logic i2s_sck_oe_o,
    input logic ddr_rcv_clk_i,
    output logic [31:0] exit_value_o,
    input logic [31:0] hart_id_i,
    if_xif.cpu_issue xif_issue_if,
    output reg_req_t pad_req_o,
    input  reg_rsp_t pad_resp_i,
    input  obi_req_t  [EXT_XBAR_NMASTER_RND-1:0] ext_xbar_master_req_i,
    output obi_rsp_t  [EXT_XBAR_NMASTER_RND-1:0] ext_xbar_master_resp_o,
    output obi_req_t  [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0] ext_dma_read_req_o,
    input  obi_rsp_t  [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0] ext_dma_read_resp_i,
    output obi_req_t  [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0] ext_dma_addr_req_o,
    input  obi_rsp_t  [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0] ext_dma_addr_resp_i,
    output obi_req_t ext_core_instr_req_o,
    input  obi_rsp_t ext_core_instr_resp_i,
    output reg_req_t ext_peripheral_slave_req_o,
    input  reg_rsp_t ext_peripheral_slave_resp_i,
    input logic intr_ext_peripheral_i,
    output logic cpu_subsystem_powergate_switch_no,
    input  logic cpu_subsystem_powergate_switch_ack_ni,
    output logic [3:0] dma_done_o
);
endmodule
"""


def test_the_mcu_ports_are_read_from_its_header():
    ports = soc.mcu_ports(MCU)
    by = {p.name: p for p in ports}
    assert "fake_i" not in by
    assert by["exit_value_o"] == soc.Port("exit_value_o", "output", "logic", "[31:0]")
    assert by["xif_issue_if"] == soc.Port("xif_issue_if", "cpu_issue", "if_xif")
    assert by["ext_dma_read_req_o"].kind == "obi_req_t"
    assert by["ext_dma_read_req_o"].dims == "[core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0]"


def test_a_port_the_reader_cannot_read_stops_it():
    with pytest.raises(soc.SocError, match="cannot read"):
        soc.mcu_ports(MCU.replace("input logic uart_rx_i,", "input wire signed x [3],"))


def test_the_top_wires_the_accelerator_and_brings_the_rest_out():
    top = soc.top_sv(soc.mcu_ports(MCU), accel_ports=1)
    head = top[: top.index(");")]
    # Pads, exit value and the external memory's OBI ports, with X-HEEP's types.
    assert "input logic uart_rx_i" in head and "output logic [31:0] exit_value_o" in head
    assert "output xheep_reg_pkg::xheep_reg_req_t pad_req_o" in head
    assert (
        "output xheep_obi_pkg::xheep_obi_req_t [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0]"
        " ext_dma_read_req_o" in head
    )
    # Tied, open, the interface and the accelerator's connections are not ports, nor are
    # the external slave ports nothing uses: the instruction port and the DMA's address port.
    for name in (
        "hart_id_i",
        "dma_done_o",
        "xif_issue_if",
        "ext_xbar_master_req_i",
        "ext_core_instr_req_o",
        "ext_dma_addr_resp_i",
    ):
        assert name not in head
    assert ".ext_core_instr_resp_i('0)" in top and ".ext_core_instr_req_o()" in top
    # The pins of peripherals the configuration leaves out are tied off; the flash's stay.
    assert "input logic spi_flash_sck_i" in head
    for name in ("spi_sck_i", "spi_sck_o", "i2s_sck_oe_o", "ddr_rcv_clk_i"):
        assert name not in head
    assert ".spi_sck_i('0)" in top and ".spi_sck_o()" in top and ".ddr_rcv_clk_i('0)" in top
    assert ".ext_dma_addr_resp_i('0)" in top and ".ext_dma_addr_req_o()" in top
    assert ".EXT_XBAR_NMASTER(2)" in top
    assert ".ext_xbar_master_req_i(acc_obi_req)" in top
    assert ".ext_peripheral_slave_req_o(acc_reg_req)" in top
    assert ".intr_ext_peripheral_i(acc_irq)" in top
    assert ".hart_id_i('0)" in top
    assert ".xif_issue_if(xif)" in top
    # A power switch with nothing in it acknowledges at once.
    assert ".cpu_subsystem_powergate_switch_no(cpu_subsystem_powergate_switch_n)" in top
    assert ".cpu_subsystem_powergate_switch_ack_ni(cpu_subsystem_powergate_switch_n)" in top
    # The write port is the master after the read ports.
    assert "assign acc_obi_req[1].we = 1'b1;" in top
    assert f"{banks.TOP} {soc.ACCEL_INST} (" in top


def test_without_external_memory_every_external_slave_port_is_tied_off():
    top = soc.top_sv(soc.mcu_ports(MCU), accel_ports=1, ext_memory=False)
    head = top[: top.index(");")]
    assert "ext_dma_read" not in head and "ext_core_instr" not in head
    assert ".ext_dma_read_resp_i('0)" in top and ".ext_dma_read_req_o()" in top
    assert "pad_req_o" in head


def test_a_port_the_top_does_not_know_stops_it():
    mcu = MCU.replace("output logic [3:0] dma_done_o", "output fifo_req_t new_thing_o")
    with pytest.raises(soc.SocError, match="new_thing_o"):
        soc.top_sv(soc.mcu_ports(mcu), accel_ports=1)
    mcu = MCU.replace("input logic intr_ext_peripheral_i,", "")
    with pytest.raises(soc.SocError, match="intr_ext_peripheral_i"):
        soc.top_sv(soc.mcu_ports(mcu), accel_ports=1)


MEMORY = """
  sram_wrapper #(
      .NumWords (8192),
      .DataWidth(32'd32)
  ) ram0_i (
      .clk_i(clk_cg[0]));
  // sram_wrapper #(.NumWords (16)) ram9_i (
  sram_wrapper #(
      .NumWords (4096),
      .DataWidth(32'd32)
  ) ram1_i (
      .clk_i(clk_cg[1]));
"""


def test_each_bank_is_read_with_its_instance_and_depth():
    assert soc.bank_words(MEMORY) == [("ram0_i", 8192), ("ram1_i", 4096)]


def test_a_32_kib_bank_is_eight_1024_word_macros_with_a_bit_mask():
    (plan,) = soc.plan_banks([8192], IHP_1P)
    assert plan.bank.macro.cell == "RM_IHPSG13_1P_1024x32_c2_bm_bist"
    assert (plan.bank.across, plan.bank.deep) == (1, 8)
    # The 8192 x 32 macro has no bit mask, and X-HEEP writes bytes.
    assert not any(s.bit_mask for s in IHP_1P if s.cell == "RM_IHPSG13_1P_8192x32_c4")
    assert soc.bank_instances(plan, "u_mcu.memory_subsystem_i.ram0_i")[7] == (
        "u_mcu.memory_subsystem_i.ram0_i.g_8192x32.bank_m7",
        "RM_IHPSG13_1P_1024x32_c2_bm_bist",
    )


def test_a_bank_that_is_not_a_power_of_two_is_refused():
    with pytest.raises(soc.SocError, match="power of two"):
        soc.plan_banks([3000], IHP_1P)
    with pytest.raises(soc.SocError, match="no X-HEEP memory bank"):
        soc.plan_banks([], IHP_1P)


def test_the_bank_writes_bytes_through_the_bit_mask_and_reads_a_cycle_later():
    plans = soc.plan_banks([8192, 4096, 8192], IHP_1P)
    assert [(p.words, p.count) for p in plans] == [(4096, 1), (8192, 2)]
    text = soc.sram_wrapper_sv(plans)
    assert "module sram_wrapper #(" in text
    assert "if (NumWords == 4096 && DataWidth == 32) begin : g_4096x32" in text
    assert "else if (NumWords == 8192 && DataWidth == 32) begin : g_8192x32" in text
    assert "{{8{be_i[3]}}, {8{be_i[2]}}, {8{be_i[1]}}, {8{be_i[0]}}}" in text
    assert "assign pwrgate_ack_no = pwrgate_ni;" in text
    # Eight macros deep, the read's select registered for the output.
    assert text.count("RM_IHPSG13_1P_1024x32_c2_bm_bist bank_m") == 4 + 8
    assert "always @(posedge clk_i) if (bank_re) bank_sel_q <= bank_addr[12:10];" in text
    assert "$error" in text.split("g_unsupported")[1]


def test_every_clock_gate_is_ihps_integrated_gate():
    text = soc.clock_cells_sv()
    for name in ("tc_clk_gating", "cve2_clock_gate"):
        body = text.split(f"module {name}")[1].split("endmodule")[0]
        assert f"{soc.ICG} i_icg" in body
    # A gate its user marks not functional passes the clock through.
    gating = text.split("module tc_clk_gating")[1].split("endmodule")[0]
    assert "if (IS_FUNCTIONAL) begin : g_gate" in gating
    assert "end else begin : g_through\n    assign clk_o = clk_i;" in gating
    for name in soc.CLOCK_MODULES:
        assert f"module {name} " in text
    assert text.count("module ") == len(soc.CLOCK_MODULES)


def test_the_model_prices_a_bank_with_the_macros_that_hold_it():
    ihp = get_tech("ihp-sg13g2")
    eight = 8 * 416.64 * 336.46 / 1e6
    assert bank_mm2(32, ihp) == pytest.approx(eight)
    # Other nodes keep the density formula.
    other = get_tech("generic-65nm")
    assert bank_mm2(32, other) == pytest.approx(other.sram_mm2(32))


def test_templates_are_found_as_x_heeps_makefile_finds_them(tmp_path):
    for rel in (
        "hw/core-v-mini-mcu/core_v_mini_mcu.sv.tpl",
        "hw/vendor/xheep/a.sv.tpl",
        "hw/vendor/lowrisc/b.sv.tpl",
        "util/x.py.tpl",
        "util/profile/p.tpl",
        "test/t.tpl",
        "sw/linker/link.ld.tpl",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")
    assert soc_step.templates(tmp_path) == [
        "hw/core-v-mini-mcu/core_v_mini_mcu.sv.tpl",
        "hw/vendor/xheep/a.sv.tpl",
        "sw/linker/link.ld.tpl",
        "util/profile/p.tpl",
    ]


def test_only_the_designs_cpu_is_read_with_the_packages_of_the_others(tmp_path):
    base = tmp_path / "fusesoc" / "t-verilator"
    base.mkdir(parents=True)
    data = {
        "files": [
            {
                "core": "openhwgroup:cve2:cve2_core:0.1",
                "name": "../src/cve2/cve2_core.sv",
                "file_type": "systemVerilogSource",
            },
            {
                "core": "openhwgroup.org:ip:cv32e40x:0",
                "name": "../src/x/cv32e40x_core.sv",
                "file_type": "systemVerilogSource",
            },
            {
                "core": "openhwgroup.org:ip:cv32e40x:0",
                "name": "../src/x/cv32e40x_pkg.sv",
                "file_type": "systemVerilogSource",
            },
            {
                "core": "openhwgroup.org:ip:cv32e40x:0",
                "name": "../src/x/if_xif.sv",
                "file_type": "systemVerilogSource",
            },
            {
                "core": "x-heep:ip:cv32e40px:0",
                "name": "../src/px/cv32e40px_core.sv",
                "file_type": "systemVerilogSource",
            },
            {
                "core": "pulp:common_cells:1",
                "name": "../src/cc/include/common_cells/r.svh",
                "file_type": "systemVerilogSource",
                "is_include_file": True,
                "include_path": "../src/cc/include",
            },
            {"core": "pulp:common_cells:1", "name": "../src/cc/waiver.vlt", "file_type": "vlt"},
        ]
    }
    files, includes = soc_step.x_heep_files(data, base, tmp_path, "cv32e20")
    assert files == [
        "fusesoc/src/cve2/cve2_core.sv",
        "fusesoc/src/x/cv32e40x_pkg.sv",
        "fusesoc/src/x/if_xif.sv",
    ]
    assert includes == ["fusesoc/src/cc/include"]


def test_a_file_outside_the_steps_directory_is_refused(tmp_path):
    base = tmp_path / "work" / "fusesoc" / "t-verilator"
    base.mkdir(parents=True)
    data = {"files": [{"core": "c", "name": "../../../../x.sv", "file_type": "verilogSource"}]}
    with pytest.raises(soc_step.SocError, match="outside the step's directory"):
        soc_step.x_heep_files(data, base, tmp_path / "work", "cv32e20")


def test_the_copy_of_x_heep_follows_symlinks_only_inside_it(tmp_path):
    co = tmp_path / "x-heep"
    (co / "sw").mkdir(parents=True)
    (co / ".git").mkdir()
    (co / "docs").mkdir()
    (co / "sw" / "regs.h").write_text("#define A 1\n")
    (co / "sw" / "link.h").symlink_to("regs.h")
    soc_step.copy_xheep(co, tmp_path / "copy")
    assert (tmp_path / "copy" / "sw" / "link.h").read_text() == "#define A 1\n"
    assert not (tmp_path / "copy" / "sw" / "link.h").is_symlink()
    assert not (tmp_path / "copy" / ".git").exists() and not (tmp_path / "copy" / "docs").exists()
    (tmp_path / "secret").write_text("x")
    (co / "sw" / "out.h").symlink_to(tmp_path / "secret")
    with pytest.raises(soc_step.SocError, match="leads outside the checkout"):
        soc_step.copy_xheep(co, tmp_path / "copy2")


def _real_tools() -> str:
    from chipleap.chip import io
    from chipleap.verify import xheep
    from chipleap.verify.tools import fusesoc, riscv_gcc

    if not fusesoc().found:
        return "FuseSoC is not installed (scripts/install_fusesoc.sh)"
    if not riscv_gcc().found:
        return "no RISC-V GCC"
    if xheep.find_checkout() is None:
        return "no X-HEEP checkout"
    if io.pdk_dir() is None:
        return "no IHP PDK"
    return ""


@pytest.mark.skipif(bool(_real_tools()), reason=_real_tools() or "tools present")
def test_the_smollm2_soc_is_prepared_for_librelane(tmp_path):
    """X-HEEP rendered, its sources from FuseSoC, ChipLeap's sources and the macros, as
    LibreLane will read them; LibreLane itself is not run."""
    from chipleap.chip import io
    from chipleap.gen.xheep import xheep_config
    from chipleap.hw.design import Design
    from chipleap.verify import xheep

    design = Design(
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
    work = tmp_path / "soc"
    work.mkdir()
    pdk = io.pdk_dir()
    co = xheep.find_checkout()
    assert pdk is not None and co is not None
    cfg, prep = soc_step.prepare(design, xheep_config(design), co, work, pdk)
    assert cfg["DESIGN_NAME"] == soc.TOP and cfg["USE_SLANG"] is True
    assert cfg["CLOCK_PERIOD"] == pytest.approx(11.111)
    assert "--extern-modules" in cfg["SLANG_ARGUMENTS"]
    for f in prep.sources.files:
        assert (work / f).is_file(), f
    # Only cve2's cores of the CPUs, the others' packages kept.
    assert not any("cv32e40x_core.sv" in f for f in prep.sources.files)
    assert any(f.endswith("if_xif.sv") for f in prep.sources.files)
    assert prep.sources.files[-1] == f"src/{soc.TOP}.sv"
    sdc = (work / soc_step.SDC).read_text()
    for clock in ("clk", "jtag_tck", "spi_slave_sck"):
        assert f"-name {clock} " in sdc
    # The I2S clock is clk through the I2S's divider and gate, with no clock of its own.
    assert "i2s_sck" not in sdc
    assert "set_clock_groups -asynchronous" in sdc
    # The I2S's source-synchronous outputs leave within the cycle, as its clock does.
    assert "set_output_delay 0 -clock [get_clocks clk] $i2s_out" in sdc
    cells = [c for _, c in prep.instances]
    assert cells.count("RM_IHPSG13_1P_1024x32_c2_bm_bist") == 8
    assert cells.count("RM_IHPSG13_1P_256x64_c2_bm_bist") == 4
    full, fp = soc_step.with_floorplan(cfg, prep, 3.0e6, work)
    assert set(full["MACROS"]) == set(cells)
    assert {p.instance for p in fp.macros} == {i for i, _ in prep.instances}
    assert (work / "pdn.tcl").is_file()
    # A buffer beside each of the accelerator's four macros drives its clock pin.
    accel = {i for i, _ in prep.instances if i.startswith(f"{soc.ACCEL_INST}.")}
    assert len(accel) == 4
    clock = {e["target"] for e in full["INSERT_ECO_BUFFERS"] if e["target"].endswith("/A_CLK")}
    assert clock == {f"{i}/A_CLK" for i in accel}
    # And one after each of the 32 output bits of the eight bank macros.
    outs = [e for e in full["INSERT_ECO_BUFFERS"] if "/A_DOUT[" in e["target"]]
    assert len(outs) == 256 and {e["buffer"] for e in outs} == {"sg13g2_buf_2"}
    # More standard cells, a larger core.
    _, small = soc_step.with_floorplan(cfg, prep, 1.0e6, work)
    assert small.core_um2 < fp.core_um2
    # CTS skips a clock net with a buffer on it: no port buffer on the I2S clock's net.
    assert cfg["DESIGN_REPAIR_BUFFER_OUTPUT_PORTS"] is False
    # Antenna repair may reroute with a little overflow; the detailed router resolves it.
    assert cfg["GRT_ALLOW_CONGESTION"] is True and cfg["DRT_ANTENNA_REPAIR_ITERS"] > 0
    # The repair after global routing keeps a margin for what routing adds to long wires,
    # and the repairs keep setup slack for what routing takes from the long paths.
    assert cfg["GRT_DESIGN_REPAIR_MAX_CAP_PCT"] == 25
    assert cfg["GRT_RESIZER_HOLD_SLACK_MARGIN"] == pytest.approx(0.2)
    assert (cfg["PL_RESIZER_SETUP_SLACK_MARGIN"], cfg["GRT_RESIZER_SETUP_SLACK_MARGIN"]) == (6, 5)
    assert cfg["CTS_CLK_BUFFERS"][0] == cfg["CTS_ROOT_BUFFER"] == "sg13g2_buf_16"
    # A clock other than the design's, for a signoff the SoC can meet.
    slow, _ = soc_step.prepare(design, xheep_config(design), co, work, pdk, clock_mhz=55.0)
    assert slow["CLOCK_PERIOD"] == pytest.approx(18.182)
    assert "-name clk -period 18.182" in (work / soc_step.SDC).read_text()


@pytest.mark.skipif(bool(_real_tools()), reason=_real_tools() or "tools present")
def test_a_soc_without_optional_peripherals_has_no_pins_for_them(tmp_path):
    """No UART, no GPIO, no SPI slave: X-HEEP rendered with ChipLeap's pad configuration,
    the top without their pins, the constraints without the SPI slave's clock."""
    from chipleap.chip import io
    from chipleap.gen.xheep import xheep_config
    from chipleap.hw.design import Design, Peripherals
    from chipleap.verify import xheep

    design = Design(
        name="bare",
        tech="ihp-sg13g2",
        cpu="cv32e20",
        bus="onetoM",
        banks=1,
        bank_kib=32,
        peripherals=Peripherals(uart=False, gpio=0, timers=2, spi_slave=False, dma_channels=1),
        accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
    )
    work = tmp_path / "soc"
    work.mkdir()
    pdk, co = io.pdk_dir(), xheep.find_checkout()
    assert pdk is not None and co is not None
    soc_step.prepare(design, xheep_config(design), co, work, pdk)
    assert (work / "xheep_cfg" / "bare_pads.py").is_file()
    top = (work / "src" / f"{soc.TOP}.sv").read_text()
    head = top[: top.index(");")]
    for name in ("uart_tx_o", "gpio_0_o", "gpio_20_i", "spi_slave_sck_i", "spi_sck_o", "i2c_"):
        assert name not in head
    assert "spi_flash_sck_o" in head and "jtag_tck_i" in head
    mcu = (work / "x-heep/hw/core-v-mini-mcu/core_v_mini_mcu.sv").read_text()
    assert "input logic spi_slave_sck_i" not in mcu
    assert "-name spi_slave_sck " not in (work / soc_step.SDC).read_text()


def _check_report(run: Path, text: str) -> None:
    rpt = run / "02-yosys-synthesis" / "reports" / "chk.rpt"
    rpt.parent.mkdir(parents=True)
    rpt.write_text(text)


def test_the_synthesized_netlist_must_pass_yosyss_check(tmp_path):
    assert "no check report" in soc_step.netlist_problems(tmp_path)
    _check_report(tmp_path, "Checking module chipleap_soc...\nFound and reported 0 problems.\n")
    assert soc_step.netlist_problems(tmp_path) == ""


def test_a_loop_in_the_synthesized_netlist_is_reported(tmp_path):
    _check_report(
        tmp_path,
        "Warning: found logic loop in module chipleap_soc:\n    cell _1_\n"
        "Found and reported 1 problems.\n",
    )
    assert soc_step.netlist_problems(tmp_path).startswith("1 problems: Warning: found logic loop")


def test_x_heeps_other_clocks_are_asynchronous_to_clk():
    sdc = soc.sdc(
        soc.mcu_ports(MCU.replace("input logic uart_rx_i,", "input logic jtag_tck_i,")), 11.111
    )
    assert "create_clock [get_ports clk_i] -name clk -period 11.111" in sdc
    # Only the clocks whose ports the top has: no SPI slave or I2S in this MCU.
    assert "-name jtag_tck -period 50" in sdc
    assert "spi_slave_sck" not in sdc and "i2s_sck" not in sdc
    assert "foreach c {clk jtag_tck}" in sdc
    assert sdc.rstrip().endswith("set_clock_groups -asynchronous {*}$groups")
    # Clock ports are not data inputs.
    assert "foreach name {clk_i jtag_tck_i}" in sdc


def test_pins_go_west_and_east_within_the_band():
    from chipleap.macro import place

    netlist = (
        "module chipleap_soc(clk_i, rst_ni, pad_resp_i, uart_tx_o, exit_value_o);\n"
        "  input clk_i;\n  input rst_ni;\n  input [33:0] pad_resp_i;\n"
        "  output uart_tx_o;\n  output [31:0] exit_value_o;\nendmodule\n"
    )
    assert soc_step.port_bits(netlist) == (36, 33)
    ms = [place.Macro(f"b{i}", 416.64, 336.46) for i in range(4)]
    fp = place.rows(ms, 1.0e6, util=soc_step.UTIL)
    cfg = soc_step.pin_order(36, 33, fp, {m.instance: m.height for m in ms})
    lines = cfg.splitlines()
    west, east = lines.index("#W"), lines.index("#E")
    assert lines[west + 2] == r".*_n?i(\[[0-9]+\])?" and lines[east + 2] == r".*_n?o(\[[0-9]+\])?"
    # Virtual pins fill the edge beside each macro row, in proportion to its height.
    below = int(lines[west + 1][1:])
    assert 0 < below < 36 and "#N" not in cfg and "#S" not in cfg


def test_each_accelerator_macro_gets_a_clock_buffer_beside_its_clock_pin():
    """IHP's 256 x 64 has A_CLK in the middle of its lower edge: below a macro of the top
    row, above one of the bottom row (mirrored), past the halo; X-HEEP's banks get none."""
    from chipleap.macro import place
    from chipleap.macro.layer import MacroPin, MacroView

    p = Path("x")
    clk = MacroPin("A_CLK", "input", "signal", (("Metal2", (386.4, 0.0, 386.66, 0.26)),))
    ram = MacroView("RM_X", "pdk", 784.48, 118.78, (clk,), p, p, p, p, ("RM_X",), ("RM_X",))
    fp = place.Floorplan(
        (3426.72, 2192.4),
        (20.16, 22.68, 3406.56, 2169.72),
        (
            place.Placed(f"{soc.ACCEL_INST}.abuf_s0_m0", 60.48, 2040.94, "N"),
            place.Placed(f"{soc.ACCEL_INST}.abuf_s1_m0", 60.48, 32.68, "FS"),
            place.Placed(f"{soc.MCU_INST}.memory_subsystem_i.bank_m0", 1669.44, 1823.26, "N"),
        ),
    )
    eco = soc_step.clock_pin_buffers(fp, {m.instance: ram for m in fp.macros})
    off = place.HALO_UM + soc_step.CLOCK_PIN_GAP_UM
    assert eco == [
        {
            "target": f"{soc.ACCEL_INST}.abuf_s0_m0/A_CLK",
            "buffer": "sg13g2_buf_4",
            "placement": [447.01, round(2040.94 - off, 3)],
        },
        {
            "target": f"{soc.ACCEL_INST}.abuf_s1_m0/A_CLK",
            "buffer": "sg13g2_buf_4",
            "placement": [447.01, round(32.68 + 118.78 + off, 3)],
        },
    ]
    # The buffers are inserted right after the clock tree, before the repairs that follow.
    cfg = soc_step.config(soc_step.Sources((), (), (), None), 36.0)  # type: ignore[arg-type]
    assert cfg["meta"]["substituting_steps"] == {"+OpenROAD.CTS": "Odb.InsertECOBuffers"}
    no_pin = MacroView("RM_Y", "pdk", 10.0, 10.0, (), p, p, p, p, ("RM_Y",), ("RM_Y",))
    with pytest.raises(soc_step.SocError, match="RM_Y has no A_CLK pin"):
        soc_step.clock_pin_buffers(fp, {m.instance: no_pin for m in fp.macros})


def test_the_soc_reports_setup_and_hold_at_every_corner():
    from chipleap.verify.run import corner_slacks

    metrics = {
        "timing__setup__ws__corner:nom_slow_1p08V_125C": 0.51,
        "timing__hold__ws__corner:nom_slow_1p08V_125C": 0.2,
        "timing__hold__ws__corner:min_fast_1p32V_m40C": 0.04,
        "timing__setup__ws": 0.51,
    }
    assert corner_slacks(metrics) == {
        "min_fast_1p32V_m40C": {"hold": 0.04},
        "nom_slow_1p08V_125C": {"setup": 0.51, "hold": 0.2},
    }


def test_the_soc_fails_on_ihps_antenna_deck():
    """The soc step signs the SoC off as every layout: a violation of IHP's antenna deck
    fails it, with its rule in the summary, and a clean deck is said so."""
    from dataclasses import replace

    from chipleap.hw.design import Design
    from chipleap.verify import pnr
    from chipleap.verify.drc import DrcResult
    from chipleap.verify.run import soc_step_result

    design = Design(
        tech="ihp-sg13g2",
        freq_mhz=36.0,
        cpu="cv32e20",
        bus="onetoM",
        banks=1,
        bank_kib=32,
        accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
    )
    routed = pnr.PnrResult(
        36.0,
        7.5e6,
        7.3e6,
        3.1e6,
        0.54,
        181280,
        0.4,
        10.0,
        0.05,
        0,
        0,
        0,
        58.6,
        None,
        ihp_antenna=DrcResult(top=soc.TOP),
    )
    s = soc_step.SocResult(routed, (), 2.1e6, 1.5e6)
    ok = soc_step_result(design, s, 1.0)
    assert ok.status == "pass" and "IHP's antenna deck included" in ok.summary
    bad = DrcResult(violations={"Ant.e_Metal5": 1}, top=soc.TOP)
    bad.places["Ant.e_Metal5"] = [(soc.TOP, 1200.0, 900.0)]
    failed = soc_step_result(design, replace(s, pnr=replace(routed, ihp_antenna=bad)), 1.0)
    assert failed.status == "fail" and "Ant.e_Metal5" in failed.summary
    assert failed.data["ihp_antenna"]["violations"] == 1


def _report_path(start: str, end: str, slack: float, group: str = "clk", edge: str = "rise") -> str:
    capture = 27.778 if edge == "rise" else 13.889
    return (
        f"Startpoint: {start} (rising edge-triggered flip-flop clocked by {group})\n"
        f"Endpoint: {end} (rising edge-triggered flip-flop clocked by {group})\n"
        f"Path Group: {group}\nPath Type: max\n\n"
        f"                                  0.000000    0.000000   clock {group} (rise edge)\n"
        f"                                 {capture:.6f}   {capture:.6f}   clock {group} ({edge} edge)\n"
        f"                                            {slack:.6f}   slack (MET)\n\n"
    )


def test_the_socs_clock_terms_are_measured_from_its_routed_paths(monkeypatch):
    """scripts/fit_soc_clock.py: the slowest path from a bank, less that macro's read time,
    and the slowest other path of clk, leaving out the accelerator's own paths, the other
    clocks and the half-cycle paths."""
    import importlib.util
    import sys

    from chipleap.macro.sram import IHP_1P_READ_NS

    path = Path(__file__).resolve().parents[1] / "scripts" / "fit_soc_clock.py"
    spec = importlib.util.spec_from_file_location("fit_soc_clock", path)
    assert spec is not None and spec.loader is not None
    fit = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "fit_soc_clock", fit)  # dataclasses look it up there
    spec.loader.exec_module(fit)
    bank = f"{soc.MCU_INST}.memory_subsystem_i.ram0_i.g_8192x32.bank_m1"
    report = "header\n" + "".join(
        [
            _report_path("_cpu_", "_dma_", -0.219),
            _report_path(f"{soc.ACCEL_INST}._a_", f"{soc.ACCEL_INST}._b_", -2.0),
            _report_path(bank, "_cpu_", 1.526),
            _report_path("_jtag_", "_jtag2_", -5.0, group="jtag_tck"),
            _report_path("_i2s_", "_ws_", -3.0, edge="fall"),
        ]
    )
    found = fit.paths(report)
    assert [p.start for p in found] == ["_cpu_", f"{soc.ACCEL_INST}._a_", bank]
    read, bus = fit.terms(found, {bank: "RM_IHPSG13_1P_1024x32_c2_bm_bist"})
    assert read is not None and bus is not None
    cq = IHP_1P_READ_NS["RM_IHPSG13_1P_1024x32_c2_bm_bist"]
    assert read[0] == pytest.approx(27.778 - 1.526 - cq)
    assert bus.start == "_cpu_" and bus.needed_ns == pytest.approx(27.997)


def test_the_socs_margins_and_a_trial_hold_margin(monkeypatch):
    """The SoC's repairs keep 6 and 5 ns of estimated setup slack and 0.2 ns of hold after
    global routing; CHIPLEAP_SOC_HOLD_MARGIN sets another hold margin, a number of ns."""
    sources = soc_step.Sources((), (), (), None)  # type: ignore[arg-type]
    monkeypatch.delenv(soc_step.HOLD_MARGIN_ENV, raising=False)
    cfg = soc_step.config(sources, 36.0)
    assert cfg["PL_RESIZER_SETUP_SLACK_MARGIN"] == 6.0
    assert cfg["GRT_RESIZER_SETUP_SLACK_MARGIN"] == 5.0
    assert cfg["GRT_RESIZER_HOLD_SLACK_MARGIN"] == 0.2
    monkeypatch.setenv(soc_step.HOLD_MARGIN_ENV, "0.3")
    assert soc_step.config(sources, 36.0)["GRT_RESIZER_HOLD_SLACK_MARGIN"] == 0.3
    for bad in ("x", "nan", "-0.1", "2"):
        monkeypatch.setenv(soc_step.HOLD_MARGIN_ENV, bad)
        with pytest.raises(soc_step.SocError, match="CHIPLEAP_SOC_HOLD_MARGIN must be"):
            soc_step.config(sources, 36.0)


_DEF = """VERSION 5.8 ;
UNITS DISTANCE MICRONS 1000 ;
COMPONENTS 3 ;
    - u_mcu.dma_i_gen\\[0\\].g_gate.i_icg sg13g2_slgcp_1 + PLACED ( 2218080 1202040 ) N ;
    - u_mcu.other sg13g2_slgcp_1x + PLACED ( 1 2 ) N ;
    - u_mcu.core_clock_gate_i.i_icg sg13g2_slgcp_1 + SOURCE TIMING + PLACED ( 2408000 809000 ) FS ;
END COMPONENTS
"""


def test_clock_gates_are_read_where_placement_put_them():
    gates = soc_step.clock_gates(_DEF)
    assert gates == [
        ("u_mcu.dma_i_gen[0].g_gate.i_icg", 2218.08, 1202.04),
        ("u_mcu.core_clock_gate_i.i_icg", 2408.0, 809.0),
    ]
    assert soc_step.gate_buffers(gates)[0] == {
        "target": "u_mcu.dma_i_gen[0].g_gate.i_icg/GCLK",
        "buffer": "sg13g2_buf_16",
        "placement": [2218.08, 1202.04],
    }
    with pytest.raises(soc_step.SocError, match="no database units"):
        soc_step.clock_gates(_DEF.replace("UNITS DISTANCE MICRONS 1000 ;", ""))


def test_the_layout_stops_after_the_clock_tree_to_buffer_the_gates(tmp_path, monkeypatch):
    """The layout runs to the clock tree, reads the gates' places from the DEF it left,
    and goes on from the buffers' step with a buffer after each gate besides the macros'."""
    import json

    from chipleap.verify import pnr

    calls: list[tuple[dict, dict]] = []
    run = tmp_path / "runs" / "chipleap"
    outside = tmp_path / "elsewhere.def"

    def fake(work, cfg, tag="chipleap", **kw):
        calls.append((cfg, kw))
        if kw.get("to") == soc_step.CTS:
            step = run / "31-openroad-cts"
            step.mkdir(parents=True)
            target = outside if cfg.get("_outside") else step / "chipleap_soc.def"
            target.write_text(_DEF)
            (step / "state_out.json").write_text(json.dumps({"def": str(target)}))
            return {}, None
        return {"done": 1}, None

    monkeypatch.setattr(pnr, "run_librelane", fake)
    macro = {"target": "u_accel.abuf_s0_m0/A_CLK", "buffer": "sg13g2_buf_4", "placement": [1, 2]}
    metrics, _ = soc_step.layout(tmp_path, {"INSERT_ECO_BUFFERS": [macro]})
    assert metrics == {"done": 1}
    assert calls[0][1] == {
        "resume_from": soc_step.AFTER_SYNTHESIS,
        "to": soc_step.CTS,
        "timeout_s": soc_step.TIMEOUT_S,
    }
    assert calls[1][1]["resume_from"] == soc_step.ECO_BUFFERS and "to" not in calls[1][1]
    eco = calls[1][0]["INSERT_ECO_BUFFERS"]
    assert eco[0] == macro and [e["target"] for e in eco[1:]] == [
        "u_mcu.dma_i_gen[0].g_gate.i_icg/GCLK",
        "u_mcu.core_clock_gate_i.i_icg/GCLK",
    ]
    # A state that names a DEF outside the run is refused.
    import shutil

    shutil.rmtree(run)
    calls.clear()
    with pytest.raises(soc_step.SocError, match="names no DEF in its run"):
        soc_step.layout(tmp_path, {"INSERT_ECO_BUFFERS": [], "_outside": True})


def test_each_bank_output_gets_a_buffer_beside_its_pin():
    """X-HEEP's bank macros' A_DOUT bits each get a buffer just past the halo beside the
    pin (below a macro of the top row, above one of the bottom row); the accelerator's
    macros get none; a bank macro without A_DOUT pins stops the step."""
    from chipleap.macro import place
    from chipleap.macro.layer import MacroPin, MacroView

    p = Path("x")
    pins = (
        MacroPin("A_DOUT[0]", "output", "signal", (("Metal2", (10.62, 0.0, 10.88, 0.26)),)),
        MacroPin("A_DOUT[31]", "output", "signal", (("Metal2", (405.76, 0.0, 406.02, 0.26)),)),
        MacroPin("A_CLK", "input", "signal", (("Metal2", (200.0, 0.0, 200.26, 0.26)),)),
    )
    bank = MacroView("RM_B", "pdk", 416.64, 336.46, pins, p, p, p, p, ("RM_B",), ("RM_B",))
    top, bottom = f"{soc.MCU_INST}.bank_m6", f"{soc.MCU_INST}.bank_m7"
    fp = place.Floorplan(
        (3426.72, 2192.4),
        (20.16, 22.68, 3406.56, 2169.72),
        (
            place.Placed(top, 2543.04, 1823.26, "N"),
            place.Placed(bottom, 2979.84, 32.68, "FS"),
            place.Placed(f"{soc.ACCEL_INST}.abuf_s0_m0", 60.48, 2040.94, "N"),
        ),
    )
    eco = soc_step.bank_output_buffers(fp, {m.instance: bank for m in fp.macros})
    off = place.HALO_UM + soc_step.CLOCK_PIN_GAP_UM
    assert [e["target"] for e in eco] == [
        f"{top}/A_DOUT[0]",
        f"{top}/A_DOUT[31]",
        f"{bottom}/A_DOUT[0]",
        f"{bottom}/A_DOUT[31]",
    ]
    assert eco[0]["placement"] == [2553.79, round(1823.26 - off, 3)]
    assert eco[3]["placement"] == [3385.73, round(32.68 + 336.46 + off, 3)]
    assert {e["buffer"] for e in eco} == {"sg13g2_buf_2"}
    bare = MacroView("RM_C", "pdk", 10.0, 10.0, pins[2:], p, p, p, p, ("RM_C",), ("RM_C",))
    with pytest.raises(soc_step.SocError, match="have no A_DOUT"):
        soc_step.bank_output_buffers(fp, {m.instance: bare for m in fp.macros})
