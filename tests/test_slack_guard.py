"""Tests for hooks/slack_guard.py: the PreToolUse hook that pins Slack posts to one destination."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = [sys.executable, str(ROOT / "hooks" / "slack_guard.py")]
SEND = "mcp__claude_ai_Slack__slack_send_message"


class SlackGuard(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "guard.log"

    def call(self, tool_input, channel="C123", thread="1700.1", tool=SEND, env_extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_GUARD_")}
        env["SLACK_GUARD_LOG"] = str(self.log)
        if channel is not None:
            env["SLACK_GUARD_CHANNEL"] = channel
        if thread is not None:
            env["SLACK_GUARD_THREAD"] = thread
        env.update(env_extra or {})
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
        return subprocess.run(HOOK, input=payload, capture_output=True, text=True, env=env)

    def decision(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None

    def test_allows_the_expected_thread(self):
        r = self.call({"channel_id": "C123", "thread_ts": "1700.1", "message": "hi"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.decision(r))  # no "allow": the normal tool allowlist still decides
        self.assertFalse(self.log.exists())

    def test_blocks_another_channel_and_logs_it(self):
        r = self.call({"channel_id": "C999", "thread_ts": "1700.1", "message": "leak"})
        self.assertEqual(self.decision(r), "deny")
        self.assertIn("C999", self.log.read_text())

    def test_blocks_another_thread_or_a_top_level_post(self):
        self.assertEqual(self.decision(self.call({"channel_id": "C123", "thread_ts": "1800.2", "message": "x"})), "deny")
        self.assertEqual(self.decision(self.call({"channel_id": "C123", "message": "x"})), "deny")

    def test_blocks_reply_broadcast(self):
        r = self.call({"channel_id": "C123", "thread_ts": "1700.1", "message": "x", "reply_broadcast": True})
        self.assertEqual(self.decision(r), "deny")

    def test_owner_dm_mode_allows_only_the_dm_without_thread(self):
        self.assertIsNone(self.decision(self.call({"channel_id": "U777", "message": "x"}, channel="U777", thread=None)))
        self.assertEqual(self.decision(self.call({"channel_id": "C123", "message": "x"}, channel="U777", thread=None)), "deny")

    def test_fails_closed_without_configuration(self):
        r = self.call({"channel_id": "C123", "message": "x"}, channel=None, thread=None)
        self.assertEqual(self.decision(r), "deny")

    def test_fails_closed_on_garbage_input(self):
        env = {**os.environ, "SLACK_GUARD_CHANNEL": "C123", "SLACK_GUARD_LOG": str(self.log)}
        r = subprocess.run(HOOK, input="not json", capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)

    def test_other_tools_pass_through(self):
        r = self.call({"query": "x"}, tool="mcp__claude_ai_Slack__slack_read_thread")
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(self.decision(r))


if __name__ == "__main__":
    unittest.main()
