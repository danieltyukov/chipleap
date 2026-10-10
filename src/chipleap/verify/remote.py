"""Run a verification tool on another machine over ssh.

Commercial EDA tools usually live on a design server, not on the laptop that runs
ChipLeap. A remote run copies one work directory to a fresh scratch directory on the
host, runs the command there in a new shell that first runs the setup snippet (the
scripts that put the tools on the path and point at the licence server), copies the
directory back and deletes the scratch directory.

Every run gets its own shell, so a site rule such as "one PDK per shell" holds by
construction. Setup snippets written for csh (``source x.csh``, ``setenv``) run under
tcsh, everything else under bash. Only key-based ssh is used: ``BatchMode`` turns a
password prompt into an error instead of a hang.

    CHIPLEAP_REMOTE=icdcs11
    CHIPLEAP_REMOTE_SETUP="source /eda/scripts/flexlm.sh; source /eda/.../GENUS_21.10.000_RHELx86.sh"
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


class RemoteError(RuntimeError):
    pass


def is_csh(setup: str) -> bool:
    """Whether a setup snippet is written for csh: it sources .csh files or uses setenv."""
    return bool(re.search(r"\.csh\b", setup) or re.search(r"(^|[;\s])(setenv|set\s+path)\b", setup))


def wrap(command: str, cwd: str, setup: str = "") -> str:
    """The command line the remote login shell runs: a fresh bash or tcsh that runs the
    setup snippet, changes into ``cwd`` and runs ``command``."""
    parts = [p for p in (setup.strip().rstrip(";"), f"cd {shlex.quote(cwd)}") if p]
    inner = "; ".join(parts) + f" && {command}"
    shell = "tcsh -f -c" if is_csh(setup) else "bash -c"
    return f"{shell} {shlex.quote(inner)}"


@dataclass(frozen=True)
class Remote:
    host: str
    setup: str = ""
    scratch_root: str = "/tmp"

    @staticmethod
    def from_env(host: str | None = None, setup: str | None = None) -> Remote | None:
        h = host or os.environ.get("CHIPLEAP_REMOTE", "")
        if not h:
            return None
        s = setup if setup is not None else os.environ.get("CHIPLEAP_REMOTE_SETUP", "")
        return Remote(h, s)

    def _ssh(self, command: str, timeout: float) -> subprocess.CompletedProcess[str]:
        cmd = ["ssh", *SSH_OPTS, self.host, command]
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
        # No redirections: the probe has to read the same in bash and csh.
        probe = "; ".join(f"echo @{n}; which {n}" for n in names)
        out = self._ssh(wrap(probe, "/", self.setup), timeout)
        return parse_which(out.stdout + "\n" + out.stderr, names)

    def run(
        self, local_dir: Path, command: str, timeout: float = 3600
    ) -> subprocess.CompletedProcess[str]:
        """Copy ``local_dir`` to the host, run ``command`` in it and copy the results back."""
        made = self._ssh(f"mktemp -d {shlex.quote(self.scratch_root)}/chipleap-XXXXXXXX", 60)
        scratch = made.stdout.strip()
        if made.returncode != 0 or not scratch.startswith("/"):
            raise RemoteError(f"{self.host}: cannot make a scratch directory: {made.stderr}")
        try:
            self._copy(f"{local_dir}/", f"{self.host}:{scratch}/")
            out = self._ssh(wrap(command, scratch, self.setup), timeout)
            self._copy(f"{self.host}:{scratch}/", f"{local_dir}/")
            return out
        finally:
            self._ssh(f"rm -rf {shlex.quote(scratch)}", 120)

    def _copy(self, src: str, dst: str) -> None:
        ssh = "ssh " + " ".join(SSH_OPTS)
        if shutil.which("rsync"):
            cmd = ["rsync", "-a", "-e", ssh, src, dst]
        else:  # scp copies a directory's contents with the trailing "/."
            cmd = ["scp", *SSH_OPTS, "-r", "-q", src.rstrip("/") + "/.", dst.rstrip("/")]
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


def run_tool(
    command: str, workdir: Path, remote: Remote | None, timeout: float = 3600
) -> subprocess.CompletedProcess[str]:
    """Run a tool command in ``workdir``, locally or on ``remote``."""
    if remote is not None:
        return remote.run(workdir, command, timeout)
    try:
        return subprocess.run(
            ["bash", "-c", command],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"{command.split()[0]} timed out after {timeout:.0f} s") from exc
