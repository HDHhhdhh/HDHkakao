"""Missing or contradictory post-WRITE responses cannot become green success."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from kakao_direct import (DirectKakaoConfig, DirectSendError, DirectSendUnknownError,
                          KakaoDirectBridge, validate_direct_send_receipt)


class DirectTextReceiptTests(unittest.TestCase):
    def receipt(self):
        return {"success": True, "status_code": 0, "chat_id": "123", "log_id": "3924592365002973185", "sent_at": 1788785067}

    def test_server_receipt_preserves_lossless_id_and_drops_extra_fields(self):
        receipt = self.receipt()
        self.assertEqual(validate_direct_send_receipt({**receipt, "private": "discard"}, "123"), receipt)

    def test_each_required_field_must_be_present(self):
        for key in self.receipt():
            with self.subTest(missing=key):
                receipt = self.receipt()
                del receipt[key]
                with self.assertRaises(DirectSendUnknownError):
                    validate_direct_send_receipt(receipt, "123")

    def test_malformed_and_contradictory_receipts_are_unknown(self):
        changes = [{"success": value} for value in (False, 1, "true", None)]
        changes += [{"status_code": value} for value in (False, "0", -1, 1)]
        changes += [{"chat_id": value} for value in ("124", 123, "", None)]
        changes += [{"log_id": value} for value in ("", "0", "-1", "01", "1.0", "１", "9223372036854775808", 123, None)]
        changes += [{"sent_at": value} for value in (0, -1, True, "1", float("nan"), float("inf"), 1.5, 10**1000)]
        for change in changes:
            with self.subTest(field=next(iter(change))):
                with self.assertRaises(DirectSendUnknownError):
                    validate_direct_send_receipt({**self.receipt(), **change}, "123")
        for payload in (None, [], True, "ok", {}):
            with self.subTest(payload=payload), self.assertRaises(DirectSendUnknownError):
                validate_direct_send_receipt(payload, "123")

    def make_bridge(self, response):
        bridge = KakaoDirectBridge.__new__(KakaoDirectBridge)
        bridge.config = DirectKakaoConfig(enabled=True)
        bridge.ready = Mock()
        bridge._request = Mock(return_value=deepcopy(response))
        return bridge

    def test_incident_shape_status_zero_without_id_is_not_success_or_replayed(self):
        bridge = self.make_bridge({"success": True, "status_code": 0, "chat_id": "123", "log_id": "", "sent_at": 0})
        with self.assertRaises(DirectSendUnknownError):
            bridge.send_message(text="synthetic fixture", chat_id="123")
        bridge._request.assert_called_once_with("send_message", chat_id="123", text="synthetic fixture")

    def test_invalid_target_ids_are_rejected_before_starting_the_bridge(self):
        for chat_id in ("0", "00123", "-1", "１", "9223372036854775808", "9" * 200):
            with self.subTest(chat_id=chat_id):
                bridge = self.make_bridge(self.receipt())
                with self.assertRaises(DirectSendError) as raised:
                    bridge.send_message(text="synthetic", chat_id=chat_id)
                self.assertEqual(raised.exception.code, "invalid_chat_id")
                bridge.ready.assert_not_called()
                bridge._request.assert_not_called()

    def test_explicit_rejection_and_invalid_failure_are_distinguished(self):
        bridge = self.make_bridge({"success": False, "status_code": 429})
        with self.assertRaises(DirectSendError) as raised:
            bridge.send_message(text="synthetic fixture", chat_id="123")
        self.assertNotIsInstance(raised.exception, DirectSendUnknownError)
        for status in (None, -1, 0, False, "429"):
            with self.subTest(status=status):
                bridge = self.make_bridge({"success": False, "status_code": status})
                with self.assertRaises(DirectSendUnknownError):
                    bridge.send_message(text="synthetic fixture", chat_id="123")


if __name__ == "__main__":
    unittest.main()
