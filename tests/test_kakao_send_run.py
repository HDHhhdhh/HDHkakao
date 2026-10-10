from __future__ import annotations

import threading
import unittest

from kakao_send_run import (
    SendRunCompleted,
    SendRunId,
    SendRunProgress,
    SendRunResult,
    SendRunSupervisor,
)


class SendRunSupervisorTests(unittest.TestCase):
    def make_supervisor(self):
        events = []
        supervisor = SendRunSupervisor(
            events.append,
            run_id=SendRunId("test-run"),
        )
        return supervisor, events

    def test_success_emits_progress_then_one_completed_event(self) -> None:
        supervisor, events = self.make_supervisor()
        finalized = []

        def worker(context) -> None:
            context.register_finalizer(lambda: finalized.append("closed"))
            context.emit("status", "starting")
            context.add_result({"row": 1, "status": "sent"})

        completed = supervisor.run(worker)

        self.assertTrue(completed.succeeded)
        self.assertEqual(finalized, ["closed"])
        self.assertEqual(
            [(event.run_id, event.kind, event.payload) for event in events[:-1]],
            [(SendRunId("test-run"), "status", "starting")],
        )
        self.assertIs(events[-1], completed)
        self.assertEqual(sum(isinstance(event, SendRunCompleted) for event in events), 1)
        self.assertEqual(completed.legacy_results(), [{"row": 1, "status": "sent"}])

    def test_unexpected_worker_failure_is_captured_and_cleanup_still_runs(self) -> None:
        supervisor, events = self.make_supervisor()
        finalized = []

        def worker(context) -> None:
            context.register_finalizer(lambda: finalized.append("closed"))
            raise ValueError("focus failed")

        completed = supervisor.run(worker)

        self.assertFalse(completed.succeeded)
        self.assertFalse(completed.cancelled)
        self.assertEqual(finalized, ["closed"])
        self.assertIsNotNone(completed.failure)
        self.assertEqual(completed.failure.phase, "worker")
        self.assertEqual(completed.failure.exception_type, "ValueError")
        self.assertEqual(completed.failure.message, "focus failed")
        self.assertEqual(events, [completed])

    def test_finalizer_failure_is_captured_without_skipping_other_finalizers(self) -> None:
        supervisor, events = self.make_supervisor()
        finalized = []

        def failing_finalizer() -> None:
            finalized.append("failed")
            raise OSError("bridge close failed")

        def worker(context) -> None:
            context.register_finalizer(lambda: finalized.append("first"))
            context.register_finalizer(failing_finalizer)
            context.register_finalizer(lambda: finalized.append("last"))

        completed = supervisor.run(worker)

        self.assertEqual(finalized, ["last", "failed", "first"])
        self.assertIsNone(completed.failure)
        self.assertFalse(completed.succeeded)
        self.assertEqual(len(completed.finalizer_failures), 1)
        cleanup_failure = completed.finalizer_failures[0]
        self.assertEqual(cleanup_failure.phase, "finalizer")
        self.assertEqual(cleanup_failure.exception_type, "OSError")
        self.assertEqual(cleanup_failure.message, "bridge close failed")
        self.assertEqual(cleanup_failure.operation, "failing_finalizer")
        self.assertEqual(events, [completed])

    def test_second_run_is_rejected_without_another_terminal_or_finalizer(self) -> None:
        supervisor, events = self.make_supervisor()
        finalizer_calls = []

        def worker(context) -> None:
            context.register_finalizer(lambda: finalizer_calls.append("closed"))

        first = supervisor.run(worker)
        with self.assertRaisesRegex(RuntimeError, "only run once"):
            supervisor.run(worker)

        self.assertIs(supervisor.completed, first)
        self.assertEqual(finalizer_calls, ["closed"])
        self.assertEqual(sum(isinstance(event, SendRunCompleted) for event in events), 1)

    def test_result_is_an_immutable_snapshot_and_legacy_copy_is_detached(self) -> None:
        supervisor, _ = self.make_supervisor()
        original = {
            "status": "sent",
            "detail": "direct",
        }

        def worker(context) -> None:
            context.add_result(original)
            original["status"] = "changed"
            original["detail"] = "changed"

        completed = supervisor.run(worker)
        result = completed.results[0]

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["detail"], "direct")
        with self.assertRaises(TypeError):
            result.record["status"] = "changed"

        legacy = completed.legacy_results()
        legacy[0]["detail"] = "adapter mutation"
        self.assertEqual(result["detail"], "direct")

        with self.assertRaisesRegex(TypeError, "values must be scalar"):
            SendRunResult({"detail": {"nested": "unsupported"}})

    def test_cancellation_event_is_shared_with_context_and_terminal(self) -> None:
        supervisor, events = self.make_supervisor()
        worker_started = threading.Event()
        worker_finished = threading.Event()
        observed = []

        def worker(context) -> None:
            observed.append(context.cancel_event is supervisor.cancel_event)
            worker_started.set()
            self.assertTrue(context.cancel_event.wait(timeout=1))
            observed.append(context.cancel_requested)
            context.raise_if_cancelled()
            worker_finished.set()

        thread = threading.Thread(target=lambda: supervisor.run(worker))
        thread.start()
        self.assertTrue(worker_started.wait(timeout=1))
        supervisor.request_cancel()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertFalse(worker_finished.is_set())
        self.assertEqual(observed, [True, True])
        completed = supervisor.completed
        self.assertIsNotNone(completed)
        self.assertTrue(completed.cancelled)
        self.assertFalse(completed.succeeded)
        self.assertIsNone(completed.failure)
        self.assertEqual(events, [completed])

    def test_progress_event_envelope_and_run_id_are_immutable(self) -> None:
        payload = []
        event = SendRunProgress(SendRunId("immutable"), "status", payload)

        with self.assertRaises((AttributeError, TypeError)):
            event.kind = "changed"
        with self.assertRaises((AttributeError, TypeError)):
            event.run_id.value = "changed"
        payload.append("confirmation capability stays mutable by design")
        self.assertEqual(event.payload, payload)


if __name__ == "__main__":
    unittest.main()
