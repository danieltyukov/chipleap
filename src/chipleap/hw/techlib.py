"""Add a process technology of your own from its Liberty files: ``chipleap tech add``.

The standard-cell library gives the gate-equivalent area (the unit-drive NAND2) and the
logic leakage density; SRAM macro libraries, when given, give the macro area fit and the
read energy. What a liberty file cannot say is scaled from the node and supply the same
way the built-in generic nodes are: MAC and CPU energy (Horowitz 2014), the practical
clock and external memory rate (interpolated between the built-in nodes), and the SRAM
fit when no macro libraries are given.

The liberty files can stay on a design server: with ``host`` set, each file is read over
ssh into memory, measured and dropped, and the technology records the paths on that
host for the synthesis backends. The YAML file written holds only the measured numbers
and the paths, in your config directory, never in a repository.
"""

from __future__ import annotations

import itertools
import math
import shlex
import subprocess
from pathlib import Path

import yaml

from chipleap.hw import liberty
from chipleap.hw.tech import (
    _CPU_KGE_45,
    _MAC_45,
    _SRAM16K_BYTE_45,
    BUILTIN,
    TECHNOLOGIES,
    Technology,
    _horowitz,
    tech_to_dict,
    user_tech_dir,
)

# Key-based ssh only: a password prompt becomes an error instead of a hang.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]


class TechError(ValueError):
    pass


def _interp(node_nm: float, pairs: list[tuple[float, float]]) -> float:
    """Log-log interpolation over (node, value) pairs, clamped at the ends."""
    pts = sorted(pairs)
    if node_nm <= pts[0][0]:
        return pts[0][1]
    if node_nm >= pts[-1][0]:
        return pts[-1][1]
    for (n0, v0), (n1, v1) in itertools.pairwise(pts):
        if n0 <= node_nm <= n1:
            t = math.log(node_nm / n0) / math.log(n1 / n0)
            return math.exp(math.log(v0) + t * (math.log(v1) - math.log(v0)))
    return pts[-1][1]


def default_clock_mhz(node_nm: float) -> float:
    return _interp(
        node_nm, [(TECHNOLOGIES[k].node_nm, TECHNOLOGIES[k].max_freq_mhz) for k in BUILTIN]
    )


def default_io_mts(node_nm: float) -> int:
    pairs = [(float(TECHNOLOGIES[k].node_nm), float(TECHNOLOGIES[k].max_io_mts)) for k in BUILTIN]
    return round(_interp(node_nm, pairs))


def read_text(path: str, host: str = "") -> str:
    """A text file, local or read over ssh from ``host`` into memory."""
    if not host:
        return Path(path).read_text(errors="replace")
    out = subprocess.run(
        ["ssh", *SSH_OPTS, host, f"cat {shlex.quote(path)}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise TechError(f"cannot read {path} on {host}: {out.stderr.strip()[-300:]}")
    return out.stdout


def list_libs(path: str, host: str = "") -> list[str]:
    """A liberty file, or the .lib files in a directory (local or on ``host``)."""
    if not host:
        return [str(p) for p in liberty.liberty_files(Path(path))]
    q = shlex.quote(path)
    probe = f"if [ -d {q} ]; then ls -1 {q}/*.lib; else echo {q}; fi"
    out = subprocess.run(
        ["ssh", *SSH_OPTS, host, f"bash -c {shlex.quote(probe)}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise TechError(f"cannot list {path} on {host}: {out.stderr.strip()[-300:]}")
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def measure(
    key: str,
    std_liberty: str,
    node_nm: int,
    *,
    sram: list[str] | None = None,
    vdd: float = 0.0,
    max_mhz: float = 0.0,
    max_io_mts: int = 0,
    lef: tuple[str, ...] = (),
    db: str = "",
    host: str = "",
    name: str = "",
    logic_util: float = 0.7,
) -> tuple[Technology, dict[str, str]]:
    """Build a technology from liberty files. Returns it with a short account of where each
    number came from."""
    if key in BUILTIN:
        raise TechError(f"{key!r} is a built-in technology; choose another name")
    if node_nm <= 0:
        raise TechError("give the process node in nm (--node)")
    lib = liberty.parse(read_text(std_liberty, host))
    nand = liberty.nand2(lib)
    ff = liberty.flop(lib)
    v = vdd or lib.nom_voltage or 1.0
    leak_mw_per_mm2 = (nand.leakage_w * 1e3) / (nand.area * 1e-6) if nand.area else 0.0
    basis = {
        "gate equivalent": f"{nand.name}, {nand.area:g} um2",
        "flip-flop": f"{ff.name}, {ff.area / nand.area:.2f} gate equivalents",
        "leakage": f"{nand.name} at the library's corner",
    }
    sram_paths = [p for s in (sram or []) for p in list_libs(s, host)]
    if sram_paths:
        cells = [c for p in sram_paths for c in liberty.parse(read_text(p, host)).cells.values()]
        fit = liberty.sram_fit(cells)
        macro_mm2, per_kib, max_kib = fit.macro_mm2, fit.mm2_per_kib, fit.max_kib
        pj = fit.pj_per_byte_16kib
        basis["sram"] = f"area fit over {fit.macros} macros"
        if math.isnan(pj):
            pj = _horowitz(node_nm, v, _SRAM16K_BYTE_45)
            basis["sram energy"] = "scaled from Horowitz 2014 (the macro libraries do not state it)"
        else:
            basis["sram energy"] = "read energy of the widest macros, at 16 KiB"
    else:
        # Same scaling the built-in generic nodes use, from the measured gate size.
        ref = TECHNOLOGIES["ihp-sg13g2"]
        shrink = nand.area / ref.ge_um2
        macro_mm2 = ref.sram_macro_mm2 * shrink
        per_kib = ref.sram_mm2_per_kib * shrink
        max_kib = 64 if node_nm <= 40 else 32
        pj = _horowitz(node_nm, v, _SRAM16K_BYTE_45)
        basis["sram"] = "scaled from IHP SG13G2 by the gate area (no macro libraries given)"
    tech = Technology(
        key=key,
        name=name or f"{key} ({node_nm} nm, measured from your liberty files)",
        node_nm=node_nm,
        vdd=round(v, 3),
        ge_um2=round(nand.area, 6),
        logic_util=logic_util,
        sram_mm2_per_kib=round(per_kib, 6),
        sram_macro_mm2=round(macro_mm2, 6),
        sram_macro_max_kib=int(max_kib),
        sram_pj_per_byte=round(pj, 4),
        mac_pj_8x8=round(_horowitz(node_nm, v, _MAC_45), 5),
        cpu_pj_per_kge_cycle=round(_horowitz(node_nm, v, _CPU_KGE_45), 5),
        leak_mw_per_mm2=round(leak_mw_per_mm2, 5),
        max_freq_mhz=max_mhz or round(default_clock_mhz(node_nm)),
        max_io_mts=max_io_mts or default_io_mts(node_nm),
        measured=True,
        note=(
            "Added with chipleap tech add: gate area, leakage"
            + (" and SRAM" if sram_paths else "")
            + " measured from your liberty files; logic energy scaled from Horowitz 2014."
        ),
        liberty=std_liberty,
        db=db,
        lef=tuple(lef),
        host=host,
        user=True,
    )
    return tech, basis


def save(tech: Technology, directory: Path | None = None, force: bool = False) -> Path:
    d = directory or user_tech_dir()
    if d is None:
        raise TechError("CHIPLEAP_TECH_DIR is empty, so technologies of your own are off")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{tech.key}.yaml"
    if path.exists() and not force:
        raise TechError(f"{path} exists; pass --force to replace it")
    header = (
        f"# ChipLeap technology {tech.key!r}, measured by chipleap tech add.\n"
        "# Numbers derived from your liberty files; keep this file out of repositories\n"
        "# when the PDK is under NDA.\n"
    )
    path.write_text(header + yaml.safe_dump(tech_to_dict(tech), sort_keys=False))
    return path


def remove(key: str, directory: Path | None = None) -> Path:
    d = directory or user_tech_dir()
    path = (d or Path(".")) / f"{key}.yaml"
    if d is None or not path.exists():
        raise TechError(f"no technology {key!r} of your own")
    path.unlink()
    return path
