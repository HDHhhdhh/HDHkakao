from __future__ import annotations

import multiprocessing
import threading
import time
import unittest
import uuid

from kakao_pc_lease import PcUiLease, PcUiLeaseCancelled, PcUiLeaseTimeout


def _hold_named_lease(name, pipe):
    lease = PcUiLease(name)
    with lease.lease(timeout=3):
        pipe.send("acquired")
        # No multiprocessing Event: terminating a fixture while it owns an
        # Event's internal semaphore can itself strand the test cleanup.
        pipe.poll(10)
    pipe.close()


class PcUiLeaseTests(unittest.TestCase):
    def setUp(self):
        self.name = "Local\\KakaoTalkHub.test." + uuid.uuid4().hex
        self.lease = PcUiLease(self.name)

    def test_nested_same_thread_entry_and_legacy_api_are_reentrant(self):
        with self.lease:
            with self.lease.lease(timeout=0.01):
                self.assertTrue(self.lease.acquire(blocking=False))
                self.lease.release()
        self.assertTrue(self.lease.acquire(blocking=False))
        self.lease.release()

    def test_wait_can_be_cancelled_while_another_thread_owns_lease(self):
        event = threading.Event()
        result = []
        started = threading.Event()
        def wait():
            started.set()
            try:
                with self.lease.lease(cancel_event=event, timeout=3):
                    result.append("unexpected entry")
            except PcUiLeaseCancelled:
                result.append("cancelled")
        with self.lease:
            thread = threading.Thread(target=wait)
            thread.start()
            self.assertTrue(started.wait(1))
            event.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["cancelled"])

    def test_pre_cancelled_lease_does_not_enter(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(PcUiLeaseCancelled):
            with self.lease.lease(cancel_event=event):
                self.fail("cancelled operation entered the UI section")

    def test_separate_process_exclusion_and_abandoned_owner_recovery(self):
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe()
        process = context.Process(target=_hold_named_lease, args=(self.name, child_pipe))
        process.start()
        child_pipe.close()
        try:
            self.assertTrue(parent_pipe.poll(5), "test lease owner did not start")
            self.assertEqual(parent_pipe.recv(), "acquired")
            started = time.monotonic()
            with self.assertRaises(PcUiLeaseTimeout):
                with self.lease.lease(timeout=0.1):
                    self.fail("a second process entered an owned lease")
            self.assertLess(time.monotonic() - started, 1)
            # Terminate only the dedicated fixture process to model an owner
            # crash. No Kakao process or production UI is involved.
            process.terminate()
            process.join(3)
            self.assertFalse(process.is_alive())
            with self.lease.lease(timeout=1):
                pass
        finally:
            if process.is_alive():
                process.terminate()
            process.join(3)
            parent_pipe.close()

    def test_non_owner_thread_cannot_release_lease(self):
        errors = []
        def release():
            try:
                self.lease.release()
            except RuntimeError:
                errors.append("not-owner")
        with self.lease:
            thread = threading.Thread(target=release)
            thread.start()
            thread.join(1)
        self.assertEqual(errors, ["not-owner"])


if __name__ == "__main__":
    unittest.main()
