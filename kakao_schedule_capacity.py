"""Local reservation slots, derived from durable receipts and the PC clock.

An elapsed scheduled time releases a slot estimate, never proves delivery or
permits resubmitting the same reservation. All dates use local time.
"""
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from kakao_schedule_journal import ScheduleJournal

LIMIT = 100


class CapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapacityView:
    used: int
    available: int
    expected_available: int
    observed_at: datetime
    next_release: datetime | None
    fresh: bool
    claimed: int = 0
    expired: int = 0
    unconfirmed: int = 0

    def label(self):
        label = f'예약 추정 {self.used}/100 · 여유 {self.available}개'
        if self.unconfirmed:
            label += f' · 접수 확인 필요 {self.unconfirmed}개 포함'
        return label


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise CapacityError('예약 카운터에는 현지 날짜와 시각이 필요합니다.')
    return value


class ScheduleCapacity:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.journal = ScheduleJournal(self.path)
        with self.journal.slot_accounting(write=True) as (db, _):
            db.execute('CREATE TABLE IF NOT EXISTS schedule_capacity (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            value = self._read(db)
            # Old native counts have no due times. Preserve receipts and any
            # in-flight claims, but no longer admit against that opaque count.
            if value is None or value.get('version') != 2:
                self._write(db, {'version': 2, 'claims': value['claims'] if value else {}})

    @staticmethod
    def _read(db):
        row = db.execute('SELECT payload FROM schedule_capacity WHERE id=1').fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            if not isinstance(value, dict) or not isinstance(value['claims'], dict):
                raise ValueError
            if 'version' in value:
                if type(value['version']) is not int or value['version'] != 2:
                    raise ValueError
            else:
                if type(value['count']) is not int or not 0 <= value['count'] <= LIMIT:
                    raise ValueError
                _time(datetime.fromisoformat(value['at']))
                if not isinstance(value['known'], dict):
                    raise ValueError
                for key, due in value['known'].items():
                    if not isinstance(key, str) or not key:
                        raise ValueError
                    _time(datetime.fromisoformat(due))
            for key, due in value['claims'].items():
                if not isinstance(key, str) or not key:
                    raise ValueError
                _time(datetime.fromisoformat(due))
            return value
        except (ValueError, TypeError, KeyError) as error:
            raise CapacityError('예약 카운터 기록을 읽지 못했습니다. 기록을 확인하기 전에는 새 등록을 시작하지 않습니다.') from error

    @staticmethod
    def _write(db, value):
        db.execute('INSERT INTO schedule_capacity VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (json.dumps(value, ensure_ascii=False),))

    @staticmethod
    def _view(value, records, now):
        submitted = {record.key: datetime.fromisoformat(record.scheduled_at)
                     for record in records if not record.safe_to_submit}
        claims = {key: datetime.fromisoformat(due) for key, due in value['claims'].items()}
        due = {**claims, **submitted}
        active = {key for key, moment in due.items() if moment > now}
        uncertain = {record.key for record in records if record.state in ('submitting', 'unknown')}
        uncertain.update(claims.keys() - submitted.keys())
        used = len(active)
        available = max(0, LIMIT - used)
        return CapacityView(used, available, available, now,
                            min((due[key] for key in active), default=None), True,
                            len(active & (claims.keys() - submitted.keys())),
                            len(due) - used, len(active & uncertain))

    def view(self, now=None):
        now = _time(now or datetime.now())
        with self.journal.slot_accounting() as (db, records):
            value = self._read(db)
            if value is None:
                raise CapacityError('예약 슬롯 기록이 없어 새 등록을 시작하지 않습니다.')
            return self._view(value, records, now)

    @staticmethod
    def _ensure(view, additional):
        if type(additional) is not int or additional < 0:
            raise CapacityError('등록할 예약 개수가 올바르지 않습니다.')
        if additional > view.available:
            raise CapacityError(f'예약 슬롯이 부족합니다. 기록상 여유 {view.available}개, 새 등록 {additional}개입니다. 추가 등록을 시작하지 않습니다.')

    def ensure_capacity(self, additional: int, now=None):
        view = self.view(now)
        self._ensure(view, additional)
        return view

    def claim(self, key: str, scheduled_at: datetime, now=None):
        now = _time(now or datetime.now())
        due = _time(scheduled_at)
        if not isinstance(key, str) or not key or due <= now:
            raise CapacityError('예약 식별자 또는 미래의 예약 시각이 올바르지 않습니다.')
        with self.journal.slot_accounting(write=True) as (db, records):
            value = self._read(db)
            if value is None:
                raise CapacityError('예약 슬롯 기록이 없어 새 등록을 시작하지 않습니다.')
            record = next((record for record in records if record.key == key), None)
            if (record is None or not record.safe_to_submit or key in value['claims']
                    or datetime.fromisoformat(record.scheduled_at) != due):
                raise CapacityError('미등록 원본과 일치하는 예약만 새 슬롯을 확보할 수 있습니다.')
            self._ensure(self._view(value, records, now), 1)
            value['claims'][key] = due.isoformat()
            self._write(db, value)

    def release_claim(self, key: str):
        """Release only a proven, unsubmitted claim; never erase receipts."""
        with self.journal.slot_accounting(write=True) as (db, records):
            value = self._read(db)
            record = next((record for record in records if record.key == key), None)
            if record is None or not record.safe_to_submit:
                raise CapacityError('제출 가능성이 있는 예약의 슬롯 기록을 임의로 지우지 않습니다.')
            if value is not None:
                value['claims'].pop(key, None)
                self._write(db, value)
