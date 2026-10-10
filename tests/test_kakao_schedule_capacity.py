"""Local slot accounting: synthetic receipts only; no KakaoTalk controls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from kakao_schedule_capacity import CapacityError, ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal, ScheduleJournalError, reservation_key

NOW = datetime(2026, 10, 2, 12)


def row(i=0, due=None):
    return {'chat_name': f'fixture {i}', 'text': 'synthetic reservation',
            'scheduled_at': due or NOW + timedelta(days=1)}


def acknowledge(journal, items):
    journal.prepare(items)
    for item in items:
        journal.mark_submitting(item)
        journal.mark_acknowledged(item)


def test_empty_local_journal_admits_100_without_native_observation(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    assert counter.ensure_capacity(100, NOW).used == 0
    assert counter.view(NOW).available == 100
    with pytest.raises(CapacityError):
        counter.ensure_capacity(101, NOW)


def test_loading_csv_or_pre_submit_failure_does_not_use_slots(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    items = [row(i) for i in range(120)]
    counter.journal.prepare(items)
    counter.journal.mark_rejected_before_submit(items[0])
    assert counter.view(NOW).used == 0


def test_whole_batch_admission_and_exact_100_boundary(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    acknowledge(counter.journal, [row(i) for i in range(54)])
    assert counter.ensure_capacity(46, NOW).available == 46
    with pytest.raises(CapacityError, match='여유 46개'):
        counter.ensure_capacity(47, NOW)


def test_due_time_releases_slots_without_delivery_or_retry_claim(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    item = row(due=NOW + timedelta(minutes=5))
    acknowledge(counter.journal, [item])
    before = counter.journal.records()
    assert counter.view(NOW + timedelta(minutes=4, seconds=59)).used == 1
    at_due = counter.view(NOW + timedelta(minutes=5))
    assert at_due.used == 0 and at_due.available == 100 and at_due.expired == 1
    assert counter.view(NOW + timedelta(minutes=10)).used == 0
    assert counter.journal.records() == before
    assert counter.journal.get(item).state == 'acknowledged'
    assert not counter.journal.safe_to_submit(item)


def test_old_records_and_future_reservations_survive_restart(tmp_path):
    path = tmp_path / 'schedule.sqlite3'
    counter = ScheduleCapacity(path)
    items = [row(0, NOW - timedelta(minutes=5)), row(1, NOW + timedelta(minutes=5))]
    acknowledge(counter.journal, items)
    restarted = ScheduleCapacity(path)
    assert restarted.view(NOW).used == 1 and restarted.view(NOW).expired == 1
    assert restarted.view(NOW + timedelta(minutes=10)).used == 0
    assert len(restarted.journal.records()) == 2


def test_local_counts_do_not_become_stale_after_two_minutes(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    acknowledge(counter.journal, [row()])
    assert counter.ensure_capacity(99, NOW + timedelta(hours=5)).fresh
    assert counter.view(NOW + timedelta(hours=5)).used == 1


def test_claim_and_receipt_are_counted_once(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    item = row()
    counter.journal.prepare([item])
    counter.claim(reservation_key(item), item['scheduled_at'], NOW)
    assert counter.view(NOW).used == 1 and counter.view(NOW).claimed == 1
    counter.journal.mark_submitting(item)
    counter.journal.mark_acknowledged(item)
    view = counter.view(NOW)
    assert view.used == 1 and view.claimed == 0 and view.unconfirmed == 0
    assert ScheduleCapacity(counter.path).view(NOW).used == 1


def test_unknown_submission_keeps_slot_until_due_and_never_becomes_retryable(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    item = row(due=NOW + timedelta(minutes=5))
    counter.journal.prepare([item])
    counter.claim(reservation_key(item), item['scheduled_at'], NOW)
    counter.journal.mark_submitting(item)
    counter.journal.mark_unknown(item)
    assert counter.view(NOW).used == 1 and counter.view(NOW).unconfirmed == 1
    with pytest.raises(CapacityError):
        counter.release_claim(reservation_key(item))
    assert counter.view(NOW + timedelta(minutes=10)).used == 0
    assert counter.journal.get(item).state == 'unknown'
    assert not counter.journal.safe_to_submit(item)


def test_only_unsubmitted_claim_can_be_released(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    item = row()
    counter.journal.prepare([item])
    counter.claim(reservation_key(item), item['scheduled_at'], NOW)
    counter.release_claim(reservation_key(item))
    assert counter.view(NOW).used == 0
    assert counter.journal.safe_to_submit(item)


def test_concurrent_claims_cannot_overfill_last_slot(tmp_path):
    path = tmp_path / 'schedule.sqlite3'
    counter = ScheduleCapacity(path)
    acknowledge(counter.journal, [row(i) for i in range(99)])
    pending = [row(i) for i in range(99, 107)]
    counter.journal.prepare(pending)
    def attempt(item):
        try:
            ScheduleCapacity(path).claim(reservation_key(item), item['scheduled_at'], NOW)
            return True
        except CapacityError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, pending)) == 1
    assert counter.view(NOW).used == 100


def test_legacy_native_count_migrates_to_local_records_and_preserves_claims(tmp_path):
    path = tmp_path / 'schedule.sqlite3'
    journal = ScheduleJournal(path)
    past, future, claimed = row(0, NOW - timedelta(minutes=5)), row(1), row(2)
    acknowledge(journal, [past, future])
    journal.prepare([claimed])
    legacy = {'at': NOW.isoformat(), 'count': 99,
              'known': {reservation_key(future): future['scheduled_at'].isoformat()},
              'claims': {reservation_key(claimed): claimed['scheduled_at'].isoformat()}}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE schedule_capacity (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('INSERT INTO schedule_capacity VALUES(1, ?)', (json.dumps(legacy),))
    before = journal.records()
    counter = ScheduleCapacity(path)
    assert counter.view(NOW).used == 2
    assert counter.view(NOW).expired == 1
    assert counter.view(NOW).claimed == 1
    assert journal.records() == before
    assert ScheduleCapacity(path).view(NOW).used == 2


def test_clock_rollback_recounts_due_records_conservatively(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    item = row(due=NOW + timedelta(minutes=5))
    acknowledge(counter.journal, [item])
    assert counter.view(NOW + timedelta(minutes=10)).used == 0
    assert counter.view(NOW).used == 1


@pytest.mark.parametrize('additional', [-1, True, 1.5, '1', None])
def test_invalid_batch_size_cannot_admit(tmp_path, additional):
    with pytest.raises(CapacityError):
        ScheduleCapacity(tmp_path / 'schedule.sqlite3').ensure_capacity(additional, NOW)


def test_timezones_and_mismatched_claims_are_rejected(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    item = row()
    counter.journal.prepare([item])
    with pytest.raises(CapacityError):
        counter.view(NOW.replace(tzinfo=timezone.utc))
    with pytest.raises(CapacityError):
        counter.claim(reservation_key(item), item['scheduled_at'] + timedelta(minutes=5), NOW)
    assert counter.view(NOW).used == 0


@pytest.mark.parametrize('payload', ['invalid json', '{"version": 99, "claims": {}}',
                                      '{"version": 2, "claims": {"bad": "nonsense"}}'])
def test_corrupt_counter_never_silently_resets(tmp_path, payload):
    path = tmp_path / 'schedule.sqlite3'
    counter = ScheduleCapacity(path)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE schedule_capacity SET payload=?', (payload,))
    with pytest.raises(CapacityError):
        counter.view(NOW)
    with pytest.raises(CapacityError):
        ScheduleCapacity(path)


def test_corrupt_receipts_block_slot_admission(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    acknowledge(counter.journal, [row()])
    with sqlite3.connect(counter.path) as db:
        db.execute("UPDATE reservations SET state='prepared'")
    with pytest.raises(ScheduleJournalError):
        counter.ensure_capacity(1, NOW)


def test_missing_journal_does_not_recreate_an_empty_counter(tmp_path):
    counter = ScheduleCapacity(tmp_path / 'schedule.sqlite3')
    counter.path.unlink()
    with pytest.raises(ScheduleJournalError):
        counter.view(NOW)
    assert not counter.path.exists()
