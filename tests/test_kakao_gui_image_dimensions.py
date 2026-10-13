"""Image preflight metadata reaches the preview without any send operation."""
from pathlib import Path
from queue import Queue
from threading import Event
import unittest
from unittest.mock import Mock, patch

from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp, SMALL_IMAGE_WARNING_THRESHOLD_PX


class ImageDimensionPreviewTests(unittest.TestCase):
    def app(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "room", "", (Path("crop.png"),))]
        app.attachment_list = Mock()
        app.attachment_help = Mock()
        app.start_sending = Mock()
        return app

    def test_small_image_warning_uses_either_dimension_and_threshold_is_exclusive(self):
        app = self.app()
        paths = app.rows[0].attachments
        for width, height, small in ((320, 140, True), (700, 699, True),
                                     (699, 700, True), (700, 700, False)):
            app.handle_attachment_dimensions((paths, [{"width": width, "height": height}]))
            text = app.attachment_help.configure.call_args.kwargs["text"]
            self.assertIn(f"{width}x{height}", text)
            self.assertEqual(" - " in text, small)
        self.assertEqual(SMALL_IMAGE_WARNING_THRESHOLD_PX, 700)
        app.start_sending.assert_not_called()

    def test_stale_dimensions_do_not_appear_in_replacement_preview(self):
        app = self.app()
        app.handle_attachment_dimensions(((Path("old.png"),), [{"width": 320, "height": 140}]))
        self.assertNotIn("320x140", app.attachment_help.configure.call_args.kwargs["text"])
        app.start_sending.assert_not_called()

    def test_preview_worker_returns_local_preflight_metadata_and_closes_bridge(self):
        app = self.app()
        app.direct_config = Mock()
        app.direct_config.to_dict.return_value = {}
        app.ui_queue = Queue()
        completed = Event()
        app.post = lambda event, payload: (app.ui_queue.put((event, payload)), completed.set())
        bridge = Mock()
        bridge.prepare_attachments.return_value = {"attachments": [{"width": 320, "height": 140}]}
        with patch("kakao_sender_gui.KakaoDirectBridge", return_value=bridge) as factory:
            app.refresh_attachment_panel()
            self.assertTrue(completed.wait(5), "preflight completion event missing")
        event, payload = app.ui_queue.get_nowait()
        self.assertEqual(event, "attachment_dimensions")
        app.handle_attachment_dimensions(payload)
        self.assertIn("320x140", app.attachment_help.configure.call_args.kwargs["text"])
        factory.assert_called_once_with({}, direct_enabled=True)
        bridge.close.assert_called_once()
        bridge.send_attachments.assert_not_called()
        bridge.ready.assert_not_called()
        app.start_sending.assert_not_called()
