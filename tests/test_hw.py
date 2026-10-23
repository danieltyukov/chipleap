import pytest

from chipleap.hw import MEMORIES, TECHNOLOGIES, Accel, Design, DesignError, get_tech
from chipleap.hw.tech import accel_logic_kge

# IHP SG13G2 SRAM macros: (KiB, area mm2, read pJ per 64-bit access) from the PDK's
# liberty files, typical corner.
IHP_MACROS = [
    (0.5, 0.050489, 39.05),
    (2, 0.093181, 49.63),
    (4, 0.150102, 68.90),
    (8, 0.263946, 91.83),
    (16, 0.491634, 122.39),
]


@pytest.mark.parametrize(("kib", "area", "_"), IHP_MACROS)
def test_ihp_sram_area_fit(kib, area, _):
    assert get_tech("ihp-sg13g2").sram_mm2(kib) == pytest.approx(area, rel=0.02)


@pytest.mark.parametrize(("kib", "_", "read_pj"), IHP_MACROS[2:])
def test_ihp_sram_energy_fit(kib, _, read_pj):
    per_byte = get_tech("ihp-sg13g2").sram_energy_per_byte(kib)
    assert per_byte == pytest.approx(read_pj / 8, rel=0.1)


@pytest.mark.parametrize(("kib", "read_pj_32bit"), [(1, 26.01), (2, 36.01), (32, 230.35)])
def test_ihp_bank_energy_fit(kib, read_pj_32bit):
    # 256x32, 512x32 and 8192x32 macros: per-access read energy over 4 bytes.
    per_byte = get_tech("ihp-sg13g2").bank_energy_per_byte(kib)
    assert per_byte == pytest.approx(read_pj_32bit / 4, rel=0.12)


def test_ihp_32kib_macro():
    # IHP's 8192x32 macro is 1520 um x 618 um.
    assert get_tech("ihp-sg13g2").sram_mm2(32) == pytest.approx(1.520 * 0.618, rel=0.08)


def test_energy_falls_with_node():
    order = sorted(TECHNOLOGIES.values(), key=lambda t: -t.node_nm)
    macs = [t.mac_pj_8x8 for t in order]
    assert macs == sorted(macs, reverse=True)


def test_accel_area_grows_with_the_array():
    for df in ("gemv", "ws", "os"):
        small = accel_logic_kge(df, 2, 8, 1)
        assert accel_logic_kge(df, 4, 8, 1) > small
        assert accel_logic_kge(df, 2, 8, 2) > small
    # Synthesized on SG13G2 for the 5 ns target: a 1 x 8 GEMV engine with one port is 38.0 kGE.
    assert abs(accel_logic_kge("gemv", 1, 8, 1) - 38.0) / 38.0 < 0.03


def test_a_macro_backed_engine_adds_its_bank_interface():
    from chipleap.hw.tech import accel_bank_kge

    # The SmolLM2 accelerator's core with its macros outside, synthesized on SG13G2 for the
    # 5 ns target: 0.3312 mm2, 45.6 kGE.
    assert abs(accel_logic_kge("gemv", 1, 8, 1) + accel_bank_kge(1, 8, True) - 45.6) < 0.5
    assert accel_bank_kge(1, 8, False) < accel_bank_kge(1, 8, True) < accel_bank_kge(4, 8, True)


def test_memory_availability():
    lp4 = MEMORIES["lpddr4-x16"]
    hyper = MEMORIES["hyperram"]
    ihp = get_tech("ihp-sg13g2")
    assert not lp4.available(ihp.node_nm, ihp.max_io_mts)
    assert lp4.available(28, 4266)
    assert not MEMORIES["lpddr5-x16"].phy_exists(28)
    assert hyper.available(ihp.node_nm, ihp.max_io_mts)
    # Derated to the pads: 155 MT/s on a byte-wide bus at 80 % efficiency.
    assert hyper.sustained_bps_at(ihp.max_io_mts) == pytest.approx(124e6)


def test_design_validation():
    Design().validate()
    with pytest.raises(DesignError, match="NtoM"):
        Design(il_banks=4).validate()
    with pytest.raises(DesignError, match="16"):
        Design(bus="NtoM", banks=12, il_banks=8).validate()
    with pytest.raises(DesignError, match="power of two"):
        Design(bank_kib=24).validate()
    with pytest.raises(DesignError, match="ports"):
        Design(accel=Accel(ports=4)).validate()
    with pytest.raises(DesignError, match="dataflow"):
        Design(bus="NtoM", accel=Accel(dataflow="tpu")).validate()


def test_design_roundtrip():
    d = Design(name="x", bus="NtoM", il_banks=2, accel=Accel("os", 4, 8, 4, 2, 8, 4, False))
    assert Design.from_dict(d.to_dict()) == d
    with pytest.raises(DesignError, match="unknown design fields"):
        Design.from_dict({**d.to_dict(), "colour": "red"})


def test_accel_clock_limit_follows_ports_lanes_and_width():
    from chipleap.hw.tech import accel_signoff_mhz, get_tech

    one, two, four, eight = (accel_signoff_mhz("gemv", 8, p) for p in (1, 2, 4, 8))
    assert one >= two > four > eight
    assert accel_signoff_mhz("gemv", 16, 1) < accel_signoff_mhz("gemv", 8, 1)
    assert accel_signoff_mhz("gemv", 8, 2, rows=4) < accel_signoff_mhz("gemv", 8, 2, rows=2)
    # Routed at the slow corner with LibreLane 3.0.14 at 100 MHz, buffers in flip-flops: the
    # GEMV 2 x 8 engine with four ports took 9.604 ns, the 4 x 8 with two 9.961 ns, the 1 x 8
    # with eight and sixteen 10.821 and 11.046 ns, the 1 x 16 and 1 x 32 10.196 and 11.528 ns,
    # and the weight-stationary 8 x 8 with two 8.744 ns. The model's period is never shorter.
    for dataflow, rows, cols, ports, routed in (
        ("gemv", 2, 8, 4, 9.604),
        ("gemv", 4, 8, 2, 9.961),
        ("gemv", 1, 8, 8, 10.821),
        ("gemv", 1, 8, 16, 11.046),
        ("gemv", 1, 16, 1, 10.196),
        ("gemv", 1, 32, 1, 11.528),
        ("ws", 8, 8, 2, 8.744),
    ):
        assert 1000 / accel_signoff_mhz(dataflow, cols, ports, rows) >= routed - 1e-9
    ihp, g28 = get_tech("ihp-sg13g2"), get_tech("generic-28nm")
    assert ihp.clock_limit_mhz("gemv", 8, 4, rows=2) == ihp.max_freq_mhz
    assert accel_signoff_mhz("gemv", 8, 2, rows=4) == pytest.approx(100.39, abs=0.01)
    assert ihp.clock_limit_mhz("gemv", 8, 8) == pytest.approx(92.40, abs=0.01)
    assert ihp.clock_limit_mhz("gemv", 8, 16) == pytest.approx(79.63, abs=0.01)
    assert ihp.clock_limit_mhz("gemv", 32, 1) == pytest.approx(86.73, abs=0.01)
    # Only the node the timing was measured on is capped by it.
    assert g28.clock_limit_mhz("gemv", 8, 8) == g28.max_freq_mhz


def test_a_macro_backed_engine_is_capped_by_its_macros_read():
    from chipleap.hw.tech import ACCEL_BANK_PATH_NS, accel_signoff_mhz, get_tech
    from chipleap.macro.sram import IHP_1P_READ_NS

    read = {d: IHP_1P_READ_NS[f"RM_IHPSG13_1P_{d}x64_c2_bm_bist"] for d in (256, 1024, 2048)}
    flop = accel_signoff_mhz("gemv", 8, 1)
    shallow, mid, deep = (accel_signoff_mhz("gemv", 8, 1, bank_read_ns=read[d]) for d in read)
    # Macro builds route slower than flop builds, and deep macros read slower still.
    assert flop > shallow >= mid > deep
    assert deep == pytest.approx(1000 / (read[2048] + ACCEL_BANK_PATH_NS))
    # Routed at the slow corner with LibreLane 3.0.14: the GEMV 1 x 4 and 1 x 8 with 256-word
    # macros took 8.921 and 9.845 ns, the 1 x 8 with 1024 and 2048 words 11.224 and 13.024 ns
    # (on the macros' read) and the 2 x 8 with 256 x 64 macros 10.63 ns, and the model's
    # period is never shorter.
    for rows, cols, depth, routed in (
        (1, 4, 256, 8.921),
        (1, 8, 256, 9.845),
        (1, 8, 1024, 11.224),
        (1, 8, 2048, 13.024),
        (2, 8, 256, 10.63),
    ):
        r = IHP_1P_READ_NS[f"RM_IHPSG13_1P_{depth}x{cols * 8}_c2_bm_bist"]
        period = 1000 / accel_signoff_mhz("gemv", cols, 1, rows, bank_read_ns=r)
        assert period >= routed - 1e-9
    ihp = get_tech("ihp-sg13g2")
    assert ihp.clock_limit_mhz("gemv", 8, 1, bank_read_ns=read[2048]) == pytest.approx(deep)
    assert get_tech("generic-28nm").clock_limit_mhz("gemv", 8, 1, bank_read_ns=read[2048]) == 800
