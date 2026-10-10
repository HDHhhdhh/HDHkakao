import queue
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from kakao_sender import MessageRow
from kakao_send_run import SendRunCompleted
from kakao_sender_gui import KakaoSenderApp, SendRunRequest


class ThreadBoundVar:
    def get(self) -> str:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Tkinter state was read from the worker thread")
        return "unused"


class FakePyAutoGui:
    PAUSE = 0.0


class GuiWorkerThreadSafetyTests(unittest.TestCase):
    def test_send_worker_uses_run_snapshot_without_reading_tkinter_state(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        posted_events = []
        setattr(app, "post", lambda event, payload: posted_events.append((event, payload)))
        setattr(app, "countdown_var", ThreadBoundVar())
        setattr(app, "coord_x_var", ThreadBoundVar())
        setattr(app, "delay_scale_var", ThreadBoundVar())
        setattr(app, "continue_error_var", ThreadBoundVar())
        setattr(app, "confirm_each_var", ThreadBoundVar())
        request = SendRunRequest(
            selected=(),
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": False},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
        )
        fake_pyautogui = FakePyAutoGui()
        failures = []

        def run_worker():
            try:
                app.send_worker(request)
            except Exception as exc:
                failures.append(exc)

        with (
            patch("kakao_sender_gui.require_pyautogui", return_value=fake_pyautogui),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
        ):
            worker = threading.Thread(target=run_worker)
            worker.start()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(posted_events[-1][0], "run_event")
        self.assertIsInstance(posted_events[-1][1], SendRunCompleted)

    def test_confirm_row_uses_ui_queue_instead_of_worker_tk_callback(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        posted_events = []
        setattr(app, "post", lambda event, payload: posted_events.append((event, payload)))
        row = MessageRow(2, "테스트방", "안녕하세요")
        result = []

        worker = threading.Thread(
            target=lambda: result.append(app.confirm_row(row, 1, 1, datetime(2026, 7, 12, 9, 0), True))
        )
        worker.start()
        worker.join(timeout=0.1)

        self.assertTrue(worker.is_alive())
        self.assertEqual(len(posted_events), 1)
        event, payload = posted_events[0]
        self.assertEqual(event, "confirm")
        self.assertIsInstance(payload[-1], queue.Queue)

        payload[-1].put("skip")
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["skip"])
