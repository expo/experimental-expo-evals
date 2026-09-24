import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("runner_service", Path(__file__).with_name("service.py"))
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def payload():
    return {
        "runId": "run-123", "taskId": "coffee-timer", "commitSha": "a" * 40,
        "agent": "claude-code", "model": "anthropic/claude-opus-5-5",
        "files": [{"path": f"tasks/coffee-timer/{name}", "content": "example\n"} for name in sorted(service.TASK_FILES)],
    }


class RunnerTests(unittest.TestCase):
    def test_rejects_escape_duplicate_and_unpinned_inputs(self):
        original = payload()
        self.assertEqual(service.validate_payload(original), original)
        for mutate in (
            lambda p: p["files"][0].update(path="../../.env"),
            lambda p: p["files"][0].update(path=p["files"][1]["path"]),
            lambda p: p.update(commitSha="main"),
            lambda p: p.update(runId="../elsewhere"),
            lambda p: p.update(agent="arbitrary-host-program"),
        ):
            value = copy.deepcopy(original)
            mutate(value)
            with self.assertRaises(ValueError):
                service.validate_payload(value)

    def test_launch_is_idempotent_and_rejects_changed_payload(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"RUN_ID": "run-123"}), patch.object(service.threading.Thread, "start") as start:
            runner = service.Runner(Path(temporary))
            value = payload()
            self.assertEqual(runner.accept(value)["status"], "running")
            runner.accept(value)
            self.assertEqual(start.call_count, 1)
            value["model"] = "other"
            with self.assertRaises(ValueError):
                runner.accept(value)

    def test_uses_e2b_and_one_trial(self):
        config = service.make_config(payload())
        self.assertEqual(config["n_attempts"], 1)
        self.assertEqual(config["n_concurrent_trials"], 1)
        self.assertEqual(config["environment"]["import_path"], "e2b_environment:OneHourE2B")
        self.assertEqual(config["tasks"], [{"path": "tasks/coffee-timer"}])

    def test_failed_cli_cannot_be_reported_as_pass_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-value-123"}):
            base = Path(temporary)
            trial = base / "jobs/run-123/trial"
            (trial / "verifier").mkdir(parents=True)
            (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1}}, "agent_result": {"cost_usd": 0.1}}))
            (base / "run.log").write_text("key=secret-value-123")
            detail = service.build_detail(base, payload(), service.now(), 1)
            self.assertEqual(detail["status"], "error")
            self.assertEqual(detail["score"], 1)
            self.assertNotIn("secret-value-123", json.dumps(detail))
            self.assertIn("[redacted]", detail["log"])
            completed = service.build_detail(base, payload(), service.now(), 0)
            self.assertEqual(completed["status"], "passed")

    def test_only_overall_reward_is_a_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            trial = base / "jobs/run-123/trial"
            trial.mkdir(parents=True)
            (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"screenshot_exists": 1}}}))
            detail = service.build_detail(base, payload(), service.now(), 0)
            self.assertEqual(detail["status"], "error")
            self.assertIsNone(detail["score"])

    def test_artifacts_preserve_snapshot_and_skip_secret_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"EXPO_TOKEN": "expo-token-secret"}):
            base = Path(temporary)
            directory = base / "jobs/run-123/trial/artifacts"
            directory.mkdir(parents=True)
            (directory / "source.ts").write_text("expo-token-secret")
            (directory / ".env").write_text("hidden")
            (directory / "credentials.json").write_text("private")
            (directory / "linked").symlink_to("/etc/hosts")
            (directory / "coffee.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (base / "harbor.yaml").write_text("configuration")
            files = service.output_files(base, payload())
            paths = [item[0] for item in files]
            self.assertIn("harbor.yaml", paths)
            self.assertTrue(paths[0].endswith("coffee.png"))
            self.assertFalse(any(path.endswith((".env", "credentials.json", "linked")) for path in paths))
            self.assertEqual(next(data for path, data, _ in files if path.endswith("source.ts")), b"[redacted]")

    def test_executes_cli_from_snapshot_and_publishes_before_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            executable = base / "fake-harbor"
            executable.write_text(f"#!{sys.executable}\n" + """
import json, os
from pathlib import Path
assert not os.environ.get('CALLBACK_TOKEN')
assert not os.environ.get('CALLBACK_URL')
assert os.environ['ANTHROPIC_API_KEY'] == 'test-model-secret'
assert Path('tasks/coffee-timer/tests/rubric.toml').read_text() == 'example\\n'
trial = Path('jobs/run-123/trial')
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({'verifier_result': {'rewards': {'reward': 1}}}))
print('model key=test-model-secret')
""")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"HARBOR_BIN": str(executable), "ANTHROPIC_API_KEY": "test-model-secret", "CALLBACK_URL": "https://example.test/callback", "CALLBACK_TOKEN": "test-callback-secret"}), patch.object(service.Callback, "send") as send:
                runner = service.Runner(base / "work")
                runner.execute(payload())
                self.assertEqual(runner.status, "passed")
                self.assertEqual(send.call_args_list[0].args[0]["status"], "running")
                final = send.call_args_list[-1].args[0]
                self.assertEqual(final["status"], "completed")
                self.assertTrue(any(item["path"].endswith("rubric.toml") for item in final["detail"]["artifacts"]))
                self.assertNotIn("test-model-secret", json.dumps(final))
                self.assertIn("[redacted]", final["detail"]["log"])

    def test_callback_destination_cannot_come_from_payload(self):
        with patch.dict(os.environ, {"CALLBACK_URL": "https://evals.example/api/runner-callback/run-123", "CALLBACK_TOKEN": "callback-secret"}):
            callback = service.Callback()
            self.assertEqual(callback.url, "https://evals.example/api/runner-callback/run-123")
            self.assertNotIn("callbackUrl", service.make_config(payload()))


if __name__ == "__main__":
    unittest.main()
