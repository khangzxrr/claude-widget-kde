"""Tests for package/contents/code/backend.py.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Standard library only. Every test runs against temporary Claude/cache directories,
and terminals, KWin, desktop notifications and the network are mocked out.
"""

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = ROOT / "package" / "contents" / "code" / "backend.py"

# keep __pycache__ out of package/, which install.sh copies wholesale
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("claude_sessions_backend", BACKEND_PATH)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


# ---------------------------------------------------------------- fixtures

def jsonl(*records):
    # Claude Code writes compact JSON, and the backend's line prefilter relies on that
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)


def user(content, cwd="/work/app", branch="main", **extra):
    return {"type": "user", "cwd": cwd, "gitBranch": branch, "isSidechain": False,
            "message": {"role": "user", "content": content}, **extra}


def tool_result(cwd="/work/app"):
    return user([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}], cwd=cwd)


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-sessions-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.claude_dir = self.tmp / "claude"
        self.project = self.home / "code" / "app"
        self.project.mkdir(parents=True)
        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "sessions").mkdir()

        self.patch_attr(backend, "HOME", self.home)
        self.patch_attr(backend, "CLAUDE_DIR", self.claude_dir)
        self.patch_attr(backend, "PROJECTS_DIR", self.claude_dir / "projects")
        self.patch_attr(backend, "LIVE_DIR", self.claude_dir / "sessions")
        self.patch_attr(backend, "CREDENTIALS", self.claude_dir / ".credentials.json")
        self.patch_attr(backend, "CACHE_DIR", self.tmp / "cache")
        self.notify = self.patch_attr(backend, "notify", mock.Mock())

    def patch_attr(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def write_session(self, session_id, *records, project_dir="-home-code-app", mtime=None):
        path = self.claude_dir / "projects" / project_dir / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(jsonl(*records))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def register_live(self, session_id, pid=None, proc_start=None, status="idle", cwd=None):
        pid = pid or os.getpid()
        if proc_start is None:
            proc_start = backend.proc_stat_fields(pid)[19]
        record = {"pid": pid, "sessionId": session_id, "cwd": cwd or str(self.project), "procStart": proc_start,
                  "status": status, "kind": "interactive", "name": "test"}
        (self.claude_dir / "sessions" / f"{session_id}.json").write_text(json.dumps(record))

    def run_command(self, func, **kwargs):
        """Run a cmd_* entry point; returns (exit code, parsed JSON output)."""
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as exited:
            func(argparse.Namespace(**kwargs))
        return exited.exception.code, json.loads(out.getvalue())

    def list_sessions(self, limit=200):
        code, data = self.run_command(backend.cmd_list, limit=limit)
        self.assertEqual(code, 0)
        return data


def dead_pid():
    return int(Path("/proc/sys/kernel/pid_max").read_text()) + 1


# ---------------------------------------------------------------- transcript parsing

class PromptTextTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(backend.prompt_text(user("  fix the login bug \n")), "fix the login bug")

    def test_text_blocks_are_joined(self):
        content = [{"type": "text", "text": "first"}, {"type": "image", "source": {}}, {"type": "text", "text": "second"}]
        self.assertEqual(backend.prompt_text(user(content)), "first\nsecond")

    def test_tool_results_are_not_prompts(self):
        self.assertIsNone(backend.prompt_text(tool_result()))

    def test_meta_sidechain_and_compact_summaries_are_ignored(self):
        for flag in ("isMeta", "isSidechain", "isCompactSummary"):
            with self.subTest(flag=flag):
                self.assertIsNone(backend.prompt_text(user("hello", **{flag: True})))

    def test_slash_command_is_shown_with_arguments(self):
        text = ("<command-message>loop</command-message>\n<command-name>/loop</command-name>\n"
                "<command-args>5m /babysit-prs</command-args>")
        self.assertEqual(backend.prompt_text(user(text)), "/loop 5m /babysit-prs")

    def test_slash_command_without_arguments(self):
        self.assertEqual(backend.prompt_text(user("<command-name>/clear</command-name>")), "/clear")

    def test_harness_injected_text_is_ignored(self):
        for text in ("", "   ", "<local-command-stdout>done</local-command-stdout>",
                     "<system-reminder>context</system-reminder>", "<bash-stdout>ls</bash-stdout>",
                     "Caveat: the messages below were generated by the user",
                     "[Request interrupted by user]"):
            with self.subTest(text=text):
                self.assertIsNone(backend.prompt_text(user(text)))


class ParseTranscriptTests(BackendTestCase):
    def test_collects_session_metadata(self):
        path = self.write_session(
            "s1",
            {"type": "permission-mode", "permissionMode": "auto"},
            user("add dark mode", cwd="/work/app", branch="HEAD"),
            assistant("sure"),
            tool_result(),
            {"type": "ai-title", "aiTitle": "Dark mode draft"},
            user("now make it the default", branch="feature/dark"),
            {"type": "ai-title", "aiTitle": "Dark mode by default"},
            {"type": "custom-title", "customTitle": "Theme work"},
            {"type": "last-prompt", "lastPrompt": "ship it"},
        )
        info = backend.parse_transcript(path)
        self.assertEqual(info, {
            "cwd": "/work/app",
            "branch": "feature/dark",
            "title": "Theme work",
            "aiTitle": "Dark mode by default",
            "firstPrompt": "add dark mode",
            "lastPrompt": "ship it",
            "prompts": 2,
        })

    def test_detached_head_is_not_reported_as_a_branch(self):
        path = self.write_session("s1", user("hi", branch="HEAD"))
        self.assertIsNone(backend.parse_transcript(path)["branch"])

    def test_subagent_messages_do_not_count(self):
        path = self.write_session(
            "s1",
            user("subagent task", isSidechain=True, cwd="/elsewhere", branch="agent-branch"),
            user("real prompt", branch="HEAD"),
            user("another subagent task", isSidechain=True),
        )
        info = backend.parse_transcript(path)
        self.assertEqual((info["prompts"], info["firstPrompt"], info["lastPrompt"], info["cwd"], info["branch"]),
                         (1, "real prompt", "real prompt", "/work/app", None))

    def test_malformed_and_half_written_lines_are_skipped(self):
        path = self.write_session("s1", user("before"))
        with open(path, "a") as f:
            f.write("not json at all\n")
            f.write('{"type":"user","message":{"content":"trunc')  # claude still writing this line
        self.assertEqual(backend.parse_transcript(path)["prompts"], 1)

    def test_prompts_are_collapsed_to_one_line_and_truncated(self):
        path = self.write_session("s1", user("line one\n\n   line two"), user("x" * 500))
        info = backend.parse_transcript(path)
        self.assertEqual(info["firstPrompt"], "line one line two")
        self.assertEqual(len(info["lastPrompt"]), 300)
        self.assertTrue(info["lastPrompt"].endswith("…"))

    def test_legacy_summary_is_a_title_fallback(self):
        path = self.write_session("s1", {"type": "summary", "summary": "Old style summary"}, user("hi"))
        self.assertEqual(backend.parse_transcript(path)["aiTitle"], "Old style summary")

    def test_ai_title_beats_legacy_summary(self):
        path = self.write_session("s1", {"type": "ai-title", "aiTitle": "New"}, {"type": "summary", "summary": "Old"})
        self.assertEqual(backend.parse_transcript(path)["aiTitle"], "New")


class HelperTests(unittest.TestCase):
    def test_display_path(self):
        with mock.patch.object(backend, "HOME", Path("/home/me")):
            self.assertEqual(backend.display_path("/home/me"), "~")
            self.assertEqual(backend.display_path("/home/me/code/app"), "~/code/app")
            self.assertEqual(backend.display_path("/home/meow/app"), "/home/meow/app")
            self.assertEqual(backend.display_path(None), "")

    def test_one_line(self):
        self.assertEqual(backend.one_line(" a \n b\tc ", 10), "a b c")
        self.assertEqual(backend.one_line("abcdefghij", 5), "abcd…")

    def test_glob_escape_matches_literally(self):
        self.assertEqual(backend.glob_escape("a*b?[c]"), "a[*]b[?][[]c]")


# ---------------------------------------------------------------- list

class ListTests(BackendTestCase):
    def test_title_priority(self):
        cwd = str(self.project)
        self.write_session("custom", user("prompt", cwd=cwd), {"type": "ai-title", "aiTitle": "AI"},
                           {"type": "custom-title", "customTitle": "Custom"})
        self.write_session("ai", user("prompt", cwd=cwd), {"type": "ai-title", "aiTitle": "AI"})
        self.write_session("prompt", user("just the prompt", cwd=cwd))
        self.write_session("untitled", {"type": "mode", "mode": "normal"})
        self.register_live("untitled")

        titles = {s["id"]: s["title"] for s in self.list_sessions()["sessions"]}
        self.assertEqual(titles, {"custom": "Custom", "ai": "AI", "prompt": "just the prompt",
                                  "untitled": "Untitled session"})

    def test_sessions_without_prompts_are_hidden_unless_running(self):
        self.write_session("empty", {"type": "mode", "mode": "normal"})
        self.write_session("only-tools", tool_result())
        self.write_session("real", user("hello"))
        self.assertEqual([s["id"] for s in self.list_sessions()["sessions"]], ["real"])

    def test_running_sessions_first_then_newest(self):
        now = time.time()
        self.write_session("old-running", user("a"), mtime=now - 3000)
        self.write_session("newest", user("b"), mtime=now - 10)
        self.write_session("older", user("c"), mtime=now - 2000)
        self.register_live("old-running", status="busy")

        sessions = self.list_sessions()["sessions"]
        self.assertEqual([s["id"] for s in sessions], ["old-running", "newest", "older"])
        self.assertEqual(sessions[0]["live"]["status"], "busy")
        self.assertIsNone(sessions[1]["live"])
        self.assertAlmostEqual(sessions[1]["modified"], (now - 10) * 1000, delta=1000)

    def test_limit(self):
        for i in range(5):
            self.write_session(f"s{i}", user("hi"), mtime=time.time() - i)
        self.assertEqual([s["id"] for s in self.list_sessions(limit=2)["sessions"]], ["s0", "s1"])

    def test_session_fields(self):
        self.write_session("s1", user("first", cwd=str(self.project), branch="main"), user("second"),
                           {"type": "last-prompt", "lastPrompt": "second"})
        session = self.list_sessions()["sessions"][0]
        self.assertEqual(session["project"], str(self.project))
        self.assertEqual(session["projectDisplay"], "~/code/app")
        self.assertTrue(session["projectExists"])
        self.assertEqual((session["branch"], session["prompts"], session["firstPrompt"], session["lastPrompt"]),
                         ("main", 2, "first", "second"))

    def test_projects_menu_is_deduplicated_newest_first_and_skips_deleted_folders(self):
        other = self.home / "other"
        other.mkdir()
        now = time.time()
        self.write_session("a", user("x", cwd=str(self.project)), mtime=now - 100)
        self.write_session("b", user("x", cwd=str(other)), mtime=now - 50)
        self.write_session("c", user("x", cwd=str(self.project)), mtime=now - 10)
        self.write_session("d", user("x", cwd=str(self.home / "deleted")), mtime=now)

        data = self.list_sessions()
        self.assertEqual(data["projects"], [{"path": str(self.project), "display": "~/code/app"},
                                            {"path": str(other), "display": "~/other"}])
        self.assertFalse(next(s for s in data["sessions"] if s["id"] == "d")["projectExists"])

    def test_nested_subagent_transcripts_are_not_sessions(self):
        self.write_session("main", user("hi"))
        nested = self.claude_dir / "projects" / "-home-code-app" / "main" / "subagents" / "agent-1.jsonl"
        nested.parent.mkdir(parents=True)
        nested.write_text(jsonl(user("subagent")))
        self.assertEqual([s["id"] for s in self.list_sessions()["sessions"]], ["main"])

    def test_index_cache_only_rereads_changed_transcripts(self):
        self.write_session("a", user("one"))
        path_b = self.write_session("b", user("two"))
        with mock.patch.object(backend, "parse_transcript", wraps=backend.parse_transcript) as parse:
            self.list_sessions()
            self.assertEqual(parse.call_count, 2)

            parse.reset_mock()
            self.list_sessions()
            parse.assert_not_called()

            with open(path_b, "a") as f:
                f.write(jsonl(user("three")))
            parse.reset_mock()
            sessions = {s["id"]: s for s in self.list_sessions()["sessions"]}
            parse.assert_called_once_with(path_b)
            self.assertEqual(sessions["b"]["prompts"], 2)

    def test_same_size_rewrite_is_detected_by_mtime(self):
        path = self.write_session("a", {"type": "custom-title", "customTitle": "abc"}, user("hi"), mtime=time.time() - 100)
        self.assertEqual(self.list_sessions()["sessions"][0]["title"], "abc")
        size = path.stat().st_size
        self.write_session("a", {"type": "custom-title", "customTitle": "xyz"}, user("hi"), mtime=time.time())
        self.assertEqual(path.stat().st_size, size)
        self.assertEqual(self.list_sessions()["sessions"][0]["title"], "xyz")

    def test_deleted_transcripts_drop_out_of_the_cache(self):
        self.write_session("a", user("one"))
        path_b = self.write_session("b", user("two"))
        self.list_sessions()
        path_b.unlink()
        self.assertEqual([s["id"] for s in self.list_sessions()["sessions"]], ["a"])
        index = json.loads((self.tmp / "cache" / "index.json").read_text())
        self.assertEqual(list(index["files"]), [str(self.claude_dir / "projects" / "-home-code-app" / "a.jsonl")])

    def test_corrupt_or_outdated_cache_is_rebuilt(self):
        self.write_session("a", user("one"))
        cache = self.tmp / "cache" / "index.json"
        cache.parent.mkdir(parents=True)
        for content in ("{not json", json.dumps({"version": -1, "files": {"bogus": {}}})):
            with self.subTest(content=content):
                cache.write_text(content)
                self.assertEqual([s["id"] for s in self.list_sessions()["sessions"]], ["a"])
                self.assertEqual(json.loads(cache.read_text())["version"], backend.INDEX_VERSION)

    def test_no_claude_directory_yet(self):
        shutil.rmtree(self.claude_dir)
        data = self.list_sessions()
        self.assertEqual((data["sessions"], data["projects"]), ([], []))


# ---------------------------------------------------------------- live sessions

class LiveSessionTests(BackendTestCase):
    def test_running_process_is_detected(self):
        self.register_live("s1", status="busy")
        live = backend.live_sessions()
        self.assertEqual(live["s1"]["pid"], os.getpid())
        self.assertEqual(live["s1"]["status"], "busy")

    def test_reused_pid_is_rejected(self):
        self.register_live("s1", proc_start="1")
        self.assertEqual(backend.live_sessions(), {})

    def test_exited_process_is_rejected(self):
        self.register_live("s1", pid=dead_pid(), proc_start="1")
        self.assertEqual(backend.live_sessions(), {})

    def test_missing_proc_start_trusts_the_pid(self):
        (self.claude_dir / "sessions" / "x.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "s1"}))
        self.assertEqual(backend.live_sessions()["s1"]["status"], "running")

    def test_unreadable_records_are_ignored(self):
        sessions = self.claude_dir / "sessions"
        (sessions / "garbage.json").write_text("{nope")
        (sessions / "list.json").write_text("[]")
        (sessions / "no-session.json").write_text(json.dumps({"pid": os.getpid()}))
        (sessions / "no-pid.json").write_text(json.dumps({"sessionId": "s1"}))
        self.assertEqual(backend.live_sessions(), {})

    def test_ancestors_walks_up_the_process_tree(self):
        chain = backend.ancestors(os.getpid())
        self.assertEqual(chain[:2], [os.getpid(), os.getppid()])
        self.assertNotIn(1, chain)

    def test_ancestors_of_exited_process_is_empty(self):
        self.assertEqual(backend.ancestors(dead_pid()), [])


# ---------------------------------------------------------------- usage

USAGE_RESPONSE = {
    "five_hour": {"utilization": 13.0, "resets_at": "2026-09-13T16:10:00.115668+00:00"},
    "seven_day": {"utilization": 23.0, "resets_at": "2026-09-17T05:00:00.115692+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 13, "severity": "normal",
         "resets_at": "2026-09-13T16:10:00.115668+00:00", "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 23.4, "severity": "normal",
         "resets_at": "2026-09-17T05:00:00Z", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 91, "severity": "critical",
         "resets_at": None, "scope": {"model": {"display_name": "Fable"}}},
    ],
}


class NormalizeUsageTests(unittest.TestCase):
    def test_limits_shape(self):
        self.assertEqual(backend.normalize_usage(USAGE_RESPONSE), [
            {"label": "Session", "group": "session", "percent": 13, "severity": "normal", "resetsAt": 1789315800115},
            {"label": "Weekly", "group": "weekly", "percent": 23, "severity": "normal", "resetsAt": 1789621200000},
            {"label": "Weekly · Fable", "group": "weekly", "percent": 91, "severity": "critical", "resetsAt": None},
        ])

    def test_unknown_limit_kinds_get_a_readable_label(self):
        limits = backend.normalize_usage({"limits": [{"kind": "monthly_extra", "percent": None}]})
        self.assertEqual((limits[0]["label"], limits[0]["group"], limits[0]["percent"]), ("Monthly extra", "monthly_extra", 0))

    def test_falls_back_to_older_response_shape(self):
        data = {"five_hour": {"utilization": 13.6, "resets_at": None}, "seven_day": {"utilization": 2.0},
                "seven_day_opus": None, "seven_day_sonnet": {"utilization": None}}
        self.assertEqual([(l["label"], l["group"], l["percent"]) for l in backend.normalize_usage(data)],
                         [("Session", "session", 14), ("Weekly", "weekly", 2)])

    def test_empty_response(self):
        self.assertEqual(backend.normalize_usage({}), [])

    def test_parse_time_ms(self):
        self.assertEqual(backend.parse_time_ms("1970-01-01T00:00:01.5+00:00"), 1500)
        self.assertEqual(backend.parse_time_ms("1970-01-01T01:00:00+01:00"), 0)
        self.assertEqual(backend.parse_time_ms("1970-01-01T00:00:02Z"), 2000)
        self.assertIsNone(backend.parse_time_ms(None))
        self.assertIsNone(backend.parse_time_ms("next tuesday"))


class UsageCommandTests(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.urlopen = self.patch_attr(backend.urllib.request, "urlopen", mock.MagicMock())
        self.respond_with(USAGE_RESPONSE)
        self.write_credentials()

    def write_credentials(self, token="tok-123", expires_in=3600):
        oauth = {"accessToken": token, "expiresAt": int((time.time() + expires_in) * 1000), "subscriptionType": "max"}
        backend.CREDENTIALS.write_text(json.dumps({"claudeAiOauth": oauth}))

    def respond_with(self, payload):
        self.urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())

    def usage(self, max_age=300):
        code, data = self.run_command(backend.cmd_usage, max_age=max_age)
        self.assertEqual(code, 0)
        return data

    def test_fetches_and_caches_usage(self):
        data = self.usage(max_age=0)
        self.assertIsNone(data["error"])
        self.assertEqual(data["plan"], "max")
        self.assertEqual([l["percent"] for l in data["limits"]], [13, 23, 91])

        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, backend.USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer tok-123")
        self.assertEqual(request.get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(json.loads((self.tmp / "cache" / "usage.json").read_text())["limits"], data["limits"])

    def test_fresh_cache_avoids_the_network(self):
        first = self.usage(max_age=0)
        self.urlopen.reset_mock()
        self.assertEqual(self.usage(max_age=300), first)
        self.urlopen.assert_not_called()

    def test_stale_cache_is_refetched(self):
        self.usage(max_age=0)
        cache_path = self.tmp / "cache" / "usage.json"
        cache = json.loads(cache_path.read_text())
        cache["fetchedAt"] = cache["attemptedAt"] = time.time() - 600
        cache_path.write_text(json.dumps(cache))
        self.urlopen.reset_mock()
        self.respond_with(USAGE_RESPONSE)
        self.usage(max_age=300)
        self.urlopen.assert_called_once()

    def test_not_logged_in(self):
        backend.CREDENTIALS.unlink()
        data = self.usage(max_age=0)
        self.assertEqual((data["error"], data["limits"]), ("Not logged in to Claude Code", []))
        self.urlopen.assert_not_called()

    def test_expired_login_is_not_sent(self):
        self.write_credentials(expires_in=-60)
        self.assertIn("Login expired", self.usage(max_age=0)["error"])
        self.urlopen.assert_not_called()

    def test_rate_limit_keeps_last_known_usage_and_backs_off(self):
        known = self.usage(max_age=0)["limits"]
        self.urlopen.side_effect = urllib.error.HTTPError(backend.USAGE_URL, 429, "Too Many Requests", None, None)

        data = self.usage(max_age=0)
        self.assertEqual(data["error"], "Rate limited, showing last known usage")
        self.assertEqual(data["limits"], known)

        self.urlopen.reset_mock()
        self.assertEqual(self.usage(max_age=60)["error"], "Rate limited, showing last known usage")
        self.urlopen.assert_not_called()

    def test_other_http_errors(self):
        self.urlopen.side_effect = urllib.error.HTTPError(backend.USAGE_URL, 500, "Server Error", None, None)
        self.assertEqual(self.usage(max_age=0)["error"], "Usage API returned HTTP 500")

    def test_offline(self):
        self.urlopen.side_effect = urllib.error.URLError("no route to host")
        self.assertEqual(self.usage(max_age=0)["error"], "Offline, showing last known usage")

    def test_invalid_json_response(self):
        self.urlopen.return_value.__enter__.return_value = io.BytesIO(b"<html>maintenance</html>")
        self.assertEqual(self.usage(max_age=0)["error"], "Offline, showing last known usage")

    def test_recovers_after_an_error(self):
        self.urlopen.side_effect = urllib.error.URLError("down")
        self.usage(max_age=0)
        self.urlopen.side_effect = None
        self.respond_with(USAGE_RESPONSE)
        self.assertIsNone(self.usage(max_age=0)["error"])


# ---------------------------------------------------------------- terminals

COMMAND = ["/bin/fish", "-l", "-c", "claude --resume s1"]


class TerminalTests(BackendTestCase):
    def argv(self, terminal, command="", cwd="/work/my app"):
        return backend.terminal_argv(argparse.Namespace(terminal=terminal, command=command), cwd, COMMAND)

    def test_known_terminals_start_in_the_project_folder(self):
        expected = {
            "alacritty": ["alacritty", "--working-directory", "/work/my app", "-e", *COMMAND],
            "konsole": ["konsole", "--workdir", "/work/my app", "-e", *COMMAND],
            "kitty": ["kitty", "--directory", "/work/my app", *COMMAND],
            "ghostty": ["ghostty", "--working-directory=/work/my app", "-e", *COMMAND],
            "wezterm": ["wezterm", "start", "--cwd", "/work/my app", "--", *COMMAND],
            "foot": ["foot", "-D", "/work/my app", *COMMAND],
            "gnome-terminal": ["gnome-terminal", "--working-directory=/work/my app", "--", *COMMAND],
            "xterm": ["xterm", "-e", *COMMAND],
        }
        self.assertEqual(set(expected), set(backend.TERMINALS))
        for name, argv in expected.items():
            with self.subTest(terminal=name):
                self.assertEqual(self.argv(name), argv)

    def test_custom_command_substitutes_placeholders(self):
        self.assertEqual(self.argv("custom", "foot --title 'Claude {cwd}' -D {cwd} {cmd}"),
                         ["foot", "--title", "Claude /work/my app", "-D", "/work/my app", *COMMAND])

    def test_custom_command_without_cmd_placeholder_appends_it(self):
        self.assertEqual(self.argv("custom", "st -d {cwd} -e"), ["st", "-d", "/work/my app", "-e", *COMMAND])

    def test_empty_custom_command_fails(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
            self.argv("custom", "  ")
        self.assertEqual(exited.exception.code, 1)
        self.notify.assert_called_once()

    def test_unknown_terminal_fails(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
            self.argv("hyper")
        self.assertEqual(exited.exception.code, 1)


class DetectTerminalTests(BackendTestCase):
    def installed(self, *names):
        return self.patch_attr(backend.shutil, "which", lambda name, **_: f"/usr/bin/{name}" if name in names else None)

    def test_prefers_the_terminal_claude_is_already_running_in(self):
        self.register_live("s1")
        self.installed("konsole", "wezterm")
        self.patch_attr(backend, "ancestors", lambda pid: [100, 101, 102])
        self.patch_attr(backend, "process_name", {100: "claude", 101: "zsh", 102: "wezterm-gui"}.get)
        self.assertEqual(backend.detect_terminal(), "wezterm")

    def test_skips_running_terminal_that_is_not_launchable(self):
        self.register_live("s1")
        self.installed("konsole")
        self.patch_attr(backend, "ancestors", lambda pid: [100, 101])
        self.patch_attr(backend, "process_name", {100: "claude", 101: "alacritty"}.get)
        self.assertEqual(backend.detect_terminal(), "konsole")

    def test_falls_back_to_kde_default_terminal(self):
        self.installed("konsole", "kitty")
        (self.home / ".config").mkdir()
        (self.home / ".config" / "kdeglobals").write_text("[General]\nTerminalApplication=/usr/bin/kitty\nTerminalService=kitty.desktop\n")
        self.assertEqual(backend.detect_terminal(), "kitty")

    def test_ignores_uninstalled_kde_default(self):
        self.installed("foot")
        (self.home / ".config").mkdir()
        (self.home / ".config" / "kdeglobals").write_text("[General]\nTerminalApplication=kitty\n")
        self.assertEqual(backend.detect_terminal(), "foot")

    def test_konsole_is_the_default_choice_on_kde(self):
        self.installed(*backend.TERMINALS)
        self.assertEqual(backend.detect_terminal(), "konsole")

    def test_nothing_installed(self):
        self.installed()
        self.assertIsNone(backend.detect_terminal())


# ---------------------------------------------------------------- open / new

class LaunchTests(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.patch_attr(backend, "find_claude", lambda: "/opt/claude")
        env = mock.patch.dict(os.environ, {"SHELL": "/bin/fish", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
        env.start()
        self.addCleanup(env.stop)
        self.focus = self.patch_attr(backend, "focus_window", mock.Mock(return_value=True))
        self.write_session("s1", user("hello", cwd=str(self.project)))

    def open(self, session_id="s1", terminal="konsole", keep_shell=False, dry_run=True):
        return self.run_command(backend.cmd_open, session_id=session_id, terminal=terminal, command="",
                                keep_shell=keep_shell, dry_run=dry_run)

    def test_closed_session_is_resumed_in_its_project_folder(self):
        code, data = self.open()
        self.assertEqual(code, 0)
        self.assertEqual(data["action"], "launch")
        self.assertEqual(data["cwd"], str(self.project))
        self.assertEqual(data["argv"], ["konsole", "--workdir", str(self.project), "-e",
                                        "/bin/fish", "-l", "-c", "/opt/claude --resume s1"])
        self.focus.assert_not_called()

    def test_keep_shell_drops_into_the_login_shell_afterwards(self):
        _, data = self.open(keep_shell=True)
        self.assertEqual(data["argv"][-1], "/opt/claude --resume s1; exec /bin/fish")

    def test_paths_with_spaces_are_quoted_for_the_shell(self):
        self.patch_attr(backend, "find_claude", lambda: "/opt/my tools/claude")
        _, data = self.open()
        self.assertEqual(data["argv"][-1], "'/opt/my tools/claude' --resume s1")

    def test_running_session_focuses_its_window_instead_of_resuming_twice(self):
        self.register_live("s1", status="idle")
        code, data = self.open()
        self.assertEqual((code, data["action"], data["pid"]), (0, "focus", os.getpid()))
        pids, dry_run = self.focus.call_args.args
        self.assertEqual(pids[:2], [os.getpid(), os.getppid()])
        self.assertTrue(dry_run)

    def test_running_session_is_resumed_when_its_window_cannot_be_found(self):
        self.register_live("s1")
        self.focus.return_value = False
        self.assertEqual(self.open()[1]["action"], "launch")

    def test_deleted_project_folder_fails_with_a_notification(self):
        self.write_session("gone", user("hi", cwd=str(self.home / "deleted")))
        code, data = self.open("gone")
        self.assertEqual((code, data["ok"], data["error"]), (1, False, "Project folder no longer exists"))
        self.notify.assert_called_once_with("Project folder no longer exists", str(self.home / "deleted"))

    def test_unknown_session_fails(self):
        code, data = self.open("does-not-exist")
        self.assertEqual((code, data["error"]), (1, "Session not found"))

    def test_session_id_is_not_treated_as_a_glob(self):
        self.assertEqual(self.open("s*")[1]["error"], "Session not found")
        self.assertEqual(self.open("s?")[1]["error"], "Session not found")

    def test_missing_claude_binary_fails(self):
        self.patch_attr(backend, "find_claude", lambda: None)
        code, data = self.open()
        self.assertEqual((code, data["error"]), (1, "Could not find the claude command"))

    def test_new_session_in_folder(self):
        code, data = self.run_command(backend.cmd_new, cwd=str(self.project), terminal="foot", command="",
                                      keep_shell=False, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(data["argv"], ["foot", "-D", str(self.project), "/bin/fish", "-l", "-c", "/opt/claude"])

    def test_new_session_in_missing_folder_fails(self):
        code, data = self.run_command(backend.cmd_new, cwd=str(self.home / "nope"), terminal="foot", command="",
                                      keep_shell=False, dry_run=True)
        self.assertEqual((code, data["error"]), (1, "Folder does not exist"))

    def test_terminal_is_spawned_detached_without_leaking_claude_env(self):
        with mock.patch.object(backend.subprocess, "Popen") as popen:
            code, data = self.open(dry_run=False)
        self.assertEqual((code, data["ok"]), (0, True))
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(argv[0], "konsole")
        self.assertEqual(kwargs["cwd"], str(self.project))
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertNotIn("CLAUDECODE", kwargs["env"])
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", kwargs["env"])
        self.assertEqual(kwargs["env"]["SHELL"], "/bin/fish")

    def test_terminal_that_fails_to_start_is_reported(self):
        with mock.patch.object(backend.subprocess, "Popen", side_effect=FileNotFoundError("konsole")):
            code, data = self.open(dry_run=False)
        self.assertEqual((code, data["error"]), (1, "Could not start konsole"))
        self.notify.assert_called_once()


class FindClaudeTests(BackendTestCase):
    def test_finds_claude_outside_the_desktop_path(self):
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\n")
        claude.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            self.assertEqual(backend.find_claude(), str(claude))


class FocusWindowTests(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.patch_attr(backend.shutil, "which", lambda name, **_: "/usr/bin/qdbus6" if name == "qdbus6" else None)
        self.run = self.patch_attr(backend.subprocess, "run", mock.Mock(side_effect=self.fake_qdbus))
        self.script_id = "7"

    def fake_qdbus(self, argv, **kwargs):
        stdout = self.script_id + "\n" if "org.kde.kwin.Scripting.loadScript" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def test_loads_runs_and_unloads_a_kwin_script(self):
        self.assertTrue(backend.focus_window([300, 200, 100], dry_run=False))
        name = "claude-sessions-plasmoid-focus"
        script = self.tmp / "cache" / "focus.js"
        self.assertEqual([c.args[0][1:] for c in self.run.call_args_list], [
            ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", name],
            ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", str(script), name],
            ["org.kde.KWin", "/Scripting/Script7", "org.kde.kwin.Script.run"],
            ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", name],
        ])
        self.assertIn("const pids = [300, 200, 100];", script.read_text())

    def test_script_load_failure(self):
        self.script_id = "-1"
        self.assertFalse(backend.focus_window([1], dry_run=False))
        self.assertEqual(len(self.run.call_args_list), 2)

    def test_without_qdbus(self):
        self.patch_attr(backend.shutil, "which", lambda name, **_: None)
        self.assertFalse(backend.focus_window([1], dry_run=False))
        self.run.assert_not_called()

    def test_dry_run_does_not_touch_kwin(self):
        self.assertTrue(backend.focus_window([1], dry_run=True))
        self.run.assert_not_called()


# ---------------------------------------------------------------- command line

class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-sessions-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "XDG_CACHE_HOME": str(self.tmp / "cache")}

    def run_backend(self, *args):
        return subprocess.run([sys.executable, str(BACKEND_PATH), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_list_uses_claude_config_dir_and_xdg_cache(self):
        transcript = self.tmp / "claude" / "projects" / "-tmp-app" / "abc.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(jsonl(user("hello from the cli", cwd=str(self.tmp))))

        result = self.run_backend("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        sessions = json.loads(result.stdout)["sessions"]
        self.assertEqual([(s["id"], s["title"]) for s in sessions], [("abc", "hello from the cli")])
        self.assertTrue((self.tmp / "cache" / "claude-sessions-plasmoid" / "index.json").exists())

    def test_usage_without_login_is_valid_json(self):
        result = self.run_backend("usage", "--max-age", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"], "Not logged in to Claude Code")

    def test_unknown_command_is_rejected(self):
        self.assertEqual(self.run_backend("explode").returncode, 2)


if __name__ == "__main__":
    unittest.main()
