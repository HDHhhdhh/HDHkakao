from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path
from typing import Any

from kakao_direct import (
    DirectConfigLockTimeout,
    DirectKakaoConfig,
    direct_config_lock,
)


def _save_from_child(config_path: str, connection: Any, timeout: float) -> None:
    try:
        DirectKakaoConfig(mappings={"Child": "child-id"}).save(
            config_path,
            lock_timeout=timeout,
        )
    except DirectConfigLockTimeout:
        outcome = "timeout"
    except BaseException as exc:  # return only a non-sensitive exception class
        outcome = f"error:{type(exc).__name__}"
    else:
        outcome = "saved"
    try:
        connection.send(outcome)
    finally:
        connection.close()


class DirectConfigProcessLockTests(unittest.TestCase):
    def _run_child_save(self, config_path: Path, timeout: float) -> str:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_save_from_child,
            args=(str(config_path), sender, timeout),
        )
        process.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(10.0), "child lock result timed out")
            outcome = receiver.recv()
        finally:
            receiver.close()
            process.join(timeout=10.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5.0)
        self.assertEqual(process.exitcode, 0)
        return outcome

    def test_ordinary_save_times_out_behind_other_process_lock_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            DirectKakaoConfig(mappings={"Parent": "parent-id"}).save(config_path)
            original_bytes = config_path.read_bytes()

            with direct_config_lock(config_path):
                outcome = self._run_child_save(config_path, timeout=0.1)

            self.assertEqual(outcome, "timeout")
            self.assertEqual(config_path.read_bytes(), original_bytes)

            # Closing the owning handle releases the OS lock; the retained
            # adjacent lock file is safe to reuse by the next process.
            self.assertEqual(self._run_child_save(config_path, timeout=2.0), "saved")
            self.assertEqual(
                DirectKakaoConfig.load(config_path).mappings,
                {"child": "child-id"},
            )


if __name__ == "__main__":
    unittest.main()
