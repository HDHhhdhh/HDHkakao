"""Attachment orchestration with fake bridges; no filesystem uploads or network."""
from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock

from kakao_delivery import progress_summary
from kakao_direct import DirectSendError, DirectSendUnknownError
from kakao_send_run import SendRunId, SendRunSupervisor
from kakao_send_service import SendPorts, SendRunRequest, SendService, row_uses_direct_send, selected_rows_require_macro
from kakao_sender import MessageRow


def attachment_receipt(*, text=True):
    receipts = [{"success": True, "kind": "text", "log_id": "200"}] if text else []
    receipts += [
        {"success": True, "kind": "photo", "log_id": "201", "media_url": "private-url"},
        {"success": True, "kind": "file", "log_id": "202", "path": "private-path"},
    ]
    for receipt in receipts:
        receipt.update(status_code=0, chat_id="100", sent_at=1800000000)
    return {
        "success": True, "status_code": 0, "chat_id": "100", "sent_at": 1800000000,
        "attachment_count": 2, "log_id": "202", "results": receipts,
    }


class DirectAttachmentServiceTests(unittest.TestCase):
    def execute(self, outcome, *, text="fixture body", direct=True, blocked=False, confirm="send", config=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        attachments = tuple(Path(temporary.name) / name for name in ("sensitive-name.png", "sensitive-name.pdf"))
        for attachment in attachments:
            attachment.write_bytes(b"synthetic contents")
        row = MessageRow(2, "fixture room", text, attachments)
        bridge = SimpleNamespace(send_attachments=Mock(), send_message=Mock(), close=Mock(),
                                 prepare_attachments=Mock(), release_attachments=Mock())
        if isinstance(outcome, Exception):
            bridge.send_attachments.side_effect = outcome
        else:
            bridge.send_attachments.return_value = outcome
        ports = SendPorts(
            bridge_factory=Mock(return_value=bridge), bridge_close=lambda obj: obj.close(),
            confirm_row=Mock(return_value=confirm),
            require_pyautogui=Mock(return_value=SimpleNamespace(PAUSE=0)),
            focus_kakao_window=Mock(return_value="fixture UI"),
            send_one=Mock(), send_scheduled_one=Mock(), return_to_recent_chat_top=Mock(),
        )
        request = SendRunRequest(
            selected=((0, row),), config=config or {}, countdown=0, delay_scale=0,
            common_schedule=None, continue_on_error=True, confirm_each=False,
            direct_enabled=direct, chat_mappings=(("fixture room", "100"),),
            blocked_direct_names=("fixture room",) if blocked else (),
        )
        attempts = []
        supervisor = SendRunSupervisor(lambda event: None, run_id=SendRunId("attachment-service-fixture"))

        def work(context):
            context.observe_attempts(lambda index, route: attempts.append((index, route)))
            SendService(ports).execute(request, context)

        completed = supervisor.run(work)
        self.assertIsNone(completed.failure)
        if direct:
            ports.require_pyautogui.assert_not_called()
            ports.focus_kakao_window.assert_not_called()
            ports.send_one.assert_not_called()
            ports.send_scheduled_one.assert_not_called()
            ports.return_to_recent_chat_top.assert_not_called()
        bridge.send_message.assert_not_called()
        return completed.legacy_results()[0], row, ports, bridge, attempts

    def test_body_and_attachments_use_one_bridge_operation_and_bounded_receipts(self):
        result, row, ports, bridge, attempts = self.execute(attachment_receipt())
        bridge.send_attachments.assert_called_once()
        sent = bridge.send_attachments.call_args.kwargs
        self.assertEqual((sent["chat_id"], sent["text"]), ("100", row.text))
        self.assertEqual([path.name for path in sent["attachments"]], [path.name for path in row.attachments])
        self.assertNotEqual(sent["attachments"], row.attachments)
        bridge.prepare_attachments.assert_called_once_with(attachments=sent["attachments"], attachment_run_id=sent["attachment_run_id"])
        bridge.release_attachments.assert_called_once_with(attachment_run_id=sent["attachment_run_id"])
        self.assertTrue(all(not path.exists() for path in sent["attachments"]))
        bridge.close.assert_called_once_with()
        self.assertEqual(attempts, [(0, "direct")])
        self.assertEqual((result["status"], result["transport"]), ("direct_sent", "direct"))
        self.assertEqual(result["direct_receipt_count"], 3)
        self.assertEqual(result["direct_text_count"], 1)
        self.assertEqual(result["direct_photo_count"], 1)
        self.assertEqual(result["direct_file_count"], 1)
        self.assertEqual(result["direct_log_ids"], "200,201,202")
        self.assertEqual(result["retry_transport"], "none")
        self.assertNotIn("results", result)
        for private in ("sensitive-name", "private-path", "private-url", "fixture body"):
            self.assertNotIn(private, repr(result))

    def test_image_file_setting_reaches_direct_bridge(self):
        outcome = attachment_receipt()
        outcome["results"][1]["kind"] = "file"
        result, _, _, bridge, _ = self.execute(outcome, config={"image_send_as": "file"})
        self.assertEqual(bridge.send_attachments.call_args.kwargs["image_send_as"], "file")
        self.assertEqual(result["direct_photo_count"], 0)
        self.assertEqual(result["direct_file_count"], 2)

    def test_attachment_only_and_blank_body_do_not_require_text_receipt(self):
        for text in ("", " \n "):
            with self.subTest(text=text):
                result, _, _, bridge, _ = self.execute(attachment_receipt(text=False), text=text)
                self.assertEqual(result["status"], "direct_sent")
                self.assertEqual(result["direct_receipt_count"], 2)
                self.assertEqual(result["direct_text_count"], 0)
                self.assertEqual(bridge.send_attachments.call_args.kwargs["text"], text)

    def test_partial_or_unknown_transport_never_enters_retry_queue(self):
        for outcome in (
            DirectSendUnknownError(code="partial_send"),
            DirectSendUnknownError(code="timeout"),
            {"success": False, "partial": True, "results": [{"success": True, "kind": "text"}]},
            None,
            {"success": True, "attachment_count": 2, "results": []},
        ):
            with self.subTest(outcome=outcome):
                result, _, _, bridge, _ = self.execute(outcome)
                self.assertEqual((result["status"], result["retry_transport"]), ("unknown", "none"))
                self.assertIn("성공: 0", progress_summary(1, [result]))
                self.assertIn("재시도: 0", progress_summary(1, [result]))
                bridge.send_attachments.assert_called_once()

    def test_inconsistent_receipt_kind_is_unknown_not_retryable(self):
        receipt = attachment_receipt()
        receipt["results"][-1]["kind"] = ["file"]
        result, _, _, _, _ = self.execute(receipt)
        self.assertEqual((result["status"], result["retry_transport"]), ("unknown", "none"))

    def test_attachments_use_the_same_canonical_receipts_as_text(self):
        for key, value in (("sent_at", 1.5), ("sent_at", 9007199254740992),
                           ("log_id", "000202")):
            with self.subTest(key=key, value=value):
                receipt = attachment_receipt()
                receipt[key] = value
                if key == "sent_at":
                    for item in receipt["results"]:
                        item[key] = value
                else:
                    receipt["results"][-1][key] = value
                result, _, _, _, _ = self.execute(receipt)
                self.assertEqual((result["status"], result["retry_transport"]), ("unknown", "none"))

    def test_rejected_foreign_or_missing_receipts_cannot_report_success(self):
        for target in ("top", "file"):
            for key, value in (
                ("status_code", 1), ("status_code", False), ("chat_id", "999"),
                ("log_id", ""), ("log_id", "0"), ("log_id", "9223372036854775808"),
                ("sent_at", 0), ("sent_at", float("nan")), ("sent_at", float("inf")),
            ):
                with self.subTest(target=target, key=key, value=value):
                    receipt = attachment_receipt()
                    record = receipt if target == "top" else receipt["results"][-1]
                    record[key] = value
                    result, _, _, _, _ = self.execute(receipt)
                    self.assertEqual((result["status"], result["retry_transport"]), ("unknown", "none"))

    def test_duplicate_or_inconsistent_final_receipts_do_not_count_as_delivered_files(self):
        for mutation in ("duplicate", "final_id", "final_time", "missing_status"):
            with self.subTest(mutation=mutation):
                receipt = deepcopy(attachment_receipt())
                if mutation == "duplicate":
                    receipt["results"][1]["log_id"] = receipt["results"][0]["log_id"]
                elif mutation == "final_id":
                    receipt["log_id"] = "303"
                elif mutation == "final_time":
                    receipt["sent_at"] = 1800000001
                else:
                    del receipt["results"][1]["status_code"]
                result, _, _, _, _ = self.execute(receipt)
                self.assertEqual((result["status"], result["retry_transport"]), ("unknown", "none"))
                self.assertNotIn("direct_receipt_count", result)

    def test_explicit_pre_send_rejection_remains_available_for_manual_retry(self):
        result, _, _, _, _ = self.execute(DirectSendError(code="attachment_invalid"))
        self.assertEqual((result["status"], result["retry_transport"]), ("error", "macro"))

    def test_direct_off_keeps_attachment_screen_dispatch(self):
        result, row, ports, bridge, attempts = self.execute(None, direct=False)
        self.assertEqual((result["status"], result["transport"]), ("sent", "macro"))
        self.assertEqual(attempts, [(0, "macro")])
        ports.bridge_factory.assert_not_called()
        bridge.send_attachments.assert_not_called()
        ports.require_pyautogui.assert_called_once()
        ports.send_one.assert_called_once()
        self.assertIs(ports.send_one.call_args.args[2], row)

    def test_unmatched_attachment_is_excluded_without_initializing_a_bridge(self):
        result, _, ports, bridge, attempts = self.execute(None, blocked=True)
        self.assertEqual((result["transport"], result["retry_transport"]), ("excluded", "none"))
        self.assertEqual(attempts, [])
        ports.bridge_factory.assert_not_called()
        ports.confirm_row.assert_not_called()
        bridge.send_attachments.assert_not_called()

    def test_cancel_before_dispatch_does_not_send_body_or_attachments(self):
        result, _, _, bridge, attempts = self.execute(None, confirm="stop")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(attempts, [])
        bridge.send_attachments.assert_not_called()
        bridge.close.assert_called_once_with()

    def test_route_decision_includes_files_but_preserves_common_reservation(self):
        row = MessageRow(2, "fixture room", "", (Path("fixture.pdf"),))
        future = datetime(2030, 1, 1, 12)
        self.assertTrue(row_uses_direct_send(row, None, True))
        self.assertFalse(selected_rows_require_macro(((0, row),), None, True))
        self.assertFalse(row_uses_direct_send(row, future, True))
        self.assertTrue(selected_rows_require_macro(((0, row),), future, True))
        self.assertFalse(row_uses_direct_send(replace(row, attachments=()), None, True))


if __name__ == "__main__":
    unittest.main()
