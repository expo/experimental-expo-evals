import importlib.util
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_cli_navigation", ROOT / "tasks/agent-cli-ios-navigation/tests/verify.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def result(data, exit_code=0):
    return {"command": [], "exit_code": exit_code, "timed_out": False, "data": data}


class ContactApp:
    """A tool-response fixture which changes its tree when the flow acts on it."""

    def __init__(self, hardcode_link=False):
        self.contact_id = None
        self.name = ""
        self.typed_names = []
        self.hardcode_link = hardcode_link

    def __call__(self, label, args, timeout=20):
        action = args[0]
        if action == "smoke":
            return result({"ok": True, "platform": "ios", "deviceBackend": "local-ios",
                           "projectRootMatched": True, "runtimeSupported": True,
                           "routeCheck": {"checked": True, "ok": True}})
        if action == "runtime:tree":
            nodes = ([{"testID": "contact-name-input", "text": self.name},
                      {"testID": "open-contact", "text": "Open contact"}]
                     if self.contact_id is None else
                     [{"testID": "contact-id", "text": self.contact_id},
                      {"testID": "contact-name", "text": self.name},
                      {"testID": "back-home", "text": "Back to form"}])
            return result({"ok": True, "allScreens": False, "nodes": nodes,
                           "truncated": False, "focusedScreen": "contact/[id]" if self.contact_id else "index"})
        if action == "runtime:type":
            self.name = args[1]
            self.typed_names.append(self.name)
        elif action == "runtime:tap":
            self.contact_id = "42" if args[1] == "open-contact" else None
        elif action == "navigate":
            url = urlparse(args[1])
            if url.path == "/":
                self.contact_id = None
            else:
                self.contact_id = "7" if self.hardcode_link else url.path.split("/")[-1]
                self.name = "Grace" if self.hardcode_link else parse_qs(url.query)["name"][0]
            return result({"attached": True, "deviceBackend": "local-ios",
                           "routeCheck": {"checked": True, "ok": True}})
        else:
            raise AssertionError(f"Unexpected command: {args}")
        return result({"ok": True, "called": True, "threw": None, "forced": False})


class NavigationVerifierTest(unittest.TestCase):
    def test_checks_live_changes_for_distinct_values_and_returned_form(self):
        app = ContactApp()
        checks = verifier.check_flow(app)
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual(len(set(app.typed_names)), 2)
        self.assertEqual(app.contact_id, "42")

    def test_hardcoded_example_does_not_pass_the_second_deep_link(self):
        checks = verifier.check_flow(ContactApp(hardcode_link=True))
        self.assertTrue(checks["direct_link"])
        self.assertFalse(checks["different_parameters"])

    def test_echoed_parameters_are_not_displayed_text(self):
        answer = result({"ok": True, "allScreens": False, "nodes": [], "truncated": False, "focusedScreen": "index",
                         "testID": "contact-id", "text": "42", "route": "/contact/42"})
        self.assertFalse(verifier.tree_has(answer, "contact-id", "42"))
        answer["data"]["nodes"] = [{"testID": "contact-id", "text": "7"}]
        self.assertFalse(verifier.tree_has(answer, "contact-id", "42"))

    def test_offscreen_matches_and_failed_commands_do_not_pass(self):
        answer = result({"ok": True, "allScreens": True,
                         "truncated": False, "focusedScreen": "contact/[id]",
                         "nodes": [{"testID": "contact-id", "text": "42"}]})
        self.assertFalse(verifier.tree_has(answer, "contact-id", "42"))
        answer["data"]["allScreens"] = False
        self.assertTrue(verifier.tree_has(answer, "contact-id", "42"))
        answer["exit_code"] = 20
        self.assertFalse(verifier.tree_has(answer, "contact-id", "42"))

    def test_partial_or_unfocused_tree_and_unchecked_route_are_inconclusive(self):
        answer = result({"ok": True, "allScreens": False, "truncated": True,
                         "focusedScreen": "contact/[id]",
                         "nodes": [{"testID": "contact-id", "text": "42"}]})
        self.assertFalse(verifier.tree_has(answer, "contact-id", "42"))
        answer["data"].update(truncated=False, focusedScreen=None)
        self.assertFalse(verifier.tree_has(answer, "contact-id", "42"))
        navigation = result({"attached": True, "deviceBackend": "local-ios", "routeCheck": None})
        self.assertFalse(verifier.route_opened(navigation))

    def test_tool_arguments_are_complete_without_raw_duplicate_or_observations(self):
        arguments = {"command": "x" * 50000}
        calls = verifier.agent_tool_calls({"steps": [
            {"source": "system", "tool_calls": [{"arguments": {"command": "expo start"}}]},
            {"source": "agent", "step_id": 2,
             "tool_calls": [{"function_name": "Bash", "arguments": arguments,
                             "extra": {"raw_arguments": str(arguments)}}],
             "observation": {"content": "internal expo command"}},
        ]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], arguments)
        self.assertNotIn("extra", calls[0])
        self.assertNotIn("observation", calls[0])

    def test_source_snapshot_reports_omissions_and_keeps_config(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true}}')
            (project / "src").mkdir()
            (project / "src/large.tsx").write_text("x" * 500)
            source = verifier.source_snapshot(project, limit=100)
            self.assertIn('"strict":true', source)
            self.assertIn("Omitted files: src/large.tsx", source)
            self.assertNotIn("x" * 100, source)


if __name__ == "__main__":
    unittest.main()
