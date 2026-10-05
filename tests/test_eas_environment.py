import asyncio
import base64
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from importlib.resources import files
from pathlib import Path
from unittest.mock import AsyncMock, patch

from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.config import ResourceMode
from harbor.models.trial.paths import TrialPaths

from environments.eas.environment import EASWorkflowEnvironment

RPC_SCRIPT = files("environments.eas").joinpath("rpc.py")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment_patch = patch.dict(os.environ, {"EXPO_TOKEN": "production-token"})
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = self.root / "environment"
        self.environment.mkdir()
        (self.environment / "setup.sh").write_text("#!/bin/bash\ntrue\n")

    def adapter(self, **kwargs):
        return EASWorkflowEnvironment(
            environment_dir=self.environment,
            environment_name="example",
            session_id="example__env",
            trial_paths=TrialPaths(self.root / "trial"),
            task_env_config=EnvironmentConfig(),
            project_id="4eddbf65-47d6-4fa4-b91d-eedda3b11893",
            **kwargs,
        )

    def test_stage_selects_only_its_credential_and_endpoint(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "EXPO_TOKEN": "production-token",
                "EXPO_TOKEN_STAGING": "staging-token",
                "EXPO_STAGING": "inherited-stage",
                "EXPO_LOCAL": "1",
                "ANTHROPIC_API_KEY": "model-secret",
                "TYPESAFE_API_KEY": "judge-secret",
            },
            clear=True,
        ):
            production = self.adapter()._cli_environment()
            staging = self.adapter(stage="staging")._cli_environment()
        self.assertEqual(production["EXPO_TOKEN"], "production-token")
        self.assertNotIn("EXPO_STAGING", production)
        self.assertEqual(staging["EXPO_TOKEN"], "staging-token")
        self.assertEqual(staging["EXPO_STAGING"], "1")
        for environment in (production, staging):
            self.assertNotIn("EXPO_LOCAL", environment)
            self.assertNotIn("EXPO_TOKEN_STAGING", environment)
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("TYPESAFE_API_KEY", environment)

    def test_staging_never_falls_back_to_production_credential(self):
        with patch.dict(os.environ, {"EXPO_TOKEN": "production-token"}, clear=True):
            with self.assertRaisesRegex(ValueError, "requires EXPO_TOKEN_STAGING"):
                self.adapter(stage="staging")._cli_environment()

    def test_invalid_stage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "production or staging"):
            self.adapter(stage="unknown")

    def test_invalid_lifetime_is_rejected(self):
        for timeout in (0, -1, True, 1.5, "3600", None, 2 * 60 * 60 + 1):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "timeout_sec"):
                self.adapter(timeout_sec=timeout)

    def test_diagnostics_preserve_cause_and_strip_terminal_controls_before_redaction(self):
        adapter = self.adapter()
        adapter._persistent_env["TASK_TOKEN"] = "private-task-token"
        message = adapter._diagnostic(
            "EAS HTTPS connection was lost: " + "banner\n" * 500
            + "\x1b]0;terminal title\x07\x1b[31mTimeout\x1b[0m "
            + "production-\x1b[1mtoken\x1b[0m private-task-token\r\x08\x00"
        )
        self.assertTrue(message.startswith("EAS HTTPS connection was lost:"))
        self.assertTrue(message.endswith("Timeout [redacted] [redacted]"))
        self.assertLessEqual(len(message), 2000)
        self.assertNotIn("terminal title", message)
        self.assertNotRegex(message, r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

    def test_preflight_allows_staging_credentials_and_explicit_project_id(self):
        with (
            patch.dict(os.environ, {"EXPO_TOKEN_STAGING": "staging-token"}, clear=True),
            patch("shutil.which", return_value="/tool"),
        ):
            EASWorkflowEnvironment.preflight()

    async def test_missing_selected_credential_fails_before_upload(self):
        adapter = self.adapter(stage="staging")
        adapter._cli = AsyncMock()
        with patch.dict(os.environ, {"EXPO_TOKEN": "production-token"}, clear=True):
            with self.assertRaisesRegex(ValueError, "EXPO_TOKEN_STAGING"):
                await adapter.start()
        adapter._cli.assert_not_called()
        self.assertIsNone(adapter._bundle)

    def test_rejects_unenforceable_network_and_resource_policies(self):
        with self.assertRaises(ValueError):
            self.adapter(
                network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
            )
        with self.assertRaises(ValueError):
            self.adapter(cpu_enforcement_policy=ResourceMode.LIMIT)

    def test_task_only_requires_setup_script(self):
        self.adapter()
        (self.environment / "setup.sh").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "environment/setup.sh"):
            self.adapter()

    def test_rejects_docker_image_override_for_macos(self):
        with self.assertRaisesRegex(ValueError, "provider workflow image"):
            EASWorkflowEnvironment(
                environment_dir=self.environment,
                environment_name="example",
                session_id="example__env",
                trial_paths=TrialPaths(self.root / "trial"),
                task_env_config=EnvironmentConfig(docker_image="node:24"),
            )

    async def test_bundle_only_contains_allowed_environment_sources(self):
        (self.environment / ".env").write_text("SECRET=not-for-upload")
        (self.environment / "Dockerfile").write_text("FROM node:24")
        (self.environment / "workflow.yml").write_text("UNTRUSTED_WORKFLOW=not-for-upload")
        app = self.environment / "app"
        app.mkdir()
        (app / ".env.local").write_text("SECRET=not-for-upload")
        for name in (".git", "node_modules", ".venv", "__pycache__"):
            (app / name).mkdir()
            (app / name / "ignored").write_text("SECRET=not-for-upload")
        adapter = self.adapter()
        root = adapter._prepare_bundle()
        self.assertEqual(
            Path(adapter._cli_environment()["EAS_PROJECT_ROOT"]), root.resolve()
        )
        self.assertEqual(
            {p.name for p in (root / "environment").iterdir()},
            {"setup.sh", "app"},
        )
        self.assertEqual(list((root / "environment/app").iterdir()), [])
        self.assertEqual(
            (root / ".eas/workflows/harbor.yml").read_bytes(),
            files("environments.eas").joinpath("workflow.yml").read_bytes(),
        )
        self.assertEqual((root / "harbor_rpc.py").read_bytes(), RPC_SCRIPT.read_bytes())
        self.assertNotIn("environments.eas.rpc", sys.modules)
        self.assertNotIn(
            "SECRET", "".join(p.read_text() for p in root.rglob("*") if p.is_file())
        )
        self.assertNotIn(
            "UNTRUSTED_WORKFLOW", "".join(p.read_text() for p in root.rglob("*") if p.is_file())
        )
        self.assertEqual(
            json.loads((root / "app.json").read_text())["expo"]["extra"]["eas"][
                "projectId"
            ],
            adapter.project_id,
        )
        await adapter.stop()
        self.assertFalse(root.exists())

    async def test_bundle_preserves_nested_fixtures_relative_to_setup(self):
        fixture = self.environment / "app/app/(tabs)/index.tsx"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("export default function Screen() { return null; }\n")
        (self.environment / "setup.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\n'
            'source_dir="$(cd "$(dirname "$0")" && pwd)"\n'
            'cat "$source_dir/app/app/(tabs)/index.tsx"\n'
        )
        adapter = self.adapter()
        try:
            root = adapter._prepare_bundle()
            self.assertEqual(
                (root / "environment/app/app/(tabs)/index.tsx").read_text(),
                fixture.read_text(),
            )
            result = subprocess.run(
                ["bash", str(root / "environment/setup.sh")],
                cwd=self.root, capture_output=True, text=True, check=True,
            )
            self.assertEqual(result.stdout, fixture.read_text())
        finally:
            await adapter.stop()

    async def test_bundle_rejects_symlinked_setup(self):
        (self.root / "outside.sh").write_text("must not be copied")
        (self.environment / "setup.sh").unlink()
        (self.environment / "setup.sh").symlink_to(self.root / "outside.sh")
        adapter = self.adapter()
        try:
            with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
                adapter._prepare_bundle()
        finally:
            await adapter.stop()

    async def test_bundle_rejects_nested_symlinks_and_special_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "fixture.tsx").write_text("must not be copied")
        app = self.environment / "app"
        app.mkdir()
        for name, target in (("file", outside / "fixture.tsx"), ("directory", outside)):
            with self.subTest(name=name):
                link = app / name
                link.symlink_to(target)
                adapter = self.adapter()
                try:
                    with self.assertRaisesRegex(ValueError, f"cannot be a symlink: app/{name}"):
                        adapter._prepare_bundle()
                finally:
                    await adapter.stop()
                    link.unlink()
        os.mkfifo(app / "pipe")
        adapter = self.adapter()
        try:
            with self.assertRaisesRegex(ValueError, "must be a regular file or directory: app/pipe"):
                adapter._prepare_bundle()
        finally:
            await adapter.stop()

    async def test_bundle_limits_individual_files_and_total_source_size(self):
        (self.environment / "large.tsx").write_bytes(b"x" * 33)
        adapter = self.adapter()
        try:
            with patch("environments.eas.environment.MAX_ENVIRONMENT_FILE_BYTES", 32):
                with self.assertRaisesRegex(ValueError, "source is too large: large.tsx"):
                    adapter._prepare_bundle()
        finally:
            await adapter.stop()
        (self.environment / "second.tsx").write_bytes(b"x" * 33)
        adapter = self.adapter()
        try:
            with patch("environments.eas.environment.MAX_ENVIRONMENT_BYTES", 64):
                with self.assertRaisesRegex(ValueError, "bundle limit"):
                    adapter._prepare_bundle()
        finally:
            await adapter.stop()

    async def test_process_timeout_identifies_the_command(self):
        with self.assertRaisesRegex(TimeoutError, "sleep 1 timed out after"):
            await self.adapter()._process(["sleep", "1"], timeout=0.01)

    async def test_cancel_interrupts_remote_command_but_keeps_vm_for_verifier(self):
        adapter = self.adapter()
        adapter._rpc = AsyncMock(side_effect=[asyncio.CancelledError(), {}])
        adapter.stop = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await adapter.exec("sleep 100")
        first, second = adapter._rpc.call_args_list
        self.assertEqual(second.args[0], {"op": "cancel", "id": first.args[0]["id"]})
        adapter.stop.assert_not_called()

    async def test_exec_uses_sandbox_lifetime_unless_command_timeout_is_explicit(self):
        adapter = self.adapter(timeout_sec=7200)
        adapter._rpc = AsyncMock(return_value={"return_code": 0})
        await adapter.exec("claude --print task")
        self.assertEqual(adapter._rpc.call_args.args[0]["timeout"], 7200)
        self.assertEqual(adapter._rpc.call_args.kwargs["timeout"], 7260)
        await adapter.exec("true", timeout_sec=100)
        self.assertEqual(adapter._rpc.call_args.args[0]["timeout"], 100)
        self.assertEqual(adapter._rpc.call_args.kwargs["timeout"], 160)

    async def test_cancel_failure_stops_vm(self):
        adapter = self.adapter()
        adapter._rpc = AsyncMock(
            side_effect=[
                asyncio.CancelledError(),
                RuntimeError("transport unavailable"),
            ]
        )
        adapter.stop = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await adapter.exec("sleep 100")
        adapter.stop.assert_awaited_once_with(delete=True)

    async def test_empty_dirs_uses_alias_aware_operation(self):
        adapter = self.adapter()
        adapter._rpc = AsyncMock(return_value={"return_code": 0})
        await adapter.empty_dirs(["/tests"])
        self.assertEqual(
            adapter._rpc.call_args.args[0],
            {
                "op": "reset",
                "remove": ["/tests"],
                "create": ["/tests"],
                "chmod": ["/tests"],
            },
        )

    async def test_rejects_directory_download_that_escapes_destination(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("../escaped")
            member.size = 3
            stream.addfile(member, io.BytesIO(b"bad"))
        adapter = self.adapter()
        adapter._rpc = AsyncMock(
            return_value={"data": base64.b64encode(archive.getvalue()).decode()}
        )
        with self.assertRaises(tarfile.FilterError):
            await adapter.download_dir("/app", self.root / "download")
        self.assertFalse((self.root / "escaped").exists())


    async def test_registration_scopes_worker_token_and_does_not_upload_controller_credentials(self):
        adapter = self.adapter()
        with patch.dict(os.environ, {
            "HARBOR_EAS_CONTROLLER_URL": "http://127.0.0.1:8080/eas",
            "HARBOR_EAS_CONTROLLER_TOKEN": "controller-" + "x" * 40,
            "HARBOR_EAS_PUBLIC_URL": "https://example.com/api/internal/eas/run-123",
        }), patch("environments.eas.environment.Bridge.request", new_callable=AsyncMock) as request:
            await adapter._register_bridge()
            root = adapter._prepare_bundle()
            config = json.loads((root / "bridge.json").read_text())
            self.assertEqual(config["token"], adapter._worker_token)
            self.assertTrue(config["url"].endswith(adapter._bridge_id))
            self.assertNotIn("controller-", (root / "bridge.json").read_text())
            self.assertEqual((root / "bridge.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(request.call_args.args[2]["worker_token"], config["token"])
            await adapter.stop()

    async def test_extended_lifetime_reaches_relay_and_worker_and_still_cleans_up(self):
        adapter = self.adapter(timeout_sec=7200)
        with patch.dict(os.environ, {
            "HARBOR_EAS_CONTROLLER_URL": "http://127.0.0.1:8080/eas",
            "HARBOR_EAS_CONTROLLER_TOKEN": "controller-" + "x" * 40,
            "HARBOR_EAS_PUBLIC_URL": "https://example.com/api/internal/eas/run-123",
        }), patch("environments.eas.environment.Bridge.request", new_callable=AsyncMock) as request, patch("environments.eas.environment.time.time", return_value=1000):
            await adapter._register_bridge()
            root = adapter._prepare_bundle()
            config = json.loads((root / "bridge.json").read_text())
            self.assertEqual(config["expires_at"], 8200)
            self.assertEqual(request.call_args.args[2]["ttl_sec"], 7200)
            adapter.workflow_run_id = "workflow-id"
            adapter._cli = AsyncMock(side_effect=[{"status": "IN_PROGRESS"}, {"status": "CANCELED"}])
            adapter._process = AsyncMock()
            await adapter.stop()
            self.assertEqual(request.call_args.args[:2], ("DELETE", "/" + adapter._bridge_id))
            self.assertEqual(adapter._process.call_args.args[0][1:3], ["workflow:cancel", "workflow-id"])
            self.assertFalse(root.exists())
            self.assertFalse(adapter._bridge_registered)

    async def test_start_uses_plain_workflow_and_cleans_up_after_completion(self):
        adapter = self.adapter()
        adapter._register_bridge = AsyncMock()
        adapter._cli = AsyncMock(side_effect=[
            {"owner": "example", "slug": "evals"}, {"id": "workflow-id"},
            {"status": "SUCCESS"}, {"status": "SUCCESS"},
        ])
        adapter._rpc = AsyncMock(return_value={"ready": True})
        adapter._process = AsyncMock()
        await adapter.start()
        self.assertEqual(adapter.workflow_run_id, "workflow-id")
        self.assertEqual(adapter._cli.call_args_list[1].args, ("workflow:run", ".eas/workflows/harbor.yml"))
        self.assertEqual(adapter._rpc.call_args.args, ({"op": "ready"},))
        await adapter.stop()
        adapter._process.assert_not_called()
        self.assertIsNone(adapter._bundle)

    async def test_bridge_failure_during_shutdown_still_cancels_eas(self):
        adapter = self.adapter()
        adapter.workflow_run_id = "workflow-id"
        adapter._bridge_registered = True
        adapter._bridge = type("FakeBridge", (), {"request": AsyncMock(side_effect=RuntimeError("offline"))})()
        adapter._cli = AsyncMock(side_effect=[{"status": "IN_PROGRESS"}, {"status": "CANCELED"}])
        adapter._process = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await adapter.stop()
        self.assertEqual(adapter._process.call_args.args[0][1:3], ["workflow:cancel", "workflow-id"])
        self.assertIsNone(adapter.workflow_run_id)

    async def test_https_waits_for_result_and_acknowledges_without_resubmitting(self):
        adapter = self.adapter()
        request = AsyncMock(side_effect=[(202,b""), (204,b""), (200,b'{"return_code":7,"stdout":"done"}'), (204,b"")])
        adapter._bridge = type("FakeBridge", (), {"request": request})()
        adapter._bridge_registered = True
        with patch("environments.eas.environment.asyncio.sleep", new_callable=AsyncMock):
            result = await adapter.exec("printf done; exit 7")
        self.assertEqual(result.return_code, 7)
        self.assertEqual(result.stdout, "done")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST","GET","GET","DELETE"])
        self.assertEqual(request.call_args_list[0].args[1], request.call_args_list[-1].args[1])

    async def test_agent_overlays_stay_out_of_eas_cli_and_reach_only_exec(self):
        adapter = self.adapter()
        adapter._rpc = AsyncMock(return_value={"return_code": 0})
        with adapter.scoped_exec_env({"AGENT_SECRET": "private-model-key"}):
            await adapter.exec("true", env={"EXPLICIT": "value"})
        self.assertEqual(adapter._rpc.call_args.args[0]["env"]["AGENT_SECRET"], "private-model-key")
        self.assertNotIn("AGENT_SECRET", adapter._cli_environment())


class RemoteProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def request(self, payload, *, env=None):
        result = subprocess.run(
            [sys.executable, str(RPC_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=True,
        )
        return json.loads(result.stdout)

    def command(self, command, **kwargs):
        return {
            "op": "exec",
            "id": str(uuid.uuid4()),
            "command": command,
            "cwd": str(self.root),
            "timeout": 3,
            **kwargs,
        }

    def test_exec_preserves_output_exit_and_explicit_environment_only(self):
        response = self.request(
            self.command(
                'printf "%s:%s" "$EXPLICIT" "${CONTROLLER_SECRET-unset}"; printf error >&2; exit 9',
                env={"EXPLICIT": "hello"},
            ),
            env={**os.environ, "CONTROLLER_SECRET": "must-not-leak"},
        )
        self.assertEqual(
            response, {"stdout": "hello:unset", "stderr": "error", "return_code": 9}
        )

    def test_timeout_kills_process_group(self):
        marker = self.root / "unexpected"
        response = self.request(
            self.command(f'(sleep 1; touch "{marker}") & wait', timeout=0.05)
        )
        self.assertEqual(response, {"timeout": True})
        import time

        time.sleep(1.1)
        self.assertFalse(marker.exists())

    def test_cancel_before_execution_prevents_command(self):
        command = self.command("touch must-not-exist")
        self.request({"op": "cancel", "id": command["id"]})
        try:
            self.assertEqual(self.request(command), {"timeout": True})
            self.assertFalse((self.root / "must-not-exist").exists())
        finally:
            Path("/tmp/harbor-command-" + command["id"] + ".canceled").unlink(
                missing_ok=True
            )

    def test_cancel_kills_running_command_group(self):
        command = self.command("sleep 30 & wait", timeout=60)
        process = subprocess.Popen(
            [sys.executable, str(RPC_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.stdin.write(json.dumps(command))
        process.stdin.close()
        try:
            import time

            state = Path("/tmp/harbor-command-" + command["id"])
            deadline = time.monotonic() + 3
            while not state.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(state.exists())
            self.assertEqual(self.request({"op": "cancel", "id": command["id"]}), {})
            process.wait(timeout=3)
            self.assertLess(json.loads(process.stdout.read())["return_code"], 0)
            self.assertFalse(state.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_file_roundtrip_is_binary_and_preserves_executable_mode(self):
        data = bytes(range(256))
        path = self.root / "nested" / "file"
        self.assertEqual(
            self.request(
                {
                    "op": "put",
                    "path": str(path),
                    "data": base64.b64encode(data).decode(),
                    "mode": 0o755,
                }
            ),
            {},
        )
        response = self.request({"op": "get", "path": str(path)})
        self.assertEqual(base64.b64decode(response["data"]), data)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_directory_upload_merges_and_preserves_empty_dirs(self):
        target = self.root / "target"
        target.mkdir()
        (target / "existing").write_text("preserved")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("empty")
            member.type = tarfile.DIRTYPE
            stream.addfile(member)
        self.assertEqual(
            self.request(
                {
                    "op": "put_dir",
                    "path": str(target),
                    "data": base64.b64encode(archive.getvalue()).decode(),
                }
            ),
            {},
        )
        self.assertTrue((target / "empty").is_dir())
        self.assertEqual((target / "existing").read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
