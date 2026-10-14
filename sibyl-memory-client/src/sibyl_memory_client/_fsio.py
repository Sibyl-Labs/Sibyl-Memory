"""Small file primitives shared by the skills installer and the lifecycle
(save and debloat) modules.

Every writer here is: symlink-refusing (the target and its parent directory),
mkstemp in the target directory + fsync + ``os.replace`` (atomic, unique per
process), mode 0600 by default. Every reader is bounded. Nothing here opens a
network connection or reads an environment variable.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


class RefusedPath(OSError):
    """A path the plugin will not write through (a symlink, a directory, a
    parent that is a symlink). ``reason`` is safe to print."""

    def __init__(self, reason: str, path: Path | str) -> None:
        super().__init__(f"{reason}: {path}")
        self.reason = reason
        self.path = str(path)


def ensure_private_dir(d: Path) -> Path:
    """Create ``d`` (and parents) 0700 when missing; refuse a symlinked ``d``."""
    d = Path(d)
    if d.is_symlink():
        raise RefusedPath("directory is a symlink", d)
    if d.exists() and not d.is_dir():
        raise RefusedPath("not a directory", d)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def ensure_dir(d: Path) -> Path:
    """Create ``d`` when missing (default mode); refuse a symlinked ``d``."""
    d = Path(d)
    if d.is_symlink():
        raise RefusedPath("directory is a symlink", d)
    if d.exists() and not d.is_dir():
        raise RefusedPath("not a directory", d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def fsync_path(p: Path) -> None:
    try:
        flags = getattr(os, "O_DIRECTORY", 0) if Path(p).is_dir() else 0
        fd = os.open(str(p), os.O_RDONLY | flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> Path:
    """Write ``data`` to ``path`` atomically. Refuses a symlinked target, a
    directory at the target, and a symlinked parent directory."""
    path = Path(path)
    if path.is_symlink():
        raise RefusedPath("target is a symlink", path)
    if path.exists() and not path.is_file():
        raise RefusedPath("target is not a regular file", path)
    parent = path.parent
    if parent.is_symlink():
        raise RefusedPath("directory is a symlink", parent)
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix="." + path.name + ".",
                               suffix=".sibyl-tmp")
    try:
        try:
            os.fchmod(fd, mode)
        except (OSError, AttributeError):
            pass
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_path(parent)
    return path


def read_bounded(path: Path, limit: int) -> bytes | None:
    """Up to ``limit`` bytes of a regular, non-symlinked file, or None when the
    file is missing, a symlink, not a regular file, unreadable, or LARGER than
    ``limit`` (never reads past ``limit + 1`` bytes)."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data
