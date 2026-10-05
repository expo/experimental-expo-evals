import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_cli_verifier", ROOT / "tasks/agent-cli-ios-basic/tests/verify.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class AgentCliEvidenceTest(unittest.TestCase):
    def test_only_agent_tool_arguments_are_exported_without_truncation(self):
        long_command = "echo '" + "x" * 50000 + "'"
        calls = verifier.agent_tool_calls({"steps": [
            {"source": "system", "tool_calls": [{"arguments": {"command": "npx expo install"}}]},
            {"source": "agent", "step_id": 2, "tool_calls": [
                {"function_name": "Bash", "arguments": {"command": long_command}},
                {"function_name": "exec_command", "arguments": {"cmd": "bunx @expo/agent-cli dev --ios"}},
            ], "observation": {"results": [{"content": "Internal command: npx expo start"}]}},
            {"source": "agent", "message": "I could run npx expo start", "tool_calls": None},
        ]})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["arguments"]["command"], long_command)
        self.assertEqual(calls[1]["step_id"], 2)
        self.assertNotIn("observation", calls[0])

    def test_screenshot_evidence_distinguishes_missing_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "screenshot.png"
            self.assertFalse(verifier.screenshot_evidence(path)["exists"])
            path.write_bytes(b"not a png")
            self.assertFalse(verifier.screenshot_evidence(path)["png_signature"])
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"example")
            evidence = verifier.screenshot_evidence(path)
            self.assertTrue(evidence["png_signature"])
            self.assertGreater(evidence["bytes"], 8)


if __name__ == "__main__":
    unittest.main()
