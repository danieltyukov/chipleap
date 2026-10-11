"""A time limit or Ctrl-C stops everything a LibreLane run started, its containers
included, and nothing else. Docker is replaced by tests/fake_docker.py."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from chipleap.verify import launch

FAKE_DOCKER = Path(__file__).with_name("fake_docker.py")

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
sleep 600 &
echo $! > stray.pid
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


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _survivors(pids: list[int], within: float = 5.0) -> list[int]:
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


def _wait_for(cond, within: float = 10.0) -> None:
    end = time.monotonic() + within
    while not cond():
        assert time.monotonic() < end, "timed out waiting"
        time.sleep(0.02)


def _pid(path: Path) -> int | None:
    text = path.read_text().strip() if path.is_file() else ""
    return int(text) if text else None


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def docker(tmp_path, monkeypatch):
    """A fake docker first on the path. Yields its executable, its state directory and a
    list for clients the test starts itself."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = _script(bin_dir / "docker", f"#!{sys.executable}\n" + FAKE_DOCKER.read_text())
    state = tmp_path / "docker-state"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setattr(launch, "STOP_GRACE_S", 1.0)
    clients: list[subprocess.Popen] = []
    yield exe, state, clients
    # Whatever a test left running belongs to the fake: its containers and clients.
    for rec in _created(state):
        if _alive(rec["pid"]):
            os.kill(rec["pid"], signal.SIGKILL)
    for c in clients:
        c.kill()
        c.wait()


def _container(exe: Path, state: Path, workdir: Path, clients: list) -> dict:
    """Start a container that has nothing to do with the run under test."""
    n = len(_created(state))
    clients.append(
        subprocess.Popen(
            [str(exe), "run", "--rm", "-w", str(workdir), "unrelated"],
            start_new_session=True,
        )
    )
    _wait_for(lambda: len(_created(state)) > n)
    return _created(state)[-1]


def _hold_cleanup(monkeypatch, ready) -> None:
    """Stop the run only once ``ready(workdir)``. The tests use limits of a second or two,
    and on a loaded machine the run may not have started its container by then."""
    stop = launch._stop

    def held(proc, workdir, before):
        _wait_for(lambda: ready(workdir))
        return stop(proc, workdir, before)

    monkeypatch.setattr(launch, "_stop", held)


def _own(state: Path, workdir: Path) -> list[dict]:
    """The containers started with ``workdir`` as their working directory."""
    return [r for r in _created(state) if Path(r["workdir"]) == workdir.resolve()]


@pytest.mark.parametrize("launcher", [WRAPPER, MIXED], ids=["exec-client", "child-client"])
def test_timeout_stops_the_run_its_processes_and_containers(
    tmp_path, docker, monkeypatch, launcher
):
    exe, state, clients = docker
    work = tmp_path / "run"
    work.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    tool = _script(tmp_path / "librelane", launcher)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        # Already running in the run's directory before the run: not the run's.
        (earlier,) = [_container(exe, state, work, clients)]
        # Started elsewhere while the run is going: not the run's either.
        during: list[dict] = []

        def start_during() -> None:
            _wait_for(lambda: len(_own(state, work)) == 2)
            during.append(_container(exe, state, other, clients))

        t = threading.Thread(target=start_during)
        t.start()
        _hold_cleanup(monkeypatch, lambda wd: bool(during))
        with pytest.raises(launch.TimedOut) as exc:
            launch.run([str(tool), "config.json"], work, work / "librelane.log", timeout=1.0)
        t.join()

        (own,) = [r for r in _own(state, work) if r["id"] != earlier["id"]]
        assert exc.value.containers == [own["id"]] and exc.value.seconds == 1.0
        mine = [own["pid"], own["client"]]
        mine += [p for p in (_pid(work / "leader.pid"), _pid(work / "stray.pid")) if p]
        assert _survivors(mine) == []
        assert _alive(earlier["pid"]) and _alive(during[0]["pid"]) and _alive(bystander.pid)
    finally:
        bystander.kill()
        bystander.wait()


def test_ctrl_c_stops_the_run_and_its_container(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    work = tmp_path / "run"
    work.mkdir()
    tool = _script(tmp_path / "librelane", MIXED)
    # The fake client presses Ctrl-C on this process once the container is up.
    monkeypatch.setenv("FAKE_DOCKER_INTERRUPT", str(os.getpid()))
    with pytest.raises(KeyboardInterrupt):
        launch.run([str(tool)], work, work / "librelane.log", timeout=600)
    (rec,) = _created(state)
    pids = [rec["pid"], rec["client"], _pid(work / "leader.pid"), _pid(work / "stray.pid")]
    assert _survivors(pids) == []


def test_a_second_ctrl_c_does_not_cut_the_cleanup_short(tmp_path, docker, monkeypatch):
    _, state, _ = docker
    work = tmp_path / "run"
    work.mkdir()
    tool = _script(tmp_path / "librelane", WRAPPER)
    monkeypatch.setenv("FAKE_DOCKER_INTERRUPT", str(os.getpid()))
    real = launch._stop_containers

    def impatient(*args):
        os.kill(os.getpid(), signal.SIGINT)
        return real(*args)

    monkeypatch.setattr(launch, "_stop_containers", impatient)
    handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        launch.run([str(tool)], work, work / "librelane.log", timeout=600)
    (rec,) = _created(state)
    assert _survivors([rec["pid"], rec["client"]]) == []
    assert signal.getsignal(signal.SIGINT) is handler


def test_timeout_without_docker_stops_the_process_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "_docker", lambda: None)
    monkeypatch.setattr(launch, "STOP_GRACE_S", 1.0)
    work = tmp_path / "run"
    work.mkdir()
    tool = _script(tmp_path / "librelane", NATIVE)
    _hold_cleanup(monkeypatch, lambda wd: _pid(wd / "stray.pid") is not None)
    with pytest.raises(launch.TimedOut) as exc:
        launch.run([str(tool)], work, work / "librelane.log", timeout=1.0)
    assert exc.value.containers == []
    assert _survivors([_pid(work / "leader.pid"), _pid(work / "stray.pid")]) == []


def test_a_run_that_finishes_returns_its_exit_code(tmp_path, docker):
    work = tmp_path / "run"
    work.mkdir()
    tool = _script(tmp_path / "librelane", "#!/bin/sh\necho routed\nexit 3\n")
    assert launch.run([str(tool)], work, work / "librelane.log", timeout=600) == 3
    assert (work / "librelane.log").read_text() == "routed\n"
