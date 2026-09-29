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

    def test_R16_windows_override_preserves_each_sandbox_mode(self):
        """R16: Windows elevation applies to both writer and reviewer sandbox modes."""
        from unittest.mock import patch

        for sandbox in ("workspace-write", "read-only"):
            with self.subTest(sandbox=sandbox):
                completed = json.dumps({"type": "turn.completed", "usage": {}}) + "\n"
                with patch("core.agents.IS_WIN", True), \
                        patch("core.agents._resolve", return_value=["codex"]), \
                        patch("core.agents.launch", return_value=(0, completed, "")) as launch_mock:
                    CodexAgent(sandbox=sandbox).run("review or write tests", Path("."))
                launch_mock.assert_called_once()
                args = launch_mock.call_args.args[0]
                self.assertEqual(args[args.index("-s") + 1], sandbox)
                override = 'windows.sandbox="elevated"'
                self.assertIn(override, args)
                index = args.index(override)
                self.assertGreater(index, 0)
                self.assertEqual(args[index - 1], "-c")


class R17SchemaTests(unittest.TestCase):
    def assert_strict(self, original, strict):
        if original.get("type") == "object":
            self.assertIs(strict.get("additionalProperties"), False)
            properties = original.get("properties", {})
            self.assertEqual(set(strict["required"]), set(properties))
            self.assertEqual(set(strict["properties"]), set(properties))
            for key, child in properties.items():
                converted = strict["properties"][key]
                if key not in original.get("required", []):
                    self.assertIsInstance(converted["type"], list)
                    self.assertIn("null", converted["type"])
                    types = child["type"] if isinstance(child["type"], list) else [child["type"]]
                    self.assertTrue(set(types) <= set(converted["type"]))
                self.assert_strict(child, converted)
        if "items" in original:
            self.assert_strict(original["items"], strict["items"])

    def test_R17_all_conductor_schemas_are_objects(self):
        """R17: all six role schemas define typed properties for their required keys."""
        from core import bootstrap
        for name in ("S_TESTS", "S_BUILD", "S_REVIEW", "S_TROUBLE", "S_DRIFT", "S_PLAN"):
            with self.subTest(schema=name):
                schema = getattr(bootstrap, name)
                self.assertEqual(schema.get("type"), "object")
                self.assertIsInstance(schema.get("properties"), dict)
                self.assertTrue(set(schema["required"]) <= set(schema["properties"]))
                for prop in schema["properties"].values():
                    self.assertIn("type", prop)

    def test_R17_strict_schema_recursive_and_nonmutating(self):
        """R17: strict conversion covers optional objects and array items without mutation."""
        import copy
        from core import agents
        schema = {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"},
            "count": {"type": ["integer", "null"]},
            "options": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
            "rows": {"type": "array", "items": {"type": "object", "required": ["id"],
                "properties": {"id": {"type": "integer"}, "label": {"type": "string"}}}}}}
        before = copy.deepcopy(schema)
        strict = agents.strict_schema(schema)
        self.assertIsNot(strict, schema)
        self.assertEqual(schema, before)
        self.assert_strict(before, strict)
        self.assertEqual(strict["properties"]["name"]["type"], "string")
        strict["properties"]["rows"]["items"]["properties"]["id"]["type"] = "string"
        self.assertEqual(schema, before)

    def test_R17_strict_plan_schema(self):
        """R17: S_PLAN has nested task objects and is recursively strict only in its copy."""
        import copy
        from core import agents, bootstrap
        original = copy.deepcopy(bootstrap.S_PLAN)
        strict = agents.strict_schema(bootstrap.S_PLAN)
        self.assertIsNot(strict, bootstrap.S_PLAN)
        self.assertEqual(bootstrap.S_PLAN, original)
        self.assertEqual(original["properties"]["tasks"]["type"], "array")
        self.assertEqual(original["properties"]["tasks"]["items"]["type"], "object")
        self.assert_strict(original, strict)

    def test_R17_codex_output_schema_file_is_strict(self):
        """R17: the schema file consumed during Codex launch contains the strict form."""
        import copy
        from unittest.mock import patch
        schema = {"type": "object", "required": ["files"], "properties": {
            "files": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"}}}
        before = copy.deepcopy(schema)
        captured = []

        def fake_launch(args, cwd, stdin_text, timeout_s):
            captured.append(json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8")))
            Path(args[args.index("-o") + 1]).write_text('{"files":[],"summary":null}', encoding="utf-8")
            return 0, json.dumps({"type": "turn.completed", "usage": {}}), ""

        with patch("core.agents._resolve", return_value=["fake-codex"]), patch("core.agents.launch", side_effect=fake_launch):
            result = CodexAgent().run("write tests", Path("."), schema)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(schema, before)
        self.assertEqual(len(captured), 1)
        self.assert_strict(before, captured[0])

    def test_R17_codex_nonzero_exit_preserves_event_message(self):
        """R17: nonzero Codex exits expose turn.failed and error event messages."""
        from unittest.mock import patch
        message = "invalid_json_schema: schema must have type object"
        for event in ({"type": "turn.failed", "error": {"message": message}},
                      {"type": "error", "message": message}):
            with self.subTest(event=event["type"]):
                with patch("core.agents._resolve", return_value=["fake-codex"]), patch(
                        "core.agents.launch", return_value=(1, json.dumps(event), "")):
                    result = CodexAgent().run("write tests", Path("."))
                self.assertFalse(result.ok)
                self.assertIn(message, result.error)

    def test_R17_shape_check_keeps_original_required_keys(self):
        """R17: local shape checks accept omitted optional fields after strict conversion."""
        from core.agents import _shape_ok
        schema = {"type": "object", "required": ["files"], "properties": {
            "files": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"}}}
        self.assertTrue(_shape_ok({"files": []}, schema))
        self.assertFalse(_shape_ok({"summary": "missing files"}, schema))
        self.assertTrue(FakeAgent(lambda p, c: ('{"files":[]}', 1)).run("tests", Path("."), schema).ok)


class R30StrictSchemaTests(unittest.TestCase):
    def test_R30_optional_enum_accepts_null_without_mutating_input(self):
        """R30: an optional enum permits null in both its type and its enum."""
        import copy
        from core.agents import strict_schema
        schema = {"type": "object", "required": ["required_status"], "properties": {
            "status": {"type": "string", "enum": ["ready", "blocked"]},
            "required_status": {"type": "string", "enum": ["ready", "blocked"]}}}
        before = copy.deepcopy(schema)
        strict = strict_schema(schema)
        optional = strict["properties"]["status"]
        self.assertIn("null", optional["type"])
        self.assertIn(None, optional["enum"])
        self.assertEqual(set(optional["enum"]), {"ready", "blocked", None})
        self.assertEqual(strict["properties"]["required_status"]["enum"], ["ready", "blocked"])
        self.assertEqual(schema, before)

    def test_R30_anyof_objects_are_converted_recursively(self):
        """R30: anyOf object branches become strict, including nested array items."""
        import copy
        from core.agents import strict_schema
        schema = {"anyOf": [{"type": "object", "properties": {
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}}}}}}, {"type": "null"}]}
        before = copy.deepcopy(schema)
        strict = strict_schema(schema)
        branch = strict["anyOf"][0]
        self.assertIs(branch.get("additionalProperties"), False)
        self.assertEqual(branch["required"], ["rows"])
        self.assertIn("null", branch["properties"]["rows"]["type"])
        item = branch["properties"]["rows"]["items"]
        self.assertIs(item.get("additionalProperties"), False)
        self.assertEqual(item["required"], ["label"])
        self.assertIn("null", item["properties"]["label"]["type"])
        self.assertEqual(schema, before)

    def test_R30_defs_and_definitions_are_converted_recursively(self):
        """R30: both definition containers recursively convert objects and enums."""
        import copy
        from core.agents import strict_schema
        for container in ("$defs", "definitions"):
            with self.subTest(container=container):
                schema = {container: {"Choice": {"type": "object", "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                    "child": {"anyOf": [{"type": "object", "properties": {
                        "value": {"type": "integer"}}}, {"type": "null"}]}}}},
                    "$ref": f"#/{container}/Choice"}
                before = copy.deepcopy(schema)
                strict = strict_schema(schema)
                choice = strict[container]["Choice"]
                self.assertIs(choice.get("additionalProperties"), False)
                self.assertEqual(set(choice["required"]), {"status", "child"})
                self.assertIn(None, choice["properties"]["status"]["enum"])
                nested = choice["properties"]["child"]["anyOf"][0]
                self.assertIs(nested.get("additionalProperties"), False)
                self.assertEqual(nested["required"], ["value"])
                self.assertEqual(strict["$ref"], schema["$ref"])
                self.assertEqual(schema, before)


class R34NullOptionalTests(unittest.TestCase):
    def answer(self, path, payload, schema):
        from unittest.mock import patch
        text = json.dumps(payload)
        if path == "fake":
            return FakeAgent(lambda p, c: (text, 1)).run("answer", Path("."), schema)

        def fake_launch(args, cwd, stdin_text, timeout_s):
            Path(args[args.index("-o") + 1]).write_text(text, encoding="utf-8")
            return 0, json.dumps({"type": "turn.completed", "usage": {}}), ""

        with patch("core.agents._resolve", return_value=["fake-codex"]), patch(
                "core.agents.launch", side_effect=fake_launch):
            return CodexAgent().run("answer", Path("."), schema)

    def test_R34_null_optional_summary_is_absent_and_valid(self):
        from core.agents import _shape_ok, schema_ok
        from core.bootstrap import S_TESTS
        for path in ("fake", "codex"):
            with self.subTest(path=path):
                result = self.answer(path, {"files": ["smoke.txt"], "summary": None}, S_TESTS)
                self.assertTrue(result.ok, result.error)
                self.assertTrue(_shape_ok(result.data, S_TESTS))
                with self.subTest(check="conductor smoke schema"):
                    self.assertTrue(schema_ok(result.data, S_TESTS))
                self.assertEqual(result.data["files"], ["smoke.txt"])
                self.assertNotIn("summary", result.data)

    def test_R34_null_required_files_still_fails_validation(self):
        from core.agents import schema_ok
        from core.bootstrap import S_TESTS
        for path in ("fake", "codex"):
            with self.subTest(path=path):
                result = self.answer(path, {"files": None}, S_TESTS)
                # Either the agent rejects the answer or the conductor's full
                # schema check does; null must never become a valid files list.
                self.assertFalse(result.ok and schema_ok(result.data, S_TESTS))
                if result.data is not None:
                    self.assertIn("files", result.data)
                    self.assertIsNone(result.data["files"])

    def test_R34_nested_optional_nulls_are_removed_recursively(self):
        import copy
        from core.agents import _shape_ok, schema_ok
        # S_PLAN's task fields are all required. Use an object inside an array
        # with a further nested object to exercise optional fields at both levels.
        schema = {"type": "object", "required": ["tasks"], "properties": {
            "tasks": {"type": "array", "items": {"type": "object", "required": ["id", "details"],
                "properties": {"id": {"type": "string"}, "summary": {"type": "string"},
                    "details": {"type": "object", "required": ["name"], "properties": {
                        "name": {"type": "string"}, "note": {"type": "string"}}}}}}}}
        payload = {"tasks": [{"id": "T1", "summary": None, "details": {"name": "one", "note": None}},
                             {"id": "T2", "summary": "keep", "details": {"name": "two", "note": "keep"}}]}
        expected = {"tasks": [{"id": "T1", "details": {"name": "one"}}, payload["tasks"][1]]}
        original_schema = copy.deepcopy(schema)
        for path in ("fake", "codex"):
            with self.subTest(path=path):
                result = self.answer(path, payload, schema)
                self.assertTrue(result.ok, result.error)
                self.assertTrue(_shape_ok(result.data, schema))
                with self.subTest(check="nested schema"):
                    self.assertTrue(schema_ok(result.data, schema))
                self.assertEqual(result.data, expected)
                self.assertEqual(schema, original_schema)

    def test_R34_nested_required_null_still_fails_validation(self):
        from core.agents import schema_ok
        schema = {"type": "object", "required": ["tasks"], "properties": {
            "tasks": {"type": "array", "items": {"type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}, "summary": {"type": "string"}}}}}}
        for path in ("fake", "codex"):
            with self.subTest(path=path):
                result = self.answer(path, {"tasks": [{"id": None, "summary": None}]}, schema)
                self.assertFalse(result.ok and schema_ok(result.data, schema))
