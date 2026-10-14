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
    output logic [31:0] exit_value_o,
    input logic [31:0] hart_id_i,
    if_xif.cpu_issue xif_issue_if,
    output reg_req_t pad_req_o,
    input  reg_rsp_t pad_resp_i,
    input  obi_req_t  [EXT_XBAR_NMASTER_RND-1:0] ext_xbar_master_req_i,
    output obi_rsp_t  [EXT_XBAR_NMASTER_RND-1:0] ext_xbar_master_resp_o,
    output obi_req_t  [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0] ext_dma_read_req_o,
    input  obi_rsp_t  [core_v_mini_mcu_pkg::DMA_NUM_MASTER_PORTS-1:0] ext_dma_read_resp_i,
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
    # Tied, open, the interface and the accelerator's connections are not ports.
    for name in ("hart_id_i", "dma_done_o", "xif_issue_if", "ext_xbar_master_req_i"):
        assert name not in head
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
    # More standard cells, a larger core.
    _, small = soc_step.with_floorplan(cfg, prep, 1.0e6, work)
    assert small.core_um2 < fp.core_um2
    # CTS keeps the gated trees' levels alike instead of chaining delay buffers.
    assert cfg["CTS_DELAY_BUFFER_DERATE_PCT"] == 0 and cfg["CTS_BALANCE_LEVELS"] is True
    # CTS skips a clock net with a buffer on it: no port buffer on the I2S clock's net.
    assert cfg["DESIGN_REPAIR_BUFFER_OUTPUT_PORTS"] is False
    # A clock other than the design's, for a signoff the SoC can meet.
    slow, _ = soc_step.prepare(design, xheep_config(design), co, work, pdk, clock_mhz=55.0)
    assert slow["CLOCK_PERIOD"] == pytest.approx(18.182)
    assert "-name clk -period 18.182" in (work / soc_step.SDC).read_text()


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
