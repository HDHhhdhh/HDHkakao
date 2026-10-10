"""Isolated authentication lease tests. No real credentials or network calls."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from kakao_auth_lease import AuthLease, AuthLeaseError
from kakao_direct import CredentialVault, DirectKakaoConfig, KakaoDirectBridge
from kakao_tablet_reauth import TabletReauthCoordinator
from test_credential_vault_concurrency import BUNDLE, FakeCrypto
from test_kakao_direct import FakeProcess


def test_waiting_lease_is_cancelled_without_waiting_for_owner(tmp_path):
    path = tmp_path / "loco-credentials.dat"
    cancel = threading.Event()
    failures = []
    with AuthLease(path):
        def wait():
            try:
                AuthLease(path, timeout=10, cancel=cancel).acquire()
            except AuthLeaseError as error:
                failures.append(error.code)
        worker = threading.Thread(target=wait)
        worker.start()
        cancel.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert failures == ["auth_lease_cancelled"]


def test_child_without_lease_cannot_persist_rotated_credentials(tmp_path):
    vault = CredentialVault(tmp_path / "loco-credentials.dat", crypto_module=FakeCrypto())
    vault.save(BUNDLE)
    snapshot = vault.load_snapshot()
    bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True), credential_vault=vault)
    bridge._credentials, bridge._credential_revision = snapshot.credentials, snapshot.revision
    bridge._write_frame = Mock()
    bridge._handle_event({"event_id": "old-child", "credentials": {**BUNDLE, "oauth_token": "rotated"}}, "credentials")
    assert bridge._write_frame.call_args.args[0]["ok"] is False
    assert vault.load() == BUNDLE


def test_late_credentials_from_expired_lease_fail_closed(tmp_path):
    vault = CredentialVault(tmp_path / "loco-credentials.dat", crypto_module=FakeCrypto())
    vault.save(BUNDLE)
    snapshot = vault.load_snapshot()
    bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True), credential_vault=vault)
    bridge._credentials, bridge._credential_revision = snapshot.credentials, snapshot.revision
    bridge._auth_lease = AuthLease(vault.path).acquire()
    bridge._auth_lease_id = "old-lease"
    bridge._release_auth_lease()
    bridge._write_frame = Mock()
    bridge._handle_event({"event_id": "late", "auth_lease_id": "old-lease",
                          "credentials": {**BUNDLE, "oauth_token": "rotated"}}, "credentials")
    assert bridge._write_frame.call_args.args[0]["ok"] is False
    assert vault.load() == BUNDLE


def test_stop_cancels_host_waiting_for_other_app_without_a_grant(tmp_path):
    vault = CredentialVault(tmp_path / "loco-credentials.dat", crypto_module=FakeCrypto())
    vault.save(BUNDLE)
    bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True), credential_vault=vault)
    process = FakeProcess()
    bridge._process = process
    cancel = bridge._auth_lease_cancel
    with AuthLease(vault.path):
        worker = threading.Thread(target=bridge._acquire_auth_lease,
            args=({"request_id": "waiting", "credentials": BUNDLE}, process, 0, cancel))
        worker.start()
        bridge.shutdown()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert not any(frame.get("command") == "auth_lease_granted" for frame in process.stdin.frames)


def test_stop_of_granted_refresh_confirms_exit_then_releases_cross_app_lock(tmp_path):
    vault = CredentialVault(tmp_path / "loco-credentials.dat", crypto_module=FakeCrypto())
    vault.save(BUNDLE)
    bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True), credential_vault=vault)
    process = FakeProcess()
    bridge._process = process
    bridge._acquire_auth_lease({"request_id": "granted", "credentials": BUNDLE}, process, 0,
                               bridge._auth_lease_cancel)
    assert bridge._auth_lease is not None
    bridge.shutdown()
    assert process.poll() is not None
    assert bridge._auth_lease is None
    with AuthLease(vault.path, timeout=0):
        pass


def test_registration_waiter_rejects_another_account_after_acquiring(tmp_path):
    vault = CredentialVault(tmp_path / "loco-credentials.dat", crypto_module=FakeCrypto())
    vault.save(BUNDLE)
    baseline_read = threading.Event()
    original_load = vault.load_snapshot
    calls = 0

    def observed_load():
        nonlocal calls
        result = original_load()
        calls += 1
        if calls == 1:
            baseline_read.set()
        return result

    vault.load_snapshot = observed_load
    authenticator = Mock()
    account = Mock()
    account.load.return_value = {"email": "fixture@example.com", "password": "synthetic"}
    coordinator = TabletReauthCoordinator(credential_vault=vault, account_vault=account,
                                        authenticator=authenticator)
    failures = []
    with AuthLease(vault.path):
        def attempt():
            try:
                coordinator("token_expired")
            except Exception as error:
                failures.append(error.code)
        worker = threading.Thread(target=attempt)
        worker.start()
        assert baseline_read.wait(timeout=1)
        vault.save({**BUNDLE, "user_id": "different-account"})
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert failures == ["auth_identity_changed"]
    authenticator.authenticate.assert_not_called()


@pytest.fixture(scope="module")
def auth_csharp(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Windows cross-language byte-range locking")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    stage = Path(__file__).resolve().parents[1] / "spoiler"
    target = tmp_path_factory.mktemp("auth-lease-csharp") / "AuthLease.exe"
    result = subprocess.run([str(compiler), "/nologo", "/target:exe", f"/out:{target}",
        "/reference:System.Security.dll", "/reference:System.Runtime.Serialization.dll",
        str(stage / "src/CredentialVault.cs"), str(stage / "test/CredentialVaultConcurrencyTests.cs")],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return target


def test_csharp_holder_blocks_python_and_death_releases_lease(auth_csharp, tmp_path):
    process = subprocess.Popen([str(auth_csharp), str(tmp_path), "auth-hold", "30000"],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(AuthLeaseError, match="auth_lease_busy"):
            AuthLease(tmp_path / "loco-credentials.dat", timeout=0.05).acquire()
        process.kill()
        process.wait(timeout=5)
        with AuthLease(tmp_path / "loco-credentials.dat", timeout=1):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_two_hosts_share_one_remote_refresh_and_loser_adopts_saved_tokens(auth_csharp, tmp_path):
    vault = CredentialVault(tmp_path / "loco-credentials.dat")
    vault.save(BUNDLE)
    process = subprocess.Popen([str(auth_csharp), str(tmp_path), "auth-refresh", "1000"],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "refreshing"
        authenticator = Mock()
        authenticator.authenticate.side_effect = AssertionError("Second remote refresh is forbidden")
        account = Mock()
        account.load.return_value = {"email": "fixture@example.com", "password": "synthetic"}
        winner = TabletReauthCoordinator(credential_vault=vault, account_vault=account,
                                         authenticator=authenticator)("token_expired")
        assert winner["oauth_token"] == "synthetic-csharp-winning"
        authenticator.authenticate.assert_not_called()
        assert process.wait(timeout=5) == 0
        assert (tmp_path / "synthetic-remote.jsonl").read_text().splitlines() == ["csharp-refresh"]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
