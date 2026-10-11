"""Run a long tool so that a time limit or Ctrl-C stops everything it started.

A subprocess timeout stops only the direct child. LibreLane starts OpenROAD, KLayout and
the rest as its own children, so the tool runs in a session of its own and its whole
process group is signalled. That still misses containers. A containerised LibreLane, from
a wrapper script that runs ``docker run`` or from LibreLane's own ``--dockerized`` mode,
runs in a container the docker daemon owns, outside this process tree. The docker client
passes SIGTERM on to the container's first process, which as PID 1 of its namespace
ignores it, and killing the client leaves the container running. So the containers the
run created are stopped through docker as well.

A container is the run's own when it did not exist before the run started and its
working directory is the run's work directory, which both launchers set with ``-w``.
Containers started for anything else are never touched.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

# How long the run's processes and containers get to exit after SIGTERM before SIGKILL.
STOP_GRACE_S = 10.0


class TimedOut(Exception):
    """The tool ran past its time limit and what it started was stopped: its processes,
    and the ``containers`` it created, except those in ``left`` that docker could not
    stop."""

    def __init__(self, seconds: float, containers: list[str], left: list[str]) -> None:
        super().__init__(f"timed out after {seconds:g} s")
        self.seconds = seconds
        self.containers = containers
        self.left = left


def run(cmd: list[str], cwd: Path, log: Path, timeout: float) -> int:
    """Run ``cmd`` in ``cwd`` with its output in ``log`` and return its exit code.

    Past ``timeout`` seconds, or on any exception including KeyboardInterrupt, the
    process group and the run's containers are stopped before the exception (TimedOut
    for the time limit) is raised."""
    before = _containers(everything=True)
    with log.open("w") as fh:
        # No stdin. Given a terminal, a wrapper adds `docker run -t`, and the client
        # switches the terminal to raw mode: Ctrl-C no longer reaches ChipLeap, and a
        # killed client cannot switch it back. Under the MCP server, `docker run -i`
        # would read the client's requests.
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stopped, left = _stop(proc, cwd, before)
            raise TimedOut(timeout, stopped, left) from None
        except BaseException:
            _stop(proc, cwd, before)
            raise


def _stop(
    proc: subprocess.Popen[bytes], workdir: Path, before: set[str] | None
) -> tuple[list[str], list[str]]:
    """Stop the run's process group and containers. The ids of the containers stopped,
    and of those still running."""
    with _finish_before_interrupt():
        _signal_group(proc, signal.SIGTERM)
        stopped, _ = _stop_containers(workdir, before)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=STOP_GRACE_S)
        # Also after the leader has gone: its children are still in the group.
        _signal_group(proc, signal.SIGKILL)
        proc.wait()
        # A client killed only now may have created a container since the first look,
        # and a container that would not stop gets a second try.
        more, left = _stop_containers(workdir, before)
    return stopped + more, left


def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


@contextmanager
def _finish_before_interrupt() -> Iterator[None]:
    """Hold back Ctrl-C until the block is done, then raise it: a second Ctrl-C while
    the run is being stopped would otherwise leave its containers running. Signal
    handlers can only be set from the main thread; elsewhere the block runs as is."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pressed: list[int] = []
    previous = signal.signal(signal.SIGINT, lambda signum, frame: pressed.append(signum))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if pressed:
        raise KeyboardInterrupt


def _docker() -> str | None:
    return shutil.which("docker")


def _run_docker(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str] | None:
    """A docker command, or None when docker is missing or does not answer. It runs in
    a session of its own so that a Ctrl-C at the terminal cannot cut it short."""
    exe = _docker()
    if exe is None:
        return None
    try:
        return subprocess.run(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _containers(everything: bool = False) -> set[str] | None:
    """Ids of the running containers (every container with ``everything``), or None
    without a docker that answers."""
    out = _run_docker(["ps", "-aq" if everything else "-q", "--no-trunc"])
    if out is None or out.returncode != 0:
        return None
    return set(out.stdout.split())


def _stop_containers(workdir: Path, before: set[str] | None) -> tuple[list[str], list[str]]:
    """Stop the running containers this run created. The ids of those docker stopped,
    and of those it did not."""
    if before is None:
        return [], []
    new = sorted((_containers() or set()) - before)
    if not new:
        return [], []
    # inspect prints what it finds even when a container has gone in the meantime.
    fmt = "{{.Id}} {{.Config.WorkingDir}}"
    out = _run_docker(["inspect", "--type", "container", "--format", fmt, *new])
    own = []
    for line in out.stdout.splitlines() if out else []:
        cid, _, wd = line.partition(" ")
        if cid in new and _same_dir(wd, workdir):
            own.append(cid)
    if not own:
        return [], []
    out = _run_docker(["stop", "-t", str(int(STOP_GRACE_S)), *own], timeout=STOP_GRACE_S + 60)
    done = set(out.stdout.split()) if out else set()
    return [c for c in own if c in done], [c for c in own if c not in done]


def _same_dir(path: str, workdir: Path) -> bool:
    try:
        return bool(path) and os.path.samefile(path, workdir)
    except OSError:
        return False
