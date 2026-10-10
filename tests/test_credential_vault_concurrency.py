"""Synthetic token races; these tests never open the per-user account vault."""

import hashlib
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from kakao_direct import (
    CredentialVault, CredentialVaultConflictError, CredentialVaultError,
    DirectBridgeError, DirectConfigFileLock, DirectKakaoConfig, KakaoDirectBridge,
)
from kakao_tablet_reauth import TabletReauthCoordinator


BUNDLE = {"oauth_token": "synthetic-original", "refresh_token": "synthetic-refresh",
          "user_id": "42", "device_uuid": "0123456789abcdef0123456789abcdef", "device_type": "tablet"}


class FakeCrypto:
    def CryptProtectData(self, data, *_args):
        return b"synthetic-protected:" + data[::-1]

    def CryptUnprotectData(self, data, *_args):
        return None, data.removeprefix(b"synthetic-protected:")[::-1]


def synthetic_vault(tmp_path, **kwargs):
    return CredentialVault(tmp_path / "loco-credentials.dat", crypto_module=FakeCrypto(), **kwargs)


def test_stale_refresh_cannot_overwrite_the_winning_rotated_tokens(tmp_path):
    first = synthetic_vault(tmp_path)
    second = synthetic_vault(tmp_path)
    first.save(BUNDLE)
    original = first.load_snapshot()
    competitor = second.load_snapshot()
    saved = first.save_if_unchanged({**BUNDLE, "oauth_token": "synthetic-winning"}, original.revision)
    with pytest.raises(CredentialVaultConflictError):
        second.save_if_unchanged({**BUNDLE, "oauth_token": "synthetic-stale"}, competitor.revision)
    assert first.load()["oauth_token"] == "synthetic-winning"
    assert saved.revision == first.load_snapshot().revision


def test_parallel_same_revision_has_exactly_one_winner(tmp_path):
    vault = synthetic_vault(tmp_path)
    vault.save(BUNDLE)
    revision = vault.load_snapshot().revision
    barrier = threading.Barrier(2)

    def attempt(token):
        barrier.wait(timeout=5)
        try:
            return synthetic_vault(tmp_path).save_if_unchanged({**BUNDLE, "oauth_token": token}, revision).credentials
        except CredentialVaultConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("synthetic-first", "synthetic-second")))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert vault.load() == winners[0]


def test_snapshot_revision_describes_the_buffer_decrypted_even_if_file_changes(tmp_path):
    vault = synthetic_vault(tmp_path)
    vault.save(BUNDLE)
    original_bytes = vault.path.read_bytes()
    delegate = FakeCrypto()

    class ChangingCrypto(FakeCrypto):
        def CryptUnprotectData(self, data, *_args):
            synthetic_vault(tmp_path).save({**BUNDLE, "oauth_token": "synthetic-newer"})
            return delegate.CryptUnprotectData(data)

    snapshot = CredentialVault(vault.path, crypto_module=ChangingCrypto()).load_snapshot()
    assert snapshot.credentials == BUNDLE
    assert snapshot.revision == hashlib.sha256(original_bytes).hexdigest()
    assert snapshot.revision != vault.load_snapshot().revision


def test_missing_snapshot_does_not_replace_a_newly_created_vault(tmp_path):
    vault = synthetic_vault(tmp_path)
    missing = vault.load_snapshot()
    vault.save(BUNDLE)
    with pytest.raises(CredentialVaultConflictError):
        vault.save_if_unchanged({**BUNDLE, "oauth_token": "synthetic-stale"}, missing.revision)


def test_direct_refresh_conflict_returns_negative_ack_and_keeps_newer_tokens(tmp_path):
    vault = synthetic_vault(tmp_path)
    vault.save(BUNDLE)
    original = vault.load_snapshot()
    bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True), credential_vault=vault)
    bridge._credentials = original.credentials
    bridge._credential_revision = original.revision
    bridge._write_frame = Mock()
    bridge._auth_lease = Mock()
    bridge._auth_lease_id = "synthetic-lease"
    vault.save({**BUNDLE, "oauth_token": "synthetic-other-app"})
    bridge._handle_event({"event_id": "synthetic-event", "auth_lease_id": "synthetic-lease", "credentials": {**BUNDLE, "oauth_token": "synthetic-stale"}}, "credentials")
    assert bridge._write_frame.call_args.args[0]["ok"] is False
    assert bridge._write_frame.call_args.args[0]["error_code"] == "credential_vault_conflict"
    assert vault.load()["oauth_token"] == "synthetic-other-app"


def test_explicit_stale_login_cannot_adopt_a_newer_vault_revision(tmp_path):
    vault = synthetic_vault(tmp_path)
    vault.save({**BUNDLE, "oauth_token": "synthetic-newer"})
    bridge = KakaoDirectBridge(DirectKakaoConfig(enabled=True), credential_vault=vault)
    bridge._request = Mock()
    with pytest.raises(DirectBridgeError) as raised:
        bridge.login(BUNDLE)
    assert raised.value.code == "credential_vault_conflict"
    bridge._request.assert_not_called()


def test_tablet_authentication_cannot_overwrite_concurrent_rotation(tmp_path):
    vault = synthetic_vault(tmp_path)
    vault.save(BUNDLE)
    account = Mock()
    account.load.return_value = {"email": "synthetic@example.com", "password": "synthetic-only"}
    authenticator = Mock()

    def auth(*_args, **_kwargs):
        vault.save({**BUNDLE, "oauth_token": "synthetic-other-app"})
        return {**BUNDLE, "oauth_token": "synthetic-stale-auth"}

    authenticator.authenticate.side_effect = auth
    coordinator = TabletReauthCoordinator(account_vault=account, credential_vault=vault, authenticator=authenticator)
    with pytest.raises(CredentialVaultConflictError):
        coordinator("token_expired")
    assert vault.load()["oauth_token"] == "synthetic-other-app"


@pytest.fixture(scope="module")
def csharp_helper(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Windows DPAPI interoperation requires Windows")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("Built-in .NET compiler unavailable")
    stage = Path(__file__).resolve().parents[1] / "spoiler"
    output = tmp_path_factory.mktemp("credential-csharp") / "CredentialVaultTests.exe"
    result = subprocess.run([str(compiler), "/nologo", "/target:exe", f"/out:{output}",
                             "/reference:System.Security.dll", "/reference:System.Runtime.Serialization.dll",
                             str(stage / "src/CredentialVault.cs"), str(stage / "test/CredentialVaultConcurrencyTests.cs")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def run_csharp(helper, tmp_path, *args):
    return subprocess.run([str(helper), str(tmp_path), *args], capture_output=True, text=True, timeout=15)


def test_python_and_csharp_share_revision_and_reject_stale_updates(csharp_helper, tmp_path):
    seed = run_csharp(csharp_helper, tmp_path, "seed")
    assert seed.returncode == 0, seed.stdout
    vault = CredentialVault(tmp_path / "loco-credentials.dat")
    snapshot = vault.load_snapshot()
    assert snapshot.revision == seed.stdout.strip()
    assert snapshot.credentials["oauth_token"] == "synthetic-csharp-token"
    saved = vault.save_if_unchanged({**BUNDLE, "oauth_token": "synthetic-python-rotated"}, snapshot.revision)
    stale = run_csharp(csharp_helper, tmp_path, "cas", snapshot.revision)
    assert stale.returncode == 3
    accepted = run_csharp(csharp_helper, tmp_path, "cas", saved.revision)
    assert accepted.returncode == 0
    with pytest.raises(CredentialVaultConflictError):
        vault.save_if_unchanged(BUNDLE, saved.revision)
    assert vault.load()["oauth_token"] == "synthetic-csharp-rotated"


def test_csharp_lock_blocks_python_writer_on_empty_lockfile(csharp_helper, tmp_path):
    process = subprocess.Popen([str(csharp_helper), str(tmp_path), "hold", "1200"], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        vault = synthetic_vault(tmp_path, lock_timeout=0.05)
        with pytest.raises(CredentialVaultError, match="lock timed out"):
            vault.save(BUNDLE)
        assert not vault.path.exists()
    finally:
        process.wait(timeout=5)


def test_python_lock_blocks_csharp_writer(csharp_helper, tmp_path):
    vault = synthetic_vault(tmp_path)
    with DirectConfigFileLock(vault.path):
        result = run_csharp(csharp_helper, tmp_path, "seed", "unused", "50")
    assert result.returncode == 4
    assert not vault.path.exists()


def test_csharp_stale_and_deleted_vault_self_test(csharp_helper, tmp_path):
    result = run_csharp(csharp_helper, tmp_path, "self-test")
    assert result.returncode == 0, result.stdout
