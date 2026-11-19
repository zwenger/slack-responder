# Data contract

Which files belong to the **template** (shared, updated by pulling upstream) and which belong to **you**
(personal, never committed, never touched by updates).

**The rule:** anything about a person or a company goes in the user layer. A tracked file never contains your
name, Slack IDs, workspace, repo, tools or internal URLs. `python3 responder.py leakcheck` enforces it.

## User layer (yours, gitignored)

| File | Purpose | Written by |
|---|---|---|
| `config/responder.toml` | Identity, sources, schedule, repo, tools, logins, alerts | `responder.py write` (guided setup) or by hand |
| `config/context.md` | What the agent needs to know: product, how to use each tool, self-service tools, what never to say | guided setup or by hand |
| `CLAUDE.local.md` | Your own notes for Claude sessions working on this clone | you |
| `.engram/` | Your Engram project settings for this folder (optional) | you |
| `followups.md`, `followups-archive.md` | Questions answered that still need you | the job |
| `state/` | `last_check`, `last_digest`, `handled.txt`, `held.jsonl`, `alerts/`, generated `mcp.json` and `guard-settings.json` | the job |
| `logs/` | `run.log`, `replies.log`, `triage.log`, `guard.log`, `stderr.log`, `launchd.*` or `cron.log` | the job, the timer, the guard hook |
| `PAUSED`, `DRY_RUN` | Switches | you |
| The installed timer: `~/Library/LaunchAgents/local.slack-responder.plist` (macOS), `~/.config/systemd/user/slack-responder.{service,timer}` (Linux), scheduled task `slack-responder` (Windows) | Runs `responder.py run` | `responder.py install` |

## Template layer (shared, tracked)

| File | Purpose |
|---|---|
| `runner.py` | The job (`responder.py run`): preflight, digest, triage, investigation, alerts |
| `platforms.py` | What differs per OS: the timer install and the local notification |
| `run.sh` | Compatibility shim for timers installed before the Python runner; the job warns until you reinstall |
| `responder.py` | Entry point and config tool: `doctor`, `write`, `run`, `install`, `uninstall`, `leakcheck`, `env`, `render`, `logins`, `mcp` |
| `triage.md`, `investigate.md`, `digest.md` | Prompt templates; values come only from `{{PLACEHOLDERS}}` |
| `config/responder.example.toml`, `config/context.example.md` | Starting points for the user layer |
| `hooks/slack_guard.py` | PreToolUse hook that pins every Slack post of a session to one destination |
| `tests/` | `python3 -m unittest discover -s tests` |
| `CLAUDE.md`, `README.md`, `DATA_CONTRACT.md` | Setup guide for Claude, runbook, this contract |

To change behavior for yourself, edit the user layer. Change the template layer only for something every user
should get, and keep it free of personal or company data.
