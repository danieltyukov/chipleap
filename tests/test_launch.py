"""A time limit, a signal or a stop request ends everything a LibreLane run started, its
containers included, and nothing else. Docker is replaced by tests/fake_docker.py."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest

from chipleap.hw import Accel, Design
from chipleap.verify import launch, pnr
from chipleap.verify.run import place_route

FAKE_DOCKER = Path(__file__).with_name("fake_docker.py")
GEMV = Design(accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2))

# Runs LibreLane in a container as /usr/local/bin/librelane-style wrappers and
# `librelane --dockerized` do: the docker client replaces the launcher.
WRAPPER = """#!/bin/sh
exec docker run --rm -i -w "$PWD" ghcr.io/librelane/librelane librelane "$@"
"""

# A launcher that keeps the docker client as a child and leaves a process of its own
# running. Both it and that process ignore SIGTERM.
MIXED = """#!/bin/sh
trap '' TERM
echo $$ > leader.pid
echo "$CHIPLEAP_RUN_ID" > run.id
sleep 600 &
echo $! > stray.pid
docker run --rm -i -w "$PWD" ghcr.io/librelane/librelane librelane "$@"
"""

# A launcher that starts a second container once the first one is stopped.
TWICE = """#!/bin/sh
trap '' TERM
docker run --rm -i -w "$PWD" ghcr.io/librelane/librelane librelane "$@"
docker run --rm -i -w "$PWD" ghcr.io/librelane/librelane librelane "$@"
"""

# LibreLane without a container: a process tree only.
NATIVE = """#!/bin/sh
trap '' TERM
echo $$ > leader.pid
sleep 600 &
echo $! > stray.pid
sleep 600
"""


def _alive(pid: int | None) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _survivors(pids: list[int | None], within: float = 10.0) -> list[int | None]:
    """The processes still running after ``within`` seconds; a killed orphan is reaped
    by init a moment later."""
    end = time.monotonic() + within
    while True:
        left = [p for p in pids if _alive(p)]
        if not left or time.monotonic() > end:
            return left
        time.sleep(0.05)


def _created(state: Path) -> list[dict]:
    log = state / "created.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _own(state: Path) -> list[dict]:
    """The containers that carry a run's label."""
    return [
        r for r in _created(state) if any(lab.startswith("chipleap.run=") for lab in r["labels"])
    ]


def _until(cond, within: float = 30.0) -> bool:
    end = time.monotonic() + within
    while not cond():
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def _wait_for(cond, within: float = 30.0) -> None:
    assert _until(cond, within), "timed out waiting"


def _kill_leftover(pid: int | None, marker: str) -> None:
    """SIGKILL a process a failed test left behind, if ``pid`` still runs ``marker``."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return
    if marker.encode() in cmdline and _alive(pid):
        os.kill(pid, signal.SIGKILL)


def _pid(path: Path) -> int | None:
    text = path.read_text().strip() if path.is_file() else ""
    return int(text) if text else None


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def _dir(path: Path) -> Path:
    path.mkdir()
    return path


@pytest.fixture
def docker(tmp_path, monkeypatch):
    """A fake docker first on the path. Yields its executable, its state directory and a
    list for clients the test starts itself."""
    bin_dir = _dir(tmp_path / "bin")
    exe = _script(bin_dir / "docker", f"#!{sys.executable}\n" + FAKE_DOCKER.read_text())
    state = tmp_path / "docker-state"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setattr(launch, "STOP_GRACE_S", 1.0)
    monkeypatch.setattr(launch, "POLL_S", 0.1)
    clients: list[subprocess.Popen] = []
    yield exe, state, clients
    # What a test leaves running: the fake's containers and clients, and the launchers'
    # strays, which ignore SIGTERM.
    for rec in _created(state):
        _kill_leftover(rec["pid"], "time.sleep(600)")
        _kill_leftover(rec["client"], "docker")
    for pid_file in tmp_path.rglob("*.pid"):
        _kill_leftover(_pid(pid_file), "librelane" if pid_file.name == "leader.pid" else "sleep")
    for c in clients:
        c.kill()
        c.wait()


def _container(exe: Path, state: Path, workdir: Path, clients: list) -> dict:
    """Start a container that has nothing to do with the run under test."""
    n = len(_created(state))
    clients.append(
        subprocess.Popen(
            [str(exe), "run", "--rm", "-w", str(workdir), "unrelated"], start_new_session=True
        )
    )
    _wait_for(lambda: len(_created(state)) > n)
    return _created(state)[-1]


def _hold(monkeypatch, name: str, ready) -> None:
    """Let ``launch.<name>`` go ahead only once ``ready()``. The tests use limits of a
    second, and on a loaded machine the run may not have started its container by then."""
    real = getattr(launch, name)

    def held(*args):
        in_time = _until(ready)
        result = real(*args)
        assert in_time, f"launch.{name} went ahead before the run was ready"
        return result

    monkeypatch.setattr(launch, name, held)


@pytest.mark.parametrize("launcher", [WRAPPER, MIXED], ids=["exec-client", "child-client"])
def test_timeout_stops_the_run_its_processes_and_containers(
    tmp_path, docker, monkeypatch, launcher
):
    exe, state, clients = docker
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", launcher)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        # Containers in the run's own directory, one already there and one started while
        # the run goes on, with the same `docker run -w` the launcher uses: not the run's.
        earlier = _container(exe, state, work, clients)
        during: list[dict] = []

        def start_during() -> None:
            _wait_for(lambda: bool(_own(state)))
            during.append(_container(exe, state, work, clients))

        t = threading.Thread(target=start_during)
        t.start()
        _hold(monkeypatch, "_stop", lambda: bool(during))
        with pytest.raises(launch.TimedOut) as exc:
            launch.run([str(tool), "config.json"], work, work / "librelane.log", timeout=1.0)
        t.join()

        (own,) = _own(state)
        assert exc.value.containers == [own["id"]] and exc.value.seconds == 1.0
        assert exc.value.left == [] and not exc.value.unchecked
        if (work / "run.id").exists():
            assert own["labels"] == [f"chipleap.run={(work / 'run.id').read_text().strip()}"]
        mine = [own["pid"], own["client"], _pid(work / "leader.pid"), _pid(work / "stray.pid")]
        assert _survivors(mine) == []
        assert _alive(earlier["pid"]) and _alive(during[0]["pid"]) and _alive(bystander.pid)
        assert not (work / ".chipleap-bin").exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_a_container_created_after_the_first_look_is_stopped(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", TWICE)
    _hold(monkeypatch, "_stop", lambda: len(_own(state)) == 1)
    # The launcher's second container appears after the first pass stopped the first one;
    # SIGKILL waits for it, so that only the second pass can find it.
    real = launch._signal_group

    def kill_late(proc, sig):
        if sig == signal.SIGKILL:
            _wait_for(lambda: len(_own(state)) == 2)
        real(proc, sig)

    monkeypatch.setattr(launch, "_signal_group", kill_late)
    with pytest.raises(launch.TimedOut) as exc:
        launch.run([str(tool)], work, work / "librelane.log", timeout=1.0)
    first, second = _own(state)
    assert exc.value.containers == [first["id"], second["id"]] and exc.value.left == []
    assert _survivors([r[k] for r in (first, second) for k in ("pid", "client")]) == []


def test_ctrl_c_stops_the_run_and_its_container(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", MIXED)
    # The fake client presses Ctrl-C on this process once the container is up.
    monkeypatch.setenv("FAKE_DOCKER_SIGNAL", f"{os.getpid()}:INT")
    with pytest.raises(KeyboardInterrupt):
        launch.run([str(tool)], work, work / "librelane.log", timeout=600)
    (rec,) = _created(state)
    pids = [rec["pid"], rec["client"], _pid(work / "leader.pid"), _pid(work / "stray.pid")]
    assert _survivors(pids) == []


SIGNALLED = """
import os, signal, sys
from pathlib import Path
from chipleap.verify import launch

# As in a terminal: both signals at their default, whatever the test runner had.
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
launch.STOP_GRACE_S = 1.0
os.environ["FAKE_DOCKER_SIGNAL"] = f"{os.getpid()}:{sys.argv[2]}"
work = Path(sys.argv[1])
launch.run([str(work.parent / "librelane")], work, work / "librelane.log", timeout=600)
"""


@pytest.mark.parametrize("name", ["TERM", "HUP"])
def test_sigterm_and_sighup_stop_the_run_before_chipleap_exits(tmp_path, docker, name):
    _, state, _ = docker
    work = _dir(tmp_path / "run")
    _script(tmp_path / "librelane", MIXED)
    out = subprocess.run(
        [sys.executable, "-c", SIGNALLED, str(work), name], capture_output=True, timeout=120
    )
    assert out.returncode == 128 + getattr(signal, f"SIG{name}"), out.stderr
    (rec,) = _created(state)
    pids = [rec["pid"], rec["client"], _pid(work / "leader.pid"), _pid(work / "stray.pid")]
    assert _survivors(pids) == []


def test_a_second_signal_waits_for_the_cleanup(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", WRAPPER)
    monkeypatch.setenv("FAKE_DOCKER_SIGNAL", f"{os.getpid()}:INT")
    real = launch._stop_containers

    def impatient(*args):
        signal.raise_signal(signal.SIGINT)
        return real(*args)

    monkeypatch.setattr(launch, "_stop_containers", impatient)
    handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        launch.run([str(tool)], work, work / "librelane.log", timeout=600)
    (rec,) = _created(state)
    assert _survivors([rec["pid"], rec["client"]]) == []
    assert signal.getsignal(signal.SIGINT) is handler


def test_a_third_signal_cuts_the_cleanup_short(tmp_path, docker, monkeypatch):
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", WRAPPER)
    monkeypatch.setenv("FAKE_DOCKER_SIGNAL", f"{os.getpid()}:INT")
    calls = []

    def hanging(*args):
        calls.append(args)
        for _ in range(3):
            signal.raise_signal(signal.SIGINT)
        raise AssertionError("the third Ctrl-C should have ended the cleanup")

    monkeypatch.setattr(launch, "_stop_containers", hanging)
    handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        launch.run([str(tool)], work, work / "librelane.log", timeout=600)
    assert len(calls) == 1 and signal.getsignal(signal.SIGINT) is handler


def test_timeout_without_docker_stops_the_process_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "_docker", lambda: None)
    monkeypatch.setattr(launch, "STOP_GRACE_S", 1.0)
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", NATIVE)
    _hold(monkeypatch, "_stop", lambda: _pid(work / "stray.pid") is not None)
    with pytest.raises(launch.TimedOut) as exc:
        launch.run([str(tool)], work, work / "librelane.log", timeout=1.0)
    assert exc.value.containers == [] and not exc.value.unchecked
    assert _survivors([_pid(work / "leader.pid"), _pid(work / "stray.pid")]) == []


def test_a_run_that_finishes_returns_its_exit_code(tmp_path, docker):
    work = _dir(tmp_path / "run")
    tool = _script(
        tmp_path / "librelane", '#!/bin/sh\necho "$CHIPLEAP_RUN_ID"\ncommand -v docker\nexit 3\n'
    )
    assert launch.run([str(tool)], work, work / "librelane.log", timeout=600) == 3
    run_id, docker_path = (work / "librelane.log").read_text().split()
    # The run sees its id and a labelling docker first on its PATH, gone afterwards.
    assert len(run_id) == 32 and docker_path == str(work / ".chipleap-bin" / "docker")
    assert not (work / ".chipleap-bin").exists()


def test_two_runs_cannot_share_a_work_directory(tmp_path, docker):
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", "#!/bin/sh\nexit 0\n")
    with (work / ".chipleap-run.lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(launch.Busy, match="another run is using"):
            launch.run([str(tool)], work, work / "librelane.log", timeout=600)
    assert not (work / "librelane.log").exists()
    assert launch.run([str(tool)], work, work / "librelane.log", timeout=600) == 0


def test_stop_all_stops_runs_in_other_threads(tmp_path, docker):
    _, state, _ = docker
    work = _dir(tmp_path / "run")
    tool = _script(tmp_path / "librelane", WRAPPER)
    raised: list[BaseException] = []

    def go() -> None:
        try:
            launch.run([str(tool)], work, work / "librelane.log", timeout=600)
        except BaseException as exc:
            raised.append(exc)

    t = threading.Thread(target=go)
    t.start()
    _wait_for(lambda: bool(_own(state)))
    launch.stop_all(wait=60)
    t.join(5)
    (rec,) = _own(state)
    assert isinstance(raised[0], launch.Cancelled) and raised[0].containers == [rec["id"]]
    assert _survivors([rec["pid"], rec["client"]]) == []


def test_cancelling_an_mcp_call_stops_its_run_and_no_other(tmp_path, docker):
    from chipleap.mcp_server import _stops_its_runs

    _, state, _ = docker
    tool = _script(tmp_path / "librelane", WRAPPER)
    mine, other = _dir(tmp_path / "mine"), _dir(tmp_path / "other")

    def verify(out_dir: str) -> dict:
        launch.run([str(tool)], Path(out_dir), Path(out_dir) / "librelane.log", timeout=600)
        return {}

    def in_dir(d: Path) -> list[dict]:
        return [r for r in _own(state) if Path(r["workdir"]) == d.resolve()]

    # Another run, started outside the MCP call, goes on.
    raised: list[BaseException] = []

    def go() -> None:
        try:
            verify(str(other))
        except BaseException as exc:
            raised.append(exc)

    t = threading.Thread(target=go)
    t.start()
    _wait_for(lambda: bool(in_dir(other)))

    async def call_then_cancel() -> None:
        with anyio.CancelScope() as scope:

            async def cancel_once_up() -> None:
                while not in_dir(mine):
                    await anyio.sleep(0.05)
                scope.cancel()

            async with anyio.create_task_group() as tg:
                tg.start_soon(cancel_once_up)
                await _stops_its_runs(verify)(out_dir=str(mine))

    anyio.run(call_then_cancel)
    (rec,) = in_dir(mine)
    assert _survivors([rec["pid"], rec["client"]], within=30) == []
    (theirs,) = in_dir(other)
    assert _alive(theirs["pid"])
    launch.stop_all(wait=60)
    t.join(5)
    assert isinstance(raised[0], launch.Cancelled) and _survivors([theirs["pid"]]) == []


SERVER = """
import os, signal, sys, threading
from pathlib import Path
from chipleap.verify import launch

signal.signal(signal.SIGTERM, signal.SIG_DFL)
launch.STOP_GRACE_S = 1.0
os.environ["FAKE_DOCKER_SIGNAL"] = f"{os.getpid()}:TERM"
work = Path(sys.argv[1])

done = threading.Event()

def worker():
    try:
        launch.run([str(work.parent / "librelane")], work, work / "librelane.log", timeout=600)
    finally:
        done.set()

# Waits as the server's event loop does, so that the signal handler gets to run. Not in
# Thread.join: a signal there makes Python 3.12 think the thread has ended.
with launch.stop_on_exit(wait=60):
    threading.Thread(target=worker).start()
    while not done.wait(0.1):
        pass
"""


def test_a_server_stops_its_runs_when_terminated(tmp_path, docker):
    """The MCP server runs tools in worker threads, which signals do not reach."""
    _, state, _ = docker
    work = _dir(tmp_path / "run")
    _script(tmp_path / "librelane", MIXED)
    out = subprocess.run(
        [sys.executable, "-c", SERVER, str(work)], capture_output=True, text=True, timeout=120
    )
    assert not (work / ".chipleap-bin").exists()
    # It stops the run, then ends as SIGTERM would have ended it.
    assert out.returncode == -signal.SIGTERM, out.stderr
    (rec,) = _created(state)
    pids = [rec["pid"], rec["client"], _pid(work / "leader.pid"), _pid(work / "stray.pid")]
    assert _survivors(pids) == []


def _pnr_with(tmp_path, monkeypatch, state, **env):
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(_script(tmp_path / "librelane", WRAPPER)))
    monkeypatch.setenv("CHIPLEAP_PNR_TIMEOUT", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _hold(monkeypatch, "_stop", lambda: bool(_own(state)))
    return place_route(GEMV, tmp_path / "pnr")


def test_pnr_step_reports_the_time_limit(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    step = _pnr_with(tmp_path, monkeypatch, state)
    assert step.status == "fail"
    assert step.summary == (
        "LibreLane timed out after 1 s (CHIPLEAP_PNR_TIMEOUT); "
        "its processes and 1 container were stopped"
    )
    (rec,) = _created(state)
    assert _survivors([rec["pid"], rec["client"]]) == []
    with pytest.raises(pnr.PnrError, match="timed out after 1 s"):
        pnr.place_and_route(GEMV.accel, 100.0, tmp_path / "again")


def test_a_container_docker_cannot_stop_is_reported(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    step = _pnr_with(tmp_path, monkeypatch, state, FAKE_DOCKER_STOP_FAILS="1")
    (rec,) = _created(state)
    assert step.status == "fail"
    assert step.summary == (
        "LibreLane timed out after 1 s (CHIPLEAP_PNR_TIMEOUT); its processes were stopped; "
        f"docker could not stop 1 container: {rec['id']}"
    )


def test_containers_docker_could_not_list_are_reported(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    monkeypatch.setenv("FAKE_DOCKER_PS_FAILS", "1")
    step = _pnr_with(tmp_path, monkeypatch, state)
    assert step.summary == (
        "LibreLane timed out after 1 s (CHIPLEAP_PNR_TIMEOUT); its processes were stopped; "
        "docker did not answer, so its containers were not checked"
    )


def test_pnr_step_reports_a_busy_work_directory(tmp_path, docker, monkeypatch):
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(_script(tmp_path / "librelane", WRAPPER)))
    monkeypatch.setenv("CHIPLEAP_PNR_TIMEOUT", "1")
    work = _dir(tmp_path / "pnr")
    with (work / ".chipleap-run.lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        step = place_route(GEMV, work)
    assert step.status == "fail"
    assert step.summary == f"LibreLane did not start: another run is using {work}"


def test_time_limits_read_in_their_unit():
    assert pnr._span(10800) == "3 h" and pnr._span(5400) == "1.5 h"
    assert pnr._span(120) == "2 min" and pnr._span(1) == "1 s"
