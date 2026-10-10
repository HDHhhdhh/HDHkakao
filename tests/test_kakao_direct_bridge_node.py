from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class DirectBridgeNodeTests(unittest.TestCase):
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
