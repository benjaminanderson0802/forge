"""R66i: file-only dashboard fixtures and loopback HTTP, with a fake fix runner."""
import hashlib
import http.client
import importlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

from core import dashboard, status_page


NOW = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
SCRIPT = "Write-Output '<prepared & checked>'\nWrite-Output complete\n"
PASTE = r"cd C:\Users\benja\Forge; python -m core.bootstrap retry --lane main b-1"


class Elements(HTMLParser):
    """Inspect form ownership and decoded attributes, independent of CSS/quoting."""
    def __init__(self, html):
        super().__init__()
        self.nodes, self.stack = [], []
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
        for node in self.stack:
            node["text"] += data

    def tags(self, tag):
        return [n for n in self.nodes if n["tag"] == tag]


class StatusFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state_root = self.root / "state"
        self.state = self.state_root / "bootstrap"
        self.limits = dict(self_fix=True, claude_daily_token_cap=1000, codex_daily_token_cap=2000,
                           agent_timeout_s=600)
        self.write("charter/limits.json", self.limits)
        self.write("docs/progress.json", {"phases": [dict(name="1. Loop", done=False)]})
        self.raw("docs/specs/layer-1-design.md", "# Spec\n\n## 1. Behaviour\n- First requirement\n")
        self.write("state/lanes.json", ["p2"])
        for folder in ("state/bootstrap", "state/lanes/p2"):
            self.write(folder + "/queue.json", {"layer": "layer-1", "tasks": []})
            self.write(folder + "/questions.json", {})
        self.sha = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
        self.main = self.record("b-1", "main", "ready_for_ben", "Repair <script> & retry", script=True)
        self.no_script = self.record("b-2", "main", "ready_for_ben", "Enter the app password")
        self.no_script["category"] = self.no_script["kit"]["category"] = "credentials"
        self.fixing = self.record("p2-b-1", "p2", "fixing", "Repairing p2")
        self.recent = self.record("p2-b-2", "p2", "fixed", "Recently repaired")
        self.recent["fixed_at"] = (NOW - timedelta(hours=23)).isoformat()
        self.old = self.record("p2-b-3", "p2", "fixed", "Old repair")
        self.old["fixed_at"] = (NOW - timedelta(hours=25)).isoformat()
        self.write("state/bootstrap/blockers.json", {"seq": 2, "items": {r["id"]: r for r in (self.main, self.no_script)}})
        self.write("state/lanes/p2/blockers.json", {"seq": 3, "items": {r["id"]: r for r in (self.fixing, self.recent, self.old)}})
        self.raw("state/bootstrap/fixkits/b-1.ps1", SCRIPT)

    def raw(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def write(self, name, value):
        return self.raw(name, json.dumps(value))

    def record(self, bid, lane, status, summary, script=False):
        rec = dict(id=bid, lane=lane, kind="blocked", key=bid, summary=summary, status=status,
                   attempts=2, category="exhausted", code="abcdefgh",
                   created_at=(NOW - timedelta(days=2)).isoformat(),
                   updated_at=(NOW - timedelta(seconds=60)).isoformat(), fixed_at=None)
        if status == "ready_for_ben":
            rec["kit"] = dict(why="Needs <one> & another try", category="exhausted", where="PowerShell",
                              paste=PASTE if script else "python -m keyring --help",
                              expect="A <green> check & progress")
            if script:
                rec["kit"].update(script=SCRIPT, script_file="fixkits/b-1.ps1", script_sha256=self.sha)
        return rec

    def snap(self):
        return dashboard.snapshot(self.root, NOW)


class SnapshotBlockers(StatusFixture):
    def test_all_lanes_active_and_only_last_twenty_four_hours_fixed(self):
        records = {r["id"]: r for r in self.snap()["blockers"]}
        self.assertEqual(set(records), {"b-1", "b-2", "p2-b-1", "p2-b-2"})
        for original in (self.main, self.no_script, self.fixing, self.recent):
            rec = records[original["id"]]
            self.assertLessEqual({"id", "lane", "kind", "summary", "status", "age_s", "attempts", "category", "kit"}, rec.keys())
            for field in ("lane", "kind", "summary", "status", "attempts", "category"):
                self.assertEqual(rec[field], original[field], field)
            self.assertEqual(rec["age_s"], 2 * 86400)
        self.assertEqual(records["b-1"]["kit"]["script_text"], SCRIPT)
        self.assertEqual(records["b-1"]["kit"]["paste"], PASTE)

    def test_tampered_script_is_never_presented_as_verified_text(self):
        self.raw("state/bootstrap/fixkits/b-1.ps1", "Write-Output changed\n")
        rec = next(r for r in self.snap()["blockers"] if r["id"] == "b-1")
        self.assertIsNone(rec["kit"]["script_text"])

    def test_snapshot_includes_last_run_and_log_tail(self):
        run = dict(started_at=(NOW - timedelta(seconds=20)).isoformat(), finished_at=NOW.isoformat(),
                   exit=0, sha=self.sha)
        self.write("state/channel/fixruns/b-1.json", run)
        self.raw("state/channel/fixruns/b-1.log", "x" * 4100 + "finished <ok>")
        rec = next(r for r in self.snap()["blockers"] if r["id"] == "b-1")
        for key in ("started_at", "finished_at", "exit"):
            self.assertEqual(rec["run"][key], run[key])
        self.assertEqual(rec["run"]["log"], ("x" * 4100 + "finished <ok>")[-4000:])

    def test_live_panel_copy_script_form_statuses_and_html_escaping(self):
        html = status_page.live_section(self.snap())
        nodes = Elements(html)
        self.assertIn("Blockers", html)
        for status in ("ready for you", "fixing", "fixed"):
            self.assertIn(status, html)
        self.assertIn("Repair &lt;script&gt; &amp; retry", html)
        self.assertNotIn("Repair <script>", html)
        self.assertIn("Needs &lt;one&gt; &amp; another try", html)
        self.assertIn("A &lt;green&gt; check &amp; progress", html)
        self.assertFalse(nodes.tags("script"))
        copies = [n for n in nodes.nodes if n["attrs"].get("data-copy") == PASTE]
        self.assertTrue(copies, "paste must be available on a Copy control")
        self.assertTrue(any("Copy" in n["text"] for n in copies))
        self.assertTrue(any(SCRIPT.strip() in n["text"] for n in nodes.nodes))
        forms = [n for n in nodes.tags("form") if n["attrs"].get("action") == "/fix"]
        self.assertEqual(len(forms), 1, "credentials kit must not have a Do it form")
        form = forms[0]
        self.assertEqual(form["attrs"]["method"].lower(), "post")
        hidden = {n["attrs"].get("name"): n["attrs"].get("value") for n in nodes.tags("input")
                  if any(a is form for a in n["ancestors"]) and n["attrs"].get("type") == "hidden"}
        self.assertEqual({k: hidden[k] for k in ("lane", "id", "sha")},
                         {"lane": "main", "id": "b-1", "sha": self.sha})
        self.assertTrue(any("Do it" in n["text"] for n in nodes.tags("button")
                            if any(a is form for a in n["ancestors"])))


class FixHTTP(StatusFixture):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.called = threading.Event()

        def runner(script, log, timeout_s):
            self.calls.append((script, log, timeout_s))
            log.write_bytes(b"fake fix completed\n")
            self.called.set()
            return 0

        self.server = status_page.make_server(self.state, self.state_root / "channel", self.limits,
                                              port=0, forge_root=self.root, fix_runner=runner)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=1)
        # Wait for the run's status write before the temporary directory is removed.
        if self.called.is_set():
            self.finished()

    def finished(self):
        mod = importlib.import_module("core.blockers")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            rec = mod.run_status(self.state_root, "b-1")
            if rec and rec.get("finished_at"):
                return rec
            time.sleep(0.02)
        self.fail("fake HTTP fix did not finish within four seconds")

    def request(self, method, path, form=None, headers=None):
        hdr = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}"}
        hdr.update(headers or {})
        body = None
        if form is not None:
            body = urlencode(form).encode("utf-8")
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request(method, path, body=body, headers=hdr)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def form(self, **changes):
        data = dict(lane="main", id="b-1", sha=self.sha)
        data.update(changes)
        return data

    def test_correct_fix_post_runs_only_injected_runner_and_stop_still_works(self):
        self.assertEqual(self.request("POST", "/fix", self.form()), 303)
        self.assertTrue(self.called.wait(1))
        self.assertEqual(self.finished()["exit"], 0)
        self.assertEqual(self.calls, [(self.state / "fixkits/b-1.ps1",
                                      self.state_root / "channel/fixruns/b-1.log", 600)])
        # Script exit zero is not evidence that the underlying blocker is fixed.
        records = json.loads((self.state / "blockers.json").read_text(encoding="utf-8"))
        self.assertEqual(records["items"]["b-1"]["status"], "ready_for_ben")
        self.assertEqual(self.request("POST", "/stop", {}), 303)
        self.assertTrue((self.state / "KILL").exists())

    def test_wrong_sha_is_rejected_without_running(self):
        code = self.request("POST", "/fix", self.form(sha="0" * 64))
        self.assertTrue(400 <= code < 500, code)
        self.assertEqual(self.calls, [])

    def test_foreign_host_and_cross_site_origin_are_forbidden(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/fix", self.form(), headers), 403)
                self.assertEqual(self.calls, [])

    def test_get_fix_never_runs_script(self):
        self.assertIn(self.request("GET", "/fix?" + urlencode(self.form())), (404, 405))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
