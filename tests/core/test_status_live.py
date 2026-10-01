"""R64 HTTP and HTML contracts, using a loopback server on an ephemeral port."""
import http.client
import importlib
import json
import re
import threading
import unittest
from datetime import datetime
from html.parser import HTMLParser
from unittest.mock import patch

from core import status_page
from tests.core.test_dashboard import ForgeFixture, NOW


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class Elements(HTMLParser):
    """Inspect HTML semantically without prescribing classes or quote style."""
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), text="", ancestors=tuple(self.stack))
        self.nodes.append(node)
        if tag not in {"meta", "input", "br", "hr", "img", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1]["text"] += data

    def tags(self, tag):
        return [n for n in self.nodes if n["tag"] == tag]


class LiveTests(ForgeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.queue([dict(id="T<x>", title="Build <b>carefully</b>", kind="build",
                         status="todo", covers=["1.1"])])
        self.add_run("probe-&x", 60, task="T<x>", title="Build <b>carefully</b>")
        self.write("state/bootstrap/questions.json", {
            "gate-1": dict(kind="gate", status="open", code="gatecode", subject="Layer ready",
                           body="Review", delivered=True, pr="7"),
            "blocked-2": dict(kind="blocked", status="open", code="blkcode2", subject="Need answer",
                              body="Help", delivered=False, hold=True)})
        # Patch only the public data boundary, so HTTP and render use the same fixed time.
        dashboard = importlib.import_module("core.dashboard")
        snapshot = dashboard.snapshot
        fixed = patch.object(dashboard, "snapshot", side_effect=lambda root, now=None: snapshot(root, NOW))
        fixed.start()
        self.addCleanup(fixed.stop)
        clock = patch.object(status_page, "datetime", FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        self.srv = status_page.make_server(self.state, self.root / "state/channel", self.limits,
                                           port=0, forge_root=self.root)
        self.addCleanup(self.srv.server_close)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.srv.shutdown()
        self.thread.join(timeout=5)

    def get(self, path="/", host=None):
        port = self.srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            conn.close()

    def test_api_live_returns_snapshot_json_and_security_headers(self):
        code, headers, body = self.get("/api/live")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"].split(";")[0], "application/json")
        data = json.loads(body)
        self.assertLessEqual({"lanes", "tokens", "project", "timeline", "attempts"}, data.keys())
        self.assertEqual(data["attempts"], dict(median=1.0, n=0, source="default"))
        self.assertEqual(data, self.snap())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_api_live_refuses_foreign_host_for_json_and_fragment(self):
        for path in ("/api/live", "/api/live?format=html"):
            with self.subTest(path=path):
                code, _, body = self.get(path, "evil.example")
                self.assertEqual(code, 403)
                self.assertNotIn("gatecode", body)
                self.assertNotIn("T&lt;x&gt;", body)

    def test_html_fragment_is_the_live_section_in_the_main_page(self):
        code, headers, fragment = self.get("/api/live?format=html")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"].split(";")[0], "text/html")
        parsed = Elements(fragment)
        self.assertTrue(parsed.tags("svg"), "The live fragment must include its timeline")
        self.assertFalse(parsed.tags("html"), "Endpoint returns a section, not a whole page")
        self.assertIn(fragment.strip(), self.get()[2])
        self.assertIn("estimate", fragment.lower())

    def test_main_page_nonce_matches_csp_and_retains_controls_and_noscript(self):
        code, headers, body = self.get()
        self.assertEqual(code, 200)
        csp = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        match = re.search(r"(?:^|;)\s*script-src\s+[^;]*'nonce-([^']+)'", csp)
        self.assertIsNotNone(match, csp)
        nodes = Elements(body)
        scripts = nodes.tags("script")
        self.assertTrue(scripts)
        for script in scripts:
            self.assertEqual(script["attrs"].get("nonce"), match.group(1))
            self.assertNotIn("src", script["attrs"])
        self.assertIn("/api/live?format=html", body)
        refresh = [n for n in nodes.tags("meta")
                   if n["attrs"].get("http-equiv", "").lower() == "refresh"]
        self.assertEqual(len(refresh), 1)
        self.assertEqual(refresh[0]["attrs"]["content"], "30")
        self.assertIn("noscript", [n["tag"] for n in refresh[0]["ancestors"]])
        actions = [n["attrs"].get("action") for n in nodes.tags("form")]
        self.assertIn("/stop", actions)
        self.assertEqual(actions.count("/answer"), 2)
        self.assertIn("Approve", body)
        self.assertIn('value="gatecode"', body)
        self.assertIn('value="blkcode2"', body)

    def test_csp_nonce_is_fresh_for_every_response(self):
        nonces = []
        for _ in range(2):
            _, headers, body = self.get()
            match = re.search(r"'nonce-([^']+)'", headers["Content-Security-Policy"])
            self.assertIsNotNone(match)
            nonces.append(match.group(1))
            self.assertIn(nonces[-1], [n["attrs"].get("nonce") for n in Elements(body).tags("script")])
        self.assertNotEqual(nonces[0], nonces[1])

    def test_live_timeline_and_one_coverage_cell_per_requirement(self):
        _, _, fragment = self.get("/api/live?format=html")
        nodes = Elements(fragment)
        self.assertTrue(nodes.tags("svg"))
        # A cell must expose its requirement text, directly or in an accessible label/tooltip.
        # No implementation-specific CSS class or data attribute is part of R64.
        for requirement in self.main_lane()["checkpoint"]["requirements"]:
            text = requirement["text"]
            cells = [n for n in nodes.nodes if text in n["text"] or
                     any(text in str(v) for v in n["attrs"].values())]
            # A tooltip and text inside the same cell describe one cell, not two.
            outermost = [n for n in cells if not any(a in cells for a in n["ancestors"])]
            self.assertEqual(len(outermost), 1, requirement)
        self.assertTrue(Elements(self.get()[2]).tags("svg"))

    def test_live_role_task_id_and_title_are_html_escaped(self):
        for path in ("/", "/api/live?format=html"):
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertIn("probe-&amp;x", body)
                self.assertIn("T&lt;x&gt;", body)
                self.assertIn("Build &lt;b&gt;carefully&lt;/b&gt;", body)
                self.assertNotIn("probe-&x", body)
                self.assertNotIn("T<x>", body)
                self.assertNotIn("<b>carefully</b>", body)

    def test_render_accepts_forge_root_and_includes_live_content(self):
        body = status_page.render(self.state, self.limits, NOW, forge_root=self.root)
        self.assertTrue(Elements(body).tags("svg"))
        self.assertIn("Build &lt;b&gt;carefully&lt;/b&gt;", body)


if __name__ == "__main__":
    unittest.main()


class ReviewRound3(unittest.TestCase):
    """R64 review round 3: a bad state file never takes the page's controls down."""

    def test_bad_queue_and_meter_keep_live_stop_and_answers(self):
        import tempfile
        from datetime import timezone
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state" / "bootstrap"
            state.mkdir(parents=True)
            (state / "queue.json").write_text('{"tasks": null}', encoding="utf-8")
            (state / "meter.json").write_text('{"2026-10-01": {"claude": "x", "codex": 1e999}}', encoding="utf-8")
            (state / "questions.json").write_text(json.dumps({"gate-1": {
                "kind": "gate", "status": "open", "code": "gatecode", "subject": "Ready?", "body": "b",
                "delivered": True}}), encoding="utf-8")
            html = status_page.render(state, {"claude_daily_token_cap": 10}, datetime(2026, 10, 1, 17, tzinfo=timezone.utc),
                                      forge_root=root, nonce="n")
            self.assertIn("<section id=live", html)
            self.assertIn('action="/stop"', html)
            self.assertIn('action="/answer"', html)
            self.assertIn("Ready?", html)

