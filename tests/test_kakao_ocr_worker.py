"""Worker lifecycle/protocol tests. No OCR engine, screen or native input."""
from __future__ import annotations

import io
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kakao_native_schedule import NativeScheduleDriver
from kakao_ocr_worker import WindowsOcrSession
from kakao_windows_ocr import WindowsOcrError, _active_session, recognize_image, use_ocr_session


PAYLOAD = {'version': 1, 'width': 100, 'height': 50, 'language': 'ko',
           'lines': [{'text': '예약 메시지', 'rect': {'x': 2, 'y': 3, 'width': 60, 'height': 12},
                      'words': [{'text': '예약', 'rect': {'x': 2, 'y': 3, 'width': 20, 'height': 12}}]}]}


class FakeOutput:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False

    def feed(self, value):
        self.queue.put(value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False) + '\n').encode())

    def readline(self, size):
        return self.queue.get()[:size]

    def close(self):
        self.closed = True
        self.queue.put(b'')


class FakeProcess:
    def __init__(self, response=None, ready=None):
        self.stdout, self.stderr = FakeOutput(), io.BytesIO()
        self.stdout.feed({'version': 1, 'ready': True} if ready is None else ready)
        self.stdin = SimpleNamespace(write=self.write, flush=lambda: None, close=lambda: None)
        self.response = response
        self.returncode = None
        self.inputs = []
        self.terminated = False

    def write(self, raw):
        request = json.loads(raw)
        self.inputs.append(request)
        reply = ({'version': 1, 'id': request['id'], 'ok': True,
                  'result': PAYLOAD | {'language': request['language']}}
                 if self.response is None else self.response(request))
        if reply is not None:
            self.stdout.feed(reply)
        return len(raw)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    kill = terminate

    def wait(self, timeout):
        return self.returncode


class OcrWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.image = Path(self.temp.name) / "한글 ' $() 파일.png"
        self.image.write_bytes(b'first saved image')

    def session(self, process=None, **kwargs):
        process = process or FakeProcess()
        session = WindowsOcrSession(**kwargs)
        self.addCleanup(session.close)
        return session, process

    def test_one_hidden_process_for_batch_and_exact_content_cache(self):
        session, process = self.session()
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process) as start:
            first = session.recognize_image(self.image)
            second_file = self.image.with_name('second.png')
            second_file.write_bytes(self.image.read_bytes())
            self.assertEqual(session.recognize_image(second_file), first)
            self.image.write_bytes(b'new saved image')
            session.recognize_image(self.image)
        self.assertEqual((session.launches, session.requests, session.cache_hits), (1, 2, 1))
        command = start.call_args.args[0]
        self.assertEqual(command[-1], '-Server')
        self.assertTrue(start.call_args.kwargs['creationflags'])
        self.assertNotIn('shell', start.call_args.kwargs)
        self.assertEqual(process.inputs[0]['path'], str(self.image.resolve()))
        session.close()
        self.assertTrue(process.terminated)
        self.assertTrue(all(not thread.is_alive() for thread in session._threads))

    def test_cache_separates_language_and_scale(self):
        session, process = self.session()
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            session.recognize_image(self.image, 'ko', scale=1)
            session.recognize_image(self.image, 'ko', scale=2)
            session.recognize_image(self.image, 'en-US', scale=2)
            session.recognize_image(self.image, 'KO', scale=2)
        self.assertEqual((session.requests, session.cache_hits), (3, 1))

    def test_bounded_cache_evicts_oldest_image(self):
        session, process = self.session()
        session.CACHE_LIMIT = 2
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            for value in (b'a', b'b', b'c', b'a'):
                self.image.write_bytes(value)
                session.recognize_image(self.image)
        self.assertEqual((session.requests, len(session._cache)), (4, 2))

    def test_changed_input_is_not_accepted_or_cached(self):
        def change(request):
            Path(request['path']).write_bytes(b'mutated while reading')
            return {'version': 1, 'id': request['id'], 'ok': True, 'result': PAYLOAD}
        session, process = self.session(FakeProcess(change))
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(WindowsOcrError, 'changed while'):
                session.recognize_image(self.image)
        self.assertFalse(session._cache)

    def test_invalid_reply_stops_worker_without_retry(self):
        replies = [b'not json\n', b'\xff\n', b'{}',
                   {'version': True, 'id': 'bad', 'ok': True},
                   {'version': 1, 'id': 'wrong image', 'ok': True, 'result': PAYLOAD},
                   {'version': 1, 'id': None, 'ok': 1}]
        for reply in replies:
            with self.subTest(reply=reply):
                session, process = self.session(FakeProcess(lambda _: reply))
                with patch('kakao_ocr_worker.subprocess.Popen', return_value=process) as start:
                    with self.assertRaises(WindowsOcrError):
                        session.recognize_image(self.image)
                    with self.assertRaises(WindowsOcrError):
                        session.recognize_image(self.image)
                    self.assertEqual(start.call_count, 1)
                self.assertTrue(process.terminated)

    def test_invalid_coordinates_still_fail_validation(self):
        payload = PAYLOAD | {'width': 1}
        session, process = self.session(FakeProcess(lambda req: {'version': 1, 'id': req['id'], 'ok': True, 'result': payload}))
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(WindowsOcrError, 'outside'):
                session.recognize_image(self.image)
        self.assertTrue(process.terminated)

    def test_oversized_reply_is_rejected(self):
        session, process = self.session(FakeProcess(lambda _: b'x' * 257 + b'\n'))
        session.MAX_REPLY_BYTES = 256
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(WindowsOcrError, 'oversized'):
                session.recognize_image(self.image)

    def test_image_error_does_not_poison_next_valid_image(self):
        def response(req):
            if len(process.inputs) == 1:
                return {'version': 1, 'id': req['id'], 'ok': False, 'error': 'invalid image'}
            return {'version': 1, 'id': req['id'], 'ok': True, 'result': PAYLOAD}
        session, process = self.session(FakeProcess(response))
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(WindowsOcrError, 'invalid image'):
                session.recognize_image(self.image)
            self.assertEqual(session.recognize_image(self.image).text, '예약 메시지')
        self.assertEqual(session.launches, 1)

    def test_timeout_ends_owned_worker(self):
        session, process = self.session(FakeProcess(lambda _: None))
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(WindowsOcrError, 'timed out'):
                session.recognize_image(self.image, timeout=0.05)
        self.assertTrue(process.terminated)

    def test_cancel_interrupts_worker_wait(self):
        cancel = threading.Event()
        def response(_):
            cancel.set()
            return None
        session, process = self.session(FakeProcess(response), cancel_event=cancel)
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            with self.assertRaises(InterruptedError):
                session.recognize_image(self.image)
        self.assertTrue(process.terminated)

    def test_dead_worker_cannot_return_cached_result(self):
        session, process = self.session()
        with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
            session.recognize_image(self.image)
            process.returncode = 1
            with self.assertRaisesRegex(WindowsOcrError, 'exited'):
                session.recognize_image(self.image)

    def test_startup_errors_cleanup_and_do_not_retry(self):
        for ready in ({'version': 1, 'ready': 1}, {'version': 1, 'ready': False}, b'broken\n'):
            with self.subTest(ready=ready):
                session, process = self.session(FakeProcess(ready=ready))
                with patch('kakao_ocr_worker.subprocess.Popen', return_value=process):
                    with self.assertRaises(WindowsOcrError):
                        session.recognize_image(self.image)
                self.assertTrue(process.terminated)
        session, _ = self.session()
        with patch('kakao_ocr_worker.subprocess.Popen', side_effect=OSError('not available')):
            with self.assertRaisesRegex(WindowsOcrError, 'Could not start'):
                session.recognize_image(self.image)

    def test_context_is_local_and_restored_even_on_error(self):
        session = Mock()
        other = Mock()
        observed = []
        self.assertIsNone(_active_session.get())
        with use_ocr_session(session):
            self.assertIs(recognize_image(self.image), session.recognize_image.return_value)
            thread = threading.Thread(target=lambda: observed.append(_active_session.get()))
            thread.start()
            thread.join()
            with self.assertRaises(RuntimeError), use_ocr_session(other):
                self.assertIs(_active_session.get(), other)
                raise RuntimeError('test')
            self.assertIs(_active_session.get(), session)
        self.assertEqual(observed, [None])
        self.assertIsNone(_active_session.get())

    def test_invalid_arguments_do_not_launch_and_close_is_idempotent(self):
        session, _ = self.session()
        with patch('kakao_ocr_worker.subprocess.Popen') as start:
            for args in ({'language': 'ko; code'}, {'scale': True}, {'timeout': -1}):
                with self.assertRaises(ValueError):
                    session.recognize_image(self.image, **args)
            start.assert_not_called()
        session.close()
        session.close()
        with self.assertRaisesRegex(WindowsOcrError, 'closed'):
            session.recognize_image(self.image)

    def test_native_close_restores_context_and_input_even_when_log_fails(self):
        driver = NativeScheduleDriver.__new__(NativeScheduleDriver)
        driver.input = SimpleNamespace(PAUSE=0.04, FAILSAFE=True)
        driver.previous_input_pause, driver.previous_input_failsafe = 0.1, False
        driver.previous_dpi = 123
        driver.user = Mock()
        driver.log = Mock(side_effect=OSError('disk full'))
        driver._ocr_session = Mock(launches=1, requests=2, cache_hits=3)
        session = driver._ocr_session
        driver._ocr_scope = use_ocr_session(session)
        driver._ocr_scope.__enter__()
        with self.assertRaises(OSError):
            driver.close()
        session.close.assert_called_once()
        self.assertIsNone(_active_session.get())
        self.assertEqual((driver.input.PAUSE, driver.input.FAILSAFE), (0.1, False))
        driver.user.SetThreadDpiAwarenessContext.assert_called_once_with(123)
        driver.close()

    def test_native_worker_close_failure_still_restores_context(self):
        driver = NativeScheduleDriver.__new__(NativeScheduleDriver)
        driver.input = SimpleNamespace(PAUSE=0.04, FAILSAFE=True)
        driver.previous_input_pause, driver.previous_input_failsafe = 0.1, False
        driver.previous_dpi = None
        driver.log = Mock()
        driver._ocr_session = Mock(launches=1, requests=2, cache_hits=3)
        driver._ocr_session.close.side_effect = OSError('process cleanup failed')
        driver._ocr_scope = use_ocr_session(driver._ocr_session)
        driver._ocr_scope.__enter__()
        with self.assertRaisesRegex(OSError, 'cleanup failed'):
            driver.close()
        self.assertIsNone(driver._ocr_session)
        self.assertIsNone(_active_session.get())
        self.assertEqual((driver.input.PAUSE, driver.input.FAILSAFE), (0.1, False))


if __name__ == '__main__':
    unittest.main()
