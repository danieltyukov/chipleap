"""Run a lint or synthesis tool so that it can read only what it needs.

``chipleap.rtl.project`` refuses an `include or a $readmem file that leads outside the
project, but a check of the text cannot see every file a tool will open: Verilator and
slang follow an `include that a macro builds, and Yosys reads a $readmemh file that a
parameter names. So on Linux with Landlock (5.13 or newer, enabled on Ubuntu, Debian and
Fedora kernels) the tools that lint and synthesize a project, Verilator, slang, sv2v and
Yosys with its ABC, run with read access to the system directories, their own
installation, the project and the directories the user allowed, the liberty and a
private temporary directory. Anything else, the rest of the home directory included,
fails to open, so its contents cannot reach a diagnostic or a netlist.

Only reading is restricted: what a tool reads is what it can repeat. Writing and running
programs are not. The tool is started through this file, run as a script by the same
Python in isolated mode without site packages (so nothing in the project's directory can
stand in for a module), which restricts itself and then executes the tool. A pre-exec
hook in this process would not be safe beside the MCP server's threads.

Simulation is not confined: it runs the project's tests, which are code. Neither is a
tool that is a container wrapper, whose container already limits what it sees.
``CHIPLEAP_CONFINE=0`` turns confinement off for a tool that needs files elsewhere.

This file is also the launcher, so it imports nothing outside the standard library.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from functools import lru_cache
from pathlib import Path

ENV = "CHIPLEAP_CONFINE"
# Landlock's system calls have the same numbers on every Linux architecture.
_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_READ_FILE, _READ_DIR = 1 << 2, 1 << 3
# What a tool needs from the system: libraries, configuration, interpreters (Verilator's
# front end is Perl) and a few devices. /home, /root, /tmp, /run, /var, /mnt and /media
# are left out, and so is /proc, which shows other processes' environments.
SYSTEM = (
    "/usr",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/bin",
    "/sbin",
    "/etc",
    "/opt",
    "/sys",
    "/nix",
    "/gnu",
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
)


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


@lru_cache(maxsize=1)
def abi() -> int:
    """Landlock's ABI version on this machine; 0 where there is none."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        version = _libc().syscall(
            ctypes.c_long(_CREATE_RULESET),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(_CREATE_RULESET_VERSION),
        )
    except (OSError, AttributeError):
        return 0
    return max(0, int(version))


def enabled() -> bool:
    """Whether tools run confined here."""
    return os.environ.get(ENV, "1") != "0" and abi() > 0


def installation(tool: str | Path) -> list[Path]:
    """What a tool reads of its own besides the system: its directory, and its prefix when
    that is a directory of its own. In a shared prefix (the home directory, ~/.local) only
    the tool's own share, lib and libexec directories, so a tool in ~/.local/bin does not
    open the rest of ~/.local; a tool directly in the home directory gets only itself."""
    exe = Path(shutil.which(tool) or tool).resolve()
    home = Path.home().resolve()
    if home.is_relative_to(exe.parent):
        return [exe]
    prefix = exe.parent.parent
    if prefix in (home, home / ".local") or home.is_relative_to(prefix):
        return [exe.parent, *(prefix / d / exe.name for d in ("share", "lib", "libexec"))]
    return [exe.parent, prefix]


@contextlib.contextmanager
def confined(
    cmd: Sequence[str], read: Iterable[Path]
) -> Iterator[tuple[list[str], dict[str, str]]]:
    """The command to run ``cmd`` with, and its environment, so that it can read only
    ``read`` (files or directories), the system and its own installation. TMPDIR points
    at a private directory it may use, removed afterwards. Unchanged when Landlock is not
    available, CHIPLEAP_CONFINE is 0 or the tool is a container wrapper."""
    from chipleap.verify.tools import is_wrapper  # not at the top: this is also the launcher

    env = dict(os.environ)
    if not enabled() or is_wrapper(cmd[0]):
        yield list(cmd), env
        return
    with tempfile.TemporaryDirectory(prefix="chipleap-tool-") as tmp:
        paths = [*SYSTEM, tmp, *map(str, read), *map(str, installation(cmd[0]))]
        if env.get("VERILATOR_ROOT"):
            paths.append(env["VERILATOR_ROOT"])
        rules = sorted({p for p in paths if os.path.exists(p)})
        launcher = [sys.executable, "-I", "-S", str(Path(__file__).resolve())]
        yield [*launcher, json.dumps(rules), "--", *cmd], {**env, "TMPDIR": tmp}


def _restrict(paths: Iterable[str]) -> None:
    """Allow this process, and what it executes, to read only ``paths``."""
    libc = _libc()
    attr = struct.pack("=Q", _READ_FILE | _READ_DIR)  # struct landlock_ruleset_attr
    ruleset = libc.syscall(
        ctypes.c_long(_CREATE_RULESET),
        ctypes.c_char_p(attr),
        ctypes.c_size_t(len(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    for p in paths:
        try:
            fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            continue  # gone since the list was made: nothing to allow
        try:
            # A rule on a file may grant only what applies to files.
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            # struct landlock_path_beneath_attr: a u64 and an s32, packed.
            rule = struct.pack("=Qi", _READ_FILE | (_READ_DIR if directory else 0), fd)
            added = libc.syscall(
                ctypes.c_long(_ADD_RULE),
                ctypes.c_int(ruleset),
                ctypes.c_int(_RULE_PATH_BENEATH),
                ctypes.c_char_p(rule),
                ctypes.c_uint32(0),
            )
            if added < 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {p}")
        finally:
            os.close(fd)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
    if libc.syscall(ctypes.c_long(_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)) < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    os.close(ruleset)


def main(argv: list[str]) -> None:
    """``confine.py RULES -- COMMAND...``: restrict, then become the command."""
    if len(argv) < 3 or argv[1] != "--":
        sys.exit("usage: confine.py RULES -- COMMAND...")
    cmd = argv[2:]
    try:
        _restrict(json.loads(argv[0]))
    except (OSError, ValueError) as exc:
        # Confinement was asked for, so the tool does not run without it.
        sys.exit(f"chipleap: cannot confine {cmd[0]}: {exc}")
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        sys.exit(
            f"chipleap: cannot run {cmd[0]} confined: {exc.strerror}; it may need files "
            f"outside what it is allowed to read ({ENV}=0 runs it without confinement)"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
