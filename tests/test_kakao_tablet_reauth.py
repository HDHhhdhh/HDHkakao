import json
import hashlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from kakao_direct import (
    CredentialVault,
    CredentialSnapshot,
    CredentialVaultConflictError,
    DirectBridgeError,
    DirectKakaoConfig,
    DirectSendNotStartedError,
    KakaoDirectBridge,
)
from kakao_tablet_reauth import (
    ACCOUNT_DPAPI_ENTROPY,
    SpoilerTabletAuthenticator,
    TabletAccountVault,
    TabletReauthCoordinator,
)


FRESH = {
    "oauth_token": "fresh-token",
    "refresh_token": "fresh-refresh",
    "user_id": "42",
    "device_uuid": "tablet-device",
    "device_type": "tablet",
}


def publish_synthetic_runtime(root, node=b"synthetic", auth=b"// synthetic"):
    payloads = (("node.exe", "Spoiler.Runtime.Node", node),
                ("spoiler-bridge.mjs", "Spoiler.Runtime.Bridge.Listener", b"// listener fixture"),
                ("spoiler-auth.mjs", "Spoiler.Runtime.Bridge.Auth", auth))
    lines = [f"{name}|{resource}|{len(data)}|{hashlib.sha256(data).hexdigest()}"
             for name, resource, data in payloads]
    seed = ("\n".join(lines) + "\n").encode("utf-8")
    version = "v1-" + hashlib.sha256(seed).hexdigest()[:16]
    runtime = root / version
    runtime.mkdir(parents=True)
    for name, _, data in payloads:
        (runtime / name).write_bytes(data)
    (root / "active-runtime.manifest").write_bytes(
        ("SpoilerRuntimeManifest/1\nversion=" + version + "\n" + "\n".join(lines) + "\n").encode("utf-8"))
    return runtime


class FakeCrypto:
    def CryptProtectData(self, data, _description, entropy, *_rest):
        assert entropy == ACCOUNT_DPAPI_ENTROPY
        return b"protected:" + data[::-1]

    def CryptUnprotectData(self, data, entropy, *_rest):
        assert entropy == ACCOUNT_DPAPI_ENTROPY
        assert data.startswith(b"protected:")
        return None, data.removeprefix(b"protected:")[::-1]


class MemoryCredentialVault:
    def __init__(self, value=None):
        self._directory = tempfile.TemporaryDirectory(prefix="synthetic-auth-lease-")
        self.path = Path(self._directory.name) / "loco-credentials.dat"
        self.value = value
        self.saved = []
        self.revision = 0

    def load(self):
        return dict(self.value) if self.value else None

    def load_snapshot(self):
        return CredentialSnapshot(self.load(), str(self.revision))

    def save_if_unchanged(self, value, expected_revision):
        if expected_revision != str(self.revision):
            raise CredentialVaultConflictError("Synthetic conflict")
        self.save(value)
        return self.load_snapshot()

    def save(self, value):
        self.value = dict(value)
        self.saved.append(dict(value))
        self.revision += 1


def test_account_secret_uses_distinct_dpapi_entropy_and_never_plaintext(tmp_path):
    path = tmp_path / "account.dat"
    vault = TabletAccountVault(path, crypto_module=FakeCrypto())
    vault.save("person@example.com", "synthetic-password")
    assert b"synthetic-password" not in path.read_bytes()
    assert vault.load() == {"email": "person@example.com", "password": "synthetic-password"}


def test_authenticator_passes_secret_only_on_stdin_and_returns_validated_credentials(tmp_path):
    runtime = publish_synthetic_runtime(tmp_path / "runtime", b"node", b"// fixture")
    observed = {}
    events = []

    class Input(io.BytesIO):
        def close(self):
            if not self.closed:
                observed["input"] = self.getvalue()
            super().close()

    def popen(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        stdout = "\n".join(
            [
                json.dumps({"type": "passcode", "code": "123456"}),
                json.dumps({"type": "credentials", "credentials": FRESH}),
            ]
        )
        process = Mock()
        process.stdin = Input()
        process.stdout = io.BytesIO(stdout.encode("utf-8"))
        process.poll.return_value = 0
        process.wait.return_value = 0
        assert kwargs["stderr"] == subprocess.DEVNULL
        return process

    auth = SpoilerTabletAuthenticator(
        runtime_root=tmp_path / "runtime",
        popen_factory=popen,
        event_callback=lambda event, detail: events.append((event, detail)),
    )
    result = auth.authenticate(
        {"email": "person@example.com", "password": "synthetic-password"},
        device_uuid="tablet-device",
    )
    assert result == FRESH
    assert all("synthetic-password" not in part for part in observed["command"])
    assert all("synthetic-password" not in value for value in observed["env"].values())
    request = json.loads(observed["input"])
    assert request["password"] == "synthetic-password" and request["force"] is True
    assert ("passcode", "123456") in events and events[-1] == ("complete", "")


def synthetic_authenticator(tmp_path, script, *, callback=None):
    runtime = publish_synthetic_runtime(tmp_path / "runtime")
    children = []
    def popen(_command, **kwargs):
        child = subprocess.Popen([sys.executable, "-u", "-c", script], **kwargs)
        children.append(child)
        return child
    return SpoilerTabletAuthenticator(runtime_root=tmp_path / "runtime", popen_factory=popen,
                                     event_callback=callback), children


def test_passcode_is_delivered_while_the_auth_process_waits_for_approval(tmp_path):
    approved = tmp_path / "approved"
    script = (
        "import json, pathlib, sys, time\n"
        "json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type':'passcode','code':'123456'}), flush=True)\n"
        f"approved = pathlib.Path({str(approved)!r})\n"
        "deadline = time.monotonic() + 3\n"
        "while not approved.exists():\n"
        "    if time.monotonic() >= deadline: sys.exit(9)\n"
        "    time.sleep(0.01)\n"
        f"print(json.dumps({{'type':'credentials','credentials':{FRESH!r}}}), flush=True)\n"
    )
    events = []
    def on_event(event, detail):
        events.append((event, detail))
        if event == "passcode": approved.write_text("synthetic approval", encoding="utf-8")
    auth, children = synthetic_authenticator(tmp_path, script, callback=on_event)
    assert auth.authenticate({"email":"person@example.com", "password":"synthetic-password"}) == FRESH
    assert events == [("starting", ""), ("passcode", "123456"), ("complete", "")]
    assert children[0].poll() == 0


@pytest.mark.parametrize("script, expected", [
    ("import sys; sys.stdout.write('x'*65537); sys.stdout.flush()", "spoiler_auth_protocol_invalid"),
    ("import sys; sys.stdout.write(('x'*1023+'\\n')*1025); sys.stdout.flush()", "spoiler_auth_protocol_invalid"),
    ("import time; time.sleep(30)", "tablet_reauth_timeout"),
])
def test_streaming_auth_bounds_output_and_deadline_and_reaps_child(tmp_path, script, expected):
    auth, children = synthetic_authenticator(tmp_path, script)
    auth.timeout = 0.5
    with pytest.raises(DirectBridgeError) as raised:
        auth.authenticate({"email":"person@example.com", "password":"synthetic-password"})
    assert raised.value.code == expected
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stdin.closed


def test_credentials_followed_by_error_do_not_report_success(tmp_path):
    script = f"import json; print(json.dumps({{'type':'credentials','credentials':{FRESH!r}}})); print(json.dumps({{'type':'error','code':'login_rejected'}}))"
    events = []
    auth, _ = synthetic_authenticator(tmp_path, script, callback=lambda event, detail: events.append(event))
    with pytest.raises(DirectBridgeError) as raised:
        auth.authenticate({"email":"person@example.com", "password":"synthetic-password"})
    assert raised.value.code == "login_rejected"
    assert "complete" not in events


def test_coordinator_is_single_cooldown_and_reuses_fresh_credentials():
    account = Mock()
    account.exists.return_value = True
    account.load.return_value = {"email": "person@example.com", "password": "synthetic-password"}
    tokens = MemoryCredentialVault({**FRESH, "oauth_token": "old-token"})
    authenticator = Mock()
    authenticator.authenticate.return_value = FRESH
    now = [100.0]
    coordinator = TabletReauthCoordinator(
        account_vault=account,
        credential_vault=tokens,
        authenticator=authenticator,
        cooldown=900,
        monotonic=lambda: now[0],
    )
    assert coordinator("refresh_rejected") == FRESH
    assert coordinator("refresh_rejected") == FRESH
    assert authenticator.authenticate.call_count == 1
    assert authenticator.authenticate.call_args.kwargs["device_uuid"] == "tablet-device"
    assert coordinator("ECONNRESET") is None


@pytest.mark.parametrize("cause", ["refresh_rejected", "invalid_access_token", "not_authenticated"])
def test_direct_send_reauthenticates_once_only_before_write(cause):
    vault = MemoryCredentialVault(FRESH)
    reloader = Mock(return_value={**FRESH, "oauth_token": "new-token"})
    bridge = KakaoDirectBridge(
        DirectKakaoConfig(enabled=True),
        credential_vault=vault,
        credential_reloader=reloader,
    )
    bridge.ready = Mock(return_value={})
    bridge.login = Mock(return_value={"authenticated": True})
    bridge._request = Mock(
        side_effect=[
            DirectSendNotStartedError(retryable=False, cause_code=cause),
            {"success": True, "status_code": 0, "chat_id": "123", "log_id": "9001", "sent_at": 1800000000},
        ]
    )
    assert bridge.send_message(chat_id="123", text="synthetic")["log_id"] == "9001"
    reloader.assert_called_once_with(cause)
    bridge.login.assert_called_once()
    assert bridge._request.call_count == 2


def test_transient_prewrite_disconnect_never_consumes_account_password():
    reloader = Mock(return_value=FRESH)
    bridge = KakaoDirectBridge(
        DirectKakaoConfig(enabled=True),
        credential_vault=MemoryCredentialVault(FRESH),
        credential_reloader=reloader,
    )
    bridge.ready = Mock(return_value={})
    bridge._request = Mock(side_effect=DirectSendNotStartedError(retryable=True, cause_code="ECONNRESET"))
    with pytest.raises(DirectSendNotStartedError):
        bridge.send_message(chat_id="123", text="synthetic")
    reloader.assert_not_called()


def test_read_only_chat_refresh_reauthenticates_once_after_explicit_token_rejection():
    reloader = Mock(return_value={**FRESH, "oauth_token": "new-token"})
    bridge = KakaoDirectBridge(
        DirectKakaoConfig(enabled=False),
        credential_vault=MemoryCredentialVault(FRESH),
        credential_reloader=reloader,
    )
    bridge.ready = Mock(return_value={})
    bridge.login = Mock(return_value={"authenticated": True})
    bridge._request = Mock(
        side_effect=[
            DirectBridgeError(code="refresh_rejected"),
            {"chats": []},
        ]
    )
    assert bridge.list_chats() == []
    reloader.assert_called_once_with("refresh_rejected")
    bridge.login.assert_called_once()
    assert bridge._request.call_count == 2


def test_session_disconnect_marks_client_for_login_before_the_next_operation():
    bridge = KakaoDirectBridge(
        DirectKakaoConfig(enabled=False),
        credential_vault=MemoryCredentialVault(FRESH),
        credential_reloader=Mock(),
    )
    bridge._authenticated = True
    bridge._handle_event({"event": "session", "state": "disconnected"}, "session")
    assert bridge._authenticated is False
    assert bridge.drain_events()[-1] == {"event": "session", "state": "disconnected"}
