"""A stand-in for the docker client and daemon, for tests/test_launch.py.

Containers are JSON records in ``$FAKE_DOCKER_STATE``, and ``created.log`` there lists
every container ever made. A container's process runs in a session of its own, outside
the process tree of the client that started it, like the processes of a real container.
It ignores SIGTERM, as LibreLane does when it is PID 1 of a container. ``run`` stays
attached until the container's process exits and ignores SIGTERM too: a real client
passes the signal on to the container and keeps waiting.

Supported: ``run [--rm] [--label K=V]... [-w DIR] IMAGE ...``,
``ps -aq --no-trunc [--filter label=K=V] [--filter status=S]...`` and
``stop -t N ID...``; anything else succeeds and does nothing. Hooks:
``$FAKE_DOCKER_SIGNAL=PID:NAME`` has ``run`` send that signal (INT, TERM, HUP) to PID once
its container is up, as a user pressing Ctrl-C would; ``$FAKE_DOCKER_STOP_FAILS`` makes
``stop`` fail and ``$FAKE_DOCKER_PS_FAILS`` makes ``ps`` fail.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
CONTAINER = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def records() -> list[dict[str, Any]]:
    out = []
    for p in sorted(STATE.glob("*.json")):
        with contextlib.suppress(OSError, ValueError):  # removed meanwhile
            out.append(json.loads(p.read_text()))
    return out


def save(rec: dict[str, Any]) -> None:
    tmp = STATE / f"{rec['id']}.tmp"
    tmp.write_text(json.dumps(rec))
    tmp.replace(STATE / f"{rec['id']}.json")


def status(rec: dict[str, Any]) -> str:
    if rec["status"] == "running" and not alive(rec["pid"]):
        return "exited"
    return str(rec["status"])


def options(args: list[str], *names: str) -> tuple[dict[str, list[str]], list[str]]:
    """The values of the named options, and the other arguments."""
    opts: dict[str, list[str]] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in names:
            opts.setdefault(args[i], []).append(args[i + 1])
            i += 1
        else:
            rest.append(args[i])
        i += 1
    return opts, rest


def ps(args: list[str]) -> int:
    if os.environ.get("FAKE_DOCKER_PS_FAILS"):
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        return 1
    opts, rest = options(args, "--filter")
    show_all = any(a == "--all" or (a[:1] == "-" and a[1:2] != "-" and "a" in a) for a in rest)
    labels = [f[len("label=") :] for f in opts.get("--filter", []) if f.startswith("label=")]
    states = [f[len("status=") :] for f in opts.get("--filter", []) if f.startswith("status=")]
    for r in records():
        st = status(r)
        if not show_all and st != "running":
            continue
        if states and st not in states:
            continue
        if any(lab not in r["labels"] for lab in labels):
            continue
        print(r["id"])
    return 0


def stop(args: list[str]) -> int:
    opts, ids = options(args, "-t")
    grace = float(opts.get("-t", ["10"])[0])
    code = 0
    for cid in ids:
        r = next((r for r in records() if r["id"] == cid), None)
        if r is None:
            print(f"Error: No such container: {cid}", file=sys.stderr)
            code = 1
            continue
        if os.environ.get("FAKE_DOCKER_STOP_FAILS"):
            print(f"Error response from daemon: cannot stop container: {cid}", file=sys.stderr)
            code = 1
            continue
        if alive(r["pid"]):
            os.kill(r["pid"], signal.SIGTERM)
            end = time.monotonic() + grace
            while alive(r["pid"]) and time.monotonic() < end:
                time.sleep(0.05)
            if alive(r["pid"]):
                os.kill(r["pid"], signal.SIGKILL)
            while alive(r["pid"]):
                time.sleep(0.01)
        print(cid)
    return code


def run(args: list[str]) -> int:
    opts, rest = options(args, "-w", "--name", "--label")
    rec: dict[str, Any] = {
        "id": uuid.uuid4().hex + uuid.uuid4().hex,
        "workdir": opts.get("-w", ["/"])[-1],
        "labels": opts.get("--label", []),
        "pid": 0,
        "status": "created",
        "client": os.getpid(),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", CONTAINER],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rec["pid"] = proc.pid
    rec["status"] = "running"
    save(rec)
    with (STATE / "created.log").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.environ.get("FAKE_DOCKER_SIGNAL"):
        pid, name = os.environ["FAKE_DOCKER_SIGNAL"].split(":")
        os.kill(int(pid), getattr(signal, f"SIG{name}"))
    while proc.poll() is None:
        time.sleep(0.05)
    if "--rm" in rest:
        (STATE / f"{rec['id']}.json").unlink(missing_ok=True)
    else:
        rec["status"] = "exited"
        save(rec)
    return 0


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    cmd, args = sys.argv[1], sys.argv[2:]
    handlers = {"ps": ps, "stop": stop, "run": run}
    return handlers[cmd](args) if cmd in handlers else 0


if __name__ == "__main__":
    sys.exit(main())
