"""What differs per operating system: the local notification and (see install/uninstall) the timer."""
import os
import plistlib
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = os.environ.get("RESPONDER_PLATFORM") or {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(
    platform.system(), "other")


def notify(title, text):
    """Best-effort desktop notification; returns True when one was shown. Never raises."""
    log = os.environ.get("RESPONDER_NOTIFY_LOG")  # tests: record instead of showing
    if log:
        with open(log, "a") as f:
            f.write(f"{title}: {text}\n")
        return True
    try:
        if SYSTEM == "macos":
            cmd = ["osascript", "-e", "on run argv", "-e",
                   'display notification (item 2 of argv) with title (item 1 of argv) sound name "Basso"',
                   "-e", "end run", title, text]
        elif SYSTEM == "linux" and shutil.which("notify-send"):
            cmd = ["notify-send", "--urgency=critical", title, text]
        elif SYSTEM == "windows":
            script = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
                      "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
                      "$n.ShowBalloonTip(10000, $env:RSP_TITLE, $env:RSP_TEXT, 'Warning'); Start-Sleep -Seconds 10; "
                      "$n.Dispose()")
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
            return subprocess.run(cmd, env={**os.environ, "RSP_TITLE": title, "RSP_TEXT": text},
                                  capture_output=True, timeout=30).returncode == 0
        else:
            return False
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except Exception as e:  # a missing notifier must never break the run
        print(f"notify failed: {e}", file=sys.stderr)
        return False


# --- timer ----------------------------------------------------------------------------------------
NAME = "slack-responder"
LABEL = "local.slack-responder"  # launchd label


def job_command(root):
    """The command every timer runs. On Windows, pythonw.exe avoids a console window every few minutes."""
    exe = Path(sys.executable)
    if SYSTEM == "windows" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"
    return [str(exe), str(Path(root) / "responder.py"), "run"]


def install_path():
    """The PATH of the terminal running `install`, kept for the timer. Timers start without the user's shell
    profile (nvm, pyenv, cloud SDKs…); the old macOS timer got it from `zsh -l`, so keep it explicitly."""
    return os.environ.get("PATH", "")


def _run(tool_env, default, *args):
    """Run a system tool; tests point RESPONDER_<TOOL> at a fake to never touch the real scheduler."""
    return subprocess.run([os.environ.get(tool_env, default), *args], capture_output=True, text=True)


def install(cfg, root, dry_run=False, agents_dir="~/Library/LaunchAgents"):
    interval = cfg["schedule"]["interval_seconds"]
    if SYSTEM == "macos":
        return _install_launchd(root, interval, dry_run, Path(agents_dir).expanduser())
    if SYSTEM == "linux":
        return _install_linux(root, interval, dry_run)
    if SYSTEM == "windows":
        return _install_windows(root, interval, dry_run)
    print(f"no timer support for this system ({platform.system()}): run `{shlex.join(job_command(root))}` "
          f"every {interval} s with your own scheduler", file=sys.stderr)
    return 1


def outdated_timer(root):
    """A message when the installed timer still calls the old run.sh (macOS installs before the Python runner)."""
    if SYSTEM != "macos":
        return ""
    agents = Path(os.environ.get("RESPONDER_AGENTS_DIR") or Path.home() / "Library" / "LaunchAgents")
    old = str(Path(root) / "run.sh")
    for plist in agents.glob("*.plist"):
        try:
            if old in map(str, plistlib.loads(plist.read_bytes()).get("ProgramArguments", [])):
                return f"{plist.name} still calls the old run.sh"
        except Exception:
            continue
    return ""


def uninstall(root, agents_dir="~/Library/LaunchAgents"):
    if SYSTEM == "macos":
        _run("RESPONDER_LAUNCHCTL", "launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        (Path(agents_dir).expanduser() / f"{LABEL}.plist").unlink(missing_ok=True)
    elif SYSTEM == "linux":
        units = Path.home() / ".config" / "systemd" / "user"
        _run("RESPONDER_SYSTEMCTL", "systemctl", "--user", "disable", "--now", f"{NAME}.timer")
        for suffix in ("service", "timer"):
            (units / f"{NAME}.{suffix}").unlink(missing_ok=True)
        _run("RESPONDER_SYSTEMCTL", "systemctl", "--user", "daemon-reload")
        print(f"if you used cron instead, remove the `{Path(root) / 'responder.py'} run` line with crontab -e")
    elif SYSTEM == "windows":
        _run("RESPONDER_SCHTASKS", "schtasks", "/Delete", "/F", "/TN", NAME)
    print(f"uninstalled {NAME}")
    return 0


def _install_launchd(root, interval, dry_run, agents):
    logs = Path(root) / "logs"
    plist = plistlib.dumps({"Label": LABEL, "ProgramArguments": job_command(root), "StartInterval": interval,
                            "EnvironmentVariables": {"PATH": install_path()},
                            "RunAtLoad": False, "StandardOutPath": str(logs / "launchd.out"),
                            "StandardErrorPath": str(logs / "launchd.err")}).decode()
    if dry_run:
        print(plist, end="")
        return 0
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(exist_ok=True)
    target, domain = agents / f"{LABEL}.plist", f"gui/{os.getuid()}"
    # An older install of this same folder (another label, or the old run.sh job) would run twice: remove it.
    ours = {str(Path(root) / "run.sh"), str(Path(root) / "responder.py")}
    for old in agents.glob("*.plist"):
        if old == target:
            continue
        try:
            data = plistlib.loads(old.read_bytes())
        except Exception:
            continue
        if ours & set(map(str, data.get("ProgramArguments", []))) and data.get("Label"):
            _run("RESPONDER_LAUNCHCTL", "launchctl", "bootout", f"{domain}/{data['Label']}")
            old.unlink()
            print(f"removed the older timer {data['Label']} ({old})")
    _run("RESPONDER_LAUNCHCTL", "launchctl", "bootout", f"{domain}/{LABEL}")  # fine if it was not loaded
    target.write_text(plist)
    r = _run("RESPONDER_LAUNCHCTL", "launchctl", "bootstrap", domain, str(target))
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"installed {target}: runs every {interval} s while the Mac is awake")
    return 0


def _install_linux(root, interval, dry_run):
    cmd = shlex.join(job_command(root))
    systemctl = os.environ.get("RESPONDER_SYSTEMCTL", "systemctl")
    if not shutil.which(systemctl):
        minutes = max(1, interval // 60)
        line = f"*/{minutes} * * * * {cmd} >> {shlex.quote(str(Path(root) / 'logs' / 'cron.log'))} 2>&1"
        print(f"systemd user timers are not available here. Add this line with `crontab -e`:\n{line}")
        return 0
    units = Path.home() / ".config" / "systemd" / "user"
    path = install_path().replace("%", "%%").replace('"', "")  # systemd escapes % as %%
    service = (f"[Unit]\nDescription=slack-responder job\n\n[Service]\nType=oneshot\nWorkingDirectory={root}\n"
               f'Environment="PATH={path}"\nExecStart={cmd}\n')
    timer = (f"[Unit]\nDescription=Run slack-responder every {interval} s\n\n[Timer]\nOnBootSec=2min\n"
             f"OnUnitActiveSec={interval}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    steps = ["systemctl --user daemon-reload", f"systemctl --user enable --now {NAME}.timer"]
    if dry_run:
        print(f"# {units / (NAME + '.service')}\n{service}\n# {units / (NAME + '.timer')}\n{timer}\n# then:\n"
              + "\n".join(steps))
        return 0
    units.mkdir(parents=True, exist_ok=True)
    (units / f"{NAME}.service").write_text(service)
    (units / f"{NAME}.timer").write_text(timer)
    for step in steps:
        r = _run("RESPONDER_SYSTEMCTL", "systemctl", *step.split()[1:])
        if r.returncode != 0:
            print(f"`{step}` failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
    print(f"installed {NAME}.timer: runs every {interval} s while the machine is awake "
          "(to keep it running while you are logged out: `loginctl enable-linger`)")
    return 0


def _install_windows(root, interval, dry_run):
    minutes = max(1, interval // 60)
    args = ["/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes), "/TN", NAME,
            "/TR", subprocess.list2cmdline(job_command(root))]
    if dry_run:
        print("schtasks " + subprocess.list2cmdline(args))
        return 0
    r = _run("RESPONDER_SCHTASKS", "schtasks", *args)
    if r.returncode != 0:
        print(f"schtasks failed: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
        return 1
    print(f"installed the scheduled task {NAME}: runs every {minutes} min while you are logged in")
    return 0


# --- prerequisite checks (used by `responder.py doctor`) ---------------------------------------------
PYTHON = "py" if SYSTEM == "windows" else "python3"  # how to call Python in the fixes we print


def python_fix(system=SYSTEM):
    return {
        "macos": "Install Python 3.11+: `brew install python@3.12` (Homebrew) or the installer from python.org",
        "linux": "Install Python 3.11+: `sudo apt install python3` on Debian 12 / Ubuntu 24.04 or newer, "
                 "`sudo dnf install python3` on Fedora; on older releases use `uv python install 3.12`",
        "windows": "Install Python 3.11+: `winget install Python.Python.3.12`, then use `py` instead of `python3`",
    }.get(system, "Install Python 3.11 or newer for your system")


def timer_status(root):
    """(installed, detail) for the timer `install` sets up on this OS."""
    job = str(Path(root) / "responder.py")
    try:
        if SYSTEM == "macos":
            agents = Path(os.environ.get("RESPONDER_AGENTS_DIR") or Path.home() / "Library" / "LaunchAgents")
            plist = agents / f"{LABEL}.plist"
            if not plist.exists():
                return False, f"no {plist.name} in {agents}"
            args = list(map(str, plistlib.loads(plist.read_bytes()).get("ProgramArguments", [])))
            return job in args, "installed" if job in args else f"{plist.name} runs something else: {' '.join(args)}"
        if SYSTEM == "linux":
            systemctl = os.environ.get("RESPONDER_SYSTEMCTL", "systemctl")
            if shutil.which(systemctl):
                ok = _run("RESPONDER_SYSTEMCTL", "systemctl", "--user", "is-enabled", f"{NAME}.timer").returncode == 0
                return ok, f"{NAME}.timer " + ("enabled" if ok else "not enabled")
            cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            ok = f"{job} run" in cron.stdout
            return ok, "cron line present" if ok else "no systemd and no cron line"
        if SYSTEM == "windows":
            ok = _run("RESPONDER_SCHTASKS", "schtasks", "/Query", "/TN", NAME).returncode == 0
            return ok, f"scheduled task {NAME} " + ("found" if ok else "not found")
    except (OSError, plistlib.InvalidFileException) as e:
        return False, f"could not check: {e}"
    return False, f"no timer support for {platform.system()}"


def notifier_status():
    """(available, fix) for the desktop notification used when Slack cannot be reached."""
    if SYSTEM == "linux" and not shutil.which("notify-send"):
        return False, ("Optional: `sudo apt install libnotify-bin` (Debian/Ubuntu) or `sudo dnf install libnotify` "
                       "(Fedora) to get a desktop notification when Slack is unreachable")
    return True, ""
