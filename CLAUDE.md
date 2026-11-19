# CLAUDE.md — slack-responder

A scheduled job that answers Slack questions meant for its owner: it triages new messages, investigates them
read-only in a local repo and replies in the thread. You are here to **set it up** or to **work on the tool**,
never to answer Slack yourself.

Personal notes for this clone, if any, live in `CLAUDE.local.md` (gitignored).

## First run: check the setup (always do this first)

```bash
python3 responder.py doctor        # Windows: py responder.py doctor
```

It is read-only and prints JSON: the config state (`missing`, `invalid`, `unpersonalized`, `setup_needed`) and
`prerequisites`, one item per need with a `status` and a `fix` written for this OS. If Python is missing or older
than 3.11, the command itself fails or prints how to install a newer one: do that first (step 1).
If `setup_needed` is `false` and every prerequisite is `ok`, skip to "Working on the tool". Otherwise tell the
user in one line what is missing and offer the guided setup below. Never copy example files over the user's
files as a side effect.

## Guided setup

Ask one step at a time, in the user's language. Look values up yourself when a tool can (Slack connector,
`ls`, `git`), and confirm instead of making the user type IDs.

1. **Prerequisites.** Go through `doctor`'s `prerequisites`, one at a time:
   - `missing`: explain what it is for, show its `fix`, and offer to run that command for the user. Run it only
     after they agree; for commands that need `sudo` or a browser login, let the user run them. Re-run `doctor`.
   - `check`: verify it yourself (the Slack connector: call `slack_search_users` with the user's name).
   - `optional`: mention it once with its `fix`; the job works without it.
   - The `timer` item stays `missing` until step 9; that is expected.
   - macOS is the verified OS. On Linux or Windows tell the user they are testers and ask them to report what
     fails. On Windows call Python as `py`.
2. **Who they are.** Name, company, one-line role ("the backend lead"), Slack member ID (find it with
   `slack_search_users`), workspace domain (from any message permalink, like `acme.slack.com`).
3. **What it listens to.** @mentions (yes/no), 1:1 DMs (yes/no; only product, code or data questions get an
   answer), and the channels whose questions they normally answer (find IDs with `slack_search_channels`).
4. **When.** Working days (ISO, 1 = Monday), start and end hour. The job only runs while the computer is awake.
5. **How it investigates.** The local repo to investigate (must exist). Optional: an Engram memory project,
   CodeGraph. Then the read-only tools they already use (a read-replica DB command, a log reader, MCP
   connectors such as Sentry or a CRM). **Only read-only tools**: refuse anything that can write.
   For each tool that needs a login that expires, add a `[[logins]]` entry: a `check` command (exit 0 = OK),
   the `fix` command, what it is `needed_for`, and `hold = true` if questions should wait for it.
   `check` runs with the user's shell (`$SHELL` on macOS/Linux, `cmd` on Windows): write it for the user's OS.
   The timer starts with a minimal PATH: put the folders of `claude`, these tools and the MCP servers in
   `environment.path` (for example the `bin` of a Node version manager).
6. **Context.** Interview them and write `config/context.md` (sections from `config/context.example.md`,
   without its TEMPLATE marker): the company and product, how to use each tool (exact syntax and limits),
   what askers can fix themselves (admin tools, URLs, where in the code to confirm an action exists), and what
   never to say.
7. **Write the config.** Build the answers as JSON with the shape of `config/responder.example.toml` and run
   `python3 responder.py write --answers - <<'JSON' … JSON`. It validates and refuses bad values: fix and retry.
   Use `--force` only when the user asked to redo the setup. Run `doctor` again until `setup_needed` is false.
8. **Dry run.** Create an empty `DRY_RUN` file, then `python3 responder.py run --force`, then show `tail logs/run.log` and the last entries of
   `logs/replies.log`. Nothing is posted to others in this mode. Adjust `config/context.md` if answers look off.
9. **Install the timer.** With the user's OK: `python3 responder.py install`. Then `rm DRY_RUN` when they want
   it to start replying for real. Explain the routine in README → "Your routine".

## Working on the tool

- **The job may be live.** Edits to `runner.py`, `responder.py` and the prompts take effect on the next run. For risky changes:
  `touch PAUSED` (messages wait; nothing is lost) or `touch DRY_RUN` (investigates, never posts), test with
  `python3 responder.py run --force` or `--item '<json>'`, read `logs/replies.log`, then remove the file.
- **Respect the data contract** (`DATA_CONTRACT.md`). Anything specific to a person or company goes in
  `config/`, never in the code, the prompts or the docs. Prompts get values only through `{{PLACEHOLDERS}}`
  that `responder.py render` fills; it rejects unknown ones.
- **Keep it read-only.** Never add write tools to `BASE_INVESTIGATE` in `runner.py`, never give Engram write tools
  in `responder.py mcp`, never let prompts post outside the answered thread or the owner's own DM.
- **Keep the Slack guard.** Every `claude` call goes through `Job.claude` in `runner.py`, which passes `--settings`
  with `hooks/slack_guard.py` and sets `SLACK_GUARD_CHANNEL` (and `SLACK_GUARD_THREAD`). Never call `claude` around it;
  never weaken the hook to make a post go through.
- `state/`, `logs/` and `followups*.md` hold Slack content: never commit, paste or publish them.
- Checks: `python3 -m unittest discover -s tests`. Changes to `platforms.py` need a tester on that OS. Before publishing or opening a PR to an
  upstream template: `python3 responder.py leakcheck` must say `clean`.
- After a meaningful change, update `README.md` (and its diagram) and `DATA_CONTRACT.md` if files moved.
