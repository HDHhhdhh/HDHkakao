from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from kakao_send_run import SendRunSupervisor
from kakao_send_service import SendPorts, SendRunRequest, SendService
from kakao_sender import MessageRow, ScheduleRegistrationResult, ScheduleNotSubmittedError
from kakao_schedule_capacity import CapacityError


def execute(*, prepare_error=None, fail_second=False, unknown_first=False, prior_unknown=False, cancel_after_first=False, cleanup_failure=False, already_registered=(), attempt_observer=None):
    due=(datetime.now()+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
    rows=[MessageRow(i+2,f'fixture {i}','test',scheduled_at=due) for i in range(2)]
    events=[]; supervisor=SendRunSupervisor(events.append)
    session=Mock(); state={'calls':0}
    from kakao_schedule_journal import reservation_key
    session.unresolved_keys = frozenset({reservation_key(rows[0])}) if prior_unknown else frozenset()
    session.accepted_keys = frozenset(reservation_key(rows[index]) for index in already_registered)
    session.result.return_value = ScheduleRegistrationResult(False,'previous receipt',True)
    session.prepare_batch.side_effect=prepare_error
    def register(row):
        state['calls']+=1
        if fail_second and state['calls']==2:
            raise ScheduleNotSubmittedError('fixture', RuntimeError('OCR'))
        if cancel_after_first:supervisor.request_cancel()
        if unknown_first and state['calls']==1:
            return ScheduleRegistrationResult(False,'receipt missing',False)
        return ScheduleRegistrationResult(False,'receipt',True, 'chat did not close' if cleanup_failure else '')
    session.register.side_effect=register
    session.verify_pending.side_effect=AssertionError('list access removed')
    ports=SendPorts(bridge_factory=Mock(),bridge_close=Mock(),confirm_row=Mock(return_value='send'),
        require_pyautogui=Mock(),focus_kakao_window=Mock(),send_one=Mock(),send_scheduled_one=Mock(),
        return_to_recent_chat_top=Mock(),confirm_schedule_ready=Mock(),native_schedule_factory=Mock(return_value=session))
    request=SendRunRequest(tuple(enumerate(rows)),{},0,1,None,True,False)
    def work(context):
        if attempt_observer is not None:context.observe_attempts(attempt_observer)
        SendService(ports).execute(request,context)
    result=supervisor.run(work)
    return result,ports,session,events


def test_gui_native_path_accepts_receipts_without_automatic_list_audit():
    result,ports,session,events=execute()
    assert result.failure is None
    assert [r['status'] for r in result.legacy_results()]==['schedule_acknowledged','schedule_acknowledged']
    assert session.register.call_count==2
    session.verify_pending.assert_not_called()
    session.close.assert_called_once()
    ports.send_scheduled_one.assert_not_called()
    ports.confirm_schedule_ready.assert_not_called()
    ports.require_pyautogui.assert_not_called()
    ports.focus_kakao_window.assert_not_called()
    ports.send_one.assert_not_called()


def test_native_capacity_rejection_prevents_every_registration():
    result,ports,session,_=execute(prepare_error=CapacityError('slots full'))
    assert result.failure is not None and result.legacy_results()==[]
    session.register.assert_not_called();session.close.assert_called_once()
    ports.send_scheduled_one.assert_not_called();ports.send_one.assert_not_called()


def test_missing_acknowledgement_stays_unknown_and_stops_new_input():
    result,_,session,_=execute(unknown_first=True)
    assert [r['status'] for r in result.legacy_results()]==['unknown']
    assert all(r['retry_transport']=='none' for r in result.legacy_results())
    assert session.register.call_count == 1
    session.verify_pending.assert_not_called()


def test_pre_submit_failure_preserves_earlier_registration_receipt():
    result,_,session,_=execute(fail_second=True)
    records={r['row_index']:r for r in result.legacy_results()}
    assert records[0]['status']=='schedule_acknowledged'
    assert records[1]['status']=='stopped'
    session.verify_pending.assert_not_called()


def test_cancel_stops_new_input_without_downgrading_confirmed_receipt():
    result,_,session,_=execute(cancel_after_first=True)
    records={r['row_index']:r for r in result.legacy_results()}
    assert records[0]['status']=='schedule_acknowledged' and records[1]['status']=='stopped'
    session.verify_pending.assert_not_called()
    assert session.register.call_count==1


def test_receipts_count_as_registration_success_and_cannot_be_retried():
    from kakao_delivery import progress_summary
    from kakao_gui_constants import SUCCESS_RESULT_STATUSES
    from kakao_gui_feedback import row_status_tag
    result,_,_,_=execute()
    records = result.legacy_results()
    assert all(r['status'] in SUCCESS_RESULT_STATUSES and r['retry_transport']=='none' for r in records)
    assert '성공: 2' in progress_summary(2, records)
    assert all(r['failure_stage']=='' for r in records)
    assert row_status_tag('예약 접수') == 'success'


def test_cleanup_failure_keeps_registered_row_successful_and_stops_later_registration():
    result, _, session, events = execute(cleanup_failure=True)
    records = result.legacy_results()
    assert [record['status'] for record in records] == ['schedule_acknowledged']
    assert records[0]['retry_transport'] == 'none'
    assert session.register.call_count == 1
    assert any(event.kind == 'error' and '대화창 정리' in event.payload for event in events)


def test_prior_unknown_row_is_reported_without_attempt_and_other_rows_continue():
    result,_,session,events=execute(prior_unknown=True)
    records=result.legacy_results()
    assert result.failure is None
    assert [r['status'] for r in records]==['unknown','schedule_acknowledged']
    assert records[0]['failure_stage']=='schedule_journal' and records[0]['retry_transport']=='none'
    assert session.register.call_count==1
    assert session.register.call_args.args[0].chat_name=='fixture 1'
    assert 'fixture 0' in records[0]['detail']


def test_existing_receipt_is_reused_without_recording_another_registration_attempt():
    attempts=[]
    result,ports,session,events=execute(already_registered=(0,),attempt_observer=lambda *args:attempts.append(args))
    assert result.failure is None
    assert attempts==[(1,'scheduled')]
    session.result.assert_called_once()
    assert session.register.call_count==1 and session.register.call_args.args[0].chat_name=='fixture 1'
    records=result.legacy_results()
    assert [record['status'] for record in records]==['schedule_acknowledged','schedule_acknowledged']
    assert records[0]['schedule_already_registered'] is True
    assert records[1]['schedule_already_registered'] is False
    assert any(event.kind=='log' and event.payload[0]=='기존 예약 유지' for event in events)
    ports.send_scheduled_one.assert_not_called()


def test_all_registered_rows_never_call_the_native_registration_port():
    observer=Mock(side_effect=AssertionError('no actual registration may be attempted'))
    result,ports,session,_=execute(already_registered=(0,1),attempt_observer=observer)
    assert result.failure is None
    observer.assert_not_called();session.register.assert_not_called()
    assert session.result.call_count==2
    assert all(record['schedule_already_registered'] for record in result.legacy_results())
