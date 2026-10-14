"""The PDK LibreLane runs on: the IHP-Open-PDK commit of the checkout ChipLeap uses.

A LibreLane release installs the IHP-Open-PDK commit it pins (3.0.14 pins c4b8b4e), which
need not be the commit of the checkout the chip step takes its IO cells, DRC and LVS
from. So ChipLeap gives LibreLane a PDK root of its own (``--manual-pdk --pdk-root``):
``CHIPLEAP_LIBRELANE_PDK_ROOT`` when it is set, else a root under ChipLeap's data
directory holding the checkout's own commit, which ChipLeap installs with ciel the first
time a run needs it (about 800 MB). Without a checkout the commit is
``PINNED_COMMIT``, the one ChipLeap is tested with.

The install happens under a lock, so two runs never install at once, into a temporary
directory that is renamed into place once it is checked: the root must link to the
commit asked for, and a few of its files (technology and cell LEFs, the cells' GDS and
liberty, IHP's LibreLane configuration and DRC deck) must be the checkout's own, byte
for byte, or match ChipLeap's record of them at ``PINNED_COMMIT``. A failed or offline
download is an error that gives the ciel command to run by hand; it never falls back to
the commit the LibreLane release pins.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ENV = "CHIPLEAP_LIBRELANE_PDK_ROOT"
CIEL_ENV = "CHIPLEAP_CIEL"
PDK = "ihp-sg13g2"
PINNED_COMMIT = "22f2a25f1734796de3debbbf29cf697cbbc54081"
MARKER = ".chipleap-pdk.json"
INSTALL_TIMEOUT = 3600.0
LOCK_TIMEOUT = 3 * 3600.0
# Files of the PDK compared with the checkout's after an install, and their SHA-256 at
# PINNED_COMMIT for a machine without a checkout.
CHECKED = {
    "libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef": (
        "a12fecdead5721dc3bd14fb75bd0915c61afba064e38af8c7b69d59d9da05080"
    ),
    "libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef": (
        "c9bbc02d0c2cba3b15cd89a208750d25de882f02a2927ae78ba6422109fb01d0"
    ),
    "libs.ref/sg13g2_stdcell/gds/sg13g2_stdcell.gds": (
        "6ee92931ad70eeb8f79ca89fe0d4f38892d9e7e14d1a1b21bfb7118e55bf232f"
    ),
    "libs.ref/sg13g2_stdcell/lib/sg13g2_stdcell_slow_1p08V_125C.lib": (
        "1ac6a0301184b3a8aa1a4a01910967f624c8e869517777f529985f11f04588c3"
    ),
    "libs.ref/sg13g2_io/lef/sg13g2_io.lef": (
        "8b3dc3960960c08e07a1cde3dd9cd0484f5b12df07c4628726fff551bb775c0c"
    ),
    "libs.tech/librelane/config.tcl": (
        "e0fdcf33d93b8c795d4622479a910b279a1ac04a02a6eb42c992bd3aa553048b"
    ),
    "libs.tech/librelane/sg13g2_stdcell/config.tcl": (
        "ee82195ad5623afe89f773b4c1f40b9d79759221f689804f39402a70f499e880"
    ),
    "libs.tech/klayout/tech/drc/ihp-sg13g2.drc": (
        "0620b737538af7c86dfb7c6ca0ba3a38f8410b0d51412978ca977ea3df3fb693"
    ),
}


class LibrelanePdkError(ValueError):
    pass


def configured_root() -> Path | None:
    """``CHIPLEAP_LIBRELANE_PDK_ROOT``: a directory holding ``ihp-sg13g2``, used as it is.
    None when unset; a setting that is not an absolute directory with ``ihp-sg13g2`` in
    it raises, so a run never falls back silently."""
    value = os.environ.get(ENV, "")
    if not value:
        return None
    if not os.path.isabs(value):
        raise LibrelanePdkError(f"{ENV} is a relative path, {value}; give an absolute one")
    root = Path(value)
    if not (root / PDK).is_dir():
        raise LibrelanePdkError(f"{ENV} names {value}, which holds no {PDK}")
    return root


def checkout() -> tuple[Path | None, str]:
    """The IHP PDK ChipLeap uses (``chipleap.chip.io.pdk_dir``) and its commit: from git,
    or from the path of a ciel install. ``(None, "")`` without one."""
    from chipleap.chip import io  # chip.io reads the liberty through verify.tools

    pdk = io.pdk_dir()
    if pdk is None:
        return None, ""
    return pdk, io.pdk_commit(pdk) or commit_of(pdk)


def wanted_commit() -> str:
    """The commit LibreLane's PDK must be: the checkout's, or ``PINNED_COMMIT`` without
    a checkout. A checkout whose commit cannot be read raises."""
    pdk, commit = checkout()
    if pdk is None:
        return PINNED_COMMIT
    if not commit:
        raise LibrelanePdkError(
            f"the IHP PDK at {pdk} is no git checkout or ciel install, so its commit is not "
            f"known; set {ENV} to a PDK root holding the commit LibreLane should use"
        )
    return commit


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base) if os.path.isabs(base) else Path.home() / ".local" / "share"
    return root / "chipleap" / "librelane-pdk"


def managed_root(commit: str) -> Path:
    return data_dir() / commit


def root() -> Path:
    """The PDK root LibreLane runs on, whether it is installed yet or not."""
    return configured_root() or managed_root(wanted_commit())


def mount_root() -> Path:
    """The directory a LibreLane container must see for its PDK root:
    ``CHIPLEAP_LIBRELANE_PDK_ROOT``, else the one ChipLeap installs its roots in."""
    return configured_root() or data_dir()


def commit_of(path: Path) -> str:
    """The commit of a PDK under a ciel root: ciel keeps each version in
    ``ciel/<family>/versions/<commit>``, which ``ihp-sg13g2`` links to. Empty when the
    path does not say."""
    parts = path.resolve().parts
    if "versions" in parts:
        i = parts.index("versions")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def installed(path: Path) -> bool:
    """Whether a root ChipLeap manages holds its checked install."""
    return (path / MARKER).is_file() and (path / PDK).is_dir()


def ensure() -> Path:
    """The PDK root, installed and checked first when ChipLeap manages it."""
    given = configured_root()
    if given is not None:
        return given
    commit = wanted_commit()
    target = managed_root(commit)
    if installed(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with _locked(target.parent / ".lock"):
        if not installed(target):  # another run may have installed it meanwhile
            _install(target, commit)
    return target


def args_for(path: Path) -> list[str]:
    """LibreLane's PDK arguments for the root ``path`` (``ensure``)."""
    return ["--pdk", PDK, "--manual-pdk", "--pdk-root", str(path)]


def manual_command(target: Path, commit: str) -> str:
    return f"ciel enable --pdk-root {target} --pdk-family {PDK} {commit}"


@contextmanager
def _locked(path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """An exclusive lock on ``path`` across processes, waited for up to ``timeout``."""
    deadline = time.monotonic() + timeout
    with path.open("a") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LibrelanePdkError(
                        f"another ChipLeap has been installing LibreLane's PDK for over "
                        f"{timeout:g} s ({path})"
                    ) from None
                time.sleep(1.0)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _install(target: Path, commit: str) -> None:
    """ciel's install of ``commit`` into a temporary directory beside ``target``,
    checked, then renamed into place. A root someone installed by hand at ``target`` is
    checked and kept."""
    if (target / PDK).is_dir():
        _check(target, commit)
        _mark(target, commit)
        return
    if target.exists():
        raise LibrelanePdkError(
            f"{target} holds no {PDK}: an install that did not finish; remove it and run again"
        )
    tmp = Path(tempfile.mkdtemp(prefix=f".{commit[:12]}-", dir=target.parent))
    try:
        cmd = ciel_command(tmp, commit)
        try:
            code, out = run_ciel(cmd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, out = -1, str(exc)
        if code != 0 or not (tmp / PDK).is_dir():
            last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
            raise LibrelanePdkError(
                f"ChipLeap could not install IHP-Open-PDK {commit[:7]} for LibreLane "
                f"(ciel exited {code}: {last[:200]}). Install it with "
                f"`{manual_command(target, commit)}` (about 800 MB), or set {ENV} to a "
                "PDK root of your own"
            )
        _check(tmp, commit)
        _mark(tmp, commit)
        os.rename(tmp, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check(path: Path, commit: str) -> None:
    """The install is ``commit``: its ``ihp-sg13g2`` links there, and the ``CHECKED``
    files are the checkout's own (or, without a checkout, ChipLeap's record of them)."""
    got = commit_of(path / PDK)
    if got != commit:
        raise LibrelanePdkError(
            f"the PDK installed at {path} is IHP-Open-PDK {got[:7] or 'of no known commit'}, "
            f"not {commit[:7]}"
        )
    pdk, checkout_commit = checkout()
    for rel, recorded in CHECKED.items():
        f = path / PDK / rel
        if not f.is_file():
            raise LibrelanePdkError(f"the PDK installed at {path} has no {rel}")
        if pdk is not None and checkout_commit == commit:
            ref = pdk / rel
            expected = _sha256(ref) if ref.is_file() else ""
            source = f"the checkout at {pdk}"
        elif commit == PINNED_COMMIT:
            expected, source = recorded, f"ChipLeap's record of {commit[:7]}"
        else:
            raise LibrelanePdkError(f"ChipLeap has nothing to check {commit[:7]} against")
        if _sha256(f) != expected:
            raise LibrelanePdkError(f"{rel} of the PDK installed at {path} differs from {source}")


def _mark(path: Path, commit: str) -> None:
    (path / MARKER).write_text(json.dumps({"commit": commit, "checked": sorted(CHECKED)}))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ciel_command(target: Path, commit: str) -> list[str]:
    """ciel's install of ``commit`` into ``target``: the ``ciel`` command
    (``CHIPLEAP_CIEL``, else on PATH), else the one in LibreLane's container image."""
    from chipleap.verify.tools import librelane_image, which

    tool = os.environ.get(CIEL_ENV, "") or which("ciel")
    ciel_args = ["enable", "--pdk-root", str(target), "--pdk-family", PDK, commit]
    if tool and os.path.isabs(tool):
        return [tool, *ciel_args]
    docker = which("docker")
    image = librelane_image()
    if docker and image:
        uid = f"{os.getuid()}:{os.getgid()}"
        mount = f"{target.parent}:{target.parent}"
        run = [docker, "run", "--rm", "-u", uid, "-v", mount, "-e", "HOME=/tmp"]
        return [*run, "--entrypoint", "ciel", image, *ciel_args]
    raise LibrelanePdkError(
        f"ChipLeap needs ciel to install IHP-Open-PDK {commit[:7]} for LibreLane: install "
        f"it (pip install ciel) and run `{manual_command(target, commit)}`, or set {ENV}"
    )


def run_ciel(cmd: list[str]) -> tuple[int, str]:
    """Run ciel; its exit code and output."""
    out = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return out.returncode, out.stdout + out.stderr
