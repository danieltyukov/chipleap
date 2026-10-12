"""Run a long tool so that a time limit, Ctrl-C or a stop request ends everything it
started, and nothing else.

A subprocess timeout stops only the direct child. LibreLane starts OpenROAD, KLayout and
the rest as its own children, so the tool runs in a session of its own and its whole
process group is signalled. That still misses containers. A containerised LibreLane, from
a wrapper script that runs ``docker run`` or from LibreLane's own ``--dockerized`` mode,
runs in a container the docker daemon owns, outside this process tree. The docker client
passes SIGTERM on to the container's first process, which as PID 1 of its namespace
ignores it, and killing the client leaves the container running. So the run's containers
are stopped through docker as well.

A container is the run's own only when it carries the label ``chipleap.run=<run id>``.
The run's PATH starts with a ``docker`` that adds the label to ``docker run`` and
``docker create`` and hands everything else to the real docker; both launchers find
docker on the PATH. A launcher that calls docker by its full path can add
``--label chipleap.run=$CHIPLEAP_RUN_ID`` itself. Containers without the label, whoever
started them and wherever, are never touched.

Runs started from worker threads, as the MCP server's are, cannot take signals: they
stop when ``stop_all`` is called or when the event given to ``stop_when`` is set.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

from chipleap.rtl import builddir
from chipleap.verify.tools import clean_env, which

if TYPE_CHECKING:
    from collections.abc import Callable

    _Handler = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None

LABEL = "chipleap.run"
# How long the run's processes and containers get to exit after SIGTERM before SIGKILL.
# LibreLane as a container's PID 1 ignores SIGTERM, so a container takes all of it.
STOP_GRACE_S = 5.0
# How often a waiting run checks whether it has been asked to stop.
POLL_S = 0.5
_LIVE = ("running", "paused", "restarting")

# Each run in progress, by the event that stops it.
_runs: set[threading.Event] = set()
_runs_changed = threading.Condition()
_stop_scope: ContextVar[threading.Event | None] = ContextVar("chipleap_stop_scope", default=None)


class Busy(RuntimeError):
    """Another run holds the work directory."""


class Stopped(Exception):
    """The run was stopped. ``containers`` were stopped through docker, ``left`` were
    still running afterwards, and ``unchecked`` means docker did not answer, so the
    run's containers could not be listed."""

    def __init__(self, msg: str, containers: list[str], left: list[str], unchecked: bool):
        super().__init__(msg)
        self.containers = containers
        self.left = left
        self.unchecked = unchecked


class TimedOut(Stopped):
    """The tool ran past its time limit."""

    def __init__(self, seconds: float, containers: list[str], left: list[str], unchecked: bool):
        super().__init__(f"timed out after {seconds:g} s", containers, left, unchecked)
        self.seconds = seconds


class Cancelled(Stopped):
    """``stop_all`` or a ``stop_when`` event stopped the run."""


def run(
    cmd: list[str], cwd: Path, log: Path, timeout: float, env: dict[str, str] | None = None
) -> int:
    """Run ``cmd`` in ``cwd`` with its output in ``log`` and return its exit code. ``env``
    replaces the environment it starts with (the run's id and docker are added to it).

    At the time limit (TimedOut), on a stop request (Cancelled), or on any exception,
    including KeyboardInterrupt and the SystemExit that SIGTERM and SIGHUP raise while
    it runs, the run's process group and containers are stopped before it is raised."""
    run_id = uuid.uuid4().hex
    stop = threading.Event()
    scope = _stop_scope.get()
    # Registered first: stop_all(wait=...) returns once the run has also unlocked its work
    # directory and removed its docker, in case the process ends right after it.
    with (
        _registered(stop),
        _one_run_in(cwd),
        _labelling_docker(cwd, run_id, env) as run_env,
        _signals_raise(),
        log.open("w") as fh,
    ):
        # No stdin. Given a terminal, a wrapper adds `docker run -t`, and the client
        # switches the terminal to raw mode: Ctrl-C no longer reaches ChipLeap, and a
        # killed client cannot switch it back. Under the MCP server, `docker run -i`
        # would read the client's requests.
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = _wait(proc, timeout, stop, scope)
        except BaseException:
            _stop(proc, run_id)
            raise
        if code is not None:
            return code
        stopped, left, unchecked = _stop(proc, run_id)
        if stop.is_set() or (scope is not None and scope.is_set()):
            raise Cancelled("stopped on request", stopped, left, unchecked)
        raise TimedOut(timeout, stopped, left, unchecked)


def stop_all(wait: float = 0.0) -> None:
    """Ask every run in progress to stop, from any thread; each stops its processes and
    containers within ``POLL_S`` and raises Cancelled. With ``wait``, block until they
    all have, for at most that many seconds."""
    with _runs_changed:
        for ev in _runs:
            ev.set()
        if wait > 0:
            _runs_changed.wait_for(lambda: not _runs, timeout=wait)


@contextmanager
def stop_when(event: threading.Event) -> Iterator[None]:
    """Runs started in this block, in this thread, stop once ``event`` is set."""
    token = _stop_scope.set(event)
    try:
        yield
    finally:
        _stop_scope.reset(token)


@contextmanager
def stop_on_exit(wait: float = 120.0) -> Iterator[None]:
    """For a process whose worker threads start runs, such as the MCP server: SIGTERM and
    SIGHUP stop every run, waiting up to ``wait`` seconds, and then end the process as
    they would have; a second one ends it at once. Leaving the block also stops every
    run and waits for them. Signals take effect on the main thread only."""

    def end(signum: int, frame: FrameType | None) -> None:
        signal.signal(signum, signal.SIG_DFL)
        stop_all(wait=wait)
        # Not SystemExit: an orderly shutdown of the server can wait for ever on the
        # thread that reads its stdin.
        os.kill(os.getpid(), signum)

    previous = _take_default((signal.SIGTERM, signal.SIGHUP), end)
    try:
        yield
    finally:
        _restore(previous)
        stop_all(wait=wait)


def _wait(
    proc: subprocess.Popen[bytes],
    timeout: float,
    stop: threading.Event,
    scope: threading.Event | None,
) -> int | None:
    """The exit code, or None at the time limit or on a stop request."""
    end = time.monotonic() + timeout
    while not stop.is_set() and not (scope is not None and scope.is_set()):
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        with suppress(subprocess.TimeoutExpired):
            return proc.wait(timeout=min(remaining, POLL_S))
    return None


def _stop(proc: subprocess.Popen[bytes], run_id: str) -> tuple[list[str], list[str], bool]:
    """Stop the run's process group and containers. The containers stopped, those still
    running, and whether docker failed to answer."""
    docker = _docker() is not None
    with _signals_held():
        _signal_group(proc, signal.SIGTERM)
        first, ok_first = _stop_containers(run_id) if docker else ([], True)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=STOP_GRACE_S)
        # Also after the leader has gone: its children are still in the group.
        _signal_group(proc, signal.SIGKILL)
        # Bounded: a process stuck in the kernel, on a hung network mount, dies only later.
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
        # A client killed only now may have created a container since the first look,
        # or started one that was still being created then.
        second, ok_second = _stop_containers(run_id) if docker else ([], True)
        left = _containers(run_id, _LIVE) if docker else []
    stopped = list(dict.fromkeys(first + second))
    return stopped, left or [], not (ok_first and ok_second and left is not None)


def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


@contextmanager
def _one_run_in(workdir: Path) -> Iterator[None]:
    """One run at a time per work directory: two would overwrite each other's results."""
    # Not through a symlink, which O_CREAT would follow to make a file wherever it points.
    fd = os.open(workdir / ".chipleap-run.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(f"another run is using {workdir}") from None
        yield
    finally:
        os.close(fd)


_SHIM = """#!/bin/sh
# ChipLeap put this docker first on the PATH of one run: it labels the containers the run
# creates, so that a time limit or Ctrl-C stops exactly those, and passes all else on.
case "$1" in
run | create) c=$1; shift; exec {docker} "$c" --label {label} "$@" ;;
container)
    case "$2" in
    run | create) c=$2; shift 2; exec {docker} container "$c" --label {label} "$@" ;;
    esac ;;
esac
exec {docker} "$@"
"""


@contextmanager
def _labelling_docker(
    workdir: Path, run_id: str, base: dict[str, str] | None = None
) -> Iterator[dict[str, str]]:
    """The run's environment: ``base`` (this process's by default) with CHIPLEAP_RUN_ID, the
    search paths reduced to what a tool may use (``tools.clean_env``), and, with docker
    installed, a PATH that starts with the labelling docker. It lives in the work
    directory, not in /tmp, which may not allow executables."""
    env = {**clean_env(exclude=[workdir], base=base), "CHIPLEAP_RUN_ID": run_id}
    real = _docker()
    if real is None:
        yield env
        return
    bin_dir = builddir.work_dir(workdir, ".chipleap-bin", empty=True)
    shim = bin_dir / "docker"
    shim.write_text(_SHIM.format(docker=shlex.quote(real), label=shlex.quote(f"{LABEL}={run_id}")))
    shim.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    try:
        yield env
    finally:
        shutil.rmtree(bin_dir, ignore_errors=True)


@contextmanager
def _registered(stop: threading.Event) -> Iterator[None]:
    with _runs_changed:
        _runs.add(stop)
    try:
        yield
    finally:
        with _runs_changed:
            _runs.discard(stop)
            _runs_changed.notify_all()


def _take_default(
    signals: tuple[signal.Signals, ...], handler: _Handler
) -> dict[signal.Signals, _Handler]:
    """Install ``handler`` for those of ``signals`` still at their default action; the
    handlers it replaced. Only the main thread can do this; elsewhere it does nothing."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {s: signal.signal(s, handler) for s in signals if signal.getsignal(s) is signal.SIG_DFL}


def _restore(previous: dict[signal.Signals, _Handler]) -> None:
    for s, h in previous.items():
        signal.signal(s, h)


@contextmanager
def _signals_raise() -> Iterator[None]:
    """While the run is going, SIGTERM and SIGHUP raise SystemExit instead of ending
    ChipLeap on the spot, so that the run is stopped first. Signals set to be ignored,
    such as SIGHUP under nohup, stay ignored."""

    def end(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    previous = _take_default((signal.SIGTERM, signal.SIGHUP), end)
    try:
        yield
    finally:
        _restore(previous)


@contextmanager
def _signals_held() -> Iterator[None]:
    """Hold back Ctrl-C, SIGTERM and SIGHUP while the run is being stopped, and deliver
    them once it is: cut short, the cleanup would leave containers running. The third
    signal is delivered at once, for a cleanup that hangs."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []
    previous: dict[signal.Signals, _Handler] = {}

    def hold(signum: int, frame: FrameType | None) -> None:
        held.append(signum)
        if len(held) >= 3:
            _restore(previous)
            signal.raise_signal(signum)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(s) not in (signal.SIG_IGN, None):
            previous[s] = signal.signal(s, hold)
    try:
        yield
    finally:
        _restore(previous)
    for signum in dict.fromkeys(held):
        signal.raise_signal(signum)


def _docker() -> str | None:
    return which("docker")


def _run_docker(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str] | None:
    """A docker command, or None when docker is missing or does not answer. It runs in
    a session of its own so that a Ctrl-C at the terminal cannot cut it short."""
    exe = _docker()
    if exe is None:
        return None
    try:
        return subprocess.run(
            [exe, *args],
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _containers(run_id: str, states: tuple[str, ...]) -> list[str] | None:
    """Ids of the run's containers in one of ``states``, or None when docker does not
    answer."""
    args = ["ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={run_id}"]
    for s in states:
        args += ["--filter", f"status={s}"]
    out = _run_docker(args)
    if out is None or out.returncode != 0:
        return None
    return out.stdout.split()


def _stop_containers(run_id: str) -> tuple[list[str], bool]:
    """Stop the run's containers that run or may yet start. The ids docker stopped, and
    whether docker answered."""
    ids = _containers(run_id, ("created", *_LIVE))
    if not ids:
        return [], ids is not None
    out = _run_docker(["stop", "-t", str(int(STOP_GRACE_S)), *ids], timeout=STOP_GRACE_S + 60)
    done = set(out.stdout.split()) if out else set()
    return [c for c in ids if c in done], True
