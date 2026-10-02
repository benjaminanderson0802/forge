"""Claude agent runs load no MCP servers (faster start, no helper processes or windows)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import agents


class StrictMcpTests(unittest.TestCase):
    def test_claude_command_skips_user_mcp_servers(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("core.agents._resolve", return_value=["fake-claude"]), \
                patch("core.agents.launch") as launch:
            launch.return_value = (0, '{"result": "ok"}', "")
            agents.ClaudeAgent().run("hi", Path(tmp))
        args = launch.call_args[0][0]
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--mcp-config", args)


if __name__ == "__main__":
    unittest.main()
