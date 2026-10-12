"""Running KLayout and IHP's DRC and LVS runners for a chip.

Each runs from an argument vector, confined to read only what it needs
(``chipleap.rtl.confine``), with a cleaned PATH, a private temporary and home directory
(the IHP pcell library writes fixed file names into the temporary directory, so two
runs must not share one) and no display. Output is captured, bounded, and written into
the build directory through ``builddir``.

IHP's runners build a shell command line from the paths they are given, quoting each
in single quotes, so they run in a private directory whose path holds only plain
characters, on copies of their inputs, and the PDK's own path must be plain too.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from chipleap.rtl import builddir, confine
from chipleap.verify.tools import Tool, find, probe, which

LOG_MAX = 8 << 20  # bytes of a tool's output kept
PLAIN_PATH = re.compile(r"[A-Za-z0-9_./+-]+")


class ToolError(RuntimeError):
    pass


def klayout() -> Tool:
    return find(
        "klayout",
        ["-v"],
        r"KLayout (\S+)",
        "chip finishing, fill, DRC and LVS (IHP's KLayout decks)",
        "CHIPLEAP_KLAYOUT",
    )


@lru_cache(maxsize=4)
def python_with_klayout() -> tuple[str, str] | None:
    """A Python that imports ``klayout.db`` (IHP's runners need it) and the directory the
    module is in, or None. ``CHIPLEAP_KLAYOUT_PYTHON`` names one; else python3."""
    exe = which(os.environ.get("CHIPLEAP_KLAYOUT_PYTHON", "") or "python3")
    if exe is None:
        return None
    try:
        out = probe([exe, "-c", "import klayout, os; print(os.path.dirname(klayout.__file__))"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = out.stdout.strip().splitlines()
    if out.returncode != 0 or not lines or not os.path.isabs(lines[-1]):
        return None
    return exe, str(Path(lines[-1]).parent)


def plain(path: Path, what: str) -> str:
    """``path`` as text, when a shell command line in single quotes holds it as it is."""
    text = str(path)
    if not PLAIN_PATH.fullmatch(text):
        raise ToolError(
            f"{what} is at {text}, a path with characters IHP's runner cannot pass to its "
            "shell; use a PDK at a path of letters, digits and _ . / + -"
        )
    return text


@contextmanager
def private_dir(prefix: str) -> Iterator[Path]:
    """A new directory of this process's own whose path is plain (see ``plain``)."""
    for base in (None, "/tmp"):
        tmp = tempfile.mkdtemp(prefix=prefix, dir=base)
        if PLAIN_PATH.fullmatch(tmp):
            break
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        raise ToolError("no temporary directory with a plain path; set TMPDIR to one")
    try:
        yield Path(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(
    cmd: list[str],
    cwd: Path,
    read: Iterable[Path],
    log_dir: Path,
    log_name: str,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Run ``cmd`` in ``cwd``, reading only ``read`` beside the system and its own
    installation, with ``env`` added; its output goes to ``log_dir/log_name``. Returns the
    exit code and the output's last part."""
    with tempfile.TemporaryDirectory(prefix="chipleap-home-") as home:
        extra = {
            "HOME": home,
            "KLAYOUT_HOME": home,
            "TMPDIR": home,
            "QT_QPA_PLATFORM": "offscreen",
            **(env or {}),
        }
        with confine.confined(cmd, [*read, Path(home), cwd]) as (argv, base):
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env={**base, **extra},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise ToolError(f"{Path(cmd[0]).name} did not finish in {timeout:g} s") from None
            except BaseException:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
    data = (out or b"")[-LOG_MAX:]
    builddir.write_bytes(log_dir, log_name, data)
    return proc.returncode, data[-4000:].decode(errors="replace")
