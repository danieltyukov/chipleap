"""Check that the files of a bundle still describe the same design.

A bundle is edited by hand once it leaves ChipLeap: someone retunes a parameter in the
RTL wrapper, or bumps a bank count in the X-HEEP configuration. This step re-derives every
generated value from ``design.yaml`` and reports each place where a file disagrees, so
drift between the design record, the RTL, the driver, the register map and the X-HEEP
configuration (or, on the standalone platform, the AXI4 wrapper) is caught before it
reaches silicon.
"""

from __future__ import annotations

import re
from pathlib import Path

from chipleap.gen.datasheet import REGISTERS
from chipleap.gen.rtl import EXT_WINDOW, rtl_params
from chipleap.hw.design import Design

_WINDOW_RE = re.compile(r'"ext_slaves", start_address=(0x[0-9a-fA-F]+), length=(0x[0-9a-fA-F]+)')


def check(bundle: Path, design: Design) -> list[str]:
    """Return a list of disagreements (empty when the bundle is consistent)."""
    problems: list[str] = []

    def read(rel: str) -> str | None:
        p = bundle / rel
        if not p.is_file():
            problems.append(f"{rel} is missing")
            return None
        return p.read_text()

    standalone = design.platform == "standalone"
    cfg = None if standalone else read(f"xheep/{design.name}.py")
    if standalone and (bundle / "xheep" / f"{design.name}.py").is_file():
        problems.append(
            f"xheep/{design.name}.py: present, but design.yaml says the platform is standalone"
        )
    if cfg is not None:
        expect = {
            "bus": f"BusType.{design.bus}",
            "cpu": f"from cpu.{design.cpu} import",
            "banks": f"add_ram_banks([{design.bank_kib}] * {design.banks})",
        }
        if design.il_banks:
            expect["interleaved banks"] = (
                f"add_ram_banks_il({design.il_banks}, {design.il_bank_kib},"
            )
        for what, needle in expect.items():
            if needle not in cfg:
                problems.append(
                    f"xheep/{design.name}.py: {what} differ from design.yaml (expected {needle!r})"
                )
        if design.il_banks == 0 and "add_ram_banks_il(" in cfg:
            problems.append(
                f"xheep/{design.name}.py: interleaved banks present but design has none"
            )

    if design.accel is None:
        return problems
    p = rtl_params(design.accel)
    want = {
        "DATAFLOW": p.dataflow,
        "ROWS": p.rows,
        "COLS": p.cols,
        "WBITS": p.wbits,
        "PORTS": p.ports,
        "ABUF_WORDS": p.abuf_words,
        "WBUF_WORDS": p.wbuf_words,
        "ACC_ROWS": p.acc_rows,
    }
    wrappers = (
        ("rtl/chipleap_accel_axi_top.sv",)
        if standalone
        else ("rtl/chipleap_accel_top.sv", "rtl/chipleap_accel_xheep.sv")
    )
    if standalone:
        read("rtl/chipleap_accel_axi.sv")
    own = bool(design.accel_mhz)
    if own:
        read("rtl/chipleap_accel_cdc.sv")
    # The direct path's window is X-HEEP's external slave window, as the configuration sets
    # it (its default when the configuration is missing or says nothing).
    direct = design.direct_memory
    window = EXT_WINDOW
    if direct:
        read("rtl/chipleap_mem.sv")
        m = _WINDOW_RE.search(cfg or "")
        if m:
            window = (int(m.group(1), 16), int(m.group(2), 16))
    for rel in wrappers:
        text = read(rel)
        if text is None:
            continue
        # An accelerator on its own clock is instantiated behind its crossings, with that
        # clock as a port; one on X-HEEP's clock is not.
        if not standalone and own != bool(re.search(r"\bchipleap_accel_cdc\s*#", text)):
            problems.append(
                f"{rel}: the accelerator is "
                + ("not behind its clock crossings" if own else "behind clock crossings")
                + f", design.yaml says accel_mhz is {design.accel_mhz:g}"
            )
        for name, value in want.items():
            m = re.search(rf"\.{name}\(([^)]*)\)", text)
            if not m or m.group(1).strip() != str(value):
                got = m.group(1).strip() if m else "missing"
                problems.append(f"{rel}: {name} is {got}, design.yaml implies {value}")
        # With external memory on X-HEEP the accelerator reads it over its direct path, for
        # the configuration's window; without, it has none.
        ext = {
            "EXT_DIRECT": "1" if direct else None,
            "EXT_BASE": f"32'h{window[0]:08X}" if direct else None,
            "EXT_SIZE": f"32'h{window[1]:08X}" if direct else None,
        }
        for name, ext_value in ext.items():
            m = re.search(rf"\.{name}\(([^)]*)\)", text)
            got = m.group(1).strip() if m else None
            if got != ext_value:
                problems.append(
                    f"{rel}: {name} is {got or 'missing'}, "
                    + (
                        f"design.yaml and the X-HEEP configuration imply {ext_value}"
                        if ext_value
                        else "but design.yaml has no external memory on X-HEEP"
                    )
                )
        m = re.search(r"\.DOUBLE_BUF\(1'b(\d)\)", text)
        if not m or int(m.group(1)) != p.double_buf:
            problems.append(f"{rel}: DOUBLE_BUF differs from design.yaml")

    hdr = read("sw/chipleap_driver.h")
    if hdr is not None:
        macros = {
            "CHIPLEAP_DATAFLOW": p.dataflow,
            "CHIPLEAP_ROWS": p.rows,
            "CHIPLEAP_COLS": p.cols,
            "CHIPLEAP_WBITS": p.wbits,
            "CHIPLEAP_PORTS": p.ports,
            "CHIPLEAP_ABUF_BYTES": p.abuf_words * 4,
            "CHIPLEAP_WBUF_BYTES": p.wbuf_words * 4,
            "CHIPLEAP_DOUBLE_BUF": p.double_buf,
            "CHIPLEAP_ACC_ROWS": p.acc_rows,
        }
        for name, value in macros.items():
            m = re.search(rf"#define {name} (\d+)u?\b", hdr)
            if not m or int(m.group(1)) != value:
                problems.append(
                    f"sw/chipleap_driver.h: {name} differs from design.yaml (expected {value})"
                )
        for r in REGISTERS:
            if not re.search(rf"#define CHIPLEAP_REG_{r.name} 0x{r.offset:02X}u", hdr):
                problems.append(
                    f"sw/chipleap_driver.h: register {r.name} is not at {r.offset:#04x}"
                )

    rtl = read("rtl/chipleap_accel.sv")
    if rtl is not None:
        decode = set(re.findall(r"6'h([0-9A-F]{2}): reg_rdata_o =", rtl))
        for r in REGISTERS:
            if r.access == "wo":
                continue
            if f"{r.offset >> 2:02X}" not in decode:
                problems.append(
                    f"rtl/chipleap_accel.sv: register {r.name} is not decoded at {r.offset:#04x}"
                )

    hj = read("rtl/chipleap_accel.hjson")
    if hj is not None:
        names = re.findall(r'^    \{ name: "(\w+)"', hj, flags=re.M)
        if names != [r.name for r in REGISTERS]:
            problems.append("rtl/chipleap_accel.hjson: register list differs from the register map")
    return problems
