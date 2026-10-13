from __future__ import annotations

import shutil
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from kakao_direct import DirectBridgeError, DirectKakaoConfig, KakaoDirectBridge


class DirectBridgeNodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the direct bridge")
    def test_missing_sdk_exits_with_actionable_code_before_vault_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-sdk"
            script = Path(__file__).resolve().parents[1] / "bridge" / "kakao_direct_bridge.mjs"
            completed = subprocess.run(
                ["node", str(script)], env={**os.environ, "KAKAO_AGENT_MESSENGER_ROOT": str(missing)},
                capture_output=True, text=True, timeout=5, check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn('"code":"sdk_missing"', completed.stdout)
            vault = Mock()
            bridge = KakaoDirectBridge(
                DirectKakaoConfig(sdk_root=missing, ready_timeout=3),
                credential_vault=vault,
            )
            try:
                with self.assertRaises(DirectBridgeError) as raised:
                    bridge.ready()
                self.assertEqual(raised.exception.code, "sdk_missing")
            finally:
                process = bridge._process
                bridge.close()
                if process is not None:
                    process.stdout.close()
            vault.load_snapshot.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the direct bridge")
    def test_send_once_boundary(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                "node",
                "--test",
                str(project_root / "tests" / "test_kakao_direct_send_once.mjs"),
                str(project_root / "tests" / "test_kakao_direct_session.mjs"),
                str(project_root / "tests" / "test_kakao_direct_media.mjs"),
                str(project_root / "tests" / "test_kakao_direct_reconcile.mjs"),
                str(project_root / "tests" / "test_kakao_chat_enrichment.mjs"),
                str(project_root / "tests" / "test_kakao_friend_aliases.mjs"),
                str(project_root / "tests" / "test_kakao_loco_method_capture.mjs"),
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
