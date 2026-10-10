"""Find the open-source tools the verification steps use."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tool:
    name: str
    path: str | None
    version: str
    purpose: str

    @property
    def found(self) -> bool:
        return self.path is not None


def _version(cmd: list[str], pattern: str) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(pattern, out.stdout + out.stderr)
    return m.group(1) if m else ""


def _is_wrapper(path: str) -> bool:
    """Shell scripts that start a container are tools too, but checking their version
    would pull an image, so they are reported without one."""
    try:
        head = Path(path).read_bytes()[:400]
    except OSError:
        return False
    return head.startswith(b"#!") and b"docker" in head


def find(
    name: str, version_args: list[str], pattern: str, purpose: str, env: str | None = None
) -> Tool:
    path = os.environ.get(env, "") if env else ""
    path = path or shutil.which(name) or ""
    if not path:
        return Tool(name, None, "", purpose)
    if _is_wrapper(path):
        return Tool(name, path, "container wrapper", purpose)
    return Tool(name, path, _version([path, *version_args], pattern), purpose)


def verilator() -> Tool:
    return find(
        "verilator",
        ["--version"],
        r"Verilator (\S+)",
        "cycle-accurate simulation and lint",
        "CHIPLEAP_VERILATOR",
    )


def yosys() -> Tool:
    return find("yosys", ["-V"], r"Yosys (\S+)", "logic synthesis", "CHIPLEAP_YOSYS")


def openroad() -> Tool:
    return find("openroad", ["-version"], r"(\S+)", "static timing (OpenSTA)", "CHIPLEAP_OPENROAD")


def sta() -> Tool:
    return find("sta", ["-version"], r"(\S+)", "static timing (standalone OpenSTA)", "CHIPLEAP_STA")


def riscv_gcc() -> Tool:
    for name in (
        "riscv32-unknown-elf-gcc",
        "riscv32-corev-elf-gcc",
        "riscv64-unknown-elf-gcc",
        "riscv-none-elf-gcc",
    ):
        t = find(
            name, ["--version"], r"\) (\S+)", "compile the firmware driver", "CHIPLEAP_RISCV_GCC"
        )
        if t.found:
            return t
    return Tool("riscv-gcc", None, "", "compile the firmware driver")


def cxx() -> Tool:
    return find("c++", ["--version"], r"(\d+\.\d+\.\d+)", "build the Verilator testbench")


def all_tools() -> list[Tool]:
    return [verilator(), cxx(), yosys(), openroad(), sta(), riscv_gcc()]


def ihp_liberty() -> Path | None:
    """The IHP SG13G2 typical-corner standard-cell library, if a PDK is installed."""
    candidates = []
    env = os.environ.get("CHIPLEAP_LIBERTY")
    if env:
        return Path(env) if Path(env).is_file() else None
    roots = [
        os.environ.get("PDK_ROOT", ""),
        str(Path.home() / ".volare"),
        str(Path.home() / ".ciel"),
        str(Path.home() / ".local/share/pdk"),
        "/usr/local/share/pdk",
        "/opt/pdk",
    ]
    name = "sg13g2_stdcell_typ_1p20V_25C.lib"
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        candidates += list(Path(root).rglob(name))
        if candidates:
            break
    return sorted(candidates)[-1] if candidates else None
