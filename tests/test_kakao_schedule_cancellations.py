"""Explicit native-list cancellation must preserve receipts and block races."""
from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import Mock, patch

import pytest

from kakao_schedule_capacity import ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal, ScheduleJournalError, reservation_key
from kakao_schedule_session import NativeScheduleSession
from kakao_sender import MessageRow
from scripts.reconcile_schedule_cancellations import apply_snapshot, snapshot

NOW = datetime(2099, 1, 1, 12)


def setup(tmp_path):
    counter = ScheduleCapacity(tmp_path/'schedule.sqlite3')
    journal = counter.journal
    rows = [{'chat_name': f'fixture {i}', 'text': 'fixture body',
             'scheduled_at': NOW + timedelta(days=1) if i != 3 else NOW - timedelta(days=1)}
            for i in range(5)]
    journal.prepare(rows)
    for i, row in enumerate(rows[:4]):
        if i < 3:
            counter.claim(reservation_key(row), row['scheduled_at'], NOW)
        journal.mark_submitting(row)
        if i == 2:
            journal.mark_unknown(row)
        else:
            journal.mark_acknowledged(row)
            if i in (1, 3):
                journal.mark_verified(row)
    return journal, counter, rows


def confirmed_snapshot(journal):
    return snapshot(journal.path, confirmation='Fixture user explicitly cleared the native pending list.',
                    observed_through=datetime.now(timezone.utc)+timedelta(seconds=1), now=NOW)


def test_snapshot_is_read_only_and_omits_body_and_expired_receipts(tmp_path):
    journal, _, rows = setup(tmp_path)
    before = journal.path.read_bytes()
    value = confirmed_snapshot(journal)
    assert journal.path.read_bytes() == before
    assert len(value['entries']) == 3
    assert {entry['key'] for entry in value['entries']} == {reservation_key(row) for row in rows[:3]}
    assert all('text' not in entry for entry in value['entries'])


def test_apply_requires_explicit_confirmation_and_never_occurs_in_normal_transitions(tmp_path):
    journal, _, rows = setup(tmp_path)
    value = confirmed_snapshot(journal)
    before = journal.records()
    with pytest.raises(RuntimeError, match='확인해야'):
        apply_snapshot(journal.path, value)
    for row in rows[:3]:
        with pytest.raises(ScheduleJournalError):
            journal._mark(row, 'cancelled_by_user', {})
    assert journal.records() == before


def test_apply_preserves_events_and_originals_releases_claims_and_allows_one_new_attempt(tmp_path):
    journal, counter, rows = setup(tmp_path)
    before = {record.key: record for record in journal.records()}
    assert counter.view(NOW).used == 3
    value = confirmed_snapshot(journal)
    apply_snapshot(journal.path, value, confirm_list_cleared=True)
    restarted = ScheduleJournal(journal.path)
    for row in rows[:3]:
        record = restarted.get(row)
        assert record.state == 'cancelled_by_user' and record.safe_to_submit
        assert record.row == before[record.key].row
        assert record.events[:-1] == before[record.key].events
        assert record.events[-1]['evidence']['source'] == 'explicit_user_confirmation'
    for row in rows[3:]:
        assert restarted.get(row) == before[reservation_key(row)]
    assert counter.view(NOW).used == 0
    counter.claim(reservation_key(rows[0]), rows[0]['scheduled_at'], NOW)
    restarted.mark_submitting(rows[0])
    assert counter.view(NOW).used == 1
    with pytest.raises(ScheduleJournalError):
        restarted.mark_submitting(rows[0])
    with pytest.raises(RuntimeError, match='변경'):
        apply_snapshot(journal.path, value, confirm_list_cleared=True)


def test_changed_receipt_rejects_whole_reset_and_keeps_all_claims(tmp_path):
    journal, counter, rows = setup(tmp_path)
    value = confirmed_snapshot(journal)
    journal.mark_unknown(rows[0], evidence={'new_observation': True})
    before = journal.records()
    with pytest.raises(RuntimeError, match='변경'):
        apply_snapshot(journal.path, value, confirm_list_cleared=True)
    assert journal.records() == before
    assert counter.view(NOW).used == 3


def test_active_or_new_submission_cannot_be_included_in_cancellation_snapshot(tmp_path):
    journal, _, rows = setup(tmp_path)
    journal.mark_submitting(rows[4])
    with pytest.raises(RuntimeError, match='진행 중'):
        confirmed_snapshot(journal)
    journal.mark_unknown(rows[4])
    with pytest.raises(RuntimeError, match='추가'):
        snapshot(journal.path, confirmation='explicit fixture',
                 observed_through=datetime(2020, 1, 1, tzinfo=timezone.utc), now=NOW)


def test_failed_commit_rolls_back_receipt_reset_and_claim_removal(tmp_path):
    journal, counter, _ = setup(tmp_path)
    value = confirmed_snapshot(journal)
    before = journal.records()
    with patch.object(ScheduleJournal, '_seal', side_effect=sqlite3.OperationalError('disk full')):
        with pytest.raises(ScheduleJournalError):
            apply_snapshot(journal.path, value, confirm_list_cleared=True)
    assert journal.records() == before
    assert counter.view(NOW).used == 3


def test_invalid_confirmation_cannot_create_snapshot(tmp_path):
    journal, _, _ = setup(tmp_path)
    with pytest.raises(RuntimeError, match='명시적'):
        snapshot(journal.path, confirmation='', observed_through=datetime.now(timezone.utc), now=NOW)


def test_cancelled_rows_register_via_normal_session_after_restart_without_list_read(tmp_path):
    journal, counter, rows = setup(tmp_path)
    apply_snapshot(journal.path, confirmed_snapshot(journal), confirm_list_cleared=True)
    items = [MessageRow(i+1, row['chat_name'], row['text'], scheduled_at=row['scheduled_at'])
             for i, row in enumerate(rows[:3])]
    driver = Mock()
    driver.prepare.return_value = ('chat', 'form', 'image')
    def submit(row, *args):
        journal.mark_submitting(row)
        journal.mark_acknowledged(row)
    driver.submit.side_effect = submit
    session = NativeScheduleSession(journal.path, tmp_path/'evidence', clock=lambda: NOW,
                                   driver_factory=lambda *args, **kwargs: driver)
    assert session.prepare_batch(items) == items
    assert not session.unresolved_keys
    assert all(session.register(row).acknowledged for row in items)
    assert driver.submit.call_count == 3
    assert session.prepare_batch(items) == []
    driver.verify_list.assert_not_called(); driver.read_pending_count.assert_not_called()
    assert counter.view(NOW).used == 3
