#!/usr/bin/env python3
"""slack-responder configuration tool.

Reads and writes the per-user configuration (config/responder.toml + config/context.md) and gives
the job (runner.py) everything it needs, so no tracked file holds company or personal data.

Commands:
  doctor                  read-only setup check, prints JSON (what is missing, invalid or unedited)
  write --answers FILE|-  validate answers (JSON) and write config/responder.toml (--force to replace)
  env                     print the config as shell assignments (for debugging and scripts)
  render TEMPLATE [K=V…]  fill {{PLACEHOLDERS}} of a prompt from the config, context.md and K=V pairs
  logins                  run the [[logins]] checks, prints JSON
  mcp OUT                 write the investigation MCP config (read-only Engram, CodeGraph)
  install [--dry-run]     install the timer (launchd / systemd / Task Scheduler); uninstall removes it
  run [--force] [--item]  run the job once (what the timer calls)
  leakcheck               fail if a tracked file contains a value from your config (before publishing)
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+; main() explains how to upgrade instead of failing with an ImportError
except ModuleNotFoundError:
    tomllib = None

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("RESPONDER_CONFIG_DIR") or ROOT / "config")
CONFIG = CONFIG_DIR / "responder.toml"
CONTEXT = CONFIG_DIR / "context.md"
TEMPLATE_MARKER = "<!-- TEMPLATE:"

DEFAULTS = {
    "owner": {"role": "the developer"},
    "sources": {"mentions": True, "dms": False, "channels": []},
    "schedule": {"days": [1, 2, 3, 4, 5], "start_hour": 9, "end_hour": 19, "interval_seconds": 300},
    "investigation": {"engram_project": "", "codegraph": False, "bash": [], "mcp_tools": []},
    "logins": [],
    "alerts": {"claude_expiry_days": 2},
    "environment": {"path": []},
    "reply": {"footer": "_🤖 Investigated automatically by {name}'s assistant. Reply here if something looks off._"},
}
# Values from config/responder.example.toml: still present means the user has not personalized them.
EXAMPLE_VALUES = {
    "owner.name": "Your Name", "owner.slack_user_id": "U00000000", "owner.company": "Acme",
    "slack.workspace_domain": "acme.slack.com", "investigation.repo": "~/dev/your-repo",
}


class ConfigError(Exception):
    pass


# --- loading and validation -------------------------------------------------------------------
def merged(cfg):
    out = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULTS.items()}
    for key, value in cfg.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def get(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# The investigation must stay read-only. These checks back up the guided setup: they refuse obvious write
# tools in the config itself. They are a guard against mistakes, not a sandbox: only allow tools you trust.
WRITE_WORDS = {"write", "create", "update", "delete", "remove", "send", "post", "put", "patch", "set", "upload",
               "apply", "exec", "execute", "run", "deploy", "add", "edit", "modify", "insert", "drop", "publish",
               "schedule", "push", "commit", "merge", "rm", "mv", "cp", "kill", "restart", "reset", "truncate",
               "grant", "revoke", "invite", "archive", "close", "move", "rename", "save", "import", "sync"}
SHELL_META = set(";|&><`$\n\\")


def writable_bash(prefix):
    if SHELL_META & set(prefix) or "*" in prefix:
        return "contains shell operators or wildcards; allow a plain command prefix"
    words = re.split(r"[\s/=]+", prefix.lower().replace("-x", "-x "))
    if words and words[0] in {"sudo", "dd", "chmod", "chown", "tee", "sh", "bash", "zsh", "python", "python3", "node"}:
        return "can run or change anything; allow a specific read-only command"
    bad = WRITE_WORDS & set(words) | ({"post", "put", "delete", "patch"} & set(words))
    return f"looks like it can write ({', '.join(sorted(bad))}); allow read-only commands only" if bad else ""


def writable_mcp(tool):
    if "*" in tool:
        return "wildcards could include write tools; list each read-only tool"
    if not tool.startswith("mcp__"):
        return "is not an MCP tool name (mcp__<server>__<tool>)"
    bad = WRITE_WORDS & set(re.split(r"[_\W]+", tool.split("__")[-1].lower()))
    return f"looks like it can write ({', '.join(sorted(bad))}); allow read-only tools only" if bad else ""


def validate(cfg):
    """Return a list of "path: problem" strings; empty means the config is usable."""
    errors = [f"{k}: must be a table" for k in ("owner", "slack", "sources", "schedule", "investigation", "alerts", "reply",
                                                 "environment")
              if not isinstance(cfg.get(k), dict)]
    if not isinstance(cfg.get("logins"), list):
        errors.append("logins: must be a list of [[logins]] tables")
    if errors:
        return errors

    def need_str(path, pattern=None, hint=""):
        v = get(cfg, path)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{path}: required")
        elif pattern and not re.fullmatch(pattern, v):
            errors.append(f"{path}: {v!r} {hint}")

    need_str("owner.name")
    need_str("owner.company")
    need_str("owner.slack_user_id", r"[UW][A-Z0-9]{6,}", "is not a Slack member ID (starts with U or W)")
    need_str("slack.workspace_domain", r"[a-z0-9-]+\.(enterprise\.)?slack\.com", "is not a workspace domain like acme.slack.com")
    need_str("investigation.repo")
    repo = get(cfg, "investigation.repo")
    if isinstance(repo, str) and repo.strip() and repo != EXAMPLE_VALUES["investigation.repo"]:
        if not Path(repo).expanduser().is_dir():
            errors.append(f"investigation.repo: {repo!r} is not a directory")

    src = cfg.get("sources", {})
    channels = src.get("channels", [])
    if not isinstance(channels, list):
        errors.append("sources.channels: must be a list")
        channels = []
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict) or not re.fullmatch(r"[CG][A-Z0-9]{6,}", str(ch.get("id", ""))):
            errors.append(f"sources.channels[{i}].id: not a Slack channel ID (starts with C or G)")
    if not (src.get("mentions") or src.get("dms") or channels):
        errors.append("sources: enable mentions, dms or at least one channel")

    sch = cfg.get("schedule", {})
    days = sch.get("days")
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
        errors.append("schedule.days: list of ISO weekdays 1-7")
    s, e = sch.get("start_hour"), sch.get("end_hour")
    if not (isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= 24):
        errors.append("schedule: need 0 <= start_hour < end_hour <= 24")

    iv = sch.get("interval_seconds")
    if not isinstance(iv, int) or isinstance(iv, bool) or iv < 60:
        errors.append("schedule.interval_seconds: whole number of seconds, at least 60")
    exp = get(cfg, "alerts.claude_expiry_days")
    if not isinstance(exp, int) or isinstance(exp, bool) or not 0 <= exp <= 30:
        errors.append("alerts.claude_expiry_days: whole number of days between 0 and 30")

    paths = get(cfg, "environment.path")
    if not isinstance(paths, list) or not all(isinstance(x, str) and Path(x).expanduser().is_absolute() for x in paths):
        errors.append("environment.path: list of absolute folders (where the timer should also look for commands)")

    for key in ("bash", "mcp_tools"):
        v = get(cfg, f"investigation.{key}")
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            errors.append(f"investigation.{key}: list of non-empty strings")
            continue
        check = writable_bash if key == "bash" else writable_mcp
        errors += [f"investigation.{key}: {x!r} {why}" for x in v if (why := check(x))]
    for i, lg in enumerate(cfg.get("logins", [])):
        for f in ("name", "check", "fix"):
            if not isinstance(lg, dict) or not isinstance(lg.get(f), str) or not lg[f].strip():
                errors.append(f"logins[{i}].{f}: required")
    return errors


def unpersonalized(cfg):
    out = [{"path": p, "reason": "still the example value"} for p, ex in EXAMPLE_VALUES.items() if get(cfg, p) == ex]
    if any(ch.get("id") == "C00000000" for ch in get(cfg, "sources.channels") or [] if isinstance(ch, dict)):
        out.append({"path": "sources.channels", "reason": "still the example channel"})
    return out


def read_toml(path):
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: not valid TOML: {e}")


def load():
    if not CONFIG.exists():
        raise ConfigError(f"{CONFIG} is missing: run the guided setup (open Claude Code in this folder)")
    cfg = merged(read_toml(CONFIG))
    errors = validate(cfg)
    if errors:
        raise ConfigError("invalid config/responder.toml:\n  " + "\n  ".join(errors))
    return cfg


# --- TOML writing (stdlib has a reader only) --------------------------------------------------
def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def dump_toml(cfg):
    lines = ["# Written by `responder.py write`. Edit by hand or rerun the guided setup.", ""]

    def table(name, data):
        scalars = {k: v for k, v in data.items() if not (isinstance(v, list) and v and isinstance(v[0], dict))}
        arrays = {k: v for k, v in data.items() if k not in scalars}
        lines.append(f"[{name}]")
        lines.extend(f"{k} = {toml_value(v)}" for k, v in scalars.items())
        lines.append("")
        for k, items in arrays.items():
            for item in items:
                lines.append(f"[[{name}.{k}]]")
                lines.extend(f"{ik} = {toml_value(iv)}" for ik, iv in item.items())
                lines.append("")

    for name, data in cfg.items():
        if isinstance(data, dict):
            table(name, data)
    for name, data in cfg.items():
        if isinstance(data, list):
            for item in data:
                lines.append(f"[[{name}]]")
                lines.extend(f"{k} = {toml_value(v)}" for k, v in item.items())
                lines.append("")
    return "\n".join(lines)


# --- commands ---------------------------------------------------------------------------------
def rel(p):
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def check_claude():
    exe = shutil.which("claude")
    if not exe:
        return "missing", "`claude` is not on PATH: open a new terminal after installing Claude Code, or add its folder to PATH"
    try:
        status = json.loads(subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30).stdout)
    except Exception as e:
        return "missing", f"`claude auth status` failed ({e}): run it in a terminal"
    if not isinstance(status, dict):
        return "missing", "`claude auth status` printed something unexpected: run it in a terminal"
    return ("ok", "") if status.get("loggedIn") else ("missing", "Run `claude auth login`")


def prerequisites(cfg):
    """What the job needs besides the config, each with its status and the fix for this OS.
    status: ok | missing (required) | check (Claude must verify it) | optional (works without it)."""
    import platforms
    py = platforms.PYTHON
    items = [{"name": "python", "status": "ok", "fix": ""}]
    status, fix = check_claude()
    items.append({"name": "claude", "status": status, "fix": fix})
    items.append({"name": "slack-connector", "status": "check",
                  "fix": "Call `slack_search_users` with the user's name. If the tool is missing or fails, enable the "
                         "Slack connector in claude.ai (Settings → Connectors) and reconnect it"})
    installed, detail = platforms.timer_status(ROOT)
    items.append({"name": "timer", "status": "ok" if installed else "missing", "detail": detail,
                  "fix": "" if installed else f"After the dry run: `{py} responder.py install`"})
    ok, fix = platforms.notifier_status()
    items.append({"name": "notifier", "status": "ok" if ok else "optional", "fix": fix})
    humanizer = False
    try:
        for skill in (Path.home() / ".claude" / "plugins").rglob("SKILL.md"):
            try:
                if re.search(r"^name: *humanizer", skill.read_text(errors="ignore"), re.M):
                    humanizer = True
                    break
            except OSError:
                continue
    except OSError:
        pass  # unreadable plugins folder: report humanizer as optional, never crash doctor
    items.append({"name": "humanizer", "status": "ok" if humanizer else "optional",
                  "fix": "" if humanizer else "Optional: replies go out un-humanized. The humanizer skill appears by itself "
                         "when your claude.ai organization provides it"})
    if cfg:
        path = os.pathsep.join([*map(str, cfg["environment"]["path"]), os.environ.get("PATH", "")])
        for name, wanted in (("engram", cfg["investigation"]["engram_project"]), ("codegraph", cfg["investigation"]["codegraph"])):
            if wanted:
                found = shutil.which(name, path=path)
                items.append({"name": name, "status": "ok" if found else "missing",
                              "fix": "" if found else f"Install {name}, or add the folder that contains it to "
                                     "[environment] path in config/responder.toml (the timer starts with a minimal PATH)"})
    return items


def cmd_doctor(_args):
    report = {"setup_needed": False, "config": str(CONFIG), "missing": [], "invalid": [], "unpersonalized": []}
    if not CONFIG.exists():
        report["missing"].append("config/responder.toml")
    else:
        try:
            cfg = merged(read_toml(CONFIG))
            report["invalid"] = validate(cfg)
            report["unpersonalized"] = unpersonalized(cfg)
        except ConfigError as e:
            report["invalid"] = [str(e)]
    if not CONTEXT.exists():
        report["missing"].append("config/context.md")
    elif TEMPLATE_MARKER in CONTEXT.read_text():
        report["unpersonalized"].append({"path": "config/context.md", "reason": "still the template"})
    report["setup_needed"] = bool(report["missing"] or report["invalid"] or report["unpersonalized"])
    try:
        cfg = load()
    except ConfigError:
        cfg = None
    report["prerequisites"] = prerequisites(cfg)
    report["prerequisites_ok"] = all(p["status"] != "missing" for p in report["prerequisites"] if p["name"] in ("python", "claude"))
    print(json.dumps(report, indent=2))
    return 0


def cmd_write(args):
    raw = sys.stdin.read() if args.answers == "-" else Path(args.answers).read_text()
    try:
        answers = json.loads(raw)
    except ValueError as e:
        print(f"not written: the answers are not valid JSON ({e})", file=sys.stderr)
        return 2
    if not isinstance(answers, dict):
        print("not written: the answers must be a JSON object", file=sys.stderr)
        return 2
    cfg = merged(answers)
    errors = validate(cfg) + [f"{u['path']}: {u['reason']}" for u in unpersonalized(cfg)]
    if errors:
        print("not written, fix these answers:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 2
    if CONFIG.exists() and not args.force:
        print(f"{rel(CONFIG)} already exists; pass --force to replace it", file=sys.stderr)
        return 3
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".toml.tmp")
    tmp.write_text(dump_toml(cfg))
    tomllib.loads(tmp.read_text())  # never leave a file the loader cannot read
    tmp.replace(CONFIG)
    print(f"wrote {rel(CONFIG)}")
    return 0


def shell_env(cfg):
    q = shlex.quote
    src, sch, inv = cfg["sources"], cfg["schedule"], cfg["investigation"]
    pairs = {
        "OWNER_NAME": cfg["owner"]["name"], "USER_ID": cfg["owner"]["slack_user_id"],
        "COMPANY": cfg["owner"]["company"], "WORKSPACE_DOMAIN": cfg["slack"]["workspace_domain"],
        "REPO": str(Path(inv["repo"]).expanduser()), "ENGRAM_PROJECT": inv["engram_project"],
        "USE_CODEGRAPH": int(bool(inv["codegraph"])), "WATCH_MENTIONS": int(bool(src["mentions"])),
        "WATCH_DMS": int(bool(src["dms"])), "START_HOUR": sch["start_hour"], "END_HOUR": sch["end_hour"],
        "INTERVAL_SECONDS": sch["interval_seconds"],
        "CLAUDE_EXPIRY_DAYS": cfg["alerts"]["claude_expiry_days"],
    }
    arrays = {
        "WORK_DAYS": [str(d) for d in sch["days"]], "CHANNEL_IDS": [c["id"] for c in src["channels"]],
        "EXTRA_BASH": inv["bash"], "EXTRA_MCP": inv["mcp_tools"],
    }
    out = [f"{k}={q(str(v))}" for k, v in pairs.items()]
    out += [f"{k}=({' '.join(q(x) for x in v)})" for k, v in arrays.items()]
    return "\n".join(out) + "\n"


def cmd_env(_args):
    print(shell_env(load()), end="")
    return 0


def triage_sources(cfg, since):
    name, uid = cfg["owner"]["name"], cfg["owner"]["slack_user_id"]
    src, out = cfg["sources"], []
    if src["mentions"]:
        out.append(f'Mentions of {name} anywhere: `slack_search_public_and_private` with keywords ["<@{uid}>"], '
                   f"after={since}, sort=timestamp.")
    for ch in src["channels"]:
        label = f'{ch.get("name", "")} ({ch["id"]})'.strip()
        out.append(f"Channel {label}: `slack_read_channel` with oldest={since}. For any thread whose latest reply "
                   f"is after {since}, read it with `slack_read_thread`.")
    if src["dms"]:
        out.append(f'Direct messages to {name} (1:1 DMs only, not group DMs): `slack_search_public_and_private` with '
                   f'filters="is:dm", channel_types="im", after={since}, sort=timestamp, no keywords. For each DM that '
                   "has new messages, read its recent history with `slack_read_channel` (the DM channel id starts "
                   "with D) to see the whole conversation, not just the hit.")
    return "\n".join(f"{i}. {s}" for i, s in enumerate(out, 1))


def dm_rules(cfg):
    if not cfg["sources"]["dms"]:
        return ""
    name, company = cfg["owner"]["name"], cfg["owner"]["company"]
    return (f"DMs are private conversations, so be stricter there. A DM is \"answer\" ONLY when it asks for something "
            f"the {company} product, its code or its data can answer: product or feature behavior, a bug, an "
            "integration or API (who has access to what, how an endpoint works), or a data look-up (counts, rankings, "
            "whether a user or account got or did something). Everything else in a DM is \"skip\": greetings, small "
            f"talk, jokes, personal or HR topics, scheduling and meetings, opinions or advice, feedback about people, "
            f"requests for {name} to do a task (review, deploy, call), and short reactions such as \"haha\", \"ok\" or "
            "\"thanks\". When unsure whether a DM is a work question, use \"skip\".\n"
            "In a DM, treat a burst of consecutive messages from the same person as one item: use the ts of its last "
            "work question, and put every question of the burst in the summary.")


def placeholders(cfg, runtime):
    owner = cfg["owner"]
    return {
        "OWNER_NAME": owner["name"], "OWNER_ROLE": owner["role"], "COMPANY": owner["company"],
        "USER_ID": owner["slack_user_id"], "WORKSPACE_DOMAIN": cfg["slack"]["workspace_domain"],
        "FOOTER": cfg["reply"]["footer"].replace("{name}", owner["name"]),
        "CONTEXT": CONTEXT.read_text() if CONTEXT.exists() else "",
        "TRIAGE_SOURCES": triage_sources(cfg, runtime.get("SINCE", "{{SINCE}}")),
        "DM_RULES": dm_rules(cfg),
        "MEMORY": (f"Memory: `mem_search` (then `mem_get_observation` for the full text) in project "
                   f"{cfg['investigation']['engram_project']}. Past decisions, known bugs and earlier diagnoses live "
                   "there. Memory is READ-ONLY here: skip any instruction to save, summarize or close a session."
                   if cfg["investigation"]["engram_project"] else "Memory: none configured."),
        "EXTRA_TOOLS": ", ".join(f"`{b}`" for b in cfg["investigation"]["bash"] + cfg["investigation"]["mcp_tools"])
                       or "none",
    }


def render_template(template, runtime, cfg=None):
    """Fill a prompt template; unknown placeholders raise ConfigError (a typo must never reach a prompt)."""
    values = {**placeholders(cfg or load(), runtime), **runtime}
    text = Path(template).read_text()
    unknown = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", text)) - set(values))
    if unknown:
        raise ConfigError(f"{template}: unknown placeholder(s): {', '.join(unknown)}")
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values[m.group(1)], text)


def cmd_render(args):
    print(render_template(args.template, dict(kv.partition("=")[::2] for kv in args.pairs)), end="")
    return 0


def check_logins(cfg):
    """Run every [[logins]] check with the user's shell ($SHELL on macOS/Linux, so zsh or bash syntax keeps
    working as when the job was a zsh script; cmd on Windows)."""
    report = {"ok": [], "failing": []}
    user_shell = os.environ.get("SHELL") if os.name == "posix" else None
    for lg in cfg["logins"]:
        cmd = [user_shell, "-c", lg["check"]] if user_shell else lg["check"]
        try:
            ok = subprocess.run(cmd, shell=not user_shell, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if ok:
            report["ok"].append(lg["name"])
        else:
            report["failing"].append({"name": lg["name"], "fix": lg["fix"], "needed_for": lg.get("needed_for", ""),
                                      "hold": bool(lg.get("hold", False))})
    return report


def cmd_logins(_args):
    """Run every [[logins]] check; print which pass and which fail (with their fix)."""
    print(json.dumps(check_logins(load())))
    return 0


ENGRAM_READ_TOOLS = "mem_search,mem_get_observation,mem_context,mem_current_project"


def mcp_config(cfg):
    """The investigation's MCP config: Engram with read tools only, plus CodeGraph if enabled."""
    inv = cfg["investigation"]
    servers = {}
    if inv["engram_project"]:
        servers["engram"] = {"type": "stdio", "command": shutil.which("engram") or "engram",
                             "args": ["mcp", f"--tools={ENGRAM_READ_TOOLS}", "--project", inv["engram_project"]]}
    if inv["codegraph"]:
        servers["codegraph"] = {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]}
    return {"mcpServers": servers}


def cmd_mcp(args):
    Path(args.out).write_text(json.dumps(mcp_config(load()), indent=2) + "\n")
    return 0


def leak_values(cfg):
    """Values from the local config that must never appear in tracked files."""
    owner, inv = cfg["owner"], cfg["investigation"]
    domain = cfg["slack"]["workspace_domain"]
    values = {owner["name"], owner["slack_user_id"], owner["company"], domain, domain.split(".")[0],
              Path(inv["repo"]).expanduser().name, inv["engram_project"], str(Path.home()), *inv["bash"]}
    for ch in cfg["sources"]["channels"]:
        values |= {ch["id"], ch.get("name", "")}
    for lg in cfg["logins"]:
        values.add(lg["check"])
    if CONTEXT.exists():  # hosts named in context.md (internal tools, admin apps…)
        values |= set(re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", CONTEXT.read_text(), re.I))
    return sorted(v for v in values if len(v) >= 4)


def cmd_leakcheck(args):
    """Fail when a tracked file contains a value from this clone's config (run before publishing)."""
    root = Path(args.root)
    values = leak_values(load())
    files = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True).stdout
    hits = []
    pattern = re.compile("|".join(re.escape(v) for v in values), re.I)
    for name in filter(None, files.split("\0")):
        if Path(name).name.upper().startswith(("LICENSE", "COPYING")):
            continue  # the copyright holder is public on purpose
        try:
            lines = (root / name).read_text().splitlines()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(lines, 1):
            for m in pattern.finditer(line):
                hits.append(f"{name}:{n}: {m.group(0)!r} in: {line.strip()[:120]}")
    if hits:
        print(f"{len(hits)} tracked line(s) contain values from your config:\n" + "\n".join(hits))
        return 1
    print(f"clean: no tracked file contains any of {len(values)} values from your config")
    return 0


def cmd_install(args):
    """Install or reload the timer that runs `responder.py run` (launchd, systemd or Task Scheduler)."""
    import platforms
    cfg = load()
    if not CONTEXT.exists():
        raise ConfigError(f"{CONTEXT} is missing: finish the guided setup first")
    return platforms.install(cfg, ROOT, dry_run=args.dry_run, agents_dir=args.agents_dir)


def cmd_uninstall(args):
    import platforms
    return platforms.uninstall(ROOT, agents_dir=args.agents_dir)


def cmd_run(args):
    import runner  # imported here: the config commands must work even if the job module has a problem
    item = args.item or os.environ.get("ITEM")
    try:
        item = json.loads(item) if item else None
    except ValueError as e:
        print(f"--item is not valid JSON: {e}", file=sys.stderr)
        return 2
    return runner.run(force=args.force or os.environ.get("FORCE") == "1", item=item)


def main(argv=None):
    if sys.version_info < (3, 11) or tomllib is None:
        import platforms
        print(f"slack-responder needs Python 3.11 or newer; this is {sys.version.split()[0]}.\n{platforms.python_fix()}",
              file=sys.stderr)
        return 3
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="read-only setup check (JSON)").set_defaults(fn=cmd_doctor)
    w = sub.add_parser("write", help="validate answers and write config/responder.toml")
    w.add_argument("--answers", required=True, help="JSON file with the answers, or - for stdin")
    w.add_argument("--force", action="store_true", help="replace an existing config")
    w.set_defaults(fn=cmd_write)
    sub.add_parser("env", help="the config as shell assignments").set_defaults(fn=cmd_env)
    r = sub.add_parser("render", help="fill a prompt template")
    r.add_argument("template")
    r.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    r.set_defaults(fn=cmd_render)
    sub.add_parser("logins", help="run the [[logins]] checks (JSON)").set_defaults(fn=cmd_logins)
    m = sub.add_parser("mcp", help="write the investigation MCP config")
    m.add_argument("out")
    m.set_defaults(fn=cmd_mcp)
    for name, fn, text in (("install", cmd_install, "install or reload the timer (launchd / systemd / Task Scheduler)"),
                           ("uninstall", cmd_uninstall, "stop and remove the timer")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--agents-dir", default="~/Library/LaunchAgents")
        if name == "install":
            s.add_argument("--dry-run", action="store_true", help="show what would be installed, change nothing")
        s.set_defaults(fn=fn)
    rn = sub.add_parser("run", help="run the job once (what the timer calls)")
    rn.add_argument("--force", action="store_true", help="ignore working days and hours (also FORCE=1)")
    rn.add_argument("--item", help="JSON of one message to investigate, skipping triage (also ITEM=…)")
    rn.set_defaults(fn=cmd_run)
    lk = sub.add_parser("leakcheck", help="fail if tracked files contain values from your config")
    lk.add_argument("--root", default=str(ROOT))
    lk.set_defaults(fn=cmd_leakcheck)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
