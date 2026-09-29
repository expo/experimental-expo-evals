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

    def test_invalid_ssh_idle_timeout_is_rejected_before_upload(self):
        for timeout in (-1, 3601, True, "600"):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "ssh_idle_timeout_sec"):
                    self.adapter(ssh_idle_timeout_sec=timeout)

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
        adapter = self.adapter(ssh_idle_timeout_sec=900)
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
        self.assertEqual(
            adapter._cli.call_args_list[1].args[-2:],
            ("--ssh-idle-timeout", "900"),
        )
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

    async def test_readiness_failure_is_logged_safely_and_retained_after_cancellation(self):
        adapter = self.adapter(persistent_env={"EXPO_TOKEN": "private-task-token"})
        running = {
            "status": "IN_PROGRESS",
            "jobs": [{"id": "job-id", "key": "sandbox", "status": "IN_PROGRESS"}],
        }
        adapter._cli = AsyncMock(
            side_effect=[
                {"owner": "example", "slug": "eval-project"},
                {"id": "workflow-id"},
                running,
                running,
                {"status": "CANCELED"},
                {"status": "CANCELED"},
            ]
        )
        adapter._rpc = AsyncMock(
            side_effect=RuntimeError(
                "channel 0: upstream unavailable production-token private-task-token"
            )
        )
        adapter._process = AsyncMock(return_value=(b"", b"", 0))
        with (
            patch("environments.eas.environment.asyncio.sleep", new_callable=AsyncMock),
            self.assertLogs(adapter.logger, level="WARNING") as captured,
            self.assertRaises(RuntimeError) as failure,
        ):
            await adapter.start()
        self.assertEqual(adapter._rpc.await_count, 2)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("EAS SSH readiness", captured.output[0])
        self.assertIn("CANCELED", str(failure.exception))
        self.assertIn("last SSH readiness failure", str(failure.exception))
        for diagnostic in (captured.output[0], str(failure.exception)):
            self.assertIn("upstream unavailable", diagnostic)
            self.assertIn("[redacted]", diagnostic)
            self.assertNotIn("production-token", diagnostic)
            self.assertNotIn("private-task-token", diagnostic)
        self.assertIsNone(adapter.workflow_run_id)
        self.assertIsNone(adapter._identity_directory)

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
        adapter, transcript, commands = await self.pty_adapter()
        with adapter.scoped_exec_env({"AGENT_SECRET": "private-secret"}):
            result = await adapter.exec(
                'test "$EXPO_TOKEN" = overridden && test "$AGENT_SECRET" = private-secret '
                '&& printf ok; printf problem >&2; exit 7',
                cwd=str(self.root), env={"EXPO_TOKEN": "overridden"},
            )
        self.assertNotIn("private-secret", str(commands))
        self.assertNotIn("private-secret", transcript.read_text())
        arguments = commands[0]
        self.assertEqual(
            arguments[arguments.index("-i") + 1],
            str(Path(adapter._identity_directory.name) / "identity"),
        )
        self.assertIn("IdentitiesOnly=yes", arguments)
        self.assertIn("BatchMode=yes", arguments)
        self.assertIn("-tt", arguments)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(result.stderr, "problem")
        self.assertEqual(result.return_code, 7)
        self.assertEqual(
            {path.name for path in Path(adapter._identity_directory.name).iterdir()},
            {"identity", "identity.pub"},
        )

    async def pty_adapter(
        self, *, ssh_stderr="", connection_failures=0, connection_stderr_delay=0.05
    ):
        """An actual independent shell PTY, replacing only the remote SSH hop."""
        adapter = self.adapter()
        await adapter._prepare_identity()
        self.addAsyncCleanup(adapter.stop)
        adapter.workflow_job_id = "job-id"
        transcript = self.root / "terminal.log"
        bridge = self.root / "pty_bridge.py"
        bridge.write_text('''import os, pty, select, sys
sys.stderr.write(os.environ.get("SSH_STDERR", ""))
sys.stderr.flush()
pid, master = pty.fork()
if pid == 0:
    os.environ["PS1"] = "fixture> "
    os.execv("/bin/bash", ["bash", "--noprofile", "--norc"])
with open(os.environ["TRANSCRIPT"], "ab", buffering=0) as output:
    while True:
        for source in select.select([master, 0], [], [])[0]:
            try:
                data = os.read(source, 65536)
            except OSError:
                data = b""
            if not data:
                os.close(master)
                sys.exit(0)
            if source == master:
                output.write(data)
                os.write(1, data)
            else:
                os.write(master, data)
''')
        original = asyncio.create_subprocess_exec
        commands = []

        async def shell(*arguments, **kwargs):
            commands.append(arguments)
            # Model EAS CLI's token-bearing temporary SSH config, including
            # when the subprocess is killed before its own finally can run.
            config = Path(kwargs["env"]["TMPDIR"]) / "eas-workflow-ssh-fixture"
            config.mkdir()
            (config / "config").write_text("User disposable-relay-token")
            kwargs["env"]["TRANSCRIPT"] = str(transcript)
            kwargs["env"]["SSH_STDERR"] = ssh_stderr
            if len(commands) <= connection_failures:
                return await original(
                    sys.executable, "-c",
                    # Closing stdout first reproduces separate pipe delivery:
                    # the retry reason must wait for stderr, not race its reader.
                    f"import os, sys, time; os.close(1); time.sleep({connection_stderr_delay}); "
                    "print('channel 0: open failed: upstream unavailable', file=sys.stderr); sys.exit(255)",
                    **kwargs,
                )
            return await original(sys.executable, str(bridge), **kwargs)

        self.enterContext(patch("asyncio.create_subprocess_exec", side_effect=shell))
        self.enterContext(patch(
            "environments.eas.environment.RPC_COMMAND",
            shlex.join([sys.executable, str(RPC_SCRIPT)]),
        ))
        return adapter, transcript, commands

    async def test_retries_relay_failure_even_when_stderr_arrives_after_stdout_closes(self):
        adapter, _, commands = await self.pty_adapter(connection_failures=1)
        marker = self.root / "executions"
        with self.assertLogs(adapter.logger, level="WARNING"):
            result = await adapter.exec(
                f'printf x >> "{marker}"; printf completed', cwd=str(self.root), timeout_sec=5
            )
        self.assertEqual(result.stdout, "completed")
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(len(commands), 2)
        self.assertFalse(list(Path(adapter._identity_directory.name).glob("rpc-*")))

    async def test_relay_retries_are_bounded_and_share_one_timeout(self):
        adapter, _, commands = await self.pty_adapter(connection_failures=10)
        with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
            await adapter._rpc({"op": "ready"}, timeout=0.5)
        self.assertEqual(len(commands), 1)
        with (
            patch("environments.eas.environment.asyncio.sleep", new_callable=AsyncMock),
            self.assertLogs(adapter.logger, level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "upstream unavailable"),
        ):
            await adapter._rpc({"op": "ready"}, timeout=10)
        self.assertEqual(len(commands), 4)  # One short-budget call, then three attempts.
        self.assertFalse(list(Path(adapter._identity_directory.name).glob("rpc-*")))

    async def test_stdout_eof_does_not_wait_for_stderr_beyond_rpc_deadline(self):
        adapter, _, commands = await self.pty_adapter(
            connection_failures=1, connection_stderr_delay=2
        )
        with self.assertRaisesRegex(TimeoutError, "EAS SSH RPC timed out"):
            await adapter._rpc({"op": "ready"}, timeout=0.3)
        self.assertEqual(len(commands), 1)
        self.assertFalse(list(Path(adapter._identity_directory.name).glob("rpc-*")))

    async def test_does_not_retry_relay_failure_after_request_is_submitted(self):
        adapter, _, commands = await self.pty_adapter()
        marker = self.root / "executions"
        helper = self.root / "failing_rpc.py"
        helper.write_text(f"""import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("rpc", {str(RPC_SCRIPT)!r})
rpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpc)
def fail(request):
    with pathlib.Path({str(marker)!r}).open("a") as output:
        output.write("x")
    print("channel 0: open failed: upstream unavailable", flush=True)
    raise SystemExit(1)
rpc.main = fail
rpc.framed(sys.argv[-1])
""")
        with patch(
            "environments.eas.environment.RPC_COMMAND",
            shlex.join([sys.executable, str(helper)]),
        ):
            with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
                await adapter._rpc({"op": "exec"}, timeout=5)
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(len(commands), 1)

    async def test_pty_transfers_large_binary_frames_without_echoing_requests(self):
        adapter, transcript, _ = await self.pty_adapter()
        data = bytes(range(256)) * 1024
        target = self.root / "binary"
        await adapter._rpc({
            "op": "put", "path": str(target), "mode": 0o600,
            "data": base64.b64encode(data).decode(),
            "private_metadata": "must-not-appear-on-terminal",
        }, timeout=15)
        response = await adapter._rpc({"op": "get", "path": str(target)}, timeout=15)
        self.assertEqual(base64.b64decode(response["data"]), data)
        self.assertNotIn(b"must-not-appear-on-terminal", transcript.read_bytes())

    async def test_missing_pty_helper_fails_without_sending_request(self):
        adapter, transcript, _ = await self.pty_adapter()
        with patch("environments.eas.environment.RPC_COMMAND", "/missing-harbor-helper"):
            with self.assertRaisesRegex(RuntimeError, "before READY"):
                await adapter._rpc({"op": "ready", "secret": "not-for-the-shell"}, timeout=3)
        self.assertNotIn("not-for-the-shell", transcript.read_text())
        self.assertFalse(list(Path(adapter._identity_directory.name).glob("rpc-*")))

    async def test_pty_timeout_retains_sanitized_stdout_and_stderr_diagnostics(self):
        adapter, transcript, _ = await self.pty_adapter(
            ssh_stderr="SSH warning production-token\n"
        )
        with patch(
            "environments.eas.environment.RPC_COMMAND",
            "printf 'worker pending\\n'; sleep 30 #",
        ):
            with self.assertRaises(TimeoutError) as failure:
                await adapter._rpc({"op": "ready", "secret": "never-sent"}, timeout=0.5)
        self.assertIn("timed out after 0.5s", str(failure.exception))
        self.assertIn("worker pending", str(failure.exception))
        self.assertIn("SSH warning [redacted]", str(failure.exception))
        self.assertNotIn("production-token", str(failure.exception))
        self.assertNotIn("never-sent", transcript.read_text())
        self.assertFalse(list(Path(adapter._identity_directory.name).glob("rpc-*")))

    async def test_pty_disconnect_kills_detached_command_and_keeps_adapter_usable(self):
        adapter, _, _ = await self.pty_adapter()
        started = self.root / "started"
        unexpected = self.root / "unexpected"
        execution = asyncio.create_task(adapter.exec(
            f'touch "{started}"; (sleep 1; touch "{unexpected}") & wait',
            cwd=str(self.root), timeout_sec=30,
        ))
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(started.exists())
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        await asyncio.sleep(1.1)
        self.assertFalse(unexpected.exists())
        result = await adapter.exec("printf reused", cwd=str(self.root), timeout_sec=5)
        self.assertEqual(result.stdout, "reused")
        self.assertFalse(list(Path(adapter._identity_directory.name).glob("rpc-*")))

    async def test_pty_disconnect_kills_descendants_after_shell_has_exited(self):
        adapter, _, _ = await self.pty_adapter()
        started = self.root / "started"
        unexpected = self.root / "unexpected"
        execution = asyncio.create_task(adapter.exec(
            f'touch "{started}"; (sleep 1; touch "{unexpected}") & exit 0',
            cwd=str(self.root), timeout_sec=30,
        ))
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(started.exists())
        # The shell exits immediately; its background child holds stdout open,
        # so communicate() is still pending when the SSH connection closes.
        await asyncio.sleep(0.1)
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        await asyncio.sleep(1.1)
        self.assertFalse(unexpected.exists())

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
