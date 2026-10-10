from pathlib import Path
from unittest.mock import patch

import pytest

from kakao_attachment_run import AttachmentRunSnapshot
from kakao_direct import DirectSendError
from kakao_send_run import SendRunCancelled


def test_repeated_bundles_copy_once_and_keep_original_bytes_and_names(tmp_path):
    first = tmp_path / "file.pdf"
    first.write_bytes(b"%PDF- original")
    other = tmp_path / "other"
    other.mkdir()
    second = other / first.name
    second.write_bytes(b"%PDF- different")
    snapshot = AttachmentRunSnapshot()
    with patch.object(AttachmentRunSnapshot, "_copy", wraps=AttachmentRunSnapshot._copy) as copying:
        snapshot.prepare([(first,), (first,), (second, first)], cancel_check=lambda: None)
    assert copying.call_count == 2
    paths = snapshot.paths_for((second, first))
    assert [p.name for p in paths] == ["file.pdf", "file.pdf"]
    assert paths[0] != paths[1]
    first.unlink()
    assert snapshot.paths_for((first,))[0].read_bytes() == b"%PDF- original"
    assert paths[0].read_bytes() == b"%PDF- different"
    snapshot.close()
    snapshot.close()
    assert not any(p.exists() for p in paths)


def test_source_changed_while_copying_is_rejected_and_every_copy_removed(tmp_path):
    source = tmp_path / "file.pdf"
    source.write_bytes(b"%PDF- original")
    snapshot = AttachmentRunSnapshot()
    checks = 0
    def change_during_copy():
        nonlocal checks
        checks += 1
        if checks == 3:
            source.write_bytes(b"%PDF- changed plus more")
    with pytest.raises(DirectSendError) as caught:
        snapshot.prepare([(source,)], cancel_check=change_during_copy)
    assert caught.value.code == "attachment_changed"
    assert not snapshot._root.exists()


def test_cancel_during_chunked_copy_removes_partial_data(tmp_path):
    source = tmp_path / "file.pdf"
    source.write_bytes(b"%PDF- original")
    snapshot = AttachmentRunSnapshot()
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise SendRunCancelled()
    with patch("kakao_attachment_run._COPY_CHUNK_BYTES", 2), pytest.raises(SendRunCancelled):
        snapshot.prepare([(source,)], cancel_check=cancel)
    assert not snapshot._root.exists()


def test_later_bad_bundle_rejects_preparation_and_cleans_earlier_copy(tmp_path):
    source = tmp_path / "file.pdf"
    source.write_bytes(b"%PDF- original")
    snapshot = AttachmentRunSnapshot()
    with pytest.raises(DirectSendError):
        snapshot.prepare([(source,), (tmp_path / "missing.pdf",)], cancel_check=lambda: None)
    assert not snapshot._root.exists()
