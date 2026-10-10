"""GUI/CLI reservation workflow. Native controls and OCR require no LLM."""
from datetime import datetime
from pathlib import Path
import threading

from kakao_schedule_capacity import ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal, reservation_key
from kakao_sender import (ScheduleRegistrationResult, ScheduleNotSubmittedError,
                          MacroSendUnknownError, MacroSendCancelled, validate_schedule_row)


class NativeScheduleSession:
    def __init__(self, journal, output, *, cancel_event=None, driver_factory=None, on_capacity=None, clock=datetime.now):
        self.journal = journal if isinstance(journal, ScheduleJournal) else ScheduleJournal(journal)
        self.capacity = ScheduleCapacity(self.journal.path)
        self.output = Path(output)
        self.cancel_event = cancel_event or threading.Event()
        self.driver_factory = driver_factory
        self.on_capacity = on_capacity
        self.clock = clock
        self._driver = None
        self.initialized = False
        self.rows = []
        self.unresolved_keys = frozenset()
        self.accepted_keys = frozenset()
        self._cleanup_error = ''

    @property
    def driver(self):
        if self._driver is None:
            factory = self.driver_factory
            if factory is None:
                from kakao_native_schedule import NativeScheduleDriver
                factory = NativeScheduleDriver
            self._driver = factory(self.output, cancel_event=self.cancel_event)
        return self._driver

    def check(self):
        if self.cancel_event.is_set():
            raise MacroSendCancelled('예약 자동 등록이 중단됐습니다.')

    def publish_capacity(self):
        view = self.capacity.view(self.clock())
        if self.on_capacity:
            self.on_capacity(view)
        return view

    def refresh_capacity(self, chat_name=None):
        """Recalculate from local receipts; no chat or reservation-list access."""
        self.check()
        return self.publish_capacity()

    def prepare_batch(self, rows):
        self.initialized = False
        self.unresolved_keys = frozenset()
        self.accepted_keys = frozenset()
        self.check()
        self.rows = list(rows)
        records = self.journal.prepare(self.rows)
        pending = [row for row, record in zip(self.rows, records) if record.safe_to_submit]
        self.accepted_keys = frozenset(record.key for record in records
                                      if record.state in ('acknowledged', 'verified'))
        self.unresolved_keys = frozenset(record.key for record in records
                                        if record.state in ('submitting', 'unknown'))
        for row in pending:
            validate_schedule_row(row, row.scheduled_at)
            # GUI/CLI hold the shared PC UI lease. A retryable receipt proves
            # no submit boundary was crossed, so reclaim an interrupted claim
            # for this exact row before admitting the resumed batch.
            self.capacity.release_claim(reservation_key(row))
        self.refresh_capacity()
        self.capacity.ensure_capacity(len(pending), self.clock())
        if pending:
            # Only a draft with an exact source body/recipient can be discarded
            # and prepared again. An unrelated open draft always blocks us.
            self.driver.dismiss_matching_draft(self.rows)
        self.initialized = True
        return pending

    def result(self, row):
        state = self.journal.get(row).state
        if state == 'verified':
            return ScheduleRegistrationResult(True, '예약 목록의 대상·시간·전체 본문 확인 완료. 동일 예약은 다시 등록하지 않습니다.', True)
        if state == 'acknowledged':
            return ScheduleRegistrationResult(False, '등록 입력과 새 접수 완료 알림 확인. 슬롯은 예약 시각까지 계산하며, 실제 발송·수신 완료를 뜻하지 않습니다.', True)
        return ScheduleRegistrationResult(False, f'이전 제출 결과 확인 필요 · {row.chat_name} · {row.scheduled_at:%Y-%m-%d %H:%M}. 이 행은 새로 등록하지 않고 보류합니다.')

    def register(self, row):
        self.check()
        state = self.journal.get(row).state
        if state in ('verified', 'acknowledged'):
            return self.result(row)
        if state in ('submitting', 'unknown'):
            raise MacroSendUnknownError('schedule_journal', RuntimeError('기존 제출 기록이 있어 재등록하지 않습니다. 해당 행의 접수 여부를 직접 확인하세요.'))
        if self._cleanup_error:
            raise ScheduleNotSubmittedError('native_cleanup', RuntimeError(self._cleanup_error))
        claimed = False
        try:
            if not self.initialized:
                raise RuntimeError('예약 묶음의 사전 검사를 먼저 수행해야 합니다.')
            self.journal.assert_safe_to_submit(row)
            self.capacity.ensure_capacity(1, self.clock())
            chat, form, image = self.driver.prepare(row)
            self.check()
            self.capacity.claim(reservation_key(row), row.scheduled_at, self.clock())
            claimed = True
            self.driver.submit(row, chat, form, image, self.journal)
            result = self.result(row)
            if not result.acknowledged and not result.verified:
                raise RuntimeError('새 예약 접수 완료 알림이 기록되지 않았습니다. 재등록하지 않습니다.')
            self.publish_capacity()
            try:
                self.check()
                self.driver.close_registered_chat(chat, expected_chat_name=row.chat_name)
            except (Exception, KeyboardInterrupt) as cleanup_error:
                # Registration is already durable. A close failure must not
                # relabel it as unknown or permit replay of the completed row.
                self._cleanup_error = str(cleanup_error) or type(cleanup_error).__name__
                if isinstance(cleanup_error, (MacroSendCancelled, InterruptedError, KeyboardInterrupt)):
                    self.cancel_event.set()
                return ScheduleRegistrationResult(result.verified,
                    result.detail+' 예약 접수 후 대화창 정리에서 중단했습니다: '+self._cleanup_error,
                    result.acknowledged, self._cleanup_error)
            return result
        except BaseException as error:
            if self.journal.safe_to_submit(row):
                if claimed:
                    self.capacity.release_claim(reservation_key(row))
                self.journal.mark_rejected_before_submit(row, evidence={'error': str(error)})
                if isinstance(error, (MacroSendCancelled, KeyboardInterrupt, InterruptedError)):
                    self.cancel_event.set()
                    raise MacroSendCancelled(str(error)) from error
                if self._driver is not None and not self.cancel_event.is_set():
                    try:
                        self.driver.discard_owned_draft()
                    except (MacroSendCancelled, KeyboardInterrupt, InterruptedError) as cancelled:
                        self.cancel_event.set()
                        raise MacroSendCancelled(str(cancelled)) from cancelled
                    except Exception:
                        # Keep the original pre-submit evidence even when cleanup
                        # cannot safely close the draft. The next run identifies it.
                        pass
                raise ScheduleNotSubmittedError('native_prepare', error) from error
            if self.journal.get(row).state == 'submitting':
                self.journal.mark_unknown(row, evidence={'error': str(error)})
            raise MacroSendUnknownError('native_submission', error) from error

    def close(self):
        if self._driver is not None:
            self._driver.close()
