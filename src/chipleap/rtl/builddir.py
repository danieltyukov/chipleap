"""Work and output directories that the files of a project or a bundle cannot redirect.

A project's ``build/`` and a bundle's ``verify/`` are inside a directory that may come
from someone else's repository. If ``build``, ``build/pnr`` or a file in one of them were
a symlink to the home directory, emptying a work directory or writing a report would
delete or overwrite files there. So ChipLeap never reaches them through a symlink:

* ``work_dir`` goes down from the directory the user named one component at a time,
  opening each relative to the one before without following a symlink (``O_NOFOLLOW``).
  A symlink or a file where a directory belongs is refused, and a missing directory is
  created in place. Then every symlink inside the work directory, at any depth, is
  removed, so nothing that ChipLeap or a tool writes there later can lead out of it;
  with ``empty``, everything inside is removed. Removal goes through the same
  descriptors, so it cannot leave the directory that was checked.
* ``write_text`` and ``write_bytes`` write a file into a directory that holds more than
  ChipLeap's own files (a bundle, ``build/verify``, a project): the directories on the
  way are reached as above, and the file is opened without following a symlink.
* ``output_dir`` checks a directory the user names for output (``-o out``), which may not
  be a symlink. ChipLeap marks a directory it creates (``MARKER``) and empties one only
  if it carries that mark, so a directory of the user's is never emptied.

Where the system has no descriptor-relative calls (Windows) the same checks are made on
paths, each just before the path is used.

A race is left. A step hands the checked directory to its tools by name, so a process
that swaps a directory for a symlink after the check, while the step runs, is not
stopped; ``work_dir`` only makes sure the path still leads to the directory it checked
when it returns. Swapping takes a program already running as the user (a project's own
tests are one, which is why simulation is only for projects you trust); the files of a
repository cannot do it.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePath

MARKER = ".chipleap"
_MARK = "ChipLeap created this directory for its output.\n"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIR = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_OPEN_FILE = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC
# Descriptor-relative calls: Linux, macOS and the BSDs have them.
_FD = bool(_NOFOLLOW and _DIRECTORY) and (
    {os.open, os.mkdir, os.rmdir, os.unlink, os.stat} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)


class BuildDirError(ValueError):
    """A work or output directory, or a file in one, that ChipLeap will not use."""


def work_dir(root: Path, *parts: str, empty: bool = False) -> Path:
    """``root/parts...``, ready for a step's files, as a resolved path inside ``root``.

    ``root`` is the directory the user named (a project, a bundle, or a work directory
    this function returned), and at least one part is needed: ``root`` itself is never
    cleaned. Below it each part must be a directory, not a symlink; a missing one is
    created. Every symlink inside is then removed, and with ``empty`` everything is."""
    names = _names(parts)
    if not names:
        raise ValueError("a work directory is a directory below the root")
    base = Path(root).resolve()
    target = base.joinpath(*names)
    if _FD:
        with _descend(base, names) as fd:
            _clean(fd, empty)
            # The path the step will use must lead to the directory checked here.
            try:
                same = os.path.samestat(os.fstat(fd), os.stat(target))
            except OSError:
                same = False
            if not same:
                raise BuildDirError(f"{target} changed while ChipLeap was checking it")
    else:
        _descend_paths(base, names)
        _clean_path(target, empty)
    if not target.resolve().is_relative_to(base):
        raise BuildDirError(f"{target} leads outside {base}")
    return target


def output_dir(path: Path, *, empty: bool = False) -> Path:
    """A directory the user named for ChipLeap's output (``-o out``), as a resolved path.

    It may not be a symlink. A missing one is created, parents and all, and marked as
    ChipLeap's with ``MARKER``. ``empty`` removes what it holds, never following a
    symlink, but only from a directory that carries the mark: one without it is the
    user's, and is refused."""
    p = Path(path)
    made = False
    with suppress(FileExistsError):
        p.mkdir(parents=True)
        made = True
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        raise BuildDirError(f"{p} is a symlink; name the directory it leads to instead")
    _refuse(p, st, directory=True)
    if made:
        write_text(p, MARKER, _MARK)
    if empty:
        try:
            mark = os.lstat(p / MARKER)
        except FileNotFoundError:
            mark = None
        if mark is None or not stat.S_ISREG(mark.st_mode):
            raise BuildDirError(
                f"{p} was not made by ChipLeap (it has no {MARKER} file), so ChipLeap does "
                "not empty it"
            )
        if _FD:
            fd = os.open(p, _OPEN_DIR)
            try:
                if not os.path.samestat(st, os.fstat(fd)):
                    raise BuildDirError(f"{p} changed while ChipLeap was checking it")
                _clean(fd, True, keep=MARKER)
            finally:
                os.close(fd)
        else:
            _clean_path(p, True, keep=MARKER)
    return p.resolve()


def write_text(root: Path, rel: str | PurePath, text: str) -> Path:
    """``write_bytes`` with ``text`` in UTF-8."""
    return write_bytes(root, rel, text.encode())


def write_bytes(root: Path, rel: str | PurePath, data: bytes) -> Path:
    """Write ``data`` to ``root/rel`` and return that path. The directories in ``rel`` are
    reached, or created, below ``root`` as ``work_dir`` reaches them, and the file is
    opened without following a symlink: a symlink anywhere in ``rel`` is refused, never
    written through."""
    *dirs, name = _names(PurePath(rel).parts)
    path = Path(root).joinpath(*dirs, name)
    if _FD:
        with _descend(Path(root), dirs) as d:
            try:
                fd = os.open(name, _OPEN_FILE, 0o666, dir_fd=d)
            except OSError:
                _refuse(path, os.stat(name, dir_fd=d, follow_symlinks=False), directory=False)
                raise
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
    else:
        _descend_paths(Path(root), dirs)
        with suppress(FileNotFoundError):
            _refuse(path, os.lstat(path), directory=False)
        path.write_bytes(data)
    return path


def _names(parts: Iterable[str]) -> list[str]:
    """``parts`` if each is a plain name: never empty, ``.``, ``..`` or holding a separator."""
    out = list(parts)
    for p in out:
        if p in ("", ".", "..") or "/" in p or os.sep in p or (os.altsep and os.altsep in p):
            raise ValueError(f"{p!r} is not a plain directory or file name")
    return out


def _refuse(path: Path, st: os.stat_result, directory: bool) -> None:
    """Raise if what ``st`` describes is not what ChipLeap may use at ``path``."""
    if stat.S_ISLNK(st.st_mode):
        raise BuildDirError(
            f"{path} is a symlink, and ChipLeap does not delete or write through one: "
            "remove the symlink and run again"
        )
    if directory and not stat.S_ISDIR(st.st_mode):
        raise BuildDirError(f"{path} is not a directory")
    if not directory and not stat.S_ISREG(st.st_mode):
        raise BuildDirError(f"{path} is not a regular file")


@contextmanager
def _descend(root: Path, names: list[str]) -> Iterator[int]:
    """A descriptor for ``root/names...``: ``root`` is opened as the user named it, each
    name below it relative to the one before, without following a symlink, and created
    first when missing."""
    fd = os.open(root, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        here = root
        for name in names:
            here = here / name
            with suppress(FileExistsError):  # mkdir never follows a symlink in its last part
                os.mkdir(name, dir_fd=fd)
            try:
                sub = os.open(name, _OPEN_DIR, dir_fd=fd)
            except OSError:
                _refuse(here, os.stat(name, dir_fd=fd, follow_symlinks=False), directory=True)
                raise
            os.close(fd)
            fd = sub
        yield fd
    finally:
        os.close(fd)


def _clean(fd: int, everything: bool, keep: str = "") -> None:
    """Remove every symlink in the directory open as ``fd``, at any depth, or with
    ``everything`` all it holds but ``keep``. Only through descriptors, so a directory
    swapped for a symlink meanwhile fails to open instead of being entered."""
    with os.scandir(fd) as it:
        entries = [(e.name, e.is_dir(follow_symlinks=False), e.is_symlink()) for e in it]
    for name, is_dir, is_link in entries:
        if name == keep:
            continue
        if is_dir:
            sub = os.open(name, _OPEN_DIR, dir_fd=fd)
            try:
                _clean(sub, everything)
            finally:
                os.close(sub)
            if everything:
                os.rmdir(name, dir_fd=fd)
        elif everything or is_link:
            os.unlink(name, dir_fd=fd)


def _descend_paths(root: Path, names: list[str]) -> None:
    """``_descend`` on paths, for systems without descriptor-relative calls."""
    here = root
    for name in names:
        here = here / name
        with suppress(FileExistsError):
            here.mkdir()
        _refuse(here, os.lstat(here), directory=True)


def _clean_path(path: Path, everything: bool, keep: str = "") -> None:
    """``_clean`` on paths."""
    with os.scandir(path) as it:
        entries = [(Path(e.path), e.is_dir(follow_symlinks=False), e.is_symlink()) for e in it]
    for p, is_dir, is_link in entries:
        if p.name == keep:
            continue
        if is_dir:
            _clean_path(p, everything)
            if everything:
                p.rmdir()
        elif everything or is_link:
            p.unlink()
