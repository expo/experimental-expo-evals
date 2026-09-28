import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "voice_recorder_verifier", ROOT / "tasks/ios-voice-recorder/tests/verify.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class VoiceRecorderEvidenceTest(unittest.TestCase):
    def test_source_excludes_secrets_dependencies_and_external_links(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "app.tsx").write_text("export const App = () => null;")
            (project / ".env.json").write_text("secret-env")
            (project / "node_modules").mkdir()
            (project / "node_modules/vendor.js").write_text("vendor-source")
            (project / "linked.ts").symlink_to(project / ".env.json")
            result = verifier.source_snapshot(project)
            self.assertIn("export const App = () => null;", result)
            self.assertNotIn("secret-env", result)
            self.assertNotIn("vendor-source", result)
            self.assertIn("OMITTED source files", result)
            self.assertIn("linked.ts", result)

    def test_budget_reports_whole_file_omissions_without_truncating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "a.ts").write_text("small-file")
            (project / "b.ts").write_text("large-file" * 100)
            result = verifier.source_snapshot(project, limit=20)
            self.assertIn("small-file", result)
            self.assertNotIn("large-file", result)
            self.assertIn("OMITTED source files (budget, links, or unreadable): b.ts", result)

    def test_missing_app_is_not_reported_as_source_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn("No source files found.", verifier.source_snapshot(Path(directory) / "missing"))


if __name__ == "__main__":
    unittest.main()
