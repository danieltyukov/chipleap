"""Run a verification tool on another machine over ssh.

Commercial EDA tools usually live on a design server, not on the laptop that runs
ChipLeap. A remote run copies one work directory to a fresh scratch directory on the
host, runs the tool steps there in a new shell that first runs the setup snippet (the
scripts that put the tools on the path and point at the licence server), copies the
directory back and deletes the scratch directory.

Every run gets its own shell, so a site rule such as "one PDK per shell" holds by
construction. Setup snippets written for csh (``source x.csh``, ``setenv``) run under
tcsh, everything else under bash; the tool steps themselves always run in bash, started
from that shell so they inherit what the setup put in the environment.

The host and the setup snippet come only from the user: the command line or the
``CHIPLEAP_REMOTE`` and ``CHIPLEAP_REMOTE_SETUP`` environment variables, never from an
MCP client. The host must be an ssh alias or ``user@host``; it always follows ``--`` on
the ssh, rsync and scp command lines, and every remote command line is assembled from
quoted pieces. Locally nothing runs through a shell: each step is an argument vector.
Only key-based ssh is used, so a password prompt is an error instead of a hang.

    CHIPLEAP_REMOTE=eda-server
    CHIPLEAP_REMOTE_SETUP="source /opt/eda/licences.sh; source /opt/eda/cadence/genus.sh"
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?")
_SCRATCH_ROOT_RE = re.compile(r"/[A-Za-z0-9._/-]*")


class RemoteError(RuntimeError):
    pass


def check_host(host: str) -> str:
    """An ssh alias or ``user@host``, and nothing that ssh could read as an option."""
    if not HOST_RE.fullmatch(host):
        raise RemoteError(
            f"remote host {host!r} must be an ssh alias or user@host "
            "(letters, digits, '.', '_', '-', not starting with '-')"
        )
    return host


@dataclass(frozen=True)
class Step:
    """One tool invocation: an argument vector, and the file in the work directory that
    receives its output ("" keeps it for the caller)."""

    argv: tuple[str, ...]
    log: str = ""

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a step needs a program")
        if self.log and (Path(self.log).is_absolute() or ".." in Path(self.log).parts):
            raise ValueError(f"step log {self.log!r} must stay in the work directory")

    def shell(self) -> str:
        """The step as a bash command line, every piece quoted."""
        line = " ".join(shlex.quote(a) for a in self.argv)
        return f"{line} > {shlex.quote(self.log)} 2>&1" if self.log else line


def is_csh(setup: str) -> bool:
    """Whether a setup snippet is written for csh: it sources .csh files or uses setenv."""
    return bool(re.search(r"\.csh\b", setup) or re.search(r"(^|[;\s])(setenv|set\s+path)\b", setup))


def shell_line(bash_script: str, cwd: str, setup: str = "") -> str:
    """The command line the remote login shell runs: a fresh bash or tcsh runs the setup
    snippet and changes into ``cwd``, then bash runs ``bash_script`` with that
    environment."""
    parts = [p for p in (setup.strip().rstrip(";"), f"cd {shlex.quote(cwd)}") if p]
    inner = "; ".join(parts) + f" && exec bash -c {shlex.quote(bash_script)}"
    shell = "tcsh -f -c" if is_csh(setup) else "bash -c"
    return f"{shell} {shlex.quote(inner)}"


def wrap(steps: list[Step], cwd: str, setup: str = "") -> str:
    """The remote command line for ``steps``: in order, stopping at the first failure."""
    return shell_line(" && ".join(s.shell() for s in steps), cwd, setup)


@dataclass(frozen=True)
class Remote:
    host: str
    setup: str = ""
    scratch_root: str = "/tmp"

    def __post_init__(self) -> None:
        check_host(self.host)
        if not _SCRATCH_ROOT_RE.fullmatch(self.scratch_root) or ".." in self.scratch_root:
            raise RemoteError(f"scratch root {self.scratch_root!r} must be a plain absolute path")

    @staticmethod
    def from_env(host: str | None = None, setup: str | None = None) -> Remote | None:
        """The remote the user configured: arguments from the command line, else the
        ``CHIPLEAP_REMOTE`` and ``CHIPLEAP_REMOTE_SETUP`` environment variables."""
        h = host or os.environ.get("CHIPLEAP_REMOTE", "")
        if not h:
            return None
        s = setup if setup is not None else os.environ.get("CHIPLEAP_REMOTE_SETUP", "")
        return Remote(h, s)

    def _ssh(self, command: str, timeout: float) -> subprocess.CompletedProcess[str]:
        cmd = ["ssh", *SSH_OPTS, "--", self.host, command]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"{self.host}: no answer within {timeout:.0f} s") from exc
        if out.returncode == 255:
            raise RemoteError(
                f"cannot reach {self.host} over ssh with a key (BatchMode, no passwords): "
                + (out.stderr.strip().splitlines() or ["no message"])[-1]
            )
        return out

    def which(self, names: list[str], timeout: float = 60) -> dict[str, str]:
        """Where each tool is on the host's path after the setup snippet, if anywhere."""
        for n in names:
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", n):
                raise RemoteError(f"not a program name: {n!r}")
        # `which` fails for a missing tool; ";" keeps going so every name is probed.
        script = "; ".join(
            f"echo {shlex.quote('@' + n)}; which {shlex.quote(n)} 2>&1" for n in names
        )
        out = self._ssh(shell_line(script, "/", self.setup), timeout)
        return parse_which(out.stdout + "\n" + out.stderr, names)

    def run(
        self, local_dir: Path, steps: list[Step], timeout: float = 3600
    ) -> subprocess.CompletedProcess[str]:
        """Copy ``local_dir`` to the host, run ``steps`` in it and copy the results back."""
        root = self.scratch_root.rstrip("/") or "/"
        made = self._ssh(f"mktemp -d {shlex.quote(root + '/chipleap-XXXXXXXX')}", 60)
        scratch = made.stdout.strip()
        if made.returncode != 0 or not re.fullmatch(
            re.escape(root.rstrip("/")) + r"/chipleap-[A-Za-z0-9]{8}", scratch
        ):
            raise RemoteError(f"{self.host}: cannot make a scratch directory: {made.stderr}")
        try:
            self._copy(f"{local_dir}/", f"{self.host}:{scratch}/")
            out = self._ssh(wrap(steps, scratch, self.setup), timeout)
            self._copy(f"{self.host}:{scratch}/", f"{local_dir}/")
            return out
        finally:
            self._ssh(f"rm -rf -- {shlex.quote(scratch)}", 120)

    def _copy(self, src: str, dst: str) -> None:
        ssh = "ssh " + " ".join(SSH_OPTS)
        if shutil.which("rsync"):
            cmd = ["rsync", "-a", "-e", ssh, "--", src, dst]
        else:  # scp copies a directory's contents with the trailing "/."
            cmd = ["scp", *SSH_OPTS, "-r", "-q", "--", src.rstrip("/") + "/.", dst.rstrip("/")]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
        if out.returncode != 0:
            raise RemoteError(f"copy {src} -> {dst} failed: {out.stderr.strip()[-500:]}")


def parse_which(text: str, names: list[str]) -> dict[str, str]:
    """Paths from the output of ``echo @name; which name`` for each name."""
    found: dict[str, str] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("@") and line[1:] in names:
            current = line[1:]
        elif current and line.startswith("/") and current not in found:
            found[current] = line
    return found


def run_steps(
    steps: list[Step], workdir: Path, remote: Remote | None, timeout: float = 3600
) -> subprocess.CompletedProcess[str]:
    """Run tool steps in ``workdir``, here or on ``remote``, stopping at the first failure.

    Locally each step runs from its argument vector with no shell in between. The output
    of steps without a log file is returned, as a remote run returns it."""
    if remote is not None:
        return remote.run(workdir, steps, timeout)
    stdout: list[str] = []
    code = 0
    for step in steps:
        try:
            res = subprocess.run(
                list(step.argv),
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"{step.argv[0]} timed out after {timeout:.0f} s") from exc
        except FileNotFoundError as exc:
            raise RemoteError(f"{step.argv[0]} is not on the path") from exc
        if step.log:
            (workdir / step.log).write_text(res.stdout or "")
        else:
            stdout.append(res.stdout or "")
        code = res.returncode
        if code != 0:
            break
    return subprocess.CompletedProcess(["chipleap-steps"], code, "".join(stdout), "")
