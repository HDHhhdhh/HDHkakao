"""One-time correction of one legacy pre-click receipt; never controls UI."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_DIR = ROOT/'exports'/'schedule-preclick-recovery-20261002'
sys.path.insert(0, str(ROOT))
from kakao_app_paths import SCHEDULE_JOURNAL_PATH
from kakao_schedule_capacity import ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal, _json

KEY = '416bb02773466c4f6e00d5d3aaee79683be9f2826b4957d2f75776f31413aa78'
RECEIPT_SHA = '6358a5f0c9e96aac5af8f0bf72d037ac61b2a624566ab4f0d9be6da17636f0c9'
TRACE_SHA = '759bc26446c3da1da81ac30b1c7104f2f08be9a30caaca303019a7c46b88eec2'
DRIVER_SHA = 'ab6219e60a10a224c0d87ba380989a5d003e8206f73bc2313cdc8332d8e2bd28'
ERROR = '클릭 위치가 다른 창에 가려져 있습니다.'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_preclick_proof(record, trace_bytes):
    """Exact legacy guard failure plus matching pre-input trace is required."""
    if (record.state != 'unknown' or len(record.events) < 2
            or record.events[-1]['evidence'].get('error') != ERROR
            or record.events[-2]['state'] != 'submitting'):
        raise RuntimeError('대상은 확인된 구버전 클릭 전 실패 기록이어야 합니다.')
    evidence = record.events[-2]['evidence']
    if ('submission_boundary' in evidence or 'click_guard_passed' in evidence
            or evidence.get('body_exact') is not True or evidence.get('date_time_exact') is not True
            or evidence.get('body_sha256') != digest(record.text.encode('utf-8'))
            or any(evidence.get(field) != record.chat_name for field in ('expected_chat_name','observed_chat_name'))
            or any(evidence.get(field) != record.scheduled_at for field in ('expected_scheduled_at','observed_scheduled_at'))):
        raise RuntimeError('구버전 미제출 원본의 대상·본문·시각 증거가 일치하지 않습니다.')
    events = [json.loads(line) for line in trace_bytes.decode('utf-8').splitlines()]
    anchors = [i for i,event in enumerate(events) if event.get('kind') == 'form_verified'
               and event.get('path') == evidence.get('form_screenshot')]
    if len(anchors) != 1:
        raise RuntimeError('대상 양식의 고유한 검증 기록이 없습니다.')
    anchor = events[anchors[0]]
    if (anchor.get('chat_name') != record.chat_name or anchor.get('scheduled_at') != record.scheduled_at
            or anchor.get('body_sha256') != evidence.get('body_sha256')):
        raise RuntimeError('조작 로그와 제출 증거가 일치하지 않습니다.')
    after = events[anchors[0]+1:]
    if (len(after) != 2 or any(event.get('kind') != 'capture' for event in after)
            or after[0].get('path') != evidence.get('before_chat_screenshot')
            or not after[1].get('path','').endswith('-dispatch-boundary.png')
            or after[1].get('window',{}).get('handle') != evidence.get('form_handle')
            or after[1].get('window',{}).get('rect') != evidence.get('form_rect')):
        raise RuntimeError('등록 클릭 가능성을 제외할 수 없어 기록을 변경하지 않습니다.')
    return {'recovery':'legacy_occluded_submit_before_input', 'error':ERROR,
            'source_events_sha256':digest(trace_bytes), 'legacy_driver_sha256':DRIVER_SHA,
            'original_events_sha256':digest(_json(list(record.events)).encode('utf-8'))}


def pinned_proof(record):
    driver = RECOVERY_DIR/'before'/'kakao_native_schedule.py'
    if digest(driver.read_bytes()) != DRIVER_SHA or record.key != KEY:
        raise RuntimeError('검토한 코드 또는 대상 식별자가 달라 복구하지 않습니다.')
    if digest(_json(list(record.events)).encode('utf-8')) != RECEIPT_SHA:
        raise RuntimeError('확인 이후 예약 기록이 변경되어 복구하지 않습니다.')
    trace = Path(record.events[-2]['evidence']['form_screenshot']).parent/'events.jsonl'
    trace_bytes = trace.read_bytes()
    if digest(trace_bytes) != TRACE_SHA:
        raise RuntimeError('확인 이후 조작 로그가 변경되어 복구하지 않습니다.')
    return validate_preclick_proof(record, trace_bytes)


def main(apply=False):
    if not SCHEDULE_JOURNAL_PATH.is_file():
        raise RuntimeError('기존 예약 기록이 없어 새 파일을 만들거나 복구하지 않습니다.')
    if not apply:
        with sqlite3.connect(SCHEDULE_JOURNAL_PATH.resolve().as_uri()+'?mode=ro', uri=True) as db:
            db.execute('BEGIN');ScheduleJournal._validate(db)
            record = ScheduleJournal._read_record(db, KEY)
            proof = pinned_proof(record)
    else:
        journal = ScheduleJournal(SCHEDULE_JOURNAL_PATH)
        with journal._transaction() as db:
            record = ScheduleJournal._read_record(db, KEY)
            proof = pinned_proof(record)
            backup = {'key':KEY,'state':record.state,'events':list(record.events),'proof':proof}
            (RECOVERY_DIR/'receipt-before.json').write_text(json.dumps(backup,ensure_ascii=False,indent=2),encoding='utf-8')
            events = [*record.events, {'at':datetime.now(timezone.utc).isoformat(),
                       'state':'rejected_before_submit', 'evidence':proof}]
            db.execute("UPDATE reservations SET state='rejected_before_submit', events=? WHERE key=?",
                       (_json(events), KEY))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'schedule_capacity' in tables:
                capacity = ScheduleCapacity._read(db)
                if capacity is not None:
                    capacity['claims'].pop(KEY, None)
                    ScheduleCapacity._write(db, capacity)
    print(json.dumps({'key':KEY,'chat_name':record.chat_name,'scheduled_at':record.scheduled_at,
                      'state':'rejected_before_submit' if apply else record.state,
                      'applied':apply,'input_sent':False,'proof':proof},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    main(parser.parse_args().apply)
