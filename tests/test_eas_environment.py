import asyncio
import base64
import io
import json
import os
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
        adapter = self.adapter()
        root = adapter._prepare_bundle()
        self.assertEqual(
            Path(adapter._cli_environment()["EAS_PROJECT_ROOT"]), root.resolve()
        )
        self.assertEqual(
            {p.name for p in (root / "environment").iterdir()},
            {"setup.sh"},
        )
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

    async def test_ssh_identity_stays_outside_upload_and_is_removed_on_stop(self):
        adapter = self.adapter()
        root = adapter._prepare_bundle()
        self.addAsyncCleanup(adapter.stop)
        await adapter._prepare_identity()
        identity = Path(adapter._identity_directory.name) / "identity"
        self.assertFalse(identity.is_relative_to(root))
        self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
        self.assertEqual(identity.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(identity.with_suffix(".pub").is_file())
        for source in root.rglob("*"):
            if source.is_file():
                self.assertNotIn(b"BEGIN OPENSSH PRIVATE KEY", source.read_bytes())
        # EAS forwards options after the destination; verify OpenSSH actually
        # applies them there, without making a network connection.
        stdout, _, _ = await adapter._process(
            [
                "ssh", "-G", "example.invalid", "-i", str(identity),
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            ],
            timeout=10,
        )
        self.assertIn(f"identityfile {identity}".encode(), stdout)
        self.assertIn(b"identitiesonly yes", stdout)
        self.assertIn(b"batchmode yes", stdout)
        await adapter.stop()
        self.assertFalse(identity.parent.exists())
        self.assertFalse(root.exists())

    async def test_process_timeout_identifies_the_command(self):
        with self.assertRaisesRegex(TimeoutError, "sleep 1 timed out after"):
            await self.adapter()._process(["sleep", "1"], timeout=0.01)

    async def test_start_waits_for_matching_job_and_readiness_then_confirms_stop(self):
        adapter = self.adapter()
        adapter._cli = AsyncMock(
            side_effect=[
                {
                    "status": "already-linked",
                    "owner": "example",
                    "slug": "eval-project",
                },
                {"id": "workflow-id"},
                {
                    "status": "IN_PROGRESS",
                    "jobs": [
                        {"id": "job-id", "key": "sandbox", "status": "IN_PROGRESS"}
                    ],
                },
                {"status": "CANCELED"},
            ]
        )
        adapter._rpc = AsyncMock(return_value={"ready": True})
        adapter._process = AsyncMock(return_value=(b"", b"", 0))
        await adapter.start()
        self.assertEqual(adapter.workflow_job_id, "job-id")
        config = json.loads((Path(adapter._bundle.name) / "app.json").read_text())
        self.assertEqual(config["expo"]["owner"], "example")
        self.assertEqual(config["expo"]["slug"], "eval-project")
        self.assertEqual(
            adapter._cli.call_args_list[0].args,
            ("init", "--id", adapter.project_id, "--force", "--no-icon"),
        )
        self.assertIn("--ssh", adapter._cli.call_args_list[1].args)
        await adapter.stop()
        self.assertIsNone(adapter.workflow_run_id)
        self.assertIsNone(adapter._bundle)

    async def test_failed_start_cancels_allocated_workflow(self):
        adapter = self.adapter()
        adapter._cli = AsyncMock(
            side_effect=[
                {
                    "status": "already-linked",
                    "owner": "example",
                    "slug": "eval-project",
                },
                {"id": "workflow-id"},
                {"status": "FAILURE"},
                {"status": "FAILURE"},
            ]
        )
        adapter._process = AsyncMock(return_value=(b"", b"", 0))
        with self.assertRaisesRegex(RuntimeError, "ended before readiness"):
            await adapter.start()
        self.assertEqual(adapter._cli.call_args_list[1].args[0], "workflow:run")
        self.assertEqual(
            sum(call.args[0] == "workflow:run" for call in adapter._cli.call_args_list),
            1,
        )
        self.assertIsNone(adapter.workflow_run_id)

    def test_startup_errors_explain_unavailable_ssh_without_credentials(self):
        adapter = self.adapter(persistent_env={"EXPO_TOKEN": "private-token"})
        message = adapter._startup_failure(
            {
                "status": "FAILURE",
                "jobs": [
                    {
                        "errors": [
                            {
                                "title": "SSH unavailable",
                                "message": "No SSH relay is configured. private-token",
                            }
                        ]
                    }
                ],
            }
        )
        self.assertIn("No SSH relay is configured", message)
        self.assertNotIn("private-token", message)

    async def test_environment_overlays_and_secrets_only_go_through_stdin(self):
        adapter = self.adapter(persistent_env={"EXPO_TOKEN": "task-token"})
        await adapter._prepare_identity()
        self.addAsyncCleanup(adapter.stop)
        adapter.workflow_job_id = "job-id"
        adapter._process = AsyncMock(
            return_value=(b'{"stdout":"ok","stderr":"","return_code":7}', b"", 0)
        )
        with adapter.scoped_exec_env({"AGENT_SECRET": "private-secret"}):
            result = await adapter.exec("echo ok", env={"EXPO_TOKEN": "overridden"})
        call = adapter._process.call_args
        self.assertNotIn("private-secret", str(call.args))
        self.assertNotIn("task-token", str(call.args))
        arguments = call.args[0]
        self.assertEqual(
            arguments[arguments.index("-i") + 1],
            str(Path(adapter._identity_directory.name) / "identity"),
        )
        self.assertIn("IdentitiesOnly=yes", arguments)
        self.assertIn("BatchMode=yes", arguments)
        payload = json.loads(call.kwargs["data"])
        self.assertEqual(
            payload["env"],
            {"EXPO_TOKEN": "overridden", "AGENT_SECRET": "private-secret"},
        )
        self.assertEqual(result.return_code, 7)

    async def test_cancel_interrupts_remote_command_but_keeps_vm_for_verifier(self):
        adapter = self.adapter()
        adapter._rpc = AsyncMock(side_effect=[asyncio.CancelledError(), {}])
        adapter.stop = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await adapter.exec("sleep 100")
        first, second = adapter._rpc.call_args_list
        self.assertEqual(second.args[0], {"op": "cancel", "id": first.args[0]["id"]})
        adapter.stop.assert_not_called()

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
