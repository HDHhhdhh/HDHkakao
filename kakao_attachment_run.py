"""Run-owned attachment copies, shared by every recipient and reconnect."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Iterable

from kakao_direct import DirectSendError, MAX_DIRECT_ATTACHMENT_BYTES, validate_direct_attachments


_COPY_CHUNK_BYTES = 1024 * 1024


def _bundle_key(paths: Iterable[Path]) -> tuple[str, ...]:
    # Do not resolve or stat originals after preparation: users may move them.
    return tuple(os.path.normcase(os.path.abspath(path)) for path in paths)


def _version(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class AttachmentRunSnapshot:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="kakao-attachments-")
        self._root = Path(self._temporary.name).resolve()
        self._bundles: dict[tuple[str, ...], tuple[Path, ...]] = {}
        self._closed = False
        self._prepared = False

    def prepare(
        self, bundles: Iterable[tuple[Path, ...]], *, cancel_check: Callable[[], None],
    ) -> None:
        if self._closed or self._prepared:
            raise RuntimeError("attachment run snapshot is not available for preparation")
        unique: dict[Path, Path] = {}
        try:
            for bundle in bundles:
                cancel_check()
                key = _bundle_key(bundle)
                if key in self._bundles:
                    continue
                originals = validate_direct_attachments(bundle)
                frozen = []
                for original in originals:
                    cancel_check()
                    if original not in unique:
                        # Keep each original filename, including equal names in
                        # different folders, without putting private paths in logs.
                        directory = self._root / str(len(unique))
                        directory.mkdir()
                        target = directory / original.name
                        self._copy(original, target, cancel_check)
                        unique[original] = target
                    frozen.append(unique[original])
                validate_direct_attachments(tuple(frozen))
                self._bundles[key] = tuple(frozen)
            cancel_check()
            self._prepared = True
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _copy(source: Path, target: Path, cancel_check: Callable[[], None]) -> None:
        try:
            with source.open("rb") as reader, target.open("xb") as writer:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_DIRECT_ATTACHMENT_BYTES:
                    raise DirectSendError(code="attachment_size_invalid")
                remaining = before.st_size
                while remaining:
                    cancel_check()
                    chunk = reader.read(min(_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise DirectSendError(code="attachment_changed")
                    writer.write(chunk)
                    remaining -= len(chunk)
                if reader.read(1) or _version(os.fstat(reader.fileno())) != _version(before):
                    raise DirectSendError(code="attachment_changed")
                # Windows/Python can expose different ctime semantics through
                # stat versus fstat. Compare ctime only between handle reads;
                # path identity, size and mtime still detect a replaced source.
                if _version(source.stat())[:4] != _version(before)[:4]:
                    raise DirectSendError(code="attachment_changed")
        except OSError:
            raise DirectSendError("첨부 준비 중 파일을 읽거나 복사할 수 없습니다.", code="attachment_read_failed") from None

    def paths_for(self, attachments: tuple[Path, ...]) -> tuple[Path, ...]:
        if self._closed or not self._prepared:
            raise RuntimeError("attachment run snapshot is not prepared")
        return self._bundles[_bundle_key(attachments)]

    @property
    def bundles(self) -> tuple[tuple[Path, ...], ...]:
        return tuple(dict.fromkeys(self._bundles.values()))

    def close(self) -> None:
        if self._closed:
            return
        # TemporaryDirectory removes only this explicitly named managed folder.
        if self._root.parent != Path(tempfile.gettempdir()).resolve() or not self._root.name.startswith("kakao-attachments-"):
            raise RuntimeError("unexpected attachment temporary directory")
        self._temporary.cleanup()
        self._closed = True
        self._bundles.clear()
