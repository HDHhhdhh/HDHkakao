"""Read Spoiler's published runtime manifest without guessing by cache age.

Spoiler publishes the canonical embedded manifest only after all payloads
have been extracted and verified. Hub accepts that publication and holds
deny-write handles through the child process lifetime on Windows.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


ACTIVE_RUNTIME_MANIFEST = "active-runtime.manifest"
_ENTRIES = (
    ("node.exe", "Spoiler.Runtime.Node"),
    ("spoiler-bridge.mjs", "Spoiler.Runtime.Bridge.Listener"),
    ("spoiler-auth.mjs", "Spoiler.Runtime.Bridge.Auth"),
)
_MAX_MANIFEST_BYTES = 4096


class RuntimeValidationError(RuntimeError):
    """A runtime has not been published or its contents no longer match."""


@dataclass(frozen=True)
class RuntimeEntry:
    filename: str
    size: int
    sha256: str


def parse_runtime_manifest(payload: bytes) -> tuple[str, tuple[RuntimeEntry, ...]]:
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise RuntimeValidationError("Runtime manifest is too large")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError:
        raise RuntimeValidationError("Runtime manifest encoding is invalid") from None
    lines = text.split("\n")
    if len(lines) != 6 or lines[0] != "SpoilerRuntimeManifest/1" or lines[-1] != "":
        raise RuntimeValidationError("Runtime manifest format is invalid")
    entries = []
    for line, (filename, resource) in zip(lines[2:5], _ENTRIES):
        fields = line.split("|")
        if (len(fields) != 4 or fields[:2] != [filename, resource]
                or re.fullmatch(r"[1-9][0-9]{0,18}", fields[2]) is None
                or re.fullmatch(r"[0-9a-f]{64}", fields[3]) is None):
            raise RuntimeValidationError("Runtime manifest entry is invalid")
        size = int(fields[2])
        if size > 2**63 - 1:
            raise RuntimeValidationError("Runtime manifest size is invalid")
        entries.append(RuntimeEntry(filename, size, fields[3]))
    seed = ("\n".join(lines[2:5]) + "\n").encode("utf-8")
    version = "v1-" + hashlib.sha256(seed).hexdigest()[:16]
    if lines[1] != "version=" + version:
        raise RuntimeValidationError("Runtime manifest version is invalid")
    return version, tuple(entries)


def _reject_links(path: Path) -> None:
    # resolve() alone accepts junctions. Reject them before following parents.
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeValidationError("Runtime path contains a link")


def _open_verified_read(path: Path, *, share: int = 0x1) -> BinaryIO:
    if os.name != "nt":
        return path.open("rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    # Sharing read access only prevents replacement/write while the child runs.
    handle = create(str(path), 0x80000000, share, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Could not open verified runtime")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


class VerifiedSpoilerRuntime:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._handles = ExitStack()
        try:
            requested = Path(root).absolute()
            _reject_links(requested)
            root_path = requested.resolve(strict=True)
            publication = root_path / ACTIVE_RUNTIME_MANIFEST
            _reject_links(publication)
            # Read with delete sharing allowed: an atomic publication update
            # can coexist with an already-starting older, still-valid lease.
            with _open_verified_read(publication, share=0x7) as stream:
                payload = stream.read(_MAX_MANIFEST_BYTES + 1)
            version, entries = parse_runtime_manifest(payload)
            directory = root_path / version
            _reject_links(directory)
            directory = directory.resolve(strict=True)
            if directory.parent != root_path:
                raise RuntimeValidationError("Runtime directory is outside the cache")
            if {item.name for item in directory.iterdir()} != {entry.filename for entry in entries}:
                raise RuntimeValidationError("Runtime directory is incomplete")
            for entry in entries:
                target = directory / entry.filename
                _reject_links(target)
                stream = self._handles.enter_context(_open_verified_read(target))
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RuntimeValidationError("Runtime payload is not a file")
                if os.fstat(stream.fileno()).st_size != entry.size:
                    raise RuntimeValidationError("Runtime payload size is invalid")
                hasher = hashlib.sha256()
                while chunk := stream.read(128 * 1024):
                    hasher.update(chunk)
                digest = hasher.hexdigest()
                if digest != entry.sha256:
                    raise RuntimeValidationError("Runtime payload hash is invalid")
            self.directory = directory
            self.version = version
            self.node = directory / "node.exe"
            self.auth = directory / "spoiler-auth.mjs"
        except Exception as error:
            self.close()
            if isinstance(error, RuntimeValidationError):
                raise
            raise RuntimeValidationError("Published Spoiler runtime is unavailable") from None

    def close(self) -> None:
        self._handles.close()

    def __enter__(self) -> VerifiedSpoilerRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
