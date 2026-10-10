import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kakao_diagnostics import connection_guidance, diagnose_local
from kakao_direct import DirectKakaoConfig, EXPECTED_SDK_VERSION


class LocalDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.node = self.root / "node.exe"
        self.node.write_bytes(b"placeholder; never executed")
        self.bridge = self.root / "bridge.mjs"
        self.bridge.write_text("throw new Error('must never launch');", encoding="utf-8")
        self.sdk = self.root / "agent-messenger"
        self.sdk.mkdir()
        self.write_package()
        self.vault = self.root / "vault.dat"
        self.vault.write_bytes(b"PRIVATE-CREDENTIAL-CONTENT-DO-NOT-READ")
        self.config = DirectKakaoConfig(
            node_path=self.node, bridge_script=self.bridge, sdk_root=self.sdk,
            credential_vault=self.vault,
        )
        self.pc = {"search_box": {"x": 200, "y": 300}}

    def write_package(self, version=EXPECTED_SDK_VERSION):
        (self.sdk / "package.json").write_text(json.dumps({
            "name": "agent-messenger", "version": version,
        }), encoding="utf-8")

    def diagnose(self, **kwargs):
        with patch("kakao_diagnostics.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout="v24.19.0\n", stderr="",
        )) as runner:
            result = diagnose_local(kwargs.get("config", self.config), kwargs.get("pc", self.pc))
        return {item.label: item for item in result}, runner

    def test_ready_local_setup_only_launches_bounded_hidden_node_version(self):
        with patch("kakao_direct.CredentialVault.load", side_effect=AssertionError("must not decrypt")), patch(
            "kakao_direct.KakaoDirectBridge", side_effect=AssertionError("must not contact server")), patch(
            "pathlib.Path.read_bytes", side_effect=AssertionError("must not read vault")):
            items, runner = self.diagnose()
        self.assertEqual(len(items), 5)
        self.assertTrue(all(item.state == "ok" for item in items.values()))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], [str(self.node), "--version"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 3)
        self.assertFalse(runner.call_args.kwargs["shell"])
        if os.name == "nt":
            self.assertEqual(runner.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertIn("파일 존재만", items["직접 전송 인증 파일"].detail)

    def test_missing_credentials_guidance_does_not_read_or_reset_file(self):
        self.config.credential_vault = self.root / "missing.dat"
        items, _runner = self.diagnose()
        self.assertEqual(items["직접 전송 인증 파일"].state, "error")
        self.assertIn("Spoiler", items["직접 전송 인증 파일"].detail)
        self.assertFalse(Path(self.config.credential_vault).exists())
        self.assertTrue(self.vault.exists())

    def test_external_sdk_must_match_pinned_version(self):
        self.write_package("2.37.0")
        items, _runner = self.diagnose()
        self.assertEqual(items["직접 전송 SDK"].state, "error")
        self.assertIn(EXPECTED_SDK_VERSION, items["직접 전송 SDK"].detail)
        self.write_package()
        items, _runner = self.diagnose()
        self.assertEqual(items["직접 전송 SDK"].state, "ok")
        self.assertIn("외부 경로", items["직접 전송 SDK"].detail)

    def test_sdk_entry_file_resolves_package_root(self):
        dist = self.sdk / "dist"
        dist.mkdir()
        entry = dist / "index.js"
        entry.write_text("// never executed", encoding="utf-8")
        self.config.sdk_root = entry
        items, _runner = self.diagnose()
        self.assertEqual(items["직접 전송 SDK"].state, "ok")

    def test_missing_node_does_not_launch_fallback_command(self):
        self.config.node_path = self.root / "missing-node.exe"
        with patch("kakao_diagnostics.shutil.which", return_value=None):
            items, runner = self.diagnose()
        self.assertEqual(items["Node.js"].state, "error")
        runner.assert_not_called()

    def test_node_timeout_and_untrusted_output_have_safe_diagnostics(self):
        with patch("kakao_diagnostics.subprocess.run", side_effect=subprocess.TimeoutExpired(
            "node", 3, output="password=DO-NOT-ECHO",
        )):
            items = {item.label: item for item in diagnose_local(self.config, self.pc)}
        self.assertIn("3초", items["Node.js"].detail)
        self.assertNotIn("DO-NOT-ECHO", str(items))
        with patch("kakao_diagnostics.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout="access_token=DO-NOT-ECHO", stderr="",
        )):
            items = {item.label: item for item in diagnose_local(self.config, self.pc)}
        self.assertEqual(items["Node.js"].state, "error")
        self.assertNotIn("DO-NOT-ECHO", str(items))

    def test_invalid_coordinates_do_not_claim_macro_readiness(self):
        for coordinates in ({}, {"x": 0, "y": 10}, {"x": "nan", "y": 10}, {"x": "inf", "y": 10}, {"x": True, "y": 10}):
            with self.subTest(coordinates=coordinates):
                items, _runner = self.diagnose(pc={"search_box": coordinates})
                self.assertEqual(items["PC 검색창 좌표"].state, "warning")

    def test_corrupt_package_does_not_echo_content(self):
        (self.sdk / "package.json").write_text("secret=DO-NOT-ECHO", encoding="utf-8")
        items, _runner = self.diagnose()
        self.assertEqual(items["직접 전송 SDK"].state, "error")
        self.assertNotIn("DO-NOT-ECHO", str(items))

    def test_auth_timeout_disconnect_and_unknown_have_distinct_guidance(self):
        auth = connection_guidance("refresh_rejected")
        timeout = connection_guidance("timeout")
        disconnected = connection_guidance("process_disconnected")
        unknown = connection_guidance("send_outcome_unknown")
        self.assertIn("태블릿 설정", " ".join(auth))
        self.assertIn("인터넷", " ".join(timeout))
        self.assertIn("브리지 프로세스", " ".join(disconnected))
        self.assertIn("실제 도착 여부", " ".join(unknown))
        self.assertEqual(len({auth, timeout, disconnected, unknown}), 4)
        self.assertIn("인증 정보가 없습니다", connection_guidance("credentials_missing")[0])
        self.assertNotIn("private-secret", " ".join(connection_guidance("private-secret")))


if __name__ == "__main__":
    unittest.main()
