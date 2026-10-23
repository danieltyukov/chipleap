"""Check a generated configuration with X-HEEP's own generator.

Two levels:

1. ``validate``: X-HEEP's ``load_config`` reads the file, then ``build()`` and
   ``validate()`` run the same sanity checks ``make mcu-gen`` runs.
2. ``render``: X-HEEP's ``mcu_gen.py`` renders all of its Mako templates (RTL packages,
   bus, memory subsystem, linker scripts, headers) for the configuration. The templates
   are copied to a scratch directory first, so the X-HEEP checkout is never modified.

Both need ``hjson`` and ``mako`` in the Python that runs them (``pip install
"chipleap[xheep]"``). ``fetch`` clones the X-HEEP commit ChipLeap is tested against.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from chipleap.verify.sim import cache_dir
from chipleap.verify.tools import clean_env, locate, which

XHEEP_URL = "https://github.com/esl-epfl/x-heep.git"
# The X-HEEP commit ChipLeap's generated configurations are tested against.
XHEEP_COMMIT = "bf0748ac5d15a1c274a47e3a7b663e6153c34187"

_VALIDATE = r"""
import json, sys
from pathlib import PurePath
sys.path.insert(0, "util/xheep_gen")
import load_config
cfg = sys.argv[1]
x = load_config.load_cfg_file(PurePath(cfg))
pads = load_config.load_pad_cfg(PurePath(sys.argv[2]), x)
x.set_padring(pads)
x.build()
x.validate()
mss = x.memory_ss()
print(json.dumps({
    "cpu": x.cpu().get_name(),
    "bus": str(x.bus_type()),
    "banks": mss.ram_numbanks(),
    "banks_il": mss.ram_numbanks_il(),
    "ram_bytes": mss.ram_size_address(),
}))
"""


class XheepError(RuntimeError):
    pass


@dataclass
class XheepResult:
    ok: bool
    level: str
    summary: dict[str, object] = field(default_factory=dict)
    rendered: list[str] = field(default_factory=list)
    log: str = ""


def find_checkout(path: str | Path | None = None) -> Path | None:
    candidates = [path, os.environ.get("CHIPLEAP_XHEEP_DIR"), cache_dir() / "x-heep"]
    for c in candidates:
        if c and (Path(c) / "util" / "xheep_gen" / "mcu_gen.py").is_file():
            return Path(c)
    return None


def fetch(dest: Path | None = None, commit: str = XHEEP_COMMIT) -> Path:
    """Shallow-clone the pinned X-HEEP commit into the cache (or ``dest``)."""
    target = dest or cache_dir() / "x-heep"
    if (target / "util" / "xheep_gen" / "mcu_gen.py").is_file():
        return target
    target.mkdir(parents=True, exist_ok=True)
    git = which("git")
    if git is None:
        raise XheepError("git is needed to fetch X-HEEP")
    for args in (
        ["init", "-q"],
        ["remote", "add", "origin", XHEEP_URL],
        ["fetch", "-q", "--depth", "1", "origin", commit],
        ["checkout", "-q", "FETCH_HEAD"],
    ):
        out = subprocess.run(
            [git, *args], cwd=target, env=clean_env(), capture_output=True, text=True, check=False
        )
        if out.returncode != 0 and args[0] != "remote":
            raise XheepError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return target


def _python() -> str:
    given = locate("python3", "CHIPLEAP_XHEEP_PYTHON")
    return given if given and os.environ.get("CHIPLEAP_XHEEP_PYTHON") else sys.executable


# X-HEEP's own pad configuration, relative to its checkout, for a design that has no
# other (``chipleap.gen.xheep.pad_config``).
DEFAULT_PADS = "configs/pad_cfg.py"


def _pads(pads: Path | None) -> str:
    return str(pads.resolve()) if pads is not None else DEFAULT_PADS


def validate(config: Path, checkout: Path, pads: Path | None = None) -> XheepResult:
    """X-HEEP's ``load_config``, ``build()`` and ``validate()`` on ``config`` with the pad
    configuration ``pads`` (X-HEEP's default without one)."""
    out = subprocess.run(
        [_python(), "-c", _VALIDATE, str(config.resolve()), _pads(pads)],
        cwd=checkout,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if out.returncode != 0:
        tail = (out.stdout + out.stderr).strip().splitlines()[-12:]
        hint = ""
        if "No module named" in out.stderr:
            hint = ' (install the X-HEEP generator dependencies: pip install "chipleap[xheep]")'
        return XheepResult(False, "validate", log="\n".join(tail) + hint)
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")][-1]
    return XheepResult(True, "validate", json.loads(line), log=out.stdout)


def render(
    config: Path, checkout: Path, keep: Path | None = None, pads: Path | None = None
) -> XheepResult:
    """Render X-HEEP's templates for ``config`` and the pad configuration ``pads`` (X-HEEP's
    default without one) into a scratch copy."""
    work = Path(tempfile.mkdtemp(prefix="chipleap-xheep-")) if keep is None else keep
    templates = []
    for top in ("hw", "sw", "tb"):
        for tpl in (checkout / top).rglob("*.tpl"):
            rel = tpl.relative_to(checkout)
            if rel.parts[:2] in (("hw", "vendor"), ("hw", "fpga")):
                continue
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tpl, dst)
            templates.append(str(dst))
    cmd = [
        _python(),
        "util/xheep_gen/mcu_gen.py",
        "--config",
        str(config.resolve()),
        "--pads_cfg",
        _pads(pads),
        "--outtpl",
        " ".join(templates),
    ]
    out = subprocess.run(
        cmd, cwd=checkout, env=clean_env(), capture_output=True, text=True, timeout=600, check=False
    )
    rendered = [
        str(Path(t).with_suffix("").relative_to(work))
        for t in templates
        if Path(t).with_suffix("").is_file()
    ]
    ok = out.returncode == 0 and len(rendered) == len(templates)
    summary: dict[str, object] = {"templates": len(templates), "rendered": len(rendered)}
    pkg = work / "hw/core-v-mini-mcu/include/core_v_mini_mcu_pkg.sv"
    if pkg.is_file():
        for line in pkg.read_text().splitlines():
            for key in ("NUM_BANKS", "NUM_BANKS_IL", "EXT_SLAVE_START_ADDRESS"):
                if f" {key} =" in line:
                    summary[key] = line.split("=", 1)[1].strip().rstrip(";")
    if keep is None:
        shutil.rmtree(work, ignore_errors=True)
    return XheepResult(
        ok,
        "render",
        summary,
        rendered,
        "\n".join((out.stdout + out.stderr).strip().splitlines()[-12:]),
    )
