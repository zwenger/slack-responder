"""Tests for responder.py: config loading, validation, doctor, write, env and render."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, str(ROOT / "responder.py")]

VALID_ANSWERS = {
    "owner": {"name": "Dana", "slack_user_id": "U12345ABC", "company": "Globex", "role": "the backend lead"},
    "slack": {"workspace_domain": "globex.slack.com"},
    "sources": {"mentions": True, "dms": True, "channels": [{"id": "C99887766", "name": "#eng-help"}]},
    "investigation": {"repo": "REPO_PLACEHOLDER", "bash": ["./bin/db-ro"], "mcp_tools": []},
    "logins": [{"name": "cloud", "check": "true", "fix": "cloud login", "needed_for": "prod data", "hold": True}],
}


class ResponderCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.env = {**os.environ, "RESPONDER_CONFIG_DIR": str(self.tmp / "config")}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args, stdin=None):
        return subprocess.run(CLI + list(args), input=stdin, capture_output=True, text=True, env=self.env)

    def answers(self, **overrides):
        a = json.loads(json.dumps(VALID_ANSWERS))
        a["investigation"]["repo"] = str(self.repo)
        for section, values in overrides.items():
            a[section].update(values)
        return json.dumps(a)

    def write_valid(self):
        r = self.run_cli("write", "--answers", "-", stdin=self.answers())
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.tmp / "config" / "context.md").write_text("Globex makes rockets.\n")

    # doctor --------------------------------------------------------------
    def test_doctor_without_config_needs_setup(self):
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["setup_needed"])
        self.assertIn("config/responder.toml", d["missing"])
        self.assertIn("config/context.md", d["missing"])

    def test_doctor_flags_unedited_example_as_unpersonalized(self):
        cfg = self.tmp / "config"
        cfg.mkdir()
        shutil.copy(ROOT / "config" / "responder.example.toml", cfg / "responder.toml")
        shutil.copy(ROOT / "config" / "context.example.md", cfg / "context.md")
        d = json.loads(self.run_cli("doctor").stdout)
        self.assertTrue(d["setup_needed"])
        paths = {u["path"] for u in d["unpersonalized"]}
        self.assertIn("owner.slack_user_id", paths)
        self.assertIn("config/context.md", paths)

    def test_doctor_after_valid_write_is_ready(self):
        self.write_valid()
        d = json.loads(self.run_cli("doctor").stdout)
        self.assertFalse(d["setup_needed"], d)
        self.assertEqual(d["invalid"], [])

    # write / validation --------------------------------------------------
    def test_write_rejects_invalid_values_and_writes_nothing(self):
        r = self.run_cli("write", "--answers", "-", stdin=self.answers(owner={"slack_user_id": "dana"}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("owner.slack_user_id", r.stderr)
        self.assertFalse((self.tmp / "config" / "responder.toml").exists())

    def test_write_keeps_existing_config_unless_force(self):
        self.write_valid()
        r = self.run_cli("write", "--answers", "-", stdin=self.answers(owner={"name": "Other"}))
        self.assertEqual(r.returncode, 3)
        self.assertIn("--force", r.stderr)
        r = self.run_cli("write", "--force", "--answers", "-", stdin=self.answers(owner={"name": "Other"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        env = self.run_cli("env").stdout
        self.assertIn("OWNER_NAME=Other", env)

    # env -----------------------------------------------------------------
    def test_env_exports_shell_safe_values(self):
        self.write_valid()
        r = self.run_cli("env")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = subprocess.run(["zsh", "-c", r.stdout + '\nprint -r -- "$USER_ID|$WORKSPACE_DOMAIN|${CHANNEL_IDS[1]}|$WATCH_DMS|${EXTRA_BASH[1]}|$REPO"'],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, f"U12345ABC|globex.slack.com|C99887766|1|./bin/db-ro|{self.repo}")

    def test_env_fails_when_config_is_invalid(self):
        self.write_valid()
        p = self.tmp / "config" / "responder.toml"
        p.write_text(p.read_text().replace("globex.slack.com", "not a domain"))
        r = self.run_cli("env")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("slack.workspace_domain", r.stderr)

    # render --------------------------------------------------------------
    def test_render_fills_config_and_runtime_placeholders(self):
        self.write_valid()
        tpl = self.tmp / "t.md"
        tpl.write_text("{{OWNER_NAME}} at {{COMPANY}} ({{USER_ID}}) ts={{TS}}\n{{CONTEXT}}")
        r = self.run_cli("render", str(tpl), "TS=1.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "Dana at Globex (U12345ABC) ts=1.2\nGlobex makes rockets.\n")

    def test_render_rejects_unknown_placeholders(self):
        self.write_valid()
        tpl = self.tmp / "t.md"
        tpl.write_text("hello {{NOPE}}")
        r = self.run_cli("render", str(tpl))
        self.assertEqual(r.returncode, 2)
        self.assertIn("NOPE", r.stderr)


    def test_render_builds_triage_sources_from_config(self):
        self.write_valid()
        tpl = self.tmp / "t.md"
        tpl.write_text("{{TRIAGE_SOURCES}}\n---\n{{DM_RULES}}")
        out = self.run_cli("render", str(tpl), "SINCE=100").stdout
        self.assertIn("<@U12345ABC>", out)          # mentions
        self.assertIn("#eng-help (C99887766)", out)  # channel
        self.assertIn('channel_types="im"', out)     # DMs
        self.assertIn("Globex", out.split("---")[1])  # DM rules mention the company
        p = self.tmp / "config" / "responder.toml"
        p.write_text(p.read_text().replace("dms = true", "dms = false"))
        out = self.run_cli("render", str(tpl), "SINCE=100").stdout
        self.assertNotIn('channel_types="im"', out)
        self.assertEqual(out.split("---")[1].strip(), "")

    # logins --------------------------------------------------------------
    def test_logins_reports_failing_checks_with_fix(self):
        a = json.loads(self.answers())
        a["logins"] = [{"name": "ok", "check": "true", "fix": "x"},
                       {"name": "cloud", "check": "false", "fix": "cloud login", "needed_for": "prod data", "hold": True}]
        self.assertEqual(self.run_cli("write", "--answers", "-", stdin=json.dumps(a)).returncode, 0)
        d = json.loads(self.run_cli("logins").stdout)
        self.assertEqual(d["ok"], ["ok"])
        self.assertEqual([f["name"] for f in d["failing"]], ["cloud"])
        self.assertEqual(d["failing"][0]["fix"], "cloud login")
        self.assertTrue(d["failing"][0]["hold"])

    def test_login_checks_run_with_the_users_shell(self):
        marker = self.tmp / "shell-used"
        fake_shell = self.tmp / "myshell"
        fake_shell.write_text(f'#!/bin/sh\necho "$@" > {marker}\nexit 0\n')
        fake_shell.chmod(0o755)
        a = json.loads(self.answers())
        a["logins"] = [{"name": "x", "check": "my-check --flag", "fix": "y"}]
        self.assertEqual(self.run_cli("write", "--answers", "-", stdin=json.dumps(a)).returncode, 0)
        self.env["SHELL"] = str(fake_shell)
        d = json.loads(self.run_cli("logins").stdout)
        self.assertEqual(d["ok"], ["x"])
        self.assertEqual(marker.read_text().strip(), "-c my-check --flag")

    # mcp -----------------------------------------------------------------
    def test_mcp_config_has_read_only_engram_and_optional_codegraph(self):
        a = json.loads(self.answers(investigation={"engram_project": "globex-api", "codegraph": True}))
        self.assertEqual(self.run_cli("write", "--answers", "-", stdin=json.dumps(a)).returncode, 0)
        out = self.tmp / "mcp.json"
        self.assertEqual(self.run_cli("mcp", str(out)).returncode, 0)
        servers = json.loads(out.read_text())["mcpServers"]
        args = " ".join(servers["engram"]["args"])
        self.assertIn("--project globex-api", args)
        self.assertNotIn("mem_save", args)
        self.assertIn("codegraph", servers)

    # install -------------------------------------------------------------
    def test_install_dry_run_renders_plist_from_config(self):
        self.write_valid()
        agents = self.tmp / "LaunchAgents"
        r = self.run_cli("install", "--dry-run", "--agents-dir", str(agents))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<string>local.slack-responder</string>", r.stdout)
        self.assertIn(f"<string>{ROOT}/responder.py</string>", r.stdout)
        self.assertIn("<integer>300</integer>", r.stdout)
        self.assertIn(f"{ROOT}/logs/launchd.err", r.stdout)
        self.assertNotIn("{{", r.stdout)
        self.assertFalse(agents.exists())  # dry run writes nothing

    def test_install_refuses_when_setup_is_incomplete(self):
        r = self.run_cli("install", "--dry-run", "--agents-dir", str(self.tmp / "LaunchAgents"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("responder.toml", r.stderr)

    # read-only enforcement / numeric validation ------------------------------
    def write_with(self, **investigation):
        return self.run_cli("write", "--answers", "-", stdin=self.answers(investigation=investigation))

    def test_write_rejects_tools_that_can_write(self):
        for bash in (["rm -rf"], ["git push"], ["psql; drop table x"], ["curl -X POST"], ["kubectl delete"]):
            r = self.write_with(bash=bash)
            self.assertEqual(r.returncode, 2, bash)
            self.assertIn("investigation.bash", r.stderr)
        for tool in (["mcp__claude_ai_Customer_io__cio_write_api"], ["mcp__claude_ai_Slack__slack_send_message"],
                     ["mcp__sentry__*"], ["mcp__linear__create_issue"]):
            r = self.write_with(mcp_tools=tool)
            self.assertEqual(r.returncode, 2, tool)
            self.assertIn("investigation.mcp_tools", r.stderr)

    def test_write_accepts_read_only_tools(self):
        r = self.write_with(bash=["./bin/db-readonly query", "kubectl get pods"],
                            mcp_tools=["mcp__claude_ai_Customer_io__cio_read_api", "mcp__sentry__search_issues",
                                       "mcp__sentry__get_event_stacktrace"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_write_rejects_relative_environment_path(self):
        a = json.loads(self.answers())
        a["environment"] = {"path": ["relative/bin"]}
        r = self.run_cli("write", "--answers", "-", stdin=json.dumps(a))
        self.assertEqual(r.returncode, 2)
        self.assertIn("environment.path", r.stderr)

    def test_write_rejects_bad_numbers(self):
        a = json.loads(self.answers())
        a["schedule"] = {"interval_seconds": 5}
        a["alerts"] = {"claude_expiry_days": "two"}
        r = self.run_cli("write", "--answers", "-", stdin=json.dumps(a))
        self.assertEqual(r.returncode, 2)
        self.assertIn("schedule.interval_seconds", r.stderr)
        self.assertIn("alerts.claude_expiry_days", r.stderr)

    def test_write_handles_null_values_without_traceback(self):
        a = json.loads(self.answers())
        a["owner"] = None
        a["sources"]["channels"] = None
        r = self.run_cli("write", "--answers", "-", stdin=json.dumps(a))
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_install_escapes_plist_values(self):
        self.write_valid()
        import plistlib
        r = self.run_cli("install", "--dry-run", "--agents-dir", str(self.tmp / "LaunchAgents"))
        self.assertEqual(plistlib.loads(r.stdout.encode())["Label"], "local.slack-responder")

    def test_install_removes_older_timer_for_this_run_sh(self):
        self.write_valid()
        import plistlib
        agents = self.tmp / "LaunchAgents"
        agents.mkdir()
        old = {"Label": "com.someone.slack-responder", "ProgramArguments": ["/bin/zsh", "-c", str(ROOT / "run.sh")]}
        other = {"Label": "com.other.job", "ProgramArguments": ["/bin/zsh", "-c", "/elsewhere/run.sh"]}
        (agents / "old.plist").write_bytes(plistlib.dumps(old))
        (agents / "other.plist").write_bytes(plistlib.dumps(other))
        log = self.tmp / "launchctl.log"
        fake = self.tmp / "launchctl"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
        fake.chmod(0o755)
        self.env["RESPONDER_LAUNCHCTL"] = str(fake)
        r = self.run_cli("install", "--agents-dir", str(agents))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((agents / "old.plist").exists())
        self.assertTrue((agents / "other.plist").exists())
        self.assertTrue((agents / "local.slack-responder.plist").exists())
        calls = log.read_text()
        self.assertIn("com.someone.slack-responder", calls)
        self.assertIn("bootstrap", calls)
        self.assertNotIn("com.other.job", calls)

    def test_install_on_macos_runs_the_python_job(self):
        self.write_valid()
        import plistlib
        r = self.run_cli("install", "--dry-run", "--agents-dir", str(self.tmp / "LaunchAgents"))
        args = plistlib.loads(r.stdout.encode())["ProgramArguments"]
        self.assertEqual(args[1:], [str(ROOT / "responder.py"), "run"])

    def test_install_on_linux_writes_a_systemd_user_timer(self):
        self.write_valid()
        self.env.update(RESPONDER_PLATFORM="linux", RESPONDER_SYSTEMCTL=shutil.which("true"))
        r = self.run_cli("install", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("slack-responder.timer", r.stdout)
        self.assertIn(f"{ROOT}/responder.py run", r.stdout)
        self.assertIn("OnUnitActiveSec=300", r.stdout)
        self.assertIn("systemctl --user enable --now slack-responder.timer", r.stdout)

    def test_install_keeps_the_installing_shell_path_for_the_timer(self):
        self.write_valid()
        import plistlib
        self.env["PATH"] = "/opt/custom-node/bin" + os.pathsep + self.env["PATH"]
        r = self.run_cli("install", "--dry-run", "--agents-dir", str(self.tmp / "LaunchAgents"))
        env = plistlib.loads(r.stdout.encode())["EnvironmentVariables"]
        self.assertTrue(env["PATH"].startswith("/opt/custom-node/bin"))
        self.env.update(RESPONDER_PLATFORM="linux", RESPONDER_SYSTEMCTL=shutil.which("true"))
        r = self.run_cli("install", "--dry-run")
        self.assertIn('Environment="PATH=/opt/custom-node/bin', r.stdout)

    def test_install_on_linux_without_systemd_prints_a_cron_line(self):
        self.write_valid()
        self.env.update(RESPONDER_PLATFORM="linux", RESPONDER_SYSTEMCTL="/nonexistent/systemctl")
        r = self.run_cli("install", "--dry-run")
        self.assertIn("*/5 * * * *", r.stdout)
        self.assertIn("crontab -e", r.stdout)

    def test_install_on_windows_creates_a_scheduled_task(self):
        self.write_valid()
        self.env["RESPONDER_PLATFORM"] = "windows"
        r = self.run_cli("install", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("schtasks /Create /F /SC MINUTE /MO 5 /TN slack-responder", r.stdout)
        self.assertIn("responder.py", r.stdout)
        self.assertIn(" run", r.stdout)

    # leakcheck -----------------------------------------------------------
    def make_git_repo(self, files):
        root = self.tmp / "tree"
        root.mkdir()
        for name, text in files.items():
            (root / name).write_text(text)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        return root

    def test_leakcheck_finds_config_values_in_tracked_files(self):
        self.write_valid()
        root = self.make_git_repo({"a.md": "generic text\n", "b.sh": "ID=U12345ABC\n# see globex.slack.com\n"})
        r = self.run_cli("leakcheck", "--root", str(root))
        self.assertEqual(r.returncode, 1)
        self.assertIn("b.sh:1", r.stdout)
        self.assertIn("U12345ABC", r.stdout)
        self.assertIn("b.sh:2", r.stdout)
        self.assertNotIn("a.md", r.stdout)

    def test_leakcheck_ignores_the_license_copyright_line(self):
        self.write_valid()
        root = self.make_git_repo({"LICENSE": "Copyright (c) 2026 Dana\n", "a.md": "generic\n"})
        r = self.run_cli("leakcheck", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_leakcheck_passes_on_a_clean_tree(self):
        self.write_valid()
        root = self.make_git_repo({"a.md": "Your Name works at Acme\n"})
        r = self.run_cli("leakcheck", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("clean", r.stdout)

    def test_write_reports_bad_json_without_traceback(self):
        r = self.run_cli("write", "--answers", "-", stdin="{not json")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
