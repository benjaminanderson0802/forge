"""T1B2a: readiness helpers the conductor uses (evaluate, evidence predicate, diagnosed fixes).

No real network, processes or agents: fake checks, fake which/probe and a fixed clock."""
import json
import math
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import readiness as rd  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def entry(ok=True, detail="fine", at=NOW):
    return {"ok": ok, "detail": detail, "checked_at": at.isoformat() if isinstance(at, datetime) else at}


def no_which(_name):
    return None


def yes_which(name):
    return f"/usr/bin/{name}"


def probe_never(_args):
    raise AssertionError("probe must not be called here")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)


class ConstantsTests(unittest.TestCase):
    def test_name_re(self):
        for good in ["git", "python_libs", "a", "x" * 40, "n8n"]:
            self.assertRegex(good, rd.NAME_RE)
        for bad in ["", "Git", "a-b", "x" * 41, "a b", "../x"]:
            self.assertIsNone(rd.NAME_RE.match(bad), bad)

    def test_ai_names_and_plain_checks(self):
        self.assertEqual(rd.AI_NAMES, ("claude", "codex"))
        self.assertEqual(set(rd.PLAIN_CHECKS), set(rd.CHECKS) - {"claude", "codex"})
        for name, fn in rd.PLAIN_CHECKS.items():
            self.assertIs(fn, rd.CHECKS[name])
        self.assertIn("git", rd.PLAIN_CHECKS)
        self.assertEqual(rd.PROBE_PROMPT, "Reply with exactly: ok")


class EvaluateTests(TempDirCase):
    def test_entry_for_every_check_with_shape(self):
        checks = {"a": lambda: (True, "fine"), "b": lambda: (False, "down"), "c": lambda: (1, 42)}
        out = rd.evaluate(checks, 5, NOW)
        self.assertEqual(set(out), {"a", "b", "c"})
        self.assertEqual(out["a"], {"ok": True, "detail": "fine", "checked_at": NOW.isoformat()})
        self.assertEqual(out["b"], {"ok": False, "detail": "down", "checked_at": NOW.isoformat()})
        self.assertIs(out["c"]["ok"], True)
        self.assertEqual(out["c"]["detail"], "42")

    def test_exception_isolated(self):
        def boom():
            raise RuntimeError("dns down")

        out = rd.evaluate({"x": boom, "y": lambda: (True, "ok")}, 5, NOW)
        self.assertEqual(out["x"]["ok"], False)
        self.assertEqual(out["x"]["detail"], "RuntimeError: dns down")
        self.assertTrue(out["y"]["ok"])

    def test_slow_check_times_out(self):
        def slow():
            time.sleep(3)
            return True, "late"

        t0 = time.monotonic()
        out = rd.evaluate({"slow": slow, "fast": lambda: (True, "quick")}, 0.5, NOW)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(out["slow"], {"ok": False, "detail": "check timed out after 0.5s",
                                       "checked_at": NOW.isoformat()})
        self.assertEqual(out["fast"]["detail"], "quick")

    def test_detail_truncated(self):
        out = rd.evaluate({"long": lambda: (False, "e" * 1000)}, 5, NOW)
        self.assertEqual(len(out["long"]["detail"]), 300)

    def test_no_file_io(self):
        with mock.patch("os.replace", side_effect=AssertionError("wrote")), \
                mock.patch("builtins.open", side_effect=AssertionError("opened")), \
                mock.patch.object(Path, "write_bytes", side_effect=AssertionError("wrote")), \
                mock.patch.object(Path, "write_text", side_effect=AssertionError("wrote")):
            out = rd.evaluate({"a": lambda: (True, "fine")}, 5, NOW)
        self.assertTrue(out["a"]["ok"])

    def test_empty(self):
        self.assertEqual(rd.evaluate({}, 1, NOW), {})


class RunChecksCompatTests(TempDirCase):
    def test_run_checks_uses_evaluate_and_writes_map(self):
        with mock.patch.object(rd, "evaluate", wraps=rd.evaluate) as ev:
            out = rd.run_checks(self.d, {"a": lambda: (True, "fine")}, 5, clock=lambda: NOW)
        ev.assert_called_once()
        saved = json.loads((self.d / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"a": entry()})
        self.assertEqual(out, saved)
        self.assertEqual([p.name for p in self.d.iterdir()], ["capabilities.json"])

    def test_run_checks_keeps_ttl_cache_and_timeout_text(self):
        calls = []

        def ai():
            calls.append(1)
            return True, "ok"

        def slow():
            time.sleep(3)
            return True, ""

        rd.run_checks(self.d, {"ai": ai}, 5, clock=lambda: NOW, ttl={"ai": 6})
        out = rd.run_checks(self.d, {"ai": ai, "slow": slow}, 1, clock=lambda: NOW + timedelta(hours=1),
                            ttl={"ai": 6})
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["ai"]["checked_at"], NOW.isoformat())
        self.assertEqual(out["slow"]["detail"], "check timed out after 1s")


class BrokenTests(unittest.TestCase):
    def assertReason(self, e, prefix, max_age=900):
        r = rd.broken(e, NOW, max_age)
        self.assertIsNotNone(r, e)
        self.assertTrue(r.startswith(prefix), f"{r!r} should start with {prefix!r}")

    def test_fresh_ok(self):
        self.assertIsNone(rd.broken(entry(), NOW, 900))
        self.assertIsNone(rd.broken(entry(at=NOW - timedelta(seconds=10)), NOW, 900))
        self.assertIsNone(rd.broken(entry(at=NOW + timedelta(seconds=30)), NOW, 900))

    def test_no_evidence(self):
        self.assertReason(None, "no evidence")

    def test_malformed(self):
        self.assertReason([True, "x"], "malformed evidence")
        self.assertReason("ok", "malformed evidence")
        self.assertReason(entry(ok="true"), "malformed evidence")
        self.assertReason(entry(ok=1), "malformed evidence")
        self.assertReason({"detail": "x", "checked_at": NOW.isoformat()}, "malformed evidence")

    def test_failing_carries_detail(self):
        r = rd.broken(entry(ok=False, detail="gh not logged in"), NOW, 900)
        self.assertEqual(r, "failing: gh not logged in")

    def test_bad_checked_at(self):
        self.assertReason({"ok": True, "detail": "x"}, "bad checked_at")
        self.assertReason(entry(at=1234567), "bad checked_at")
        self.assertReason(entry(at="yesterday"), "bad checked_at")
        self.assertReason(entry(at="2026-10-01T12:00:00"), "bad checked_at")  # naive

    def test_future(self):
        self.assertReason(entry(at=NOW + timedelta(minutes=2)), "evidence from the future")

    def test_stale_and_boundary(self):
        self.assertReason(entry(at=NOW - timedelta(seconds=901)), "stale evidence")
        self.assertIsNone(rd.broken(entry(at=NOW - timedelta(seconds=900)), NOW, 900))

    def test_infinite_age(self):
        self.assertIsNone(rd.broken(entry(at=NOW - timedelta(days=365)), NOW, math.inf))


class LimitsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(rd.ok_ttl_s("claude", {}), 6 * 3600)
        self.assertEqual(rd.ok_ttl_s("codex", {}), 6 * 3600)
        self.assertEqual(rd.ok_ttl_s("gmail", {}), 1800)
        self.assertEqual(rd.ok_ttl_s("browser", {}), 1800)
        self.assertEqual(rd.ok_ttl_s("git", {}), 0)
        for n in ("claude", "codex", "gmail"):
            self.assertEqual(rd.fail_retry_s(n, {}), 900)
        for n in ("git", "browser", "docker"):
            self.assertEqual(rd.fail_retry_s(n, {}), 0)
        self.assertEqual(rd.max_age_for("git", {}), 900)
        self.assertEqual(rd.max_age_for("claude", {}), 6 * 3600)
        self.assertEqual(rd.max_age_for("gmail", {}), 1800)

    def test_overrides(self):
        lim = {"ai_check_ttl_h": 2, "readiness_fail_retry_s": 60, "readiness_max_age_s": 3000}
        self.assertEqual(rd.ok_ttl_s("claude", lim), 7200)
        self.assertEqual(rd.ok_ttl_s("gmail", lim), 1800)
        self.assertEqual(rd.fail_retry_s("gmail", lim), 60)
        self.assertEqual(rd.fail_retry_s("docker", lim), 0)
        self.assertEqual(rd.max_age_for("git", lim), 3000)
        self.assertEqual(rd.max_age_for("gmail", lim), 3000)
        self.assertEqual(rd.max_age_for("codex", lim), 7200)

    def test_requirements(self):
        self.assertEqual(rd.requirements(None), {"git"})
        self.assertEqual(rd.requirements(""), {"git"})
        self.assertEqual(rd.requirements("claude", ["docker", "vpn"]), {"git", "claude", "docker", "vpn"})
        self.assertEqual(rd.requirements("codex", ("git", 5, None)), {"git", "codex"})


class MapFileTests(TempDirCase):
    def test_read_missing_invalid_array(self):
        self.assertEqual(rd.read_map(self.d / "nope.json"), {})
        bad = self.d / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(rd.read_map(bad), {})
        arr = self.d / "arr.json"
        arr.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(rd.read_map(arr), {})
        self.assertEqual(rd.read_map(self.d), {})  # a directory is unreadable
        raw = self.d / "raw.json"
        raw.write_bytes(b"\xff\xfe\x00garbage")
        self.assertEqual(rd.read_map(raw), {})

    def test_write_round_trip(self):
        p = self.d / "sub" / "caps.json"
        m = {"zeta": entry(), "alpha": entry(ok=False, detail="naïve – down")}
        rd.write_map(p, m)
        self.assertEqual(rd.read_map(p), m)
        raw = p.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertLess(text.index('"alpha"'), text.index('"zeta"'))
        self.assertEqual([x.name for x in p.parent.iterdir()], ["caps.json"])
        rd.write_map(p, {"only": entry()})
        self.assertEqual(rd.read_map(p), {"only": entry()})
        self.assertEqual([x.name for x in p.parent.iterdir()], ["caps.json"])

    def test_write_is_atomic_via_replace(self):
        p = self.d / "caps.json"
        rd.write_map(p, {"a": entry()})
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                rd.write_map(p, {"b": entry()})
        self.assertEqual(rd.read_map(p), {"a": entry()})
        self.assertEqual([x.name for x in self.d.iterdir()], ["caps.json"])


class ProbeTests(unittest.TestCase):
    def test_probe_agents_constructs_without_launching(self):
        with mock.patch("core.agents.launch", side_effect=AssertionError("launched")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            agents = rd.probe_agents({"check_timeout_s": 60})
            small = rd.probe_agents({"check_timeout_s": 3})
            default = rd.probe_agents({})
        from core.agents import ClaudeAgent, CodexAgent
        self.assertEqual(set(agents), {"claude", "codex"})
        self.assertIsInstance(agents["claude"], ClaudeAgent)
        self.assertIsInstance(agents["codex"], CodexAgent)
        self.assertEqual(agents["claude"].timeout_s, 60 - rd.INNER_MARGIN_S)
        self.assertEqual(agents["codex"].timeout_s, 60 - rd.INNER_MARGIN_S)
        self.assertEqual(agents["claude"].permission_mode, "plan")
        self.assertEqual(small["claude"].timeout_s, 5)
        self.assertEqual(default["codex"].timeout_s, 60 - rd.INNER_MARGIN_S)

    def test_probe_ok(self):
        R = lambda text, ok=True, error=None, tokens=7: SimpleNamespace(text=text, ok=ok, error=error, tokens=tokens)  # noqa: E731
        self.assertEqual(rd.probe_ok(R("ok")), (True, "ok (7 tokens)"))
        self.assertEqual(rd.probe_ok(R(" OK.")), (True, "ok (7 tokens)"))
        self.assertEqual(rd.probe_ok(R("Sure! here you go")), (False, "unexpected reply: Sure! here you go"))
        self.assertEqual(rd.probe_ok(R("x" * 300)), (False, "unexpected reply: " + "x" * 100))
        self.assertEqual(rd.probe_ok(R("ok", ok=False, error="rate limited")), (False, "rate limited"))
        bad, why = rd.probe_ok(R("ok", ok=False, error=None))
        self.assertFalse(bad)
        self.assertTrue(why.startswith("unexpected reply"))


QUOTED_PATH = re.compile(r"[\"'][^\"']*[\\/][^\"']*[\"']")


class DiagnoseTests(unittest.TestCase):
    KEYS = {"condition", "fix", "then", "troubleshoot", "depends_on"}

    def dx(self, name, detail=None, cap_map=None, which=yes_which, probe=probe_never):
        e = None if detail is None else entry(ok=False, detail=detail)
        d = rd.diagnose(name, e, cap_map or {}, which=which, probe=probe)
        self.assertEqual(set(d), self.KEYS)
        self.assertIsInstance(d["condition"], str)
        self.assertIsInstance(d["then"], str)
        self.assertIsInstance(d["troubleshoot"], bool)
        self.assertTrue(d["fix"].startswith(("PowerShell: ", "Win + R: ", "Reply ")), d["fix"])
        self.assertIsNone(QUOTED_PATH.search(d["fix"]), d["fix"])
        return d

    def test_no_evidence_and_no_check(self):
        d = self.dx("docker")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("no_evidence", True, "PowerShell: python -m core.readiness"))
        self.assertEqual(self.dx("claude")["condition"], "no_evidence")
        d = self.dx("vpn")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("no_check", False, "Reply to this email with: not needed"))
        self.assertEqual(self.dx("vpn", "whatever")["condition"], "no_check")

    def test_git(self):
        d = self.dx("git", "git not installed or not on PATH")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("missing", False, "PowerShell: winget install --id Git.Git -e"))
        d = self.dx("git", "fatal: weird")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]), ("error", True, "PowerShell: git --version"))

    def test_github_missing_vs_logged_out(self):
        d = self.dx("github", "gh not installed or not on PATH")
        self.assertEqual((d["condition"], d["fix"]), ("missing", "PowerShell: winget install --id GitHub.cli -e"))
        self.assertFalse(d["troubleshoot"])
        for detail in ["You are not logged into any GitHub hosts. Run gh auth login to authenticate.",
                       "The token in keyring is invalid"]:
            d = self.dx("github", detail)
            self.assertEqual(d["condition"], "logged_out")
            self.assertEqual(d["fix"], "PowerShell: gh auth login --hostname github.com --git-protocol https --web")
            self.assertFalse(d["troubleshoot"])
        d = self.dx("github", "HTTP 502 from api.github.com")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]), ("error", True, "PowerShell: gh auth status"))

    def test_docker_missing_vs_daemon(self):
        d = self.dx("docker", "docker not installed or not on PATH")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("missing", False, "PowerShell: winget install --id Docker.DockerDesktop -e"))
        d = self.dx("docker", "error during connect: this error may indicate that the docker daemon is not running")
        self.assertEqual((d["condition"], d["troubleshoot"]), ("daemon_down", True))
        self.assertEqual(d["fix"], "Win + R: C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe")
        d = self.dx("docker", "something odd")
        self.assertEqual((d["condition"], d["fix"]), ("error", "PowerShell: docker info"))

    def test_n8n_docker_broken(self):
        for caps in [{}, {"docker": entry(ok=False, detail="daemon down")}, {"docker": {"ok": 1}}]:
            d = self.dx("n8n", "URLError: refused", cap_map=caps)
            self.assertEqual(d["condition"], "docker_down")
            self.assertEqual(d["depends_on"], "docker")
            self.assertFalse(d["troubleshoot"])

    def n8n(self, probe):
        caps = {"docker": entry(at=datetime.now(timezone.utc) - timedelta(days=3))}
        seen = []

        def p(args):
            seen.append(args)
            return probe(args)

        d = self.dx("n8n", "URLError: refused", cap_map=caps, probe=p)
        if seen:
            self.assertEqual(seen[0], ["docker", "ps", "-a", "--filter", "name=^/n8n$", "--format", "{{.Status}}"])
        self.assertIsNone(d["depends_on"])
        return d

    def test_n8n_container_states(self):
        d = self.n8n(lambda a: (0, ""))
        self.assertEqual((d["condition"], d["troubleshoot"]), ("container_missing", True))
        self.assertEqual(d["fix"], "PowerShell: docker run -d --name n8n --restart unless-stopped -p "
                                   "127.0.0.1:5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n")
        for status in ["Exited (1) 3 hours ago", "Created"]:
            d = self.n8n(lambda a, s=status: (0, s + "\n"))
            self.assertEqual((d["condition"], d["fix"]), ("container_stopped", "PowerShell: docker start n8n"))
        d = self.n8n(lambda a: (0, "Up 2 minutes (unhealthy)"))
        self.assertEqual((d["condition"], d["fix"]), ("unhealthy", "PowerShell: docker restart n8n"))
        d = self.n8n(lambda a: (1, "permission denied"))
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("probe_failed", True, "PowerShell: docker ps -a"))

    def test_n8n_probe_raises(self):
        def boom(_):
            raise OSError("no docker")

        d = self.n8n(boom)
        self.assertEqual((d["condition"], d["fix"]), ("probe_failed", "PowerShell: docker ps -a"))

    def test_ollama(self):
        d = self.dx("ollama", "URLError: refused", which=no_which)
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("missing", False, "PowerShell: winget install --id Ollama.Ollama -e"))
        d = self.dx("ollama", "URLError: refused", which=yes_which)
        self.assertEqual((d["condition"], d["troubleshoot"]), ("not_running", True))
        self.assertEqual(d["fix"], "PowerShell: Start-Process ollama -ArgumentList serve -WindowStyle Hidden")

    def test_python_libs(self):
        d = self.dx("python_libs", "missing: yaml, pandas, playwright")
        self.assertEqual((d["condition"], d["troubleshoot"]), ("missing_libs", True))
        self.assertEqual(d["fix"], "PowerShell: python -m pip install pyyaml pandas playwright")
        d = self.dx("python_libs", "ValueError: odd")
        self.assertEqual((d["condition"], d["fix"]), ("error", "PowerShell: python -m core.readiness"))

    def test_gmail(self):
        d = self.dx("gmail", "no app password in Windows Credential Manager (forge-gmail)")
        self.assertEqual((d["condition"], d["troubleshoot"]), ("no_password", False))
        self.assertTrue(d["fix"].startswith("PowerShell: python -c \"import keyring,getpass; keyring.set_password("))
        self.assertIn("'forge-gmail','benjaminanderson0802@gmail.com'", d["fix"])
        self.assertIn("https://myaccount.google.com/apppasswords", d["then"])
        rejected = self.dx("gmail", "SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted')")
        self.assertEqual((rejected["condition"], rejected["troubleshoot"]), ("auth_rejected", False))
        self.assertEqual(rejected["fix"], d["fix"])
        self.assertIn("rejected", rejected["then"])
        d = self.dx("gmail", "gaierror: [Errno 11001] getaddrinfo failed")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("network", True, "PowerShell: Test-NetConnection smtp.gmail.com -Port 465"))
        d = self.dx("gmail", "ModuleNotFoundError: No module named 'keyring'")
        self.assertEqual((d["condition"], d["fix"]), ("missing_lib", "PowerShell: python -m pip install keyring"))

    def test_claude(self):
        for detail in ["agent command not found: claude", "'claude' is not recognized as an internal command",
                       "FileNotFoundError: [WinError 2]"]:
            d = self.dx("claude", detail)
            self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                             ("missing", False, "PowerShell: npm install -g @anthropic-ai/claude-code"), detail)
        d = self.dx("claude", "Invalid API key · Please run /login")
        self.assertEqual((d["condition"], d["fix"]), ("logged_out", "PowerShell: claude.cmd"))
        self.assertEqual(d["then"], "type /login and follow the browser sign-in")
        d = self.dx("claude", "API Error: 429 usage limit reached")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                         ("rate_limited", False, "PowerShell: claude.cmd -p ok"))
        d = self.dx("claude", "unexpected reply: hmm")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]), ("error", True, "PowerShell: claude.cmd -p ok"))

    def test_codex(self):
        d = self.dx("codex", "agent command not found: codex")
        self.assertEqual((d["condition"], d["fix"]), ("missing", "PowerShell: npm install -g @openai/codex"))
        d = self.dx("codex", "Codex run failed (exit 1): 401 Unauthorized")
        self.assertEqual((d["condition"], d["fix"]), ("logged_out", "PowerShell: codex login"))
        d = self.dx("codex", "Codex run failed (exit 1): rate limit exceeded")
        self.assertEqual((d["condition"], d["fix"]), ("rate_limited", "PowerShell: codex exec ok"))
        d = self.dx("codex", "agent timed out after 45s")
        self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]), ("error", True, "PowerShell: codex exec ok"))

    def test_browser(self):
        d = self.dx("browser", "ModuleNotFoundError: No module named 'playwright'")
        self.assertEqual((d["condition"], d["fix"]),
                         ("missing_lib", "PowerShell: python -m pip install playwright; python -m playwright install chromium"))
        d = self.dx("browser", "Error: Executable doesn't exist at C:\\x\\chrome.exe")
        self.assertEqual((d["condition"], d["fix"]), ("no_chromium", "PowerShell: python -m playwright install chromium"))
        d = self.dx("browser", "crashed")
        self.assertEqual(d["condition"], "error")

    def test_generic_known_name(self):
        with mock.patch.dict(rd.CHECKS, {"extra": lambda: (True, "")}):
            d = self.dx("extra", "check timed out after 60s")
            self.assertEqual((d["condition"], d["troubleshoot"], d["fix"]),
                             ("timeout", True, "PowerShell: python -m core.readiness"))
            self.assertEqual(self.dx("extra", "boom")["condition"], "error")

    def test_case_insensitive_and_malformed_entries_never_raise(self):
        self.assertEqual(self.dx("github", "NOT LOGGED IN")["condition"], "logged_out")
        for bad in [[1, 2], "text", {"ok": False, "detail": None}, {"ok": False, "detail": 12}]:
            d = rd.diagnose("docker", bad, {}, which=yes_which, probe=probe_never)
            self.assertEqual(d["condition"], "error")
        d = rd.diagnose("n8n", None, {"docker": entry()}, which=yes_which, probe=probe_never)
        self.assertEqual(d["condition"], "no_evidence")

    def test_every_fix_is_a_pasteable_line(self):
        cases = [(n, det) for n in list(rd.CHECKS) + ["vpn"] for det in
                 [None, "not installed", "not logged in", "missing: yaml", "No module named x", "timed out",
                  "daemon", "429", "Executable doesn't exist"]]
        for name, det in cases:
            with self.subTest(name=name, detail=det):
                d = self.dx(name, det, cap_map={"docker": entry(at=datetime.now(timezone.utc))},
                            probe=lambda a: (0, "Up"))
                self.assertNotIn("\n", d["fix"])


if __name__ == "__main__":
    unittest.main()
