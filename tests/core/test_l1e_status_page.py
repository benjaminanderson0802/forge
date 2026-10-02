"""L1E_3: usable local approvals and owner-email approvals from the status page."""
import http.client
import json
import re
import threading
import unittest
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from core import channel, status_page
from drills.run_drills import _channel_conductor
from tests.core.test_status_page import LIM, NOW, PageHarness


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def find(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and all(
                    child.attrs.get(k) == v for k, v in attrs.items()
                ):
                    found.append(child)
                found.extend(child.find(tag, **attrs))
        return found

    def text(self):
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


class Approvals(PageHarness):
    def setUp(self):
        super().setUp()
        self.questions = json.loads((self.state / "questions.json").read_text())
        self.questions["replan-3"].update(status="open", subject="Forge paused")
        self.questions["closed-4"] = dict(self.questions["gate-1"], status="answered",
                                          subject="Already resolved")
        self.write("questions.json", self.questions)

    def rendered(self, **kwargs):
        return status_page.render(self.state, LIM, NOW, **kwargs)

    def question(self, html, qid):
        page = Page(html).root
        forms = [f for f in page.find("form", action="/answer")
                 if f.find("input", name="qid", value=qid)]
        self.assertEqual(len(forms), 1, f"Exactly one answer form for {qid}")
        form = forms[0]
        self.assertEqual(form.attrs.get("method", "").lower(), "post")
        self.assertEqual(len(form.find("textarea", name="answer")), 1)
        self.assertTrue(any(b.text().strip() == "Answer" for b in form.find("button")))
        card = form.parent
        while card.parent is not None and "card" not in card.attrs.get("class", "").split():
            card = card.parent
        self.assertIn("card", card.attrs.get("class", "").split())
        return form, card

    def email_link(self, html, qid, code, owner):
        form, card = self.question(html, qid)
        self.assertFalse(card.find("button", name="approve"))
        self.assertFalse(card.find("input", name="approve"))
        links = [a for a in card.find("a")
                 if "approve-mail" in a.attrs.get("class", "").split()]
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link.text().strip(), "Approve by email")
        self.assertIn("This approval counts only by email from you.", card.text())
        expected = "mailto:" + owner + "?subject=" + quote(
            channel.answer_tag(qid, code) + " y", safe="") + "&body=y"
        self.assertEqual(link.attrs["href"], expected)
        self.assertIn(escape(expected, quote=True), html)
        parsed = urlsplit(link.attrs["href"])
        self.assertEqual((parsed.scheme, parsed.path), ("mailto", owner))
        fields = parse_qs(parsed.query)
        self.assertEqual(fields["body"], ["y"])
        match = re.search(r"\[Forge Q-([\w-]+) ([\w-]{8})\]", fields["subject"][0])
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), (qid, code))
        return fields

    def local_button(self, html, qid):
        form, card = self.question(html, qid)
        buttons = form.find("button", name="approve", value="y", type="submit")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text().strip(), "Approve")
        self.assertFalse([a for a in card.find("a")
                          if "approve-mail" in a.attrs.get("class", "").split()])
        return form

    def test_render_routes_approvals_and_preserves_status_information(self):
        html = self.rendered(owner="ben@example.com")
        page = Page(html).root
        self.assertEqual(len(page.find("form", action="/answer")), 3)
        self.assertNotIn("Already resolved", html)
        for qid, q in self.questions.items():
            if q["status"] != "open":
                continue
            _, card = self.question(html, qid)
            self.assertIn(q["subject"], card.text())
            self.assertIn(channel.default_for(q["kind"]), card.text())
            self.assertTrue(card.find("input", name="code", value=q["code"]))
        for qid in ("blocked-2", "replan-3"):
            self.local_button(html, qid)
        self.email_link(html, "gate-1", "gatecode", "ben@example.com")
        self.assertRegex(page.text(), r"Now:\s*T3\s+third\s*\(tests_ok\)")
        for text in ("400 of 1000", "50 of 2000", "1 of 3 this hour", "2 of 10 today"):
            self.assertIn(text, page.text())
        rows = [row.text() for row in page.find("tr")]
        self.assertTrue(any("git" in r and "OK" in r for r in rows))
        self.assertTrue(any("gmail" in r and "BROKEN" in r for r in rows))
        stop, = page.find("form", action="/stop")
        self.assertEqual(stop.attrs["method"].lower(), "post")
        self.assertTrue(stop.find("button", type="submit"))

    def test_every_email_only_kind_uses_mail_and_other_kinds_use_local_approve(self):
        kinds = set(channel.DEFAULTS) | set(channel.EMAIL_ONLY_KINDS) | {"future-kind"}
        self.write("questions.json", {
            f"{kind}-1": dict(kind=kind, status="open", code="code1234", subject=kind)
            for kind in kinds
        })
        html = self.rendered(owner="ben@example.com")
        for kind in sorted(kinds):
            with self.subTest(kind=kind):
                if kind in channel.EMAIL_ONLY_KINDS:
                    self.email_link(html, f"{kind}-1", "code1234", "ben@example.com")
                else:
                    self.local_button(html, f"{kind}-1")

    def test_render_without_owner_leaves_mail_recipient_empty(self):
        self.email_link(self.rendered(), "gate-1", "gatecode", "")
        self.email_link(self.rendered(owner=None), "gate-1", "gatecode", "")

    def start_server(self, state, chan, limits, **kwargs):
        srv = status_page.make_server(state, chan, limits, port=0, **kwargs)
        self.addCleanup(srv.server_close)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(srv.shutdown)
        return srv

    def request(self, srv, method="GET", path="/", form=None, host=None):
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {"Host": host or f"127.0.0.1:{port}"}
            body = None
            if form is not None:
                body = urlencode(form)
                headers.update({"Content-Type": "application/x-www-form-urlencoded",
                                "Origin": f"http://127.0.0.1:{port}"})
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def conductor(self):
        c, _, inbox, _ = _channel_conductor()
        c.gh = Mock(return_value=(0, ""))
        return c, inbox

    def test_get_propagates_owner_and_email_link_really_approves_gate(self):
        c, inbox = self.conductor()
        qid = c._ask("gate", "layer-1 is ready", "report", pr="7")
        code = json.loads((c.state / "questions.json").read_text())[qid]["code"]
        srv = self.start_server(c.state, c.channel_in.parent, c.limits, owner="ben@example.com")
        status, html = self.request(srv)
        self.assertEqual(status, 200)
        fields = self.email_link(html, qid, code, "ben@example.com")
        inbox.append({"from": "ben@example.com", "subject": fields["subject"][0],
                      "body": fields["body"][0]})
        c._handle_inbox()
        calls = [call.args[0] for call in c.gh.call_args_list]
        self.assertIn(["pr", "edit", "7", "--add-label", "human-approved"], calls)
        self.assertIn(["pr", "merge", "7", "--merge", "--delete-branch"], calls)
        q = json.loads((c.state / "questions.json").read_text())[qid]
        self.assertEqual((q["status"], q["answer"]), ("answered", "y"))

    def test_page_approve_answers_replan_and_unpauses_real_conductor(self):
        c, _ = self.conductor()
        (c.state / "PAUSED").write_text("x")
        qid = c._ask("replan", "Forge paused", "reasons")
        outbox = status_page.AnswerOutbox(c.channel_in, Path(self.tmp.name), idle=lambda *_: True)
        srv = self.start_server(c.state, c.channel_in.parent, c.limits,
                                outbox=outbox, owner="ben@example.com")
        status, html = self.request(srv)
        self.assertEqual(status, 200)
        form = self.local_button(html, qid)
        data = {n.attrs["name"]: n.attrs["value"] for n in form.find("input", type="hidden")}
        data.update(approve="y", answer="")
        self.assertEqual(self.request(srv, "POST", "/answer", data)[0], 303)
        outbox.flush()
        c._handle_inbox()
        q = json.loads((c.state / "questions.json").read_text())[qid]
        self.assertEqual((q["status"], q["answer"]), ("answered", "y"))
        self.assertFalse((c.state / "PAUSED").exists())
        c.gh.assert_not_called()

    def test_localhost_host_checks_and_stop_halt_real_conductor(self):
        c, _ = self.conductor()
        self.assertEqual(status_page.PORT, 8765)
        with self.assertRaises(ValueError):
            status_page.make_server(c.state, c.channel_in.parent, c.limits, host="0.0.0.0", port=0)
        srv = self.start_server(c.state, c.channel_in.parent, c.limits, owner="ben@example.com")
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        self.assertEqual(self.request(srv, host="evil.example")[0], 403)
        status, html = self.request(srv)
        self.assertEqual(status, 200)
        self.assertTrue(Page(html).root.find("form", action="/stop"))
        self.assertEqual(self.request(srv, "POST", "/stop", {})[0], 303)
        self.assertTrue((c.state / "KILL").exists())
        self.assertEqual(c.step(), "killed")

    def test_cli_passes_default_and_explicit_owner_to_server(self):
        for args, owner in (([], "benjaminanderson0802@gmail.com"),
                            (["--owner", "alternate@example.com"], "alternate@example.com")):
            with self.subTest(args=args):
                srv = Mock(server_address=("127.0.0.1", 8765))
                srv.serve_forever.side_effect = KeyboardInterrupt
                with patch.object(status_page, "make_server", return_value=srv) as make, \
                        patch("core.agents.load_limits", return_value=LIM):
                    self.assertEqual(status_page.main(args), 0)
                self.assertEqual(make.call_args.kwargs.get("owner"), owner)
                srv.serve_forever.assert_called_once()
                srv.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
