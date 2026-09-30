"""Layer 1E: the local status page (design §6, D-022, D-024). Bound to 127.0.0.1 only; no mail involved."""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from core import channel, status_page

NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
LIM = {"claude_daily_token_cap": 1000, "codex_daily_token_cap": 2000, "mail_per_hour": 3, "mail_per_day": 10}


class PageHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state, self.chan = root / "state", root / "channel"
        self.state.mkdir()
        self.write("questions.json", {
            "gate-1": {"kind": "gate", "status": "open", "code": "gatecode", "subject": "layer-1 is ready",
                       "body": "report", "delivered": True, "pr": "7"},
            "blocked-2": {"kind": "blocked", "status": "open", "code": "blkcode2", "subject": "Task <T2> is blocked",
                          "body": "x", "delivered": False, "hold": True, "digest": True},
            "replan-3": {"kind": "replan", "status": "answered", "code": "oldcode3", "subject": "old question",
                         "body": "", "delivered": True}})
        self.write("queue.json", {"layer": "layer-1", "tasks": [
            {"id": "T1", "title": "first", "status": "done"},
            {"id": "T2", "title": "second <b>", "status": "blocked"},
            {"id": "T3", "title": "third", "status": "tests_ok"}]})
        self.write("meter.json", {"2026-10-01": {"claude": 400, "codex": 50}})
        self.write("mail_log.json", {"sent": [(NOW - timedelta(minutes=5)).isoformat(),
                                              (NOW - timedelta(hours=5)).isoformat()], "ids": []})
        self.write("capabilities.json", {
            "git": {"ok": True, "detail": "git 2.4", "checked_at": NOW.isoformat()},
            "gmail": {"ok": False, "detail": "login failed <x>", "checked_at": NOW.isoformat()}})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        (self.state / name).write_text(json.dumps(data), encoding="utf-8")

    def page(self):
        return status_page.render(self.state, LIM, NOW)


class Render(PageHarness):
    def test_shows_queue_with_forms_and_defaults(self):
        html = self.page()
        self.assertIn("layer-1 is ready", html)
        self.assertIn("Task &lt;T2&gt; is blocked", html)
        self.assertNotIn("Task <T2>", html)
        self.assertNotIn("old question", html)
        self.assertIn(channel.default_for("gate"), html)
        self.assertEqual(html.count('action="/answer"'), 2)
        self.assertIn('value="gatecode"', html)
        self.assertIn("Approve", html)
        self.assertIn('action="/stop"', html)

    def test_shows_current_work_caps_and_capabilities(self):
        html = self.page()
        self.assertIn("T3", html); self.assertIn("third", html)
        self.assertIn("second &lt;b&gt;", html)
        self.assertIn("400 of 1000", html); self.assertIn("50 of 2000", html)
        self.assertIn("1 of 3", html); self.assertIn("2 of 10", html)
        self.assertIn("git", html); self.assertIn("login failed &lt;x&gt;", html)
        self.assertIn("BROKEN", html)

    def test_banners(self):
        self.assertNotIn("Forge is stopped", self.page())
        (self.state / "KILL").write_text("x")
        (self.state / "PAUSED").write_text("x")
        html = self.page()
        self.assertIn("Forge is stopped", html)
        self.assertIn("paused", html.lower())

    def test_survives_missing_or_broken_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "questions.json").write_text("{broken", encoding="utf-8")
            (Path(d) / "queue.json").write_text("[]", encoding="utf-8")
            html = status_page.render(Path(d), {}, NOW)
            self.assertIn("Nothing is waiting on you", html)


class Server(PageHarness):
    def setUp(self):
        super().setUp()
        self.srv = status_page.make_server(self.state, self.chan, LIM, port=0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, form=None, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urlencode(form).encode() if form is not None else None
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read().decode("utf-8", "replace")
        conn.close()
        return r.status, data, r

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "192.168.1.5", "", "example.com"):
            with self.assertRaises(ValueError):
                status_page.make_server(self.state, self.chan, LIM, host=host, port=0)

    def test_get_page_with_safe_headers(self):
        status, html, r = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("layer-1 is ready", html)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.req("GET", "/", host=f"localhost:{self.port}")[0], 200)

    def test_foreign_host_is_refused(self):
        """DNS rebinding: a page on evil.example resolving to 127.0.0.1 must not read codes."""
        status, html, _ = self.req("GET", "/", host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        self.assertNotIn("gatecode", html)
        self.assertEqual(self.req("POST", "/stop", form={}, host="evil.example")[0], 403)
        self.assertFalse((self.state / "KILL").exists())

    def test_stop_button_creates_kill(self):
        status, _, r = self.req("POST", "/stop", form={}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 303)
        self.assertIn("status page", (self.state / "KILL").read_text(encoding="utf-8"))

    def test_cross_site_posts_are_refused(self):
        for hdr in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
            self.assertEqual(self.req("POST", "/stop", form={}, headers=hdr)[0], 403, hdr)
            self.assertEqual(self.req("POST", "/answer", form={"qid": "gate-1", "code": "gatecode", "answer": "y"},
                                      headers=hdr)[0], 403, hdr)
        self.assertFalse((self.state / "KILL").exists())
        self.assertEqual(channel.take_answers(self.chan / "in"), [])

    def test_answer_is_dropped_for_the_conductor(self):
        status, _, _ = self.req("POST", "/answer", form={"qid": "gate-1", "code": "gatecode", "answer": "y"},
                                headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 303)
        got = channel.take_answers(self.chan / "in")
        self.assertEqual([(a["qid"], a["code"], a["answer"], a["source"]) for a in got],
                         [("gate-1", "gatecode", "y", "status page")])

    def test_approve_button_sends_y(self):
        status, _, _ = self.req("POST", "/answer",
                                form={"qid": "gate-1", "code": "gatecode", "answer": "", "approve": "y"})
        self.assertEqual(status, 303)
        self.assertEqual([a["answer"] for a in channel.take_answers(self.chan / "in")], ["y"])

    def test_bad_answers_are_refused_and_not_dropped(self):
        for form in ({"qid": "gate-1", "code": "wrong", "answer": "y"},
                     {"qid": "replan-3", "code": "oldcode3", "answer": "y"},
                     {"qid": "../x", "code": "gatecode", "answer": "y"},
                     {"qid": "gate-1", "code": "gatecode", "answer": ""},
                     {"qid": "gate-1"}):
            self.assertEqual(self.req("POST", "/answer", form=form)[0], 400, form)
        self.assertEqual(channel.take_answers(self.chan / "in"), [])

    def test_oversized_body_and_unknown_paths(self):
        big = {"qid": "gate-1", "code": "gatecode", "answer": "a" * 50000}
        self.assertEqual(self.req("POST", "/answer", form=big)[0], 413)
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.req("POST", "/nope", form={})[0], 404)


if __name__ == "__main__":
    unittest.main()
