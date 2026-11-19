"""doctor's prerequisite checks: each item says its status and the exact fix for the user's OS."""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import platforms  # noqa: E402


def fake(path, body):
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


class Prerequisites(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake(self.bin / "claude", "echo '{\"loggedIn\": true}'")
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
                    "RESPONDER_CONFIG_DIR": str(self.tmp / "config"), "HOME": str(self.tmp),
                    "RESPONDER_AGENTS_DIR": str(self.tmp / "LaunchAgents")}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def doctor(self, **env):
        r = subprocess.run([sys.executable, str(ROOT / "responder.py"), "doctor"], capture_output=True, text=True,
                           env={**self.env, **env})
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        return report, {p["name"]: p for p in report["prerequisites"]}

    def test_every_item_has_status_and_a_fix_when_not_ok(self):
        _, items = self.doctor(RESPONDER_PLATFORM="macos")
        for name in ("python", "claude", "slack-connector", "timer", "humanizer"):
            self.assertIn(name, items)
        for p in items.values():
            self.assertIn(p["status"], {"ok", "missing", "check", "optional"})
            if p["status"] != "ok":
                self.assertTrue(p["fix"], p)

    def test_python_and_claude_ok(self):
        report, items = self.doctor()
        self.assertEqual(items["python"]["status"], "ok")
        self.assertEqual(items["claude"]["status"], "ok")

    def test_claude_logged_out_or_missing(self):
        fake(self.bin / "claude", "echo '{\"loggedIn\": false}'")
        report, items = self.doctor()
        self.assertEqual(items["claude"]["status"], "missing")
        self.assertIn("claude auth login", items["claude"]["fix"])
        self.assertFalse(report["prerequisites_ok"])
        (self.bin / "claude").unlink()
        _, items = self.doctor()
        self.assertEqual(items["claude"]["status"], "missing")
        self.assertIn("PATH", items["claude"]["fix"])

    def test_slack_connector_is_left_to_claude(self):
        _, items = self.doctor()
        self.assertEqual(items["slack-connector"]["status"], "check")
        self.assertIn("slack_search_users", items["slack-connector"]["fix"])

    def test_macos_timer_missing_then_installed(self):
        _, items = self.doctor(RESPONDER_PLATFORM="macos")
        self.assertEqual(items["timer"]["status"], "missing")
        self.assertIn("responder.py install", items["timer"]["fix"])
        agents = self.tmp / "LaunchAgents"
        agents.mkdir()
        plist = {"Label": "local.slack-responder", "ProgramArguments": [sys.executable, str(ROOT / "responder.py"), "run"]}
        (agents / "local.slack-responder.plist").write_bytes(plistlib.dumps(plist))
        _, items = self.doctor(RESPONDER_PLATFORM="macos")
        self.assertEqual(items["timer"]["status"], "ok")

    def test_linux_timer_and_notifier(self):
        fake(self.bin / "systemctl", "exit 1")
        _, items = self.doctor(RESPONDER_PLATFORM="linux", RESPONDER_SYSTEMCTL=str(self.bin / "systemctl"))
        self.assertEqual(items["timer"]["status"], "missing")
        self.assertEqual(items["notifier"]["status"], "optional")
        self.assertIn("libnotify", items["notifier"]["fix"])
        fake(self.bin / "systemctl", "exit 0")
        fake(self.bin / "notify-send", "exit 0")
        _, items = self.doctor(RESPONDER_PLATFORM="linux", RESPONDER_SYSTEMCTL=str(self.bin / "systemctl"))
        self.assertEqual(items["timer"]["status"], "ok")
        self.assertEqual(items["notifier"]["status"], "ok")

    def test_windows_timer_uses_schtasks(self):
        fake(self.bin / "schtasks", "exit 1")
        _, items = self.doctor(RESPONDER_PLATFORM="windows", RESPONDER_SCHTASKS=str(self.bin / "schtasks"))
        self.assertEqual(items["timer"]["status"], "missing")
        self.assertIn("py responder.py install", items["timer"]["fix"])

    def test_configured_tools_must_be_found(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        answers = {"owner": {"name": "Dana", "slack_user_id": "U12345ABC", "company": "Globex"},
                   "slack": {"workspace_domain": "globex.slack.com"}, "sources": {"mentions": True},
                   "investigation": {"repo": str(repo), "engram_project": "globex", "codegraph": True}}
        r = subprocess.run([sys.executable, str(ROOT / "responder.py"), "write", "--answers", "-"],
                           input=json.dumps(answers), capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        _, items = self.doctor()
        self.assertEqual(items["engram"]["status"], "missing")
        self.assertIn("environment", items["engram"]["fix"])
        fake(self.bin / "engram", "exit 0")
        _, items = self.doctor()
        self.assertEqual(items["engram"]["status"], "ok")
        self.assertEqual(items["codegraph"]["status"], "missing")

    def test_unexpected_claude_status_is_reported_not_a_crash(self):
        fake(self.bin / "claude", "echo '[1, 2]'")
        _, items = self.doctor()
        self.assertEqual(items["claude"]["status"], "missing")
        self.assertIn("unexpected", items["claude"]["fix"])

    def test_python_fix_per_os(self):
        self.assertIn("brew", platforms.python_fix("macos"))
        self.assertIn("apt", platforms.python_fix("linux"))
        self.assertIn("winget", platforms.python_fix("windows"))


if __name__ == "__main__":
    unittest.main()
