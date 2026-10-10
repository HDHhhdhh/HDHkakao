"""Record explicit user cancellation of saved reservations; no UI or sending.

A snapshot pins the already-observed receipts. Applying it requires an explicit
list-cleared confirmation and rejects any changed receipt. Normal reservation
execution never calls this tool; missing receipts and clock expiry cannot reset
an attempted row.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kakao_app_paths import SCHEDULE_JOURNAL_PATH
from kakao_schedule_capacity import ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal, _json

CANCELLABLE = frozenset({'acknowledged', 'verified', 'unknown'})


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path, *, confirmation, observed_through, now=None):
    if not isinstance(confirmation, str) or not confirmation.strip():
        raise RuntimeError('예약 목록을 비웠다는 사용자의 명시적 확인이 필요합니다.')
    if not isinstance(observed_through, datetime) or observed_through.tzinfo is None:
        raise RuntimeError('이미 확인한 제출 기록의 UTC 기준 시각이 필요합니다.')
    now = now or datetime.now()
    if now.tzinfo is not None:
        raise RuntimeError('예약일시와 같은 PC 현지 시각이 필요합니다.')
    entries = []
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.execute('BEGIN'); ScheduleJournal._validate(db)
        for raw in ScheduleJournal._snapshot(db):
            record = ScheduleJournal._record(raw)
            if record.safe_to_submit or datetime.fromisoformat(record.scheduled_at) <= now:
                continue
            if (record.state not in CANCELLABLE
                    or datetime.fromisoformat(record.events[-1]['at']) > observed_through):
                raise RuntimeError('추가·진행 중 제출이 있어 일괄 취소를 반영하지 않습니다.')
            entries.append({'key': record.key, 'chat_name': record.chat_name,
                'scheduled_at': record.scheduled_at, 'state': record.state,
                'events': list(record.events),
                'payload_sha256': digest(_json(record.row).encode('utf-8'))})
    return {'version': 1, 'confirmation': confirmation,
        'recorded_at_local': now.isoformat(),
        'observed_through_utc': observed_through.astimezone(timezone.utc).isoformat(),
        'entries': entries}


def apply_snapshot(path, value, *, confirm_list_cleared=False):
    if confirm_list_cleared is not True:
        raise RuntimeError('사용자가 실제 예약 목록을 비웠다고 확인해야 반영할 수 있습니다.')
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('confirmation'), str) or not value['confirmation'].strip()
            or not isinstance(value.get('entries'), list) or not value['entries']):
        raise RuntimeError('유효한 취소 확인 스냅샷이 필요합니다.')
    cutoff = datetime.fromisoformat(value['recorded_at_local'])
    observed_through = datetime.fromisoformat(value['observed_through_utc'])
    if cutoff.tzinfo is not None or observed_through.tzinfo is None:
        raise RuntimeError('취소 확인 시각의 시간대가 올바르지 않습니다.')
    keys = [entry['key'] for entry in value['entries']]
    if len(set(keys)) != len(keys):
        raise RuntimeError('취소 확인 대상이 중복됩니다.')
    journal = ScheduleJournal.__new__(ScheduleJournal)
    journal.path = Path(path)
    snapshot_sha = digest(_json(value).encode('utf-8'))
    with journal._transaction() as db:
        records = []
        for entry in value['entries']:
            record = journal._read_record(db, entry['key'])
            if (record.state not in CANCELLABLE or record.state != entry['state']
                    or record.chat_name != entry['chat_name'] or record.scheduled_at != entry['scheduled_at']
                    or datetime.fromisoformat(record.scheduled_at) <= cutoff
                    or datetime.fromisoformat(record.events[-1]['at']) > observed_through
                    or digest(_json(record.row).encode('utf-8')) != entry['payload_sha256']
                    or _json(list(record.events)) != _json(entry['events'])):
                raise RuntimeError('확인한 예약 기록이 변경되어 취소를 반영하지 않습니다.')
            records.append(record)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        capacity = ScheduleCapacity._read(db) if 'schedule_capacity' in tables else None
        for record in records:
            events = [*record.events, {'at': datetime.now(timezone.utc).isoformat(),
                'state': 'cancelled_by_user', 'evidence': {
                    'source': 'explicit_user_confirmation', 'confirmation': value['confirmation'],
                    'snapshot_sha256': snapshot_sha, 'previous_state': record.state,
                    'recorded_at_local': value['recorded_at_local']}}]
            db.execute("UPDATE reservations SET state='cancelled_by_user', events=? WHERE key=?",
                       (_json(events), record.key))
            if capacity is not None:
                capacity['claims'].pop(record.key, None)
        if capacity is not None:
            ScheduleCapacity._write(db, capacity)
    return keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, default=SCHEDULE_JOURNAL_PATH)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--confirmation')
    parser.add_argument('--observed-through-utc')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-list-cleared', action='store_true')
    args = parser.parse_args()
    if args.apply:
        value = json.loads(args.snapshot.read_text(encoding='utf-8'))
        keys = apply_snapshot(args.journal, value, confirm_list_cleared=args.confirm_list_cleared)
    else:
        if not args.observed_through_utc:
            parser.error('--observed-through-utc is required for a read-only snapshot')
        value = snapshot(args.journal, confirmation=args.confirmation,
                         observed_through=datetime.fromisoformat(args.observed_through_utc))
        args.snapshot.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        keys = [entry['key'] for entry in value['entries']]
    print(json.dumps({'applied': args.apply, 'count': len(keys), 'keys': keys,
                      'native_input_or_sending': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
