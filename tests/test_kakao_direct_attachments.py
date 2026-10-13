from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from kakao_direct import (
    DirectBridgeDisconnectedError, DirectBridgeError, DirectKakaoConfig, DirectSendDisabledError,
    DirectSendError, DirectSendUnknownError, KakaoDirectBridge, validate_direct_attachments,
)


def receipt(kind="file", log_id="901"):
    return {"success": True, "status_code": 0, "chat_id": "777", "log_id": log_id,
            "sent_at": 1788800000, "kind": kind}


def batch(text=True):
    last = receipt()
    return {**last, "attachment_count": 1, "results": ([receipt("text", "900")] if text else []) + [last]}


class DirectAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / "example.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 synthetic")
        self.bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True, mappings={"room": "777"}))
        self.bridge.ready = Mock()
        self.bridge._request = Mock(return_value=batch())

    def test_text_then_attachment_receipts_and_entire_batch_timeout(self):
        result = self.bridge.send_attachments("room", "hello", attachments=(self.pdf,))
        self.assertEqual([r["kind"] for r in result["results"]], ["text", "file"])
        self.assertEqual(result["attachment_count"], 1)
        args, kwargs = self.bridge._request.call_args
        self.assertEqual(args, ("send_attachments",))
        self.assertEqual(kwargs["chat_id"], "777")
        self.assertEqual(kwargs["attachments"], [str(self.pdf.resolve())])
        self.assertGreaterEqual(kwargs["timeout"], 210)

    def test_run_preparation_is_local_only_and_passes_explicit_run_identity(self):
        self.bridge._request.return_value = {"attachment_count": 1, "total_bytes": self.pdf.stat().st_size}
        self.bridge.prepare_attachments(attachments=(self.pdf,), attachment_run_id="run-1")
        self.bridge.ready.assert_not_called()
        args, kwargs = self.bridge._request.call_args
        self.assertEqual(args, ("prepare_attachments",))
        self.assertEqual(kwargs["attachments"], [str(self.pdf.resolve())])
        self.assertEqual(kwargs["attachment_run_id"], "run-1")
        self.bridge._request.return_value = batch()
        self.bridge.send_attachments(chat_id="777", text="hello", attachments=(self.pdf,), attachment_run_id="run-1")
        self.assertEqual(self.bridge._request.call_args.kwargs["attachment_run_id"], "run-1")

    def test_invalid_preparation_results_and_run_ids_are_rejected(self):
        for result in ({}, {"attachment_count": True, "total_bytes": 1},
                       {"attachment_count": 1, "total_bytes": True}, {"attachment_count": 1, "total_bytes": 0}):
            self.bridge._request.return_value = result
            with self.subTest(result=result), self.assertRaises(DirectBridgeError) as raised:
                self.bridge.prepare_attachments(attachments=(self.pdf,), attachment_run_id="run")
            self.assertEqual(raised.exception.code, "invalid_attachment_preparation")
        self.bridge._request.reset_mock()
        for run_id in ("", "../run", "x" * 129, 123):
            with self.subTest(run_id=run_id), self.assertRaises(DirectSendError):
                self.bridge.send_attachments(chat_id="777", attachments=(self.pdf,), attachment_run_id=run_id)
        self.bridge.ready.assert_not_called()
        self.bridge._request.assert_not_called()

    def test_release_does_not_start_an_inactive_child(self):
        self.bridge.release_attachments("run")
        self.bridge._request.assert_not_called()
        self.bridge._process = Mock()
        self.bridge._process.poll.return_value = None
        self.bridge.release_attachments("run")
        self.bridge._request.assert_called_once_with("release_attachments", timeout=2.0, attachment_run_id="run")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_real_bridge_preflight_reuses_buffers_without_touching_vault_or_network(self):
        sdk = Path(self.temp.name) / "sdk"
        sdk.mkdir()
        (sdk / "package.json").write_text(json.dumps({"name": "agent-messenger", "version": "2.37.1", "type": "module"}), encoding="utf-8")
        bson = sdk / "node_modules" / "bson"
        bson.mkdir(parents=True)
        (bson / "index.js").write_text("module.exports={Long:{fromString:v=>v}};", encoding="utf-8")
        trace = Path(self.temp.name) / "media-trace.jsonl"
        entry = sdk / "index.mjs"
        entry.write_text("""
import { appendFileSync } from 'node:fs';
import net from 'node:net';
net.Socket.prototype.connect = function() { throw new Error('NETWORK FORBIDDEN'); };
globalThis.fetch = () => { throw new Error('NETWORK FORBIDDEN'); };
export class KakaoTalkClient { constructor() { throw new Error('LOGIN FORBIDDEN'); } }
export function detectImageDimensions(data) {
  appendFileSync(TRACE, JSON.stringify({ event: 'detect', bytes: data.toString() }) + '\\n');
  return { width: 2, height: 3, mimeType: 'image/png' };
}
""".replace("TRACE", json.dumps(str(trace))), encoding="utf-8")
        class ForbiddenVault:
            def load_snapshot(self):
                raise AssertionError("preflight must not access credentials")
        photo = Path(self.temp.name) / "frozen.png"
        photo.write_bytes(b"first snapshot")
        config = DirectKakaoConfig(enabled=True, node_path=shutil.which("node"), sdk_root=entry, request_timeout=3.0)
        bridge = KakaoDirectBridge(config, credential_vault=ForbiddenVault())
        try:
            prepared = bridge.prepare_attachments(attachments=(photo,), attachment_run_id="first-run")
            self.assertEqual(prepared["attachments"], [{"width": 2, "height": 3}])
            photo.write_bytes(b"next snapshot")
            bridge.prepare_attachments(attachments=(photo,), attachment_run_id="first-run")
            bridge.release_attachments("first-run")
            bridge.prepare_attachments(attachments=(photo,), attachment_run_id="second-run")
        finally:
            bridge.close()
        records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records, [{"event": "detect", "bytes": "first snapshot"},
                                   {"event": "detect", "bytes": "next snapshot"}])

    def test_file_only_and_blank_body_have_no_text_receipt(self):
        self.bridge._request.return_value = batch(False)
        for text in ("", " \n", "\u0085", "\u001c\u001d\u001e\u001f"):
            result = self.bridge.send_attachments(chat_id="777", text=text, attachments=(self.pdf,))
            self.assertEqual(len(result["results"]), 1)

    def test_bom_only_body_keeps_original_text_and_requires_its_receipt(self):
        text = "\ufeff"
        result = self.bridge.send_attachments(chat_id="777", text=text, attachments=(self.pdf,))
        self.assertEqual([r["kind"] for r in result["results"]], ["text", "file"])
        self.assertEqual(self.bridge._request.call_args.kwargs["text"], text)

    def test_disabled_or_invalid_file_batch_never_starts_bridge(self):
        self.bridge.config.enabled = False
        with self.assertRaises(DirectSendDisabledError):
            self.bridge.send_attachments(chat_id="777", attachments=(self.pdf,))
        self.bridge.config.enabled = True
        for paths in ((), (self.pdf,) * 11, (self.pdf, self.pdf.with_name("missing.pdf"))):
            with self.subTest(paths=paths), self.assertRaises(DirectSendError):
                self.bridge.send_attachments(chat_id="777", text="must not send", attachments=paths)
        self.bridge.ready.assert_not_called()
        self.bridge._request.assert_not_called()

    def test_type_size_and_total_preflight(self):
        forbidden = self.pdf.with_suffix(".exe")
        forbidden.write_bytes(b"synthetic")
        empty = self.pdf.with_name("empty.txt")
        empty.write_bytes(b"")
        for path in (forbidden, empty, self.pdf.parent):
            with self.subTest(path=path), self.assertRaises(DirectSendError):
                validate_direct_attachments((path,))
        with patch("kakao_direct.MAX_DIRECT_ATTACHMENT_BYTES", 3), self.assertRaises(DirectSendError):
            validate_direct_attachments((self.pdf,))
        with patch("kakao_direct.MAX_DIRECT_ATTACHMENT_TOTAL_BYTES", self.pdf.stat().st_size), self.assertRaises(DirectSendError):
            validate_direct_attachments((self.pdf, self.pdf))

    def test_invalid_target_ids_are_rejected_before_upload_or_login(self):
        for chat_id in ("0", "00777", "-1", "７", "9223372036854775808"):
            with self.subTest(chat_id=chat_id), self.assertRaises(DirectSendError) as raised:
                self.bridge.send_attachments(chat_id=chat_id, attachments=(self.pdf,))
            self.assertEqual(raised.exception.code, "invalid_chat_id")
        self.bridge.ready.assert_not_called()
        self.bridge._request.assert_not_called()

    def test_incomplete_malformed_or_inconsistent_receipts_are_unknown(self):
        variants = [{}, {**batch(), "attachment_count": True}, {**batch(), "results": [receipt()]},
                    {**batch(), "chat_id": "999"}, {**batch(), "log_id": "902"},
                    {**batch(), "results": [receipt("text", "901"), receipt()]}]
        for change in ({"success": False}, {"status_code": -1}, {"status_code": False}, {"log_id": ""},
                       {"log_id": "9223372036854775808"}, {"sent_at": 0}, {"sent_at": float("nan")},
                       {"chat_id": "999"}, {"kind": "photo"}):
            result = copy.deepcopy(batch())
            result["results"][1].update(change)
            variants.append(result)
        for result in variants:
            with self.subTest(result=result):
                self.bridge._request.return_value = result
                with self.assertRaises(DirectSendUnknownError):
                    self.bridge.send_attachments(chat_id="777", text="hello", attachments=(self.pdf,))

    def test_receipt_redaction_drops_provider_fields(self):
        result = batch()
        result["token"] = "PRIVATE"
        result["results"][0]["payload"] = "PRIVATE"
        self.bridge._request.return_value = result
        clean = self.bridge.send_attachments(chat_id="777", text="hello", attachments=(self.pdf,))
        self.assertNotIn("PRIVATE", str(clean))

    def test_attachment_request_timeout_and_disconnect_are_unknown(self):
        bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True))
        bridge.start = Mock()
        bridge._write_frame = Mock()
        with self.assertRaises(DirectSendUnknownError):
            bridge._request("send_attachments", timeout=0.1)
        self.assertEqual(bridge._write_frame.call_count, 1)
        bridge._write_frame = Mock(side_effect=DirectBridgeDisconnectedError())
        with self.assertRaises(DirectSendUnknownError):
            bridge._request("send_attachments", timeout=0.1)
        self.assertEqual(bridge._write_frame.call_count, 1)

    def test_unknown_error_frame_is_not_retried(self):
        bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True))
        bridge.start = Mock()
        def respond(frame):
            bridge._pending[frame["id"]].put({"ok": False, "error": {
                "code": "send_outcome_unknown", "ambiguous": True, "kind": "unknown"}})
        bridge._write_frame = Mock(side_effect=respond)
        with self.assertRaises(DirectSendUnknownError):
            bridge._request("send_attachments", timeout=0.1)
        self.assertEqual(bridge._write_frame.call_count, 1)


if __name__ == "__main__":
    unittest.main()
