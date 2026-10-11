"""Proof that ChipLeap wrote what a later step reads back from ``build/``.

Place and route, an ECO and the power step leave results that later steps reuse: the
routed DEF, netlist, constraints and parasitics, LibreLane's configuration and state, the
gate-level VCD. A project's ``build/`` may come from someone else's repository, and the
tools run what those files say (a LibreLane configuration names Tcl scripts, an SDC file
is Tcl, a netlist can hold initial blocks), so a step reuses a file only when ChipLeap on
this machine recorded it.

When a step finishes, ``seal`` writes a manifest with the SHA-256 of every file it
produced, authenticated with HMAC-SHA256 under a key that only this user's ChipLeap reads
(``~/.local/state/chipleap/build.key``, made on first use; ``CHIPLEAP_BUILD_KEY`` names
another file). ``open_sealed`` refuses a manifest that is missing or whose code does not
verify, which is what an edited manifest or one from another machine looks like, and a
``Sealed`` result refuses a file the manifest does not list or whose hash differs. A file
a tool will read is copied with the hash checked on the bytes copied, so the tool reads
what was checked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir

FORMAT = "chipleap.manifest/v1"
KEY_BYTES = 32
# A manifest lists the files of one run; this many is far beyond any LibreLane run.
MAX_FILES = 100_000
MANIFEST_LIMIT = 32 << 20
READ_LIMIT = 256 << 20
_CHUNK = 1 << 20
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class SealError(ValueError):
    """A result in ``build/`` that ChipLeap cannot show it wrote."""


def key_file() -> Path:
    env = os.environ.get("CHIPLEAP_BUILD_KEY")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "chipleap" / "build.key"


def key() -> bytes:
    """This user's manifest key, made on first use. Two processes making it at once agree:
    the key is written to a file of its own and linked into place, and the loser reads
    the winner's."""
    path = key_file()
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(KEY_BYTES) + "\n")
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        tmp.unlink()
    return _read_key(path)


def _read_key(path: Path) -> bytes:
    text = path.read_text().strip()
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raw = b""
    if len(raw) < 16:
        raise SealError(f"{path} is not a ChipLeap build key; remove it to make a new one")
    return raw


def _mac(body: dict[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(key(), text.encode(), hashlib.sha256).hexdigest()


def file_digest(path: Path, limit: int = READ_LIMIT) -> str:
    """The SHA-256 of a regular file, opened without following a symlink."""
    h = hashlib.sha256()
    for chunk in _chunks(path, limit):
        h.update(chunk)
    return h.hexdigest()


def _open_regular(path: Path, limit: int) -> int:
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise SealError(f"{path.name} is not a regular file")
    if st.st_size > limit:
        os.close(fd)
        raise SealError(f"{path.name} is larger than {limit >> 20} MB")
    return fd


def _chunks(path: Path, limit: int) -> Iterable[bytes]:
    fd = _open_regular(path, limit)
    with os.fdopen(fd, "rb") as fh:
        read = 0
        while chunk := fh.read(_CHUNK):
            read += len(chunk)
            if read > limit:
                raise SealError(f"{path.name} is larger than {limit >> 20} MB")
            yield chunk


def _walk(root: Path, part: str) -> Iterable[tuple[str, Path]]:
    """Every regular file under ``root/part`` (or ``part`` itself), relative to ``root``,
    without following a symlink: LibreLane writes none, and one is never recorded."""
    start = root / part
    try:
        st = os.lstat(start)
    except FileNotFoundError:
        return
    if stat.S_ISREG(st.st_mode):
        yield part, start
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    todo = [start]
    while todo:
        d = todo.pop()
        with os.scandir(d) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                todo.append(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                p = Path(e.path)
                yield p.relative_to(root).as_posix(), p


def seal(
    root: Path, name: str, what: str, parts: Iterable[str], extra: dict[str, Any] | None = None
) -> Sealed:
    """Record every file under ``root/part`` for each of ``parts`` in ``root/name``, as the
    result of ``what`` (``pnr``, ``eco``, ``gl``). ``extra`` is data the manifest vouches
    for along with the files."""
    files: dict[str, str] = {}
    for part in parts:
        for rel, path in _walk(root, part):
            if rel == name:
                continue
            # A file larger than any step reads back is left out, not recorded: a later
            # step that asks for it is refused.
            if os.lstat(path).st_size > READ_LIMIT:
                continue
            files[rel] = file_digest(path)
            if len(files) > MAX_FILES:
                raise SealError(f"more than {MAX_FILES} files to record in {root}")
    body = {"format": FORMAT, "what": what, "files": files, "extra": extra or {}}
    mac = _mac(body)
    builddir.write_text(root, name, json.dumps({**body, "mac": mac}, indent=1, sort_keys=True))
    return Sealed(root.resolve(), what, files, extra or {}, mac)


def unseal(root: Path, name: str) -> None:
    """Remove a manifest before its files change, so a run that stops halfway leaves none."""
    with suppress(FileNotFoundError):
        st = os.lstat(root / name)
        if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
            (root / name).unlink()


def open_sealed(root: Path, name: str, what: str) -> Sealed:
    """The manifest ``root/name`` for a result of ``what``, if this user's ChipLeap wrote it."""
    path = root / name
    try:
        data = json.loads(b"".join(_chunks(path, MANIFEST_LIMIT)))
    except FileNotFoundError:
        raise SealError(
            f"{name} is missing: ChipLeap did not make {_where(root)} on this machine"
        ) from None
    except (OSError, ValueError) as exc:
        raise SealError(f"{name} cannot be read: {exc}") from None
    if not isinstance(data, dict):
        raise SealError(f"{name} is not a manifest")
    body = {k: data.get(k) for k in ("format", "what", "files", "extra")}
    files = body["files"]
    if (
        body["format"] != FORMAT
        or body["what"] != what
        or not isinstance(files, dict)
        or not isinstance(body["extra"], dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        raise SealError(f"{name} is not a ChipLeap manifest of {what}")
    mac = data.get("mac")
    if not isinstance(mac, str) or not hmac.compare_digest(mac, _mac(body)):
        raise SealError(
            f"{name} was not written by ChipLeap on this machine (its code does not verify): "
            f"run the step that makes {_where(root)} again"
        )
    return Sealed(root.resolve(), what, dict(files), dict(body["extra"]), mac)


def _where(root: Path) -> str:
    parts = root.parts
    return "/".join(parts[parts.index("build") :]) if "build" in parts else root.name


@dataclass(frozen=True)
class Sealed:
    """A result ChipLeap recorded: its files by path relative to ``root`` and their hashes.
    ``mac`` identifies the result, so a later result can name the one it was made from."""

    root: Path
    what: str
    files: dict[str, str] = field(repr=False)
    extra: dict[str, Any] = field(repr=False)
    mac: str

    def locate(self, path: str | Path) -> tuple[Path, str]:
        """``path`` (absolute, or relative to ``root``) as a recorded file and its hash."""
        p = Path(path)
        p = p if p.is_absolute() else self.root / p
        # Lexically, without resolving: a recorded file is never reached through a symlink.
        rel_parts = os.path.normpath(p).split(os.sep)
        root_parts = str(self.root).split(os.sep)
        if rel_parts[: len(root_parts)] != root_parts:
            raise SealError(f"{path} is not a file of {_where(self.root)}")
        rel = "/".join(rel_parts[len(root_parts) :])
        digest = self.files.get(rel)
        if digest is None:
            raise SealError(f"{rel} is not a file ChipLeap recorded in {_where(self.root)}")
        return self.root / rel, digest

    def check(self, path: str | Path) -> Path:
        """A recorded file whose contents still have the recorded hash, for a tool that
        reads it by name."""
        real, digest = self.locate(path)
        try:
            found = file_digest(real)
        except FileNotFoundError:
            raise SealError(f"{real.name} is missing from {_where(self.root)}") from None
        if not hmac.compare_digest(found, digest):
            raise SealError(
                f"{real.relative_to(self.root)} changed after ChipLeap recorded it in "
                f"{_where(self.root)}"
            )
        return real

    def read_bytes(self, path: str | Path, limit: int = READ_LIMIT) -> bytes:
        real, digest = self.locate(path)
        try:
            data = b"".join(_chunks(real, limit))
        except FileNotFoundError:
            raise SealError(f"{real.name} is missing from {_where(self.root)}") from None
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise SealError(
                f"{real.relative_to(self.root)} changed after ChipLeap recorded it in "
                f"{_where(self.root)}"
            )
        return data

    def read_text(self, path: str | Path, limit: int = READ_LIMIT) -> str:
        return self.read_bytes(path, limit).decode(errors="replace")

    def read_json(self, path: str | Path, limit: int = 64 << 20) -> Any:
        try:
            return json.loads(self.read_bytes(path, limit))
        except ValueError as exc:
            raise SealError(f"{Path(path).name} is not valid JSON: {exc}") from None

    def copy(self, path: str | Path, dest: Path) -> None:
        """Copy a recorded file to ``dest``, checking the hash of the bytes copied."""
        real, digest = self.locate(path)
        copy_file(real, dest, digest)


def copy_file(src: Path, dest: Path, digest: str | None = None, limit: int = READ_LIMIT) -> None:
    """Copy the regular file ``src`` to the new file ``dest``, never following a symlink at
    either end; with ``digest``, the copy is removed and refused unless its SHA-256 is
    that."""
    h = hashlib.sha256()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in _chunks(src, limit):
                h.update(chunk)
                out.write(chunk)
        if digest is not None and not hmac.compare_digest(h.hexdigest(), digest):
            raise SealError(f"{src.name} changed after ChipLeap recorded it")
    except BaseException:
        dest.unlink()
        raise
