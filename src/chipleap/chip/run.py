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
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from chipleap.rtl import builddir, confine
from chipleap.verify.drc import DB_MAX
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


def ihp_runner(
    pdk: Path,
    which: str,
    out_dir: Path,
    inputs: dict[str, Path],
    args: Callable[[Path], list[str]],
    keep: tuple[str, ...],
    timeout: float,
) -> tuple[int, str]:
    """Run IHP's run_drc.py or run_lvs.py in a private directory on copies of
    ``inputs``, for at most ``timeout`` seconds; copy back the files named in ``keep``,
    every report (.lyrdb) and every log. Returns the exit code and the runner's output
    with the logs named in ``keep``. A report too large to read is an error: it would be
    the one with the most in it."""
    found = python_with_klayout()
    if found is None:
        raise ToolError(
            "IHP's DRC and LVS runners need a Python with KLayout's module (pip install "
            "klayout); set CHIPLEAP_KLAYOUT_PYTHON to one"
        )
    python, module_dir = found
    runner = pdk / "libs.tech" / "klayout" / "tech" / which / f"run_{which}.py"
    plain(runner, "IHP's KLayout decks")
    with private_dir(f"chipleap-{which}-") as tmp:
        for dest, src in inputs.items():
            shutil.copyfile(src, tmp / dest)
        run_dir = tmp / "run"
        # The runners import klayout.db; with HOME private, Python would not look in
        # the user's site directory, where pip puts it.
        # run_drc.py runs its tables in parallel with Python's multiprocessing, whose
        # semaphores are files in /dev/shm.
        shm = [Path("/dev/shm")] if which == "drc" and Path("/dev/shm").is_dir() else []
        code, tail = run(
            [python, str(runner), *args(run_dir)],
            tmp,
            [pdk, Path(module_dir), *shm],
            out_dir,
            f"{which}.log",
            timeout,
            {"PYTHONPATH": module_dir},
        )
        log = tail
        if run_dir.is_dir():
            for f in sorted(run_dir.iterdir()):
                wanted = f.name in keep or f.suffix in (".lyrdb", ".log")
                if not f.is_file() or not wanted:
                    continue
                if f.stat().st_size > DB_MAX:
                    raise ToolError(
                        f"IHP's {which.upper()} wrote {f.name}, larger than ChipLeap reads"
                    )
                builddir.write_bytes(out_dir, f.name, f.read_bytes())
                if f.name in keep and f.suffix == ".log":
                    log += "\n" + f.read_text(errors="replace")
    return code, log
