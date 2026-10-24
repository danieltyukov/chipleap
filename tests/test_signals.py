"""SIGTERM and SIGHUP end every tool a ChipLeap command started in a session of its own,
as a time limit or Ctrl-C does, before ChipLeap exits. At their default the signals
ended ChipLeap on the spot and left IHP's DRC, Yosys or the solvers running."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chipleap import cli

# A ChipLeap command whose tool, started by chip.run as IHP's DRC is, runs in a session of
# its own with a child of its own, and reports both. The command stands in for `models`.
COMMAND = """
import signal, sys
from pathlib import Path
from chipleap import cli
from chipleap.chip import run

# As in a terminal: both signals at their default, whatever the test runner had.
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
work = Path(sys.argv[1])
tool = "sleep 600 & echo $! > stray.pid; echo $$ > tool.pid; wait"

def drc(args):
    run.run(["sh", "-c", tool], work, [work], work, "tool.log", timeout=600)
    return 0

cli.cmd_models = drc
sys.exit(cli.main(["models"]))
"""


def _alive(pid: int | None) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _pid(path: Path) -> int | None:
    text = path.read_text().strip() if path.is_file() else ""
    return int(text) if text else None


def _until(cond, within: float = 30.0) -> bool:
    end = time.monotonic() + within
    while not cond():
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
@pytest.mark.parametrize("name", ["TERM", "HUP"])
def test_a_signal_ends_the_tools_a_command_started_in_their_own_session(tmp_path, name):
    env = {**os.environ, "CHIPLEAP_CONFINE": "0"}
    proc = subprocess.Popen(
        [sys.executable, "-c", COMMAND, str(tmp_path)], env=env, stderr=subprocess.PIPE
    )
    pids: list[int | None] = []
    try:
        assert _until(lambda: _pid(tmp_path / "stray.pid") and _pid(tmp_path / "tool.pid"))
        pids = [_pid(tmp_path / "tool.pid"), _pid(tmp_path / "stray.pid")]
        assert all(_alive(p) for p in pids)
        proc.send_signal(getattr(signal, f"SIG{name}"))
        _, err = proc.communicate(timeout=60)
        # A killed orphan is reaped by init a moment later.
        assert _until(lambda: not any(_alive(p) for p in pids), within=10), pids
        assert proc.returncode == 128 + getattr(signal, f"SIG{name}"), err
    finally:
        proc.kill()
        proc.wait()
        for p in pids:
            if _alive(p) and b"sleep" in Path(f"/proc/{p}/cmdline").read_bytes():
                os.kill(p, signal.SIGKILL)


def test_the_mcp_server_keeps_the_signals_for_its_own_handlers(monkeypatch):
    """The server stops its runs on SIGTERM and SIGHUP itself (launch.stop_on_exit), which
    takes the signals only while they are at their default."""
    seen = {}

    def record(name):
        def cmd(args):
            seen[name] = signal.getsignal(signal.SIGTERM)
            return 0

        return cmd

    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        monkeypatch.setattr(cli, "cmd_mcp", record("mcp"))
        monkeypatch.setattr(cli, "cmd_models", record("models"))
        assert cli.main(["mcp"]) == 0
        assert cli.main(["models"]) == 0
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert seen["mcp"] is signal.SIG_DFL
    assert seen["models"] is not signal.SIG_DFL
