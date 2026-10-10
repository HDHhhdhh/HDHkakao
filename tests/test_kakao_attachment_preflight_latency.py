"""Deduplicate disk checks within one preflight, never between runs."""
from pathlib import Path
from unittest.mock import patch

from kakao_sender import MessageRow, missing_attachment_paths


def test_shared_attachment_is_checked_once_but_every_missing_row_is_retained():
    missing = Path('missing.pdf')
    good = Path('good.png')
    rows = [MessageRow(i, f'room-{i}', 'body', attachments=(good, missing)) for i in range(1, 101)]
    checked = []

    def exists(path):
        checked.append(path)
        return path == good

    with patch.object(Path, 'exists', exists):
        assert missing_attachment_paths(iter(rows)) == [(i, missing) for i in range(1, 101)]
    assert checked == [good, missing]


def test_second_preflight_checks_files_again_after_their_state_changes():
    file = Path('changes.pdf')
    rows = [MessageRow(2, 'room', 'body', attachments=(file,))]
    with patch.object(Path, 'exists', return_value=True) as exists:
        assert missing_attachment_paths(rows) == []
        exists.return_value = False
        assert missing_attachment_paths(rows) == [(2, file)]
        assert exists.call_count == 2
