"""A stand-in for the docker client and daemon, for tests/test_launch.py.

Containers are JSON records in ``$FAKE_DOCKER_STATE``, and ``created.log`` there lists
every container ever made. A container's process runs in a
session of its own, outside the process tree of the client that started it, like the
processes of a real container. It ignores SIGTERM, as LibreLane does when it is PID 1 of
a container. ``run`` stays attached until the container's process exits and ignores
SIGTERM too: a real client passes the signal on to the container and keeps waiting.

Supported: ``run [--rm] [--name N] [-w DIR] IMAGE ...``, ``ps [-a] -q --no-trunc``,
``inspect --type container --format FMT ID...`` and ``stop -t N ID...``. ``$FAKE_DOCKER_INTERRUPT``
is a process id that ``run`` sends SIGINT once its container exists, as a user pressing
Ctrl-C would; with ``$FAKE_DOCKER_STOP_FAILS`` set, ``stop`` fails.
"""

from __future__ import annotations

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
    return [json.loads(p.read_text()) for p in sorted(STATE.glob("*.json"))]


def save(rec: dict[str, Any]) -> None:
    (STATE / f"{rec['id']}.json").write_text(json.dumps(rec))


def find(cid: str) -> dict[str, Any] | None:
    return next((r for r in records() if r["id"] == cid), None)


def ps(args: list[str]) -> int:
    show_all = any(a == "--all" or (a[:1] == "-" and a[1:2] != "-" and "a" in a) for a in args)
    for r in records():
        if show_all or (r["running"] and alive(r["pid"])):
            print(r["id"])
    return 0


def options(args: list[str], *names: str) -> tuple[dict[str, str], list[str]]:
    """The values of the named options, and the other arguments."""
    opts: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in names:
            opts[args[i]] = args[i + 1]
            i += 1
        else:
            rest.append(args[i])
        i += 1
    return opts, rest


def inspect(args: list[str]) -> int:
    opts, ids = options(args, "--format", "--type")
    fmt = opts["--format"]
    code = 0
    for cid in ids:
        r = find(cid)
        if r is None:
            print(f"Error: No such object: {cid}", file=sys.stderr)
            code = 1
            continue
        out = fmt.replace("{{.Id}}", r["id"]).replace("{{.Config.WorkingDir}}", r["workdir"])
        if "{{" in out:
            print(f"fake docker cannot format {fmt!r}", file=sys.stderr)
            return 2
        print(out)
    return code


def stop(args: list[str]) -> int:
    opts, ids = options(args, "-t")
    grace = float(opts.get("-t", "10"))
    code = 0
    for cid in ids:
        r = find(cid)
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
        r["running"] = False
        r["stopped_by"] = "stop"
        save(r)
        print(cid)
    return code


def run(args: list[str]) -> int:
    opts, rest = options(args, "-w", "--name")
    workdir = opts.get("-w", "/")
    rm = "--rm" in rest
    name = opts.get("--name", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", CONTAINER],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rec = {
        "id": uuid.uuid4().hex + uuid.uuid4().hex,
        "name": name,
        "workdir": workdir,
        "pid": proc.pid,
        "running": True,
        "client": os.getpid(),
    }
    save(rec)
    with (STATE / "created.log").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.environ.get("FAKE_DOCKER_INTERRUPT"):
        os.kill(int(os.environ["FAKE_DOCKER_INTERRUPT"]), signal.SIGINT)
    while proc.poll() is None:
        time.sleep(0.05)
    if rm:
        (STATE / f"{rec['id']}.json").unlink(missing_ok=True)
    return 0


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    cmd, args = sys.argv[1], sys.argv[2:]
    handlers = {"ps": ps, "inspect": inspect, "stop": stop, "run": run}
    if cmd not in handlers:
        print(f"fake docker: unknown command {cmd}", file=sys.stderr)
        return 2
    return handlers[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
