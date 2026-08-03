from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agentflow import publication


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class FullHistoryScanTests(unittest.TestCase):
    def repository(self, root: Path) -> None:
        git(root, "init", "-q")
        git(root, "config", "user.email", "tests@example.invalid")
        git(root, "config", "user.name", "Agentflow tests")

    def commit(self, root: Path, name: str, payload: bytes) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        git(root, "add", "--", name)
        git(root, "commit", "-qm", name)

    def test_safe_history_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            self.commit(root, "README.md", b"safe public project\n")
            findings, count = publication.scan_repository(root)
            self.assertEqual(findings, [])
            self.assertGreater(count, 0)

    def test_secret_removed_from_head_is_still_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            marker = b"-----BEGIN " + b"PRIVATE KEY-----\nnot-a-real-key\n"
            self.commit(root, "temporary.txt", marker)
            (root / "temporary.txt").unlink()
            git(root, "add", "-u")
            git(root, "commit", "-qm", "remove")

            findings, _ = publication.scan_repository(root)

            self.assertTrue(any(item.code == "private-key" for item in findings))

    def test_historical_runtime_path_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            self.commit(root, ".agentflow/sessions/result.json", b"{}\n")
            findings, _ = publication.scan_repository(root)
            self.assertTrue(any(item.code == "session-or-transcript-path" for item in findings))

    def test_private_denylist_finding_does_not_contain_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            marker = b"private-customer-codename"
            self.commit(root, "notes.md", b"reference: " + marker + b"\n")
            findings, _ = publication.scan_repository(root, deny_markers=[marker])
            rendered = repr(findings).encode("utf-8")
            self.assertTrue(any(item.code == "private-denylist-marker" for item in findings))
            self.assertNotIn(marker, rendered)

    def test_sensitive_path_is_detected_without_reading_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            self.commit(root, "config/.env.local", b"placeholder\n")
            findings, _ = publication.scan_repository(root)
            self.assertTrue(any(item.code == "sensitive-path" for item in findings))

    def test_short_private_denylist_marker_is_rejected(self) -> None:
        with self.assertRaises(publication.ScanError):
            publication._deny_markers(["abc"])


if __name__ == "__main__":
    unittest.main()
