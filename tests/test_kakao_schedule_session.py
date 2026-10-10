from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from kakao_schedule_session import NativeScheduleSession
from kakao_schedule_journal import ScheduleJournal
from kakao_schedule_capacity import CapacityError
from kakao_sender import MessageRow, ScheduleNotSubmittedError, MacroSendUnknownError, MacroSendCancelled


def row(i=1):
    due=(datetime.now()+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
    return MessageRow(i,f'fixture {i}','fixture body',scheduled_at=due)


def setup(tmp_path, count=54):
    journal=ScheduleJournal(tmp_path/'schedule.sqlite3')
    existing = [row(i + 1000) for i in range(count)]
    journal.prepare(existing)
    for item in existing:
        journal.mark_submitting(item)
        journal.mark_acknowledged(item)
    driver=Mock()
    driver.windows.return_value=[]
    driver.read_pending_count.side_effect=AssertionError('automatic list access removed')
    driver.prepare.return_value=('chat','form','image')
    def submit(item,*args):
        journal.mark_submitting(item)
        journal.mark_acknowledged(item)
    driver.submit.side_effect=submit
    driver.verify_list.side_effect=AssertionError('automatic list access removed')
    session=NativeScheduleSession(journal,tmp_path/'evidence',cancel_event=threading.Event(),driver_factory=lambda *a,**k:driver)
    return session,journal,driver


def test_over_capacity_blocks_entire_batch_before_preparing_any_form(tmp_path):
    session,journal,driver=setup(tmp_path)
    with pytest.raises(CapacityError):session.prepare_batch([row(i) for i in range(47)])
    driver.prepare.assert_not_called();driver.submit.assert_not_called()


def test_acknowledgement_finishes_registration_without_list_access_or_false_verification(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item])
    result=session.register(item)
    assert result.acknowledged is True and result.verified is False
    assert journal.get(item).state=='acknowledged'
    assert session.capacity.view().used==55
    assert session.result(item).verified is False
    driver.read_pending_count.assert_not_called()
    driver.verify_list.assert_not_called()
    assert session.prepare_batch([item]) == []
    assert session.register(item).acknowledged
    assert driver.submit.call_count == 1
    driver.close_registered_chat.assert_called_once_with('chat', expected_chat_name=item.chat_name)


def test_chat_close_failure_preserves_success_and_blocks_next_native_input(tmp_path):
    session, journal, driver = setup(tmp_path, count=0)
    first, second = row(1), row(2)
    session.prepare_batch([first, second])
    driver.close_registered_chat.side_effect = RuntimeError('chat did not close')
    result = session.register(first)
    assert result.acknowledged and result.cleanup_error == 'chat did not close'
    assert journal.get(first).state == 'acknowledged'
    assert session.capacity.view().used == 1
    with pytest.raises(ScheduleNotSubmittedError, match='native_cleanup'):
        session.register(second)
    assert driver.prepare.call_count == 1
    assert driver.submit.call_count == 1
    assert journal.get(second).safe_to_submit
    assert session.register(first).acknowledged


def test_previously_verified_row_never_reenters_native_input(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();journal.prepare([item]);journal.mark_submitting(item);journal.mark_verified(item)
    session.prepare_batch([item])
    assert session.accepted_keys == frozenset({journal.get(item).key})
    assert session.register(item).verified is True
    driver.read_pending_count.assert_not_called();driver.prepare.assert_not_called()


def test_known_pre_submit_failure_releases_claim_and_is_retryable(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item])
    driver.prepare.side_effect=RuntimeError('fixture OCR failure')
    with pytest.raises(ScheduleNotSubmittedError):session.register(item)
    assert journal.get(item).state=='rejected_before_submit'
    assert session.capacity.view().used==54
    driver.submit.assert_not_called()
    driver.close_registered_chat.assert_not_called()


def test_unknown_submission_keeps_slot_and_must_not_be_replayed(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item])
    def uncertain(*args):
        journal.mark_submitting(item);journal.mark_unknown(item)
        raise RuntimeError('fixture connection/receipt loss')
    driver.submit.side_effect=uncertain
    with pytest.raises(MacroSendUnknownError):session.register(item)
    assert session.capacity.view().used==55
    with pytest.raises(MacroSendUnknownError):session.register(item)
    assert driver.submit.call_count==1
    driver.close_registered_chat.assert_not_called()


def test_cancelled_batch_never_starts_a_form(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item]);session.cancel_event.set()
    with pytest.raises(Exception):session.register(item)
    driver.prepare.assert_not_called()


def test_capacity_detects_other_local_registrations_before_registering(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item])
    extra = [row(i+2000) for i in range(46)]
    journal.prepare(extra)
    for previous in extra:
        journal.mark_submitting(previous)
        journal.mark_acknowledged(previous)
    with pytest.raises(ScheduleNotSubmittedError):session.register(item)
    driver.submit.assert_not_called()


def test_physical_escape_skips_cleanup_input_and_retains_retryable_evidence(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item])
    driver.prepare.side_effect=InterruptedError('physical Esc')
    with pytest.raises(MacroSendCancelled):session.register(item)
    assert session.cancel_event.is_set()
    assert journal.get(item).state=='rejected_before_submit'
    driver.discard_owned_draft.assert_not_called();driver.submit.assert_not_called()


def test_cleanup_error_cannot_erase_pre_submit_evidence(tmp_path):
    session,journal,driver=setup(tmp_path)
    item=row();session.prepare_batch([item])
    driver.prepare.side_effect=ValueError('original readback')
    driver.discard_owned_draft.side_effect=RuntimeError('cleanup failed')
    with pytest.raises(ScheduleNotSubmittedError,match='original readback'):session.register(item)
    assert journal.get(item).safe_to_submit


def test_failed_repreparation_does_not_reuse_previous_batch_admission(tmp_path):
    session, journal, driver = setup(tmp_path)
    item = row()
    session.prepare_batch([item])
    extra = [row(i+2000) for i in range(46)]
    journal.prepare(extra)
    for previous in extra:
        journal.mark_submitting(previous)
        journal.mark_acknowledged(previous)
    with pytest.raises(CapacityError):
        session.prepare_batch([item])
    with pytest.raises(ScheduleNotSubmittedError, match='사전 검사'):
        session.register(item)
    driver.prepare.assert_not_called()
    driver.submit.assert_not_called()


def test_empty_list_read_failure_cannot_block_a_new_batch(tmp_path):
    session, journal, driver = setup(tmp_path, count=0)
    item = row()
    session.prepare_batch([item])
    assert session.register(item).acknowledged
    assert session.capacity.view().used == 1
    driver.read_pending_count.assert_not_called()
    driver.verify_list.assert_not_called()


def test_clock_expiry_frees_slot_but_does_not_replay_accepted_row(tmp_path):
    session, journal, driver = setup(tmp_path, count=0)
    item = row()
    session.prepare_batch([item])
    session.register(item)
    session.clock = lambda: item.scheduled_at + timedelta(minutes=5)
    assert session.refresh_capacity().used == 0
    assert session.prepare_batch([item]) == []
    assert session.register(item).acknowledged and not session.result(item).verified
    assert journal.get(item).state == 'acknowledged'
    assert driver.submit.call_count == 1
    driver.read_pending_count.assert_not_called()
    driver.verify_list.assert_not_called()


def test_unknown_previous_attempt_is_held_while_unrelated_pending_rows_proceed(tmp_path):
    session, journal, driver = setup(tmp_path, count=0)
    item = row()
    journal.prepare([item])
    journal.mark_submitting(item)
    journal.mark_unknown(item)
    pending=row(2)
    assert session.prepare_batch([item, pending]) == [pending]
    assert not session.accepted_keys
    assert journal.get(item).key in session.unresolved_keys
    assert session.register(pending).acknowledged
    with pytest.raises(MacroSendUnknownError):session.register(item)
    assert journal.get(item).state=='unknown'
    assert driver.submit.call_count==1
    driver.dismiss_matching_draft.assert_called_once_with([item,pending])
    driver.read_pending_count.assert_not_called()
    driver.verify_list.assert_not_called()
    driver.prepare.assert_called_once_with(pending)


def test_batch_containing_only_prior_unknown_never_opens_a_window(tmp_path):
    session,journal,driver=setup(tmp_path,count=0)
    item=row();journal.prepare([item]);journal.mark_submitting(item);journal.mark_unknown(item)
    assert session.prepare_batch([item]) == []
    assert journal.get(item).key in session.unresolved_keys
    driver.dismiss_matching_draft.assert_not_called()
    driver.prepare.assert_not_called()


def test_resumed_pre_submit_claim_does_not_block_the_same_pending_row(tmp_path):
    from kakao_schedule_journal import reservation_key
    session, journal, driver = setup(tmp_path, count=0)
    item = row()
    journal.prepare([item])
    session.capacity.claim(reservation_key(item), item.scheduled_at)
    assert session.capacity.view().used == 1
    assert session.prepare_batch([item]) == [item]
    assert session.capacity.view().used == 0
    assert session.register(item).acknowledged
    assert session.capacity.view().used == 1


def test_missing_submit_receipt_keeps_unknown_state_and_slot(tmp_path):
    session, journal, driver = setup(tmp_path, count=0)
    item = row()
    session.prepare_batch([item])
    driver.submit.side_effect = lambda *args: journal.mark_submitting(item)
    with pytest.raises(MacroSendUnknownError):
        session.register(item)
    assert journal.get(item).state == 'unknown'
    assert session.capacity.view().used == 1
    with pytest.raises(MacroSendUnknownError):
        session.register(item)
    assert driver.submit.call_count == 1
