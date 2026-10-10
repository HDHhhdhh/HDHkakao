"""CLI routing checks: all external actions are mock ports."""
from datetime import datetime
import json
from unittest.mock import Mock, patch

import pytest

from kakao_app_paths import SCHEDULE_JOURNAL_PATH
from kakao_schedule_batch import load_source, parser, run, submit_batch
from kakao_schedule_journal import ScheduleJournal, reservation_key


def arguments(tmp_path):
    source=tmp_path/'rows.json'
    source.write_text(json.dumps([{'index':1,'chat_name':'fixture','text':'fixture',
                                  'scheduled_at':'2099-01-01T10:00:00'}]),encoding='utf-8')
    return parser().parse_args(['--source-json',str(source),'--journal',str(tmp_path/'journal.sqlite3'),
                               '--output',str(tmp_path/'out'),'--submit'])


def test_cli_uses_same_native_session_as_gui(tmp_path):
    args=arguments(tmp_path)
    session=Mock()
    with patch('kakao_schedule_batch.NativeScheduleSession',return_value=session) as factory, \
         patch('kakao_schedule_batch.pc_kakao_ui_lease'):
        assert run(args)==0
    assert factory.call_count==1
    session.prepare_batch.assert_called_once()
    session.register.assert_called_once()
    session.close.assert_called_once()
    session.verify_pending.assert_not_called()


@pytest.mark.parametrize('state', ['verified', 'acknowledged'])
def test_cli_replay_of_accepted_source_never_constructs_a_native_driver(tmp_path, state):
    args=arguments(tmp_path)
    item={'chat_name':'fixture','text':'fixture','scheduled_at':datetime(2099,1,1,10)}
    journal=ScheduleJournal(args.journal)
    journal.prepare([item]);journal.mark_submitting(item)
    if state == 'verified':journal.mark_verified(item)
    else:journal.mark_acknowledged(item)
    with patch('kakao_schedule_batch.NativeScheduleSession') as factory:
        assert run(args)==0
    factory.assert_not_called()


def test_default_cli_journal_is_shared_with_gui():
    args=parser().parse_args(['--csv','source.csv','--output','out','--submit'])
    assert args.journal==SCHEDULE_JOURNAL_PATH


def test_cli_cleanup_failure_stops_without_downgrading_registration(tmp_path, capsys):
    from kakao_sender import ScheduleRegistrationResult
    args = arguments(tmp_path)
    data = json.loads(args.source_json.read_text(encoding='utf-8'))
    data.append({**data[0], 'index':2, 'chat_name':'other fixture'})
    args.source_json.write_text(json.dumps(data), encoding='utf-8')
    rows = load_source(args)
    journal = ScheduleJournal(args.journal); journal.prepare(rows)
    session = Mock()
    def register(row):
        journal.mark_submitting(row); journal.mark_acknowledged(row)
        return ScheduleRegistrationResult(False, 'receipt', True, 'chat did not close')
    session.register.side_effect = register
    with patch('kakao_schedule_batch.NativeScheduleSession', return_value=session), \
         patch('kakao_schedule_batch.pc_kakao_ui_lease'):
        assert run(args) == 2
    assert session.register.call_count == 1
    assert journal.get(rows[0]).state == 'acknowledged'
    assert journal.get(rows[1]).state == 'prepared'
    assert 'chat_cleanup_failed' in capsys.readouterr().out


def test_status_write_failure_still_closes_submission_session(tmp_path):
    args = arguments(tmp_path)
    rows = load_source(args)
    journal = ScheduleJournal(args.journal)
    journal.prepare(rows)
    session = Mock()
    with patch('kakao_schedule_batch.NativeScheduleSession', return_value=session), \
         patch('kakao_schedule_batch.pc_kakao_ui_lease'), \
         patch('kakao_schedule_batch.write_status', side_effect=OSError('disk full')):
        with pytest.raises(OSError, match='disk full'):
            submit_batch(args, rows, journal)
    session.close.assert_called_once()


def test_status_write_failure_still_closes_verification_driver(tmp_path):
    args = arguments(tmp_path)
    args.submit = False
    args.verify_only = True
    args.output.mkdir()
    rows = load_source(args)
    journal = ScheduleJournal(args.journal)
    journal.prepare(rows)
    journal.mark_submitting(rows[0])
    driver = Mock()
    driver.last_list_count = 1
    driver.verify_list.return_value = {reservation_key(rows[0])}
    with patch('kakao_native_schedule.NativeScheduleDriver', return_value=driver), \
         patch('kakao_schedule_batch.pc_kakao_ui_lease'), \
         patch('kakao_schedule_batch.write_status', side_effect=[{}, OSError('disk full')]):
        with pytest.raises(OSError, match='disk full'):
            run(args)
    driver.close.assert_called_once()


def test_cli_holds_prior_unknown_and_registers_other_rows(tmp_path, capsys):
    from kakao_schedule_session import NativeScheduleSession
    args=arguments(tmp_path)
    data=json.loads(args.source_json.read_text(encoding='utf-8'))
    data.append({**data[0],'index':2,'chat_name':'other fixture'})
    args.source_json.write_text(json.dumps(data),encoding='utf-8')
    rows=load_source(args)
    journal=ScheduleJournal(args.journal);journal.prepare(rows)
    journal.mark_submitting(rows[0]);journal.mark_unknown(rows[0])
    driver=Mock();driver.prepare.return_value=('chat','form','image')
    def submit(row,*unused):
        journal.mark_submitting(row);journal.mark_acknowledged(row)
    driver.submit.side_effect=submit
    def factory(journal,output):
        return NativeScheduleSession(journal,output,driver_factory=lambda *a,**k:driver)
    with patch('kakao_schedule_batch.NativeScheduleSession',side_effect=factory), \
         patch('kakao_schedule_batch.pc_kakao_ui_lease'):
        assert run(args)==2
    assert journal.get(rows[0]).state=='unknown'
    assert journal.get(rows[1]).state=='acknowledged'
    driver.submit.assert_called_once()
    assert driver.submit.call_args.args[0]==rows[1]
    driver.read_pending_count.assert_not_called();driver.verify_list.assert_not_called()
    assert 'previous_submission_unresolved' in capsys.readouterr().out


def test_cli_with_only_prior_unknown_reports_it_without_opening_native_ui(tmp_path, capsys):
    args=arguments(tmp_path)
    row=load_source(args)[0]
    journal=ScheduleJournal(args.journal);journal.prepare([row])
    journal.mark_submitting(row);journal.mark_unknown(row)
    with patch('kakao_schedule_batch.NativeScheduleSession') as factory:
        assert run(args)==2
    factory.assert_not_called()
    assert journal.get(row).state=='unknown'
    assert 'previous_submission_unresolved' in capsys.readouterr().out
