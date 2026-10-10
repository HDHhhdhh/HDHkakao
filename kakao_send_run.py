from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True, order=True)
class SendRunId:
    """Stable identity carried by every event produced by one send run."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("run ID cannot be empty")

    @classmethod
    def new(cls) -> "SendRunId":
        return cls(uuid.uuid4().hex)

    def __str__(self) -> str:
        return self.value


_RESULT_SCALAR_TYPES = (str, int, float, bool, type(None))


@dataclass(frozen=True, slots=True)
class SendRunResult(Mapping[str, Any]):
    """Immutable snapshot of one legacy-shaped result dictionary."""

    record: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.record, Mapping):
            raise TypeError("result record must be a mapping")
        if any(not isinstance(key, str) for key in self.record):
            raise TypeError("result record keys must be strings")
        if any(not isinstance(value, _RESULT_SCALAR_TYPES) for value in self.record.values()):
            raise TypeError("result record values must be scalar")
        object.__setattr__(self, "record", MappingProxyType(dict(self.record)))

    def __getitem__(self, key: str) -> Any:
        return self.record[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.record)

    def __len__(self) -> int:
        return len(self.record)

    def to_legacy(self) -> dict[str, Any]:
        """Return a detached mutable dictionary for existing log/UI adapters."""

        return dict(self.record)


@dataclass(frozen=True, slots=True)
class SendRunFailure:
    phase: str
    exception_type: str
    message: str
    operation: str | None = None

    @classmethod
    def capture(
        cls,
        phase: str,
        error: Exception,
        *,
        operation: str | None = None,
    ) -> "SendRunFailure":
        return cls(
            phase=phase,
            exception_type=type(error).__name__,
            message=str(error),
            operation=operation,
        )


@dataclass(frozen=True, slots=True)
class SendRunProgress:
    """Immutable run envelope; payload may be an explicit mutable capability."""

    run_id: SendRunId
    kind: str
    payload: Any = None

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("progress event kind cannot be empty")


@dataclass(frozen=True, slots=True)
class SendRunCompleted:
    run_id: SendRunId
    results: tuple[SendRunResult, ...]
    cancelled: bool = False
    failure: SendRunFailure | None = None
    finalizer_failures: tuple[SendRunFailure, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.cancelled and self.failure is None and not self.finalizer_failures

    def legacy_results(self) -> list[dict[str, Any]]:
        """Return detached records suitable for assigning on the Tk thread."""

        return [result.to_legacy() for result in self.results]


SendRunEvent: TypeAlias = SendRunProgress | SendRunCompleted
SendRunEventSink: TypeAlias = Callable[[SendRunEvent], None]
SendRunWorker: TypeAlias = Callable[["SendRunContext"], None]
SendRunFinalizer: TypeAlias = Callable[[], None]


class SendRunCancelled(Exception):
    """Raised cooperatively when a worker observes a cancellation request."""


def _finalizer_name(finalizer: SendRunFinalizer) -> str:
    return str(
        getattr(finalizer, "__name__", None)
        or getattr(finalizer, "__qualname__", None)
        or type(finalizer).__name__
    )


class SendRunContext:
    """Worker-facing capabilities owned by one supervisor transaction."""

    __slots__ = (
        "_cancel_event",
        "_closed",
        "_emit_event",
        "_finalized",
        "_finalizers",
        "_lock",
        "_results",
        "_result_observer",
        "_attempt_observer",
        "run_id",
    )

    def __init__(
        self,
        run_id: SendRunId,
        cancel_event: threading.Event,
        emit_event: SendRunEventSink,
    ) -> None:
        self.run_id = run_id
        self._cancel_event = cancel_event
        self._emit_event = emit_event
        self._lock = threading.RLock()
        self._results: list[SendRunResult] = []
        self._result_observer: Callable[[Mapping[str, Any]], None] | None = None
        self._attempt_observer: Callable[[int, str], None] | None = None
        self._finalizers: list[SendRunFinalizer] = []
        self._closed = False
        self._finalized = False

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise SendRunCancelled("send run cancelled")

    def emit(self, kind: str, payload: Any = None) -> None:
        with self._lock:
            self._ensure_open()
            self._emit_event(SendRunProgress(self.run_id, kind, payload))

    def add_result(self, record: Mapping[str, Any]) -> SendRunResult:
        result = SendRunResult(record)
        with self._lock:
            self._ensure_open()
            if self._result_observer is not None:
                self._result_observer(result)
            self._results.append(result)
        return result

    def observe_results(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        with self._lock:
            self._ensure_open()
            self._result_observer = callback

    def observe_attempts(self, callback: Callable[[int, str], None]) -> None:
        with self._lock:
            self._ensure_open()
            self._attempt_observer = callback

    def mark_attempt(self, row_index: int, route: str) -> None:
        """Durably record intent before entering an irreversible transport."""
        self.raise_if_cancelled()
        with self._lock:
            self._ensure_open()
            if self._attempt_observer is not None:
                self._attempt_observer(row_index, route)
        self.raise_if_cancelled()

    def register_finalizer(self, finalizer: SendRunFinalizer) -> None:
        if not callable(finalizer):
            raise TypeError("finalizer must be callable")
        with self._lock:
            self._ensure_open()
            self._finalizers.append(finalizer)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("send run context is closed")

    def _close_and_snapshot(
        self,
    ) -> tuple[tuple[SendRunResult, ...], tuple[SendRunFinalizer, ...]]:
        with self._lock:
            if self._closed:
                raise RuntimeError("send run context was already closed")
            self._closed = True
            results = tuple(self._results)
            finalizers = tuple(reversed(self._finalizers))
            self._finalizers.clear()
            return results, finalizers

    def _mark_finalized(self) -> None:
        with self._lock:
            if self._finalized:
                raise RuntimeError("send run finalizers were already executed")
            self._finalized = True


class SendRunSupervisor:
    """One-shot transaction boundary for a background send worker."""

    def __init__(
        self,
        event_sink: SendRunEventSink,
        *,
        run_id: SendRunId | None = None,
    ) -> None:
        if not callable(event_sink):
            raise TypeError("event sink must be callable")
        self.run_id = run_id or SendRunId.new()
        self._event_sink = event_sink
        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._started = False
        self._completed: SendRunCompleted | None = None

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def completed(self) -> SendRunCompleted | None:
        with self._state_lock:
            return self._completed

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def run(self, worker: SendRunWorker) -> SendRunCompleted:
        if not callable(worker):
            raise TypeError("worker must be callable")
        with self._state_lock:
            if self._started:
                raise RuntimeError("send run supervisor can only run once")
            self._started = True

        context = SendRunContext(self.run_id, self._cancel_event, self._event_sink)
        failure: SendRunFailure | None = None
        cancelled = False
        try:
            worker(context)
        except SendRunCancelled:
            cancelled = True
        except Exception as error:
            failure = SendRunFailure.capture("worker", error)
        finally:
            results, finalizers = context._close_and_snapshot()
            finalizer_failures: list[SendRunFailure] = []
            for finalizer in finalizers:
                try:
                    finalizer()
                except Exception as error:
                    finalizer_failures.append(
                        SendRunFailure.capture(
                            "finalizer",
                            error,
                            operation=_finalizer_name(finalizer),
                        )
                    )
            context._mark_finalized()

            completed = SendRunCompleted(
                run_id=self.run_id,
                results=results,
                cancelled=cancelled or self._cancel_event.is_set(),
                failure=failure,
                finalizer_failures=tuple(finalizer_failures),
            )
            with self._state_lock:
                self._completed = completed
            self._event_sink(completed)

        return completed


__all__ = [
    "SendRunCancelled",
    "SendRunCompleted",
    "SendRunContext",
    "SendRunEvent",
    "SendRunFailure",
    "SendRunId",
    "SendRunProgress",
    "SendRunResult",
    "SendRunSupervisor",
]
