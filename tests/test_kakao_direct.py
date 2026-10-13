from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from kakao_direct import (
    DPAPI_ENTROPY,
    EXPECTED_SDK_VERSION,
    CredentialVault,
    CredentialVaultError,
    DirectBridgeError,
    DirectKakaoConfig,
    DirectSendDisabledError,
    DirectSendError,
    DirectSendUnknownError,
    KakaoDirectBridge,
    UnmappedChatError,
    discover_sdk_root,
    _sanitize_chat_record,
    normalize_chat_name,
    normalize_guarded_mapping_ids,
    normalize_mappings,
    resolve_chat_id,
)


class FakeCrypto:
    def CryptProtectData(self, data, description, entropy, prompt, reserved, flags):
        assert entropy == DPAPI_ENTROPY
        assert flags == 0
        return b"fake-dpapi:" + bytes(value ^ 0xA5 for value in bytes(data))

    def CryptUnprotectData(self, data, entropy, reserved, prompt, flags):
        assert entropy == DPAPI_ENTROPY
        assert flags == 0
        return (None, bytes(value ^ 0xA5 for value in bytes(data)[len(b"fake-dpapi:") :]))


class FailingCrypto:
    def CryptProtectData(self, *_args):
        raise RuntimeError("SECRET-OAUTH must never escape")


class FakeStdout:
    def __init__(self) -> None:
        self.frames: queue.Queue[str | None] = queue.Queue()

    def push(self, payload: dict[str, Any]) -> None:
        self.frames.put(json.dumps(payload) + "\n")

    def readline(self) -> str:
        value = self.frames.get()
        return "" if value is None else value


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process
        self.frames: list[dict[str, Any]] = []

    def write(self, value: str) -> int:
        payload = json.loads(value)
        self.frames.append(payload)
        self.process.handle(payload)
        if self.process.fail_send_write and payload.get("command") == "send_message":
            raise BrokenPipeError("write outcome is ambiguous")
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.process.stdout.frames.put(None)


class FakeProcess:
    def __init__(
        self,
        *,
        send_error: dict[str, Any] | None = None,
        fail_send_write: bool = False,
    ) -> None:
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self.send_error = send_error
        self.fail_send_write = fail_send_write
        self.stdout.push({"event": "ready", "protocol": 1, "direct_enabled": True, "authenticated": False})

    def handle(self, payload: dict[str, Any]) -> None:
        command = payload.get("command")
        if command == "list_chats":
            self.stdout.push({"id": payload["id"], "ok": True, "result": {"chats": []}})
        elif command == "send_message" and self.send_error:
            self.stdout.push({"id": payload["id"], "ok": False, "error": self.send_error})
        elif command == "send_message":
            self.stdout.push(
                {
                    "id": payload["id"],
                    "ok": True,
                    "result": {
                        "success": True,
                        "status_code": 0,
                        "chat_id": payload["chat_id"],
                        "log_id": "10001",
                        "sent_at": 1,
                    },
                }
            )
        elif command == "shutdown":
            self.stdout.push({"id": payload["id"], "ok": True, "result": {"closed": True}})
        else:
            self.stdout.push({"id": payload["id"], "ok": True, "result": {}})

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.frames.put(None)

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.frames.put(None)

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class NoCredentials:
    def load_snapshot(self):
        from kakao_direct import CredentialSnapshot
        return CredentialSnapshot(None, None)

    def load(self):
        return None

    def save(self, credentials):
        raise AssertionError("credential writes are not expected in this test")


class AuthenticatedFakeProcess(FakeProcess):
    """A replacement child starts without an authenticated SDK client."""

    def __init__(self) -> None:
        super().__init__()
        self.authenticated = False

    def handle(self, payload: dict[str, Any]) -> None:
        command = payload.get("command")
        if command == "login":
            self.authenticated = True
            self.stdout.push({"id": payload["id"], "ok": True, "result": {"authenticated": True}})
        elif command in {"list_chats", "send_message"} and not self.authenticated:
            self.stdout.push({"id": payload["id"], "ok": False, "error": {"code": "not_authenticated"}})
        else:
            super().handle(payload)


class DirectConfigTests(unittest.TestCase):
    def test_pinned_sdk_manifest_matches_runtime_compatibility_version(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        manifest = json.loads((project_root / "bridge" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["dependencies"]["agent-messenger"], EXPECTED_SDK_VERSION)

    def test_sdk_discovery_uses_only_local_install_or_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "bridge" / "node_modules" / "agent-messenger"
            checkout = root / "agent-messenger"
            legacy = root / "legacy-sdk"
            for path in (installed, checkout, legacy):
                path.mkdir(parents=True)
            with (
                patch("kakao_direct.__file__", str(root / "kakao_direct.py")),
                patch.dict("os.environ", {"KAKAO_AGENT_MESSENGER_ROOT": ""}),
            ):
                self.assertEqual(discover_sdk_root(), installed)
                installed.rmdir()
                self.assertEqual(discover_sdk_root(), installed)
                self.assertEqual(discover_sdk_root(root / "explicit"), root / "explicit")
                with patch.dict("os.environ", {"KAKAO_AGENT_MESSENGER_ROOT": str(legacy)}):
                    self.assertEqual(discover_sdk_root(), legacy)

    def test_default_direct_mode_is_off(self) -> None:
        config = DirectKakaoConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.mappings, {})
        self.assertEqual(config.guarded_mapping_ids, ())

    def test_guarded_mapping_ids_normalize_deduplicate_and_sort(self) -> None:
        self.assertEqual(
            normalize_guarded_mapping_ids([" 20 ", 3, "20", "001"]),
            ("001", "20", "3"),
        )
        with self.assertRaises(ValueError):
            normalize_guarded_mapping_ids("20")
        with self.assertRaises(ValueError):
            normalize_guarded_mapping_ids({"20": True})

    def test_config_prunes_guards_for_unmapped_ids(self) -> None:
        config = DirectKakaoConfig(
            mappings={"room": "20"},
            guarded_mapping_ids=[" 20 ", "stale", "20"],
        )
        self.assertEqual(config.guarded_mapping_ids, ("20",))

    def test_guarded_mapping_ids_roundtrip_and_bridge_config_copy(self) -> None:
        config = DirectKakaoConfig(
            enabled=True,
            mappings={"room": "20", "other": "3"},
            guarded_mapping_ids=("20", "3"),
        )
        payload = config.to_dict()
        self.assertEqual(payload["guarded_mapping_ids"], ["20", "3"])
        restored = DirectKakaoConfig.from_dict(payload)
        self.assertEqual(restored.guarded_mapping_ids, ("20", "3"))

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            config.save(config_path)
            self.assertEqual(
                DirectKakaoConfig.load(config_path).guarded_mapping_ids,
                ("20", "3"),
            )

        bridge = KakaoDirectBridge(
            config,
            credential_vault=NoCredentials(),
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
        )
        try:
            self.assertEqual(bridge.config.guarded_mapping_ids, ("20", "3"))
        finally:
            bridge.close()

    def test_chat_name_normalization_and_exact_mapping(self) -> None:
        self.assertEqual(normalize_chat_name("  Room\u00a0Name  "), "room name")
        self.assertEqual(resolve_chat_id(" ROOM  NAME ", {"Room Name": "123"}), "123")
        with self.assertRaises(UnmappedChatError):
            resolve_chat_id("Other", {"Room Name": "123"})

    def test_two_pc_names_cannot_map_to_one_chat_id(self) -> None:
        with self.assertRaises(ValueError):
            normalize_mappings({"수학방": "777", "홍길동": "777"})


class CredentialVaultTests(unittest.TestCase):
    def test_vault_persists_only_protection_api_output_and_roundtrips(self) -> None:
        credentials = {"oauth_token": "SECRET-OAUTH", "user_id": "42", "refresh_token": "SECRET-REFRESH"}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "loco-credentials.dat"
            vault = CredentialVault(path, crypto_module=FakeCrypto())
            vault.save(credentials)
            stored = path.read_bytes()
            self.assertNotIn(b"SECRET-OAUTH", stored)
            self.assertNotIn(b"SECRET-REFRESH", stored)
            self.assertEqual(vault.load(), credentials)

    def test_crypto_failure_text_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = CredentialVault(Path(temp_dir) / "loco-credentials.dat", crypto_module=FailingCrypto())
            with self.assertRaises(CredentialVaultError) as context:
                vault.save({"oauth_token": "SECRET-OAUTH", "user_id": "42"})
            self.assertNotIn("SECRET-OAUTH", str(context.exception))


class DirectBridgeTests(unittest.TestCase):
    def _authenticated_bridge(self):
        processes: list[AuthenticatedFakeProcess] = []

        def factory(*_args, **_kwargs):
            process = AuthenticatedFakeProcess()
            processes.append(process)
            return process

        bridge = KakaoDirectBridge(
            DirectKakaoConfig(enabled=True, mappings={"room": "123"}),
            credential_vault=NoCredentials(),
            popen_factory=factory,
        )
        bridge._credentials = {
            "oauth_token": "synthetic-only",
            "user_id": "10",
            "device_uuid": "synthetic-only",
            "device_type": "tablet",
        }
        return bridge, processes

    def test_restarted_child_logs_in_before_next_send_without_replaying_previous_send(self) -> None:
        bridge, processes = self._authenticated_bridge()
        try:
            bridge.send_message("room", "synthetic first")
            processes[0].terminate()
            bridge._reader_thread.join(timeout=1)
            bridge.send_message("room", "synthetic second")
            self.assertEqual(len(processes), 2)
            for process in processes:
                self.assertEqual(
                    [frame["command"] for frame in process.stdin.frames],
                    ["login", "send_message"],
                )
            self.assertEqual(processes[1].stdin.frames[-1]["text"], "synthetic second")
        finally:
            bridge.close()

    def test_stale_reader_cannot_change_replacement_readiness_or_disconnect_it(self) -> None:
        bridge, processes = self._authenticated_bridge()
        stale_reader = None
        try:
            bridge.list_chats()
            stale_reader = bridge._reader_thread
            # Simulate process exit being observed before stdout drains.
            processes[0].returncode = 0
            bridge.list_chats()
            processes[0].stdout.push({"event": "ready", "protocol": 999, "authenticated": False})
            stale_reader.join(timeout=1)
            self.assertFalse(stale_reader.is_alive())
            self.assertEqual(bridge._ready_info["protocol"], 1)
            self.assertTrue(bridge._authenticated)
            self.assertFalse(bridge._disconnected_event.is_set())
            bridge.send_message("room", "synthetic current child")
            self.assertEqual(processes[1].stdin.frames[-1]["command"], "send_message")
        finally:
            if processes:
                processes[0].stdout.frames.put(None)
            if stale_reader:
                stale_reader.join(timeout=1)
            bridge.close()

    def test_replacement_wakes_pending_send_as_unknown_without_replay(self) -> None:
        bridge, processes = self._authenticated_bridge()
        submitted = threading.Event()
        errors: list[Exception] = []
        sender = None
        old_reader = None
        try:
            bridge.ready()
            old_process = processes[0]
            old_reader = bridge._reader_thread
            original_handle = old_process.handle

            def consume_without_response(payload):
                if payload.get("command") == "send_message":
                    submitted.set()
                    return
                original_handle(payload)

            old_process.handle = consume_without_response

            def send():
                try:
                    bridge.send_message("room", "synthetic uncertain send")
                except Exception as exc:
                    errors.append(exc)

            sender = threading.Thread(target=send)
            sender.start()
            self.assertTrue(submitted.wait(timeout=1))
            old_process.returncode = 0
            bridge.start()
            sender.join(timeout=1)
            self.assertFalse(sender.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DirectSendUnknownError)
            self.assertEqual(
                sum(frame["command"] == "send_message" for process in processes for frame in process.stdin.frames),
                1,
            )
        finally:
            if processes:
                processes[0].stdout.frames.put(None)
            if sender:
                sender.join(timeout=1)
            if old_reader:
                old_reader.join(timeout=1)
            bridge.close()

    def _bridge(
        self,
        *,
        enabled: bool = True,
        send_error: dict[str, Any] | None = None,
        fail_send_write: bool = False,
    ):
        processes: list[FakeProcess] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess(send_error=send_error, fail_send_write=fail_send_write)
            processes.append(process)
            return process

        bridge = KakaoDirectBridge(
            DirectKakaoConfig(enabled=enabled, mappings={"room": "123"}),
            credential_vault=NoCredentials(),
            popen_factory=factory,
        )
        return bridge, processes

    def test_list_chats_is_allowed_when_send_switch_is_off(self) -> None:
        bridge, processes = self._bridge(enabled=False)
        sync_dir = None
        try:
            self.assertEqual(bridge.list_chats(), [])
            self.assertEqual(len(processes), 1)
            sync_dir = bridge._sdk_config_dir
            self.assertIsNotNone(sync_dir)
            self.assertTrue(sync_dir.exists())
            with self.assertRaises(DirectSendDisabledError):
                bridge.send_message("room", "hello")
        finally:
            bridge.close()
        self.assertIsNotNone(sync_dir)
        self.assertFalse(sync_dir.exists())

    def test_get_chats_by_ids_deduplicates_numeric_ids_and_sanitizes_records(self) -> None:
        bridge, _ = self._bridge(enabled=False)
        bridge._request = Mock(  # type: ignore[method-assign]
            return_value={
                "chats": [
                    {
                        "chat_id": "101",
                        "type": "DirectChat",
                        "active_members": 2,
                        "title": "새 방",
                        "display_name": "새 방",
                        "peer_user_id": "20",
                        "peer_names": ["상대"],
                        "last_message": {"message": "PRIVATE MESSAGE"},
                        "oauth_token": "SECRET TOKEN",
                    }
                ]
            }
        )
        try:
            chats = bridge.get_chats_by_ids([" 101 ", 101, "202", "101"])
        finally:
            bridge.close()

        self.assertEqual(
            chats,
            [
                {
                    "chat_id": "101",
                    "type": "DirectChat",
                    "display_name": "새 방",
                    "title": "새 방",
                    "active_members": 2,
                    "peer_user_id": "20",
                    "peer_names": ["상대"],
                    "peer_local_names": [],
                }
            ],
        )
        lookup_calls = [
            call for call in bridge._request.call_args_list
            if call.args == ("get_chats_by_ids",)
        ]
        self.assertEqual(len(lookup_calls), 1)
        args, kwargs = lookup_calls[0]
        self.assertEqual(args, ("get_chats_by_ids",))
        self.assertEqual(kwargs["chat_ids"], ["101", "202"])
        self.assertGreaterEqual(kwargs["timeout"], 60.0)
        self.assertNotIn("PRIVATE", repr(chats))
        self.assertNotIn("SECRET", repr(chats))

    def test_get_chats_by_ids_empty_input_never_starts_a_request(self) -> None:
        bridge, _ = self._bridge(enabled=False)
        bridge._request = Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("empty lookup must not start the bridge")
        )
        try:
            self.assertEqual(bridge.get_chats_by_ids([]), [])
        finally:
            bridge.close()
        bridge._request.assert_not_called()

    def test_get_chats_by_ids_rejects_non_numeric_and_oversized_batches(self) -> None:
        bridge, _ = self._bridge(enabled=False)
        bridge._request = Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("invalid lookup must not start the bridge")
        )
        try:
            with self.assertRaises(ValueError):
                bridge.get_chats_by_ids(["101", "not-a-chat-id"])
            with self.assertRaises(ValueError):
                bridge.get_chats_by_ids([str(value) for value in range(1, 130)])
        finally:
            bridge.close()
        bridge._request.assert_not_called()

    def test_chat_sanitizer_keeps_only_verified_direct_peer_identity(self) -> None:
        safe = _sanitize_chat_record(
            {
                "chat_id": "700",
                "type": "DirectChat",
                "active_members": 2,
                "title": None,
                "display_name": None,
                "peer_user_id": "20",
                "peer_names": ["상대 이름"],
                "peer_local_names": ["내 지정명"],
                "members": [{"status_message": "PRIVATE"}],
                "last_message": {"message": "PRIVATE MESSAGE"},
            }
        )
        self.assertEqual(safe["peer_user_id"], "20")
        self.assertEqual(safe["peer_names"], ["상대 이름"])
        self.assertEqual(safe["peer_local_names"], ["내 지정명"])
        self.assertNotIn("members", safe)
        self.assertNotIn("last_message", safe)

        unsafe = _sanitize_chat_record(
            {
                "chat_id": "701",
                "type": "MultiChat",
                "active_members": 2,
                "peer_user_id": "20",
                "peer_names": ["상대 이름"],
            }
        )
        self.assertIsNone(unsafe["peer_user_id"])
        self.assertEqual(unsafe["peer_names"], [])
        self.assertEqual(unsafe["peer_local_names"], [])

    def test_request_id_format_and_chat_id_mapping(self) -> None:
        bridge, processes = self._bridge()
        try:
            self.assertEqual(bridge.send_message("Room", "hello")["chat_id"], "123")
            frames = processes[0].stdin.frames
            self.assertEqual(frames[0]["command"], "send_message")
            self.assertRegex(frames[0]["id"], r"^req-\d{8}$")
            self.assertEqual(frames[0]["chat_id"], "123")
        finally:
            bridge.close()

    def test_explicit_provider_failure_is_not_classified_as_unknown(self) -> None:
        bridge, _ = self._bridge(send_error={"code": "server_rejected", "status_code": 403, "kind": "explicit"})
        try:
            with self.assertRaises(DirectSendError) as context:
                bridge.send_message("room", "hello")
            self.assertNotIsInstance(context.exception, DirectSendUnknownError)
            self.assertEqual(context.exception.status_code, 403)
        finally:
            bridge.close()

    def test_transport_failure_is_classified_as_unknown(self) -> None:
        bridge, _ = self._bridge(send_error={"code": "transport_closed", "kind": "unknown", "ambiguous": True})
        try:
            with self.assertRaises(DirectSendUnknownError):
                bridge.send_message("room", "hello")
        finally:
            bridge.close()

    def test_partial_write_after_child_consumes_send_is_unknown(self) -> None:
        bridge, processes = self._bridge(fail_send_write=True)
        try:
            with self.assertRaises(DirectSendUnknownError):
                bridge.send_message("room", "hello")
            self.assertEqual(processes[0].stdin.frames[0]["command"], "send_message")
        finally:
            bridge.close()

    def test_direct_enabled_override_does_not_mutate_shared_config(self) -> None:
        config = DirectKakaoConfig(enabled=True, mappings={"room": "123"})
        bridge = KakaoDirectBridge(
            config,
            direct_enabled=False,
            credential_vault=NoCredentials(),
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
        )
        try:
            self.assertTrue(config.enabled)
            self.assertFalse(bridge.config.enabled)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
