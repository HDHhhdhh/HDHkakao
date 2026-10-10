"""Synthetic legacy receipt corrections; these tests never read user data."""
from datetime import datetime
import json

import pytest

from kakao_schedule_capacity import ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal, _json, reservation_key
from scripts import repair_schedule_preclick as repair


@pytest.fixture
def receipt(tmp_path, monkeypatch):
    folder=tmp_path/'repair';folder.mkdir();(folder/'before').mkdir()
    source=b'synthetic pre-click controller fixture'
    (folder/'before'/'kakao_native_schedule.py').write_bytes(source)
    path=tmp_path/'journal.sqlite3'
    counter=ScheduleCapacity(path);journal=counter.journal
    row={'chat_name':'fixture','text':'synthetic body','scheduled_at':datetime(2099,1,1,10)}
    other={**row,'chat_name':'other fixture'}
    journal.prepare([row,other]);counter.claim(reservation_key(row),row['scheduled_at'])
    body_hash=repair.digest(row['text'].encode())
    form=str(folder/'00088-ready.png');before=str(folder/'00089-before-submit-chat.png')
    evidence={'form_handle':1,'form_rect':[0,0,740,1200], 'form_screenshot':form,
              'body_exact':True,'date_time_exact':True,'body_sha256':body_hash,
              'expected_chat_name':row['chat_name'],'observed_chat_name':row['chat_name'],
              'expected_scheduled_at':row['scheduled_at'].isoformat(),
              'observed_scheduled_at':row['scheduled_at'].isoformat(),'before_chat_screenshot':before}
    journal.mark_submitting(row,evidence=evidence);journal.mark_unknown(row,evidence={'error':repair.ERROR})
    journal.mark_submitting(other);journal.mark_acknowledged(other)
    trace=[{'kind':'form_verified','path':form,'chat_name':row['chat_name'],
            'scheduled_at':row['scheduled_at'].isoformat(),'body_sha256':body_hash},
           {'kind':'capture','path':before,'window':{'handle':2}},
           {'kind':'capture','path':str(folder/'00090-dispatch-boundary.png'),
            'window':{'handle':1,'rect':[0,0,740,1200]}}]
    trace_bytes='\n'.join(json.dumps(event) for event in trace).encode()
    (folder/'events.jsonl').write_bytes(trace_bytes)
    monkeypatch.setattr(repair,'RECOVERY_DIR',folder)
    monkeypatch.setattr(repair,'SCHEDULE_JOURNAL_PATH',path)
    monkeypatch.setattr(repair,'KEY',reservation_key(row))
    monkeypatch.setattr(repair,'RECEIPT_SHA',repair.digest(_json(list(journal.get(row).events)).encode()))
    monkeypatch.setattr(repair,'TRACE_SHA',repair.digest(trace_bytes))
    monkeypatch.setattr(repair,'DRIVER_SHA',repair.digest(source))
    return journal,counter,row,other,trace,folder


def test_preview_has_no_database_writes(receipt):
    journal,counter,row,other,trace,folder=receipt
    before=journal.path.read_bytes()
    repair.main(False)
    assert journal.path.read_bytes()==before
    assert journal.get(row).state=='unknown' and counter.view().used==2
    assert not (folder/'receipt-before.json').exists()


def test_apply_changes_only_proven_unsubmitted_receipt_and_claim(receipt):
    journal,counter,row,other,trace,folder=receipt
    previous=journal.get(row);other_before=journal.get(other)
    repair.main(True)
    current=journal.get(row)
    assert current.state=='rejected_before_submit' and current.safe_to_submit
    assert current.events[:-1]==previous.events
    assert current.chat_name==previous.chat_name and current.text==previous.text
    assert current.scheduled_at==previous.scheduled_at
    assert journal.get(other)==other_before and counter.view().used==1
    assert (folder/'receipt-before.json').exists()


def test_changed_record_cannot_be_repaired_with_stale_proof(receipt):
    journal,counter,row,other,trace,folder=receipt
    journal.mark_unknown(row,evidence={'error':repair.ERROR})
    with pytest.raises(RuntimeError,match='기록이 변경'):repair.main(True)
    assert journal.get(row).state=='unknown' and counter.view().used==2


def test_dispatch_log_prevents_repair_even_with_matching_guard_error(receipt):
    journal,counter,row,other,trace,folder=receipt
    trace.append({'kind':'click','label':'submit','window':{'handle':1}})
    raw='\n'.join(json.dumps(event) for event in trace).encode()
    with pytest.raises(RuntimeError,match='클릭 가능성'):
        repair.validate_preclick_proof(journal.get(row),raw)


def test_incomplete_trace_cannot_be_used_as_no_dispatch_evidence(receipt):
    journal,counter,row,other,trace,folder=receipt
    raw='\n'.join(json.dumps(event) for event in trace[:-1]).encode()
    with pytest.raises(RuntimeError):repair.validate_preclick_proof(journal.get(row),raw)


def test_new_guarded_dispatch_receipt_is_never_legacy_recovery_candidate(receipt):
    from dataclasses import replace
    journal,counter,row,other,trace,folder=receipt
    record=journal.get(row)
    events=list(record.events)
    events[-2]={**events[-2],'evidence':{**events[-2]['evidence'],'click_guard_passed':True}}
    candidate=replace(record,events=tuple(events))
    with pytest.raises(RuntimeError):
        repair.validate_preclick_proof(candidate,(folder/'events.jsonl').read_bytes())
