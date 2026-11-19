"""End-to-end tests of the job (runner.py) with a fake `claude` on PATH; nothing reaches Slack."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_CLAUDE = r'''
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(os.environ.get("FAKE_AUTH", '{"loggedIn": true, "loginExpiresAt": "2099-01-01T00:00:00Z"}'))
    sys.exit(0)
prompt = args[args.index("-p") + 1]
tools = args[args.index("--allowedTools") + 1:] if "--allowedTools" in args else []
call = {"prompt": prompt[:60], "guard_channel": os.environ.get("SLACK_GUARD_CHANNEL"),
        "guard_thread": os.environ.get("SLACK_GUARD_THREAD"), "settings": "--settings" in args, "tools": tools,
        "cwd": os.getcwd(), "text": prompt}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(call) + "\n")
def out(result, error=False):
    print(json.dumps({"type": "result", "is_error": error, "result": result}))
if "triage step" in prompt:
    if os.environ.get("FAKE_TRIAGE_FAIL"):
        out("OAuth token has expired", error=True); sys.exit(1)
    out(os.environ.get("FAKE_TRIAGE", "[]"))
elif "Send the text below" in prompt or "morning Slack summary" in prompt:
    out('{"posted": true}')
elif "answering a Slack question" in prompt:
    if os.environ.get("FAKE_GUARD_BLOCK"):
        with open(os.environ["SLACK_GUARD_LOG"], "a") as f:
            f.write("blocked: channel 'C0OTHER' is not the allowed\n")
    out(os.environ.get("FAKE_INV", '{"posted": true, "needs_owner": false, "needs_login": false, "summary": "s", "reply": "r"}'))
'''

ITEM = {"channel_id": "C0CHAN1", "ts": "111.222", "thread_ts": "111.000", "author": "Ana", "kind": "answer", "summary": "q1"}


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin, self.data, self.cfg = self.tmp / "bin", self.tmp / "data", self.tmp / "config"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        claude.chmod(0o755)
        repo = self.tmp / "repo"
        repo.mkdir()
        self.calls_log = self.tmp / "calls.jsonl"
        self.notify_log = self.tmp / "notify.log"
        self.login_flag = self.tmp / "login_down"
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                    "RESPONDER_CONFIG_DIR": str(self.cfg), "RESPONDER_DATA_DIR": str(self.data),
                    "RESPONDER_NOTIFY_LOG": str(self.notify_log), "FAKE_LOG": str(self.calls_log),
                    "HOME": str(self.tmp)}
        answers = {
            "owner": {"name": "Dana", "slack_user_id": "U12345ABC", "company": "Globex", "role": "the lead"},
            "slack": {"workspace_domain": "globex.slack.com"},
            "sources": {"mentions": True, "dms": True, "channels": [{"id": "C0CHAN1", "name": "#help"}]},
            "investigation": {"repo": str(repo), "bash": ["./bin/db-ro"], "mcp_tools": ["mcp__sentry__search_issues"]},
            "logins": [{"name": "cloud", "check": f'{sys.executable} -c "import os,sys; sys.exit(os.path.exists(r\'{self.login_flag}\'))"',
                        "fix": "cloud login", "needed_for": "prod data", "hold": True}],
        }
        r = subprocess.run([sys.executable, str(ROOT / "responder.py"), "write", "--answers", "-"],
                           input=json.dumps(answers), capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.cfg / "context.md").write_text("Globex context.\n")
        skill = self.tmp / ".claude" / "plugins" / "humanizer" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: humanizer\n---\n")
        (self.data / "state").mkdir(parents=True)
        (self.data / "state" / "last_digest").write_text(time.strftime("%Y-%m-%d"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_job(self, *extra, **env):
        return subprocess.run([sys.executable, str(ROOT / "responder.py"), "run", "--force", *extra],
                              capture_output=True, text=True, env={**self.env, **env})

    def calls(self, contains=""):
        if not self.calls_log.exists():
            return []
        return [c for c in map(json.loads, self.calls_log.read_text().splitlines()) if contains in c["prompt"]]

    def file(self, rel):
        p = self.data / rel
        return p.read_text() if p.exists() else ""

    # --- the main path ---------------------------------------------------------------------------
    def test_answers_a_triaged_question_with_the_right_guard_and_tools(self):
        r = self.run_job(FAKE_TRIAGE=json.dumps([ITEM]),
                         FAKE_INV='{"posted": true, "needs_owner": true, "needs_login": false, "summary": "s", "reply": "r"}')
        self.assertEqual(r.returncode, 0, r.stderr)
        triage, inv = self.calls("triage step")[0], self.calls("answering a Slack")[0]
        self.assertEqual(triage["guard_channel"], "")
        self.assertEqual((inv["guard_channel"], inv["guard_thread"]), ("C0CHAN1", "111.000"))
        self.assertTrue(inv["settings"])
        self.assertIn("Bash(./bin/db-ro:*)", inv["tools"])
        self.assertIn("mcp__sentry__search_issues", inv["tools"])
        self.assertIn("mcp__claude_ai_Slack__slack_send_message", inv["tools"])
        self.assertIn("C0CHAN1:111.222", self.file("state/handled.txt"))
        self.assertIn("<https://globex.slack.com/archives/C0CHAN1/p111222> q1", (self.data / "followups.md").read_text())
        self.assertIn("done C0CHAN1:111.222", self.file("logs/run.log"))
        self.assertTrue(self.file("state/last_check").strip().isdigit())
        settings = json.loads(self.file("state/guard-settings.json"))
        self.assertIn("slack_guard.py", settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])

    def test_handled_messages_are_not_investigated_twice(self):
        self.run_job(FAKE_TRIAGE=json.dumps([ITEM]))
        self.run_job(FAKE_TRIAGE=json.dumps([ITEM]))
        self.assertEqual(len(self.calls("answering a Slack")), 1)

    def test_ignored_items_are_logged_but_not_handled(self):
        ign = {**ITEM, "kind": "ignore", "summary": "ignored: already answered"}
        self.run_job(FAKE_TRIAGE=json.dumps([ign]))
        self.assertIn("ignored: already answered", self.file("logs/triage.log"))
        self.assertNotIn("111.222", self.file("state/handled.txt"))
        self.assertIn("triage: 0 item(s)", self.file("logs/run.log"))

    def test_dry_run_never_offers_the_send_tool_and_never_holds(self):
        (self.data / "DRY_RUN").write_text("")
        self.login_flag.write_text("")
        self.run_job(FAKE_TRIAGE=json.dumps([ITEM]),
                     FAKE_INV='{"posted": false, "needs_owner": false, "needs_login": true, "summary": "s", "reply": ""}')
        inv = self.calls("answering a Slack")[0]
        self.assertNotIn("mcp__claude_ai_Slack__slack_send_message", inv["tools"])
        self.assertEqual(self.file("state/held.jsonl").strip(), "")

    # --- logins and the hold queue ---------------------------------------------------------------
    def test_hold_login_down_queues_alerts_once_and_retries_when_back(self):
        self.login_flag.write_text("")
        r = self.run_job(FAKE_TRIAGE=json.dumps([ITEM]),
                         FAKE_INV='{"posted": false, "needs_owner": false, "needs_login": true, "summary": "s", "reply": ""}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"111.222"', self.file("state/held.jsonl"))
        self.assertIn("UNAVAILABLE right now: prod data", self.calls("answering a Slack")[0]["text"])
        alerts = self.calls("Send the text below")
        self.assertEqual(len(alerts), 1)
        self.assertIn("Fix: run `cloud login`", alerts[0]["text"])
        self.assertEqual((alerts[0]["guard_channel"], alerts[0]["guard_thread"]), ("U12345ABC", ""))
        self.run_job()  # still down: no new investigation, no second alert today
        self.assertEqual(len(self.calls("answering a Slack")), 1)
        self.assertEqual(len(self.calls("Send the text below")), 1)
        self.login_flag.unlink()
        self.run_job()  # back: retried once and removed from the queue
        self.assertEqual(len(self.calls("answering a Slack")), 2)
        self.assertEqual(self.file("state/held.jsonl").strip(), "")
        self.assertIn("logins are back: retrying 1 held item(s)", self.file("logs/run.log"))

    def test_held_question_older_than_three_days_becomes_a_follow_up(self):
        self.login_flag.write_text("")
        old = {**ITEM, "held_at": time.time() - 4 * 86400}
        (self.data / "state" / "held.jsonl").write_text(json.dumps(old) + "\n")
        self.run_job()
        self.assertEqual(self.file("state/held.jsonl").strip(), "")
        self.assertIn("waited 3 days", (self.data / "followups.md").read_text())

    def test_non_hold_login_down_alerts_right_away(self):
        cfg = self.cfg / "responder.toml"
        cfg.write_text(cfg.read_text().replace("hold = true", "hold = false"))
        self.login_flag.write_text("")
        self.run_job()
        self.assertIn("cloud login is not working", self.calls("Send the text below")[0]["text"])

    # --- failures and alerts --------------------------------------------------------------------
    def test_guard_block_during_investigation_alerts(self):
        self.run_job(FAKE_TRIAGE=json.dumps([ITEM]), FAKE_GUARD_BLOCK="1")
        self.assertIn("Blocked a Slack post outside the thread", self.calls("Send the text below")[0]["text"])

    def test_triage_failure_alerts_and_exits_1(self):
        r = self.run_job(FAKE_TRIAGE_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Triage failed", self.calls("Send the text below")[0]["text"])
        self.assertIn("OAuth token has expired", self.calls("Send the text below")[0]["text"])

    def test_claude_logged_out_alerts_and_stops(self):
        r = self.run_job(FAKE_AUTH='{"loggedIn": false}')
        self.assertEqual(r.returncode, 1)
        self.assertIn("Claude is logged out", self.calls("Send the text below")[0]["text"])
        self.assertEqual(self.calls("triage step"), [])

    def test_invalid_config_notifies_locally_and_exits_1(self):
        cfg = self.cfg / "responder.toml"
        cfg.write_text(cfg.read_text().replace("globex.slack.com", "bad domain"))
        r = self.run_job()
        self.assertEqual(r.returncode, 1)
        self.assertIn("config error", self.file("logs/run.log"))
        self.assertIn("Config error", self.notify_log.read_text())
        self.assertEqual(self.calls(), [])

    def test_missing_humanizer_alerts_and_still_runs(self):
        shutil.rmtree(self.tmp / ".claude")
        self.run_job(FAKE_TRIAGE=json.dumps([ITEM]))
        self.assertIn("Humanizer skill not found", self.calls("Send the text below")[0]["text"])
        self.assertEqual(len(self.calls("answering a Slack")), 1)

    # --- switches and schedule ------------------------------------------------------------------
    def test_paused_does_nothing(self):
        (self.data / "PAUSED").write_text("")
        self.run_job()
        self.assertEqual(self.calls(), [])

    def test_outside_working_days_does_nothing_without_force(self):
        cfg = self.cfg / "responder.toml"
        today = time.localtime().tm_wday + 1
        cfg.write_text(cfg.read_text().replace("days = [1, 2, 3, 4, 5]", f"days = [{today % 7 + 1}]"))
        subprocess.run([sys.executable, str(ROOT / "responder.py"), "run"], env=self.env, capture_output=True)
        self.assertEqual(self.calls(), [])

    def test_morning_digest_is_sent_once_and_archives_follow_ups(self):
        (self.data / "state" / "last_digest").unlink()
        (self.data / "followups.md").write_text("- item one\n")
        self.run_job()
        self.assertEqual(len(self.calls("morning Slack summary")), 1)
        self.assertEqual((self.data / "followups.md").read_text(), "")
        self.assertIn("item one", self.file("followups-archive.md"))
        self.run_job()
        self.assertEqual(len(self.calls("morning Slack summary")), 1)

    def test_markers_written_by_the_old_shell_runner_are_honored(self):
        (self.data / "state" / "last_digest").write_text(time.strftime("%Y-%m-%d") + "\n")
        (self.data / "state" / "alerts").mkdir()
        (self.data / "state" / "alerts" / "humanizer").write_text(time.strftime("%Y-%m-%d") + "\n")
        shutil.rmtree(self.tmp / ".claude")
        self.run_job()
        self.assertEqual(self.calls("morning Slack summary"), [])
        self.assertEqual(self.calls("Send the text below"), [])

    def test_environment_path_from_config_finds_tools(self):
        cfg = self.cfg / "responder.toml"
        self.assertIn("path = []", cfg.read_text())
        cfg.write_text(cfg.read_text().replace("path = []", f'path = ["{self.bin}"]'))
        path_without_bin = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p != str(self.bin))
        self.run_job(PATH=path_without_bin, FAKE_TRIAGE=json.dumps([ITEM]))
        self.assertEqual(len(self.calls("answering a Slack")), 1)

    def test_old_run_sh_shim_forwards_to_the_python_job(self):
        r = subprocess.run(["/bin/sh", str(ROOT / "run.sh"), "--item", "{not json"], capture_output=True, text=True,
                           env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--item is not valid JSON", r.stderr)

    def test_outdated_launchd_timer_is_reported(self):
        import plistlib
        agents = self.tmp / "LaunchAgents"
        agents.mkdir()
        old = {"Label": "local.slack-responder", "ProgramArguments": ["/bin/zsh", "-l", "-c", str(ROOT / "run.sh")]}
        (agents / "local.slack-responder.plist").write_bytes(plistlib.dumps(old))
        self.run_job(RESPONDER_PLATFORM="macos", RESPONDER_AGENTS_DIR=str(agents))
        alert = self.calls("Send the text below")[0]["text"]
        self.assertIn("old run.sh", alert)
        self.assertIn("python3 responder.py install", alert)

    def test_current_launchd_timer_is_not_reported(self):
        import plistlib
        agents = self.tmp / "LaunchAgents"
        agents.mkdir()
        new = {"Label": "local.slack-responder", "ProgramArguments": [sys.executable, str(ROOT / "responder.py"), "run"]}
        (agents / "local.slack-responder.plist").write_bytes(plistlib.dumps(new))
        self.run_job(RESPONDER_PLATFORM="macos", RESPONDER_AGENTS_DIR=str(agents))
        self.assertEqual(self.calls("Send the text below"), [])

    def test_triage_entries_that_are_not_objects_are_skipped(self):
        r = self.run_job(FAKE_TRIAGE=json.dumps([1, "noise", ITEM]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(self.calls("answering a Slack")), 1)
        self.assertIn("triage: 1 item(s)", self.file("logs/run.log"))

    def test_claude_that_cannot_start_is_reported_without_traceback(self):
        (self.bin / "claude").write_text("#!/nonexistent/interpreter\n")
        r = self.run_job()
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("could not start claude", self.file("logs/stderr.log"))
        self.assertTrue(self.notify_log.exists())

    def test_fresh_lock_blocks_and_stale_lock_is_taken(self):
        lock = self.data / "state" / "lock"
        lock.mkdir()
        self.run_job()
        self.assertEqual(self.calls(), [])
        old = time.time() - 41 * 60
        os.utime(lock, (old, old))
        self.run_job()
        self.assertEqual(len(self.calls("triage step")), 1)
        self.assertFalse(lock.exists())
        self.assertEqual(list((self.data / "state").glob("lock*")), [])

    def test_item_flag_skips_triage(self):
        self.run_job("--item", json.dumps(ITEM))
        self.assertEqual(self.calls("triage step"), [])
        self.assertEqual(len(self.calls("answering a Slack")), 1)


if __name__ == "__main__":
    unittest.main()
