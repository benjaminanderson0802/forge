# tests/core/test_agents.py
import json, os, sys, tempfile, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.agents import (AgentResult, ClaudeAgent, CodexAgent, FakeAgent, launch, load_limits,
                         parse_claude, parse_codex)

PY = sys.executable

class LaunchTests(unittest.TestCase):
    def test_stdin_reaches_process(self):
        code, out, _ = launch([PY, "-c", "import sys; print(sys.stdin.read().upper())"], Path("."), "hi", 30)
        self.assertEqual((code, out.strip()), (0, "HI"))

    def test_long_prompt_via_stdin(self):
        big = "x" * 50_000
        code, out, _ = launch([PY, "-c", "import sys; print(len(sys.stdin.read()))"], Path("."), big, 30)
        self.assertEqual(out.strip(), "50000")

    def test_timeout_kills_tree(self):
        d = Path(tempfile.mkdtemp()); flag = d / "child_alive"
        child = f"import time,pathlib; time.sleep(3); pathlib.Path(r'{flag}').write_text('x')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
        with self.assertRaises(TimeoutError):
            launch([PY, "-c", parent], Path("."), "", 1)
        time.sleep(4)
        self.assertFalse(flag.exists(), "child process survived the timeout")

class ClaudeParseTests(unittest.TestCase):
    def test_success_counts_all_tokens(self):
        out = json.dumps({"is_error": False, "result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}})
        r = parse_claude(out, None)
        self.assertEqual((r.ok, r.text, r.tokens, r.provider), (True, "ok", 20, "claude"))

    def test_not_logged_in(self):
        r = parse_claude(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}), None)
        self.assertFalse(r.ok); self.assertIn("Not logged in", r.error)

    def test_claude_garbage(self):
        r = parse_claude("Traceback: boom", None)
        self.assertFalse(r.ok); self.assertEqual(r.tokens, 0)

    def test_schema_ok(self):
        schema = {"type": "object", "required": ["verdict"]}
        r = parse_claude(json.dumps({"is_error": False, "result": 'Here: {"verdict": "pass"}', "usage": {}}), schema)
        self.assertTrue(r.ok); self.assertEqual(r.data, {"verdict": "pass"})

    def test_schema_mismatch(self):
        schema = {"type": "object", "required": ["verdict"]}
        r = parse_claude(json.dumps({"is_error": False, "result": "I think it passes", "usage": {}}), schema)
        self.assertFalse(r.ok); self.assertIn("required shape", r.error); self.assertIn("passes", r.text)

class CodexParseTests(unittest.TestCase):
    def events(self, *evs): return "\n".join(json.dumps(e) for e in evs)

    def test_success(self):
        out = self.events({"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                          {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80,
                                                               "output_tokens": 5, "reasoning_output_tokens": 7}})
        r = parse_codex(0, out, "ok", None)
        self.assertEqual((r.ok, r.text, r.tokens, r.provider), (True, "ok", 112, "codex"))

    def test_codex_no_turn_completed(self):
        r = parse_codex(1, self.events({"type": "thread.started"}), "", None)
        self.assertFalse(r.ok); self.assertEqual(r.tokens, 0)

    def test_codex_schema(self):
        out = self.events({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        r = parse_codex(0, out, '{"verdict": "fail", "reasons": ["x"]}', {"type": "object", "required": ["verdict"]})
        self.assertTrue(r.ok); self.assertEqual(r.data["verdict"], "fail")

class AdapterTests(unittest.TestCase):
    def test_missing_cli_is_failed_result(self):
        for a in (ClaudeAgent(cmd=["no-such-binary-xyz"]), CodexAgent(cmd=["no-such-binary-xyz"])):
            r = a.run("hi", Path("."))
            self.assertFalse(r.ok); self.assertIn("not found", r.error)

    def test_fake_agent(self):
        r = FakeAgent(lambda p, c: ('{"verdict": "pass"}', 7)).run("x", Path("."), {"type": "object", "required": ["verdict"]})
        self.assertTrue(r.ok); self.assertEqual((r.tokens, r.data), (7, {"verdict": "pass"}))

    def test_fake_agent_exception(self):
        def boom(p, c): raise RuntimeError("crash")
        r = FakeAgent(boom).run("x", Path("."))
        self.assertFalse(r.ok); self.assertIn("crash", r.error)

    def test_limits(self):
        lim = load_limits(ROOT)
        self.assertGreater(lim["claude_daily_token_cap"], 0); self.assertGreater(lim["codex_daily_token_cap"], 0)

if __name__ == "__main__":
    unittest.main()


class R16CodexSandboxTests(unittest.TestCase):
    def test_R16_windows_sandbox_override_is_platform_specific(self):
        """R16: Codex receives the elevated Windows sandbox override only on Windows."""
        from unittest.mock import patch

        for is_win in (True, False):
            with self.subTest(is_win=is_win):
                completed = json.dumps({"type": "turn.completed", "usage": {}}) + "\n"
                with patch("core.agents.IS_WIN", is_win), \
                        patch("core.agents._resolve", return_value=["codex"]), \
                        patch("core.agents.launch", return_value=(0, completed, "")) as launch_mock:
                    CodexAgent(sandbox="workspace-write").run("write tests", Path("."))
                launch_mock.assert_called_once()
                args = launch_mock.call_args.args[0]
                self.assertIn("--ignore-user-config", args)
                self.assertEqual(args[args.index("-s") + 1], "workspace-write")
                self.assertEqual(args[-1], "-")
                override = 'windows.sandbox="elevated"'
                if is_win:
                    self.assertIn(override, args)
                    index = args.index(override)
                    self.assertGreater(index, 0)
                    self.assertEqual(args[index - 1], "-c")
                    self.assertLess(index, len(args) - 1)
                else:
                    self.assertFalse(any("windows.sandbox" in arg for arg in args))
