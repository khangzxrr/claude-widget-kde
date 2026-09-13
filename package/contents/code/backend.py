#!/usr/bin/env python3
"""Backend for the Claude Sessions plasmoid.

Every command prints a single JSON object on stdout.

  list  [--limit N]                  recent Claude Code sessions, newest first, running ones on top
  usage [--max-age SECONDS]          plan usage from the OAuth usage endpoint (cached on disk)
  open  SESSION_ID [terminal opts]   focus the window of a running session, or resume it in a terminal
  new   CWD [terminal opts]          start a fresh session in CWD

Terminal opts: --terminal NAME|auto|custom  --command TEMPLATE  --keep-shell  --dry-run
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIVE_DIR = CLAUDE_DIR / "sessions"
CREDENTIALS = CLAUDE_DIR / ".credentials.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "claude-sessions-plasmoid"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
INDEX_VERSION = 2

COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SKIP_PROMPT_PREFIXES = ("<local-command", "<system-reminder>", "<bash-", "Caveat:", "[Request interrupted")

# argv builders for known terminals: (cwd, command argv) -> argv
TERMINALS = {
    "alacritty": lambda cwd, cmd: ["alacritty", "--working-directory", cwd, "-e", *cmd],
    "konsole": lambda cwd, cmd: ["konsole", "--workdir", cwd, "-e", *cmd],
    "kitty": lambda cwd, cmd: ["kitty", "--directory", cwd, *cmd],
    "ghostty": lambda cwd, cmd: ["ghostty", f"--working-directory={cwd}", "-e", *cmd],
    "wezterm": lambda cwd, cmd: ["wezterm", "start", "--cwd", cwd, "--", *cmd],
    "foot": lambda cwd, cmd: ["foot", "-D", cwd, *cmd],
    "gnome-terminal": lambda cwd, cmd: ["gnome-terminal", f"--working-directory={cwd}", "--", *cmd],
    "xterm": lambda cwd, cmd: ["xterm", "-e", *cmd],
}
# /proc/<pid>/comm is truncated to 15 chars and some terminals use helper binaries
COMM_ALIASES = {"wezterm-gui": "wezterm", "gnome-terminal-": "gnome-terminal", "gnome-terminal-server": "gnome-terminal"}


def emit(obj, code=0):
    json.dump(obj, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.exit(code)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def display_path(path):
    if not path:
        return ""
    home = str(HOME)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def one_line(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- sessions

def prompt_text(obj):
    """The human-typed text of a transcript `user` record, or None for tool results and meta records."""
    if obj.get("isMeta") or obj.get("isSidechain") or obj.get("isCompactSummary"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            return None
        content = "\n".join(texts)
    if not isinstance(content, str):
        return None
    text = content.strip()
    name = COMMAND_NAME_RE.search(text)
    if name:
        args = COMMAND_ARGS_RE.search(text)
        return f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    if not text or text.startswith(SKIP_PROMPT_PREFIXES):
        return None
    return text


def parse_transcript(path):
    info = {"cwd": None, "branch": None, "title": None, "aiTitle": None,
            "firstPrompt": None, "lastPrompt": None, "prompts": 0}
    with open(path, "rb") as f:
        for raw in f:
            # cheap prefilter: transcripts can be many MB of assistant/tool output
            if b'"type":"user"' not in raw and b'title"' not in raw and b'"last-prompt"' not in raw \
                    and b'"summary"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            kind = obj.get("type")
            if kind == "user":
                if obj.get("isSidechain"):
                    continue
                info["cwd"] = info["cwd"] or obj.get("cwd")
                branch = obj.get("gitBranch")
                if branch and branch != "HEAD":
                    info["branch"] = branch
                text = prompt_text(obj)
                if text:
                    info["prompts"] += 1
                    info["firstPrompt"] = info["firstPrompt"] or one_line(text, 200)
                    info["lastPrompt"] = one_line(text, 300)
            elif kind == "custom-title":
                info["title"] = obj.get("customTitle") or obj.get("title") or info["title"]
            elif kind == "ai-title":
                info["aiTitle"] = obj.get("aiTitle") or info["aiTitle"]
            elif kind == "summary" and not info["aiTitle"]:
                info["aiTitle"] = obj.get("summary")
            elif kind == "last-prompt" and obj.get("lastPrompt"):
                info["lastPrompt"] = one_line(obj["lastPrompt"], 300)
    return info


def scan_transcripts():
    """{session_id: (path, mtime, info)} using an mtime/size keyed cache so only changed files are re-read."""
    index_path = CACHE_DIR / "index.json"
    index = read_json(index_path, {})
    if index.get("version") != INDEX_VERSION:
        index = {"version": INDEX_VERSION, "files": {}}
    old_files, new_files, result = index["files"], {}, {}
    for path in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        entry = old_files.get(key)
        if not entry or entry["mtime"] != st.st_mtime_ns or entry["size"] != st.st_size:
            try:
                entry = {"mtime": st.st_mtime_ns, "size": st.st_size, "info": parse_transcript(path)}
            except OSError:
                continue
        new_files[key] = entry
        result[path.stem] = (path, st.st_mtime_ns // 1_000_000, entry["info"])
    if new_files != old_files:
        try:
            write_json(index_path, {"version": INDEX_VERSION, "files": new_files})
        except OSError:
            pass
    return result


def proc_stat_fields(pid):
    """Fields of /proc/<pid>/stat starting at field 3 (state), or None if the process is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2:].split()


def ancestors(pid):
    chain = []
    while pid and pid > 1 and len(chain) < 32:
        fields = proc_stat_fields(pid)
        if fields is None:
            break
        chain.append(pid)
        pid = int(fields[1])
    return chain


def live_sessions():
    live = {}
    for path in LIVE_DIR.glob("*.json"):
        data = read_json(path)
        if not isinstance(data, dict) or not data.get("pid") or not data.get("sessionId"):
            continue
        fields = proc_stat_fields(data["pid"])
        # starttime (field 22) guards against pid reuse after a crash left the file behind
        if fields is None or (data.get("procStart") and fields[19] != str(data["procStart"])):
            continue
        live[data["sessionId"]] = {
            "pid": data["pid"],
            "status": data.get("status") or "running",
            "name": data.get("name"),
            "kind": data.get("kind"),
            "cwd": data.get("cwd"),
        }
    return live


def cmd_list(args):
    live = live_sessions()
    sessions = []
    for sid, (path, modified, info) in scan_transcripts().items():
        running = live.get(sid)
        if not info["prompts"] and not running:
            continue
        cwd = info["cwd"] or (running or {}).get("cwd")
        sessions.append({
            "id": sid,
            "title": one_line(info["title"] or info["aiTitle"] or info["firstPrompt"] or "Untitled session", 120),
            "project": cwd or "",
            "projectDisplay": display_path(cwd),
            "projectExists": bool(cwd and os.path.isdir(cwd)),
            "branch": info["branch"] or "",
            "firstPrompt": info["firstPrompt"] or "",
            "lastPrompt": info["lastPrompt"] or "",
            "prompts": info["prompts"],
            "modified": modified,
            "live": running,
        })
    sessions.sort(key=lambda s: (s["live"] is None, -s["modified"]))
    sessions = sessions[: args.limit]

    projects, seen = [], set()
    for s in sorted(sessions, key=lambda s: -s["modified"]):
        if s["project"] and s["projectExists"] and s["project"] not in seen:
            seen.add(s["project"])
            projects.append({"path": s["project"], "display": s["projectDisplay"]})
    emit({"sessions": sessions, "projects": projects[:15], "generatedAt": int(time.time() * 1000)})


# ---------------------------------------------------------------- usage

def parse_time_ms(value):
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def normalize_usage(data):
    limits = []
    for item in data.get("limits") or []:
        kind = item.get("kind") or ""
        model = (((item.get("scope") or {}).get("model")) or {}).get("display_name")
        label = {"session": "Session", "weekly_all": "Weekly"}.get(kind) or kind.replace("_", " ").capitalize()
        if model:
            label = f"Weekly · {model}" if kind.startswith("weekly") else f"{label} · {model}"
        limits.append({"label": label, "group": item.get("group") or kind, "percent": round(item.get("percent") or 0),
                       "severity": item.get("severity") or "normal", "resetsAt": parse_time_ms(item.get("resets_at"))})
    if limits:
        return limits
    # older response shape
    for key, label, group in (("five_hour", "Session", "session"), ("seven_day", "Weekly", "weekly"),
                              ("seven_day_opus", "Weekly · Opus", "weekly"), ("seven_day_sonnet", "Weekly · Sonnet", "weekly")):
        window = data.get(key)
        if window and window.get("utilization") is not None:
            limits.append({"label": label, "group": group, "percent": round(window["utilization"]),
                           "severity": "normal", "resetsAt": parse_time_ms(window.get("resets_at"))})
    return limits


def cmd_usage(args):
    cache_path = CACHE_DIR / "usage.json"
    cache = read_json(cache_path) or {"limits": [], "fetchedAt": 0}
    now = time.time()
    if now - max(cache.get("fetchedAt", 0), cache.get("attemptedAt", 0)) < args.max_age:
        emit(cache)

    def fail(message):
        cache.update(error=message, attemptedAt=now)
        try:
            write_json(cache_path, cache)
        except OSError:
            pass
        emit(cache)

    oauth = (read_json(CREDENTIALS) or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        fail("Not logged in to Claude Code")
    if oauth.get("expiresAt") and oauth["expiresAt"] / 1000 < now:
        fail("Login expired, run claude once to refresh it")

    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-sessions-plasmoid",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        fail("Rate limited, showing last known usage" if e.code == 429 else f"Usage API returned HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError):
        fail("Offline, showing last known usage")

    cache = {"limits": normalize_usage(data), "plan": oauth.get("subscriptionType"),
             "fetchedAt": now, "attemptedAt": now, "error": None}
    try:
        write_json(cache_path, cache)
    except OSError:
        pass
    emit(cache)


# ---------------------------------------------------------------- launching

def notify(summary, body=""):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Claude Sessions", "-i", "utilities-terminal", summary, body],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fail_launch(message, detail=""):
    notify(message, detail)
    emit({"ok": False, "error": message, "detail": detail}, 1)


def find_claude():
    extra = [HOME / ".local/bin", HOME / ".claude/local", HOME / ".npm-global/bin", HOME / ".bun/bin",
             Path("/usr/local/bin"), Path("/usr/bin")]
    search = os.pathsep.join([os.environ.get("PATH", "")] + [str(p) for p in extra])
    return shutil.which("claude", path=search)


def kde_default_terminal():
    try:
        text = (HOME / ".config/kdeglobals").read_text()
    except OSError:
        return None
    match = re.search(r"^\s*TerminalApplication\s*=\s*(\S+)", text, re.M)
    return Path(match.group(1)).name if match else None


def process_name(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def detect_terminal():
    # prefer whatever terminal the user is already running claude in
    for info in live_sessions().values():
        for pid in ancestors(info["pid"]):
            comm = process_name(pid)
            name = COMM_ALIASES.get(comm, comm)
            if name in TERMINALS and shutil.which(name):
                return name
    preferred = kde_default_terminal()
    if preferred in TERMINALS and shutil.which(preferred):
        return preferred
    for name in ("konsole", "alacritty", "kitty", "ghostty", "wezterm", "foot", "gnome-terminal", "xterm"):
        if shutil.which(name):
            return name
    return None


def terminal_argv(args, cwd, command):
    if args.terminal == "custom":
        if not args.command.strip():
            fail_launch("No custom terminal command configured")
        argv = []
        for token in shlex.split(args.command):
            if token == "{cmd}":
                argv.extend(command)
            else:
                argv.append(token.replace("{cwd}", cwd))
        return argv if "{cmd}" in args.command else argv + command
    name = detect_terminal() if args.terminal == "auto" else args.terminal
    if name not in TERMINALS:
        fail_launch("No supported terminal found", "Pick one in the widget settings.")
    return TERMINALS[name](cwd, command)


def launch(args, cwd, claude_args):
    claude = find_claude()
    if not claude:
        fail_launch("Could not find the claude command", "Is Claude Code installed and on PATH?")
    shell = os.environ.get("SHELL") or "/bin/sh"
    script = shlex.join([claude, *claude_args])
    if args.keep_shell:
        script += f"; exec {shlex.quote(shell)}"
    argv = terminal_argv(args, cwd, [shell, "-l", "-c", script])
    if args.dry_run:
        emit({"ok": True, "action": "launch", "argv": argv, "cwd": cwd})
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        fail_launch(f"Could not start {argv[0]}", str(e))
    emit({"ok": True, "action": "launch", "argv": argv, "cwd": cwd})


FOCUS_SCRIPT = """
const pids = %s;
let best = null, bestRank = pids.length;
for (const w of workspace.windowList()) {
    const rank = pids.indexOf(w.pid);
    if (rank >= 0 && rank < bestRank && w.normalWindow) { best = w; bestRank = rank; }
}
if (best) {
    if (best.minimized) best.minimized = false;
    workspace.activeWindow = best;
}
"""


def focus_window(pids, dry_run):
    """Activate the terminal window that owns one of `pids`, via a throwaway KWin script."""
    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    if not qdbus:
        return False
    if dry_run:
        return True
    script = CACHE_DIR / "focus.js"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    script.write_text(FOCUS_SCRIPT % json.dumps(pids))
    name = "claude-sessions-plasmoid-focus"

    def call(*argv):
        return subprocess.run([qdbus, "org.kde.KWin", *argv], capture_output=True, text=True, timeout=5)

    call("/Scripting", "org.kde.kwin.Scripting.unloadScript", name)
    loaded = call("/Scripting", "org.kde.kwin.Scripting.loadScript", str(script), name)
    script_id = loaded.stdout.strip()
    if loaded.returncode != 0 or not script_id.lstrip("-").isdigit() or int(script_id) < 0:
        return False
    ran = call(f"/Scripting/Script{script_id}", "org.kde.kwin.Script.run")
    call("/Scripting", "org.kde.kwin.Scripting.unloadScript", name)
    return ran.returncode == 0


def cmd_open(args):
    running = live_sessions().get(args.session_id)
    if running and focus_window(ancestors(running["pid"]), args.dry_run):
        emit({"ok": True, "action": "focus", "pid": running["pid"]})

    path = next(PROJECTS_DIR.glob(f"*/{glob_escape(args.session_id)}.jsonl"), None)
    if not path:
        fail_launch("Session not found", args.session_id)
    cwd = parse_transcript(path)["cwd"] or (running or {}).get("cwd")
    if not cwd or not os.path.isdir(cwd):
        fail_launch("Project folder no longer exists", cwd or "")
    launch(args, cwd, ["--resume", args.session_id])


def glob_escape(text):
    return re.sub(r"([*?\[])", r"[\1]", text)


def cmd_new(args):
    cwd = os.path.expanduser(args.cwd)
    if not os.path.isdir(cwd):
        fail_launch("Folder does not exist", cwd)
    launch(args, cwd, [])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("usage")
    p.add_argument("--max-age", type=float, default=300)
    p.set_defaults(func=cmd_usage)

    def terminal_opts(p):
        p.add_argument("--terminal", default="auto")
        p.add_argument("--command", default="")
        p.add_argument("--keep-shell", action="store_true")
        p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("open")
    p.add_argument("session_id")
    terminal_opts(p)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("new")
    p.add_argument("cwd")
    terminal_opts(p)
    p.set_defaults(func=cmd_new)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
