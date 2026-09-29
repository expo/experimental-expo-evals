"""Exercise SSH connection sharing against an isolated localhost OpenSSH server."""

import asyncio
import os
import pwd
import shlex
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from environments.eas.environment import EASWorkflowEnvironment


@unittest.skipUnless(shutil.which("sshd"), "OpenSSH server is not installed")
class SSHIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harbor-ssh-test-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = self.root / "eas-calls"
        self.server_log = self.root / "sshd.log"
        self.enterContext(patch.dict(os.environ, {"EXPO_TOKEN": "test-controller-token"}))
        await self.command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.root / "host"))
        environment = self.root / "environment"
        environment.mkdir()
        (environment / "setup.sh").write_text("true\n")
        self.adapter = EASWorkflowEnvironment(
            environment_dir=environment, environment_name="test", session_id="test",
            trial_paths=TrialPaths(self.root / "trial"), task_env_config=EnvironmentConfig(),
            project_id="4eddbf65-47d6-4fa4-b91d-eedda3b11893",
        )
        await self.adapter._prepare_identity()
        self.adapter.workflow_job_id = "test-job"
        key = Path(self.adapter._identity_directory.name) / "identity"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        configuration = self.root / "sshd_config"
        configuration.write_text(
            f"Port {port}\nListenAddress 127.0.0.1\nHostKey {self.root}/host\n"
            f"PidFile {self.root}/sshd.pid\nAuthorizedKeysFile {key}.pub\n"
            "StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\n"
            "LogLevel VERBOSE\nForceCommand /bin/bash --noprofile --norc\n"
        )
        self.server = await asyncio.create_subprocess_exec(
            shutil.which("sshd"), "-D", "-e", "-f", str(configuration),
            "-E", str(self.server_log), stderr=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.close_server)
        self.addAsyncCleanup(self.adapter.stop)
        for _ in range(100):
            if self.server_log.exists() and "Server listening" in self.server_log.read_text():
                break
            if self.server.returncode is not None:
                self.fail("Local sshd could not start: " + (await self.server.stderr.read()).decode())
            await asyncio.sleep(0.02)
        else:
            self.fail("Local sshd did not begin listening")
        client_configuration = self.root / "ssh_config"
        client_configuration.write_text(
            f"Host fixture\n  HostName 127.0.0.1\n  Port {port}\n"
            f"  User {pwd.getpwuid(os.getuid()).pw_name}\n"
            "  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n"
        )
        executable = self.root / "eas"
        executable.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "assert sys.argv[1:3] == ['workflow:ssh', 'test-job']\n"
            f"with open({str(self.calls)!r}, 'a') as output: output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"os.execv({shutil.which('ssh')!r}, ['ssh', '-F', {str(client_configuration)!r}, 'fixture', *sys.argv[3:]])\n"
        )
        executable.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]}))
        rpc_script = Path(__file__).resolve().parents[1] / "environments/eas/rpc.py"
        self.enterContext(patch("environments.eas.environment.RPC_COMMAND", shlex.join([sys.executable, str(rpc_script)])))

    async def command(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        self.assertEqual(process.returncode, 0, stderr.decode())
        return stdout.decode()

    async def close_server(self):
        if self.server.returncode is None:
            self.server.terminate()
        await asyncio.wait_for(self.server.communicate(), 5)

    async def execute(self, command, **kwargs):
        return await self.adapter.exec(command, cwd=str(self.root), timeout_sec=10, **kwargs)

    def master_count(self):
        return len(self.calls.read_text().splitlines())

    async def test_commands_transfers_and_cancellation_share_one_transport(self):
        result = await self.execute("printf first; printf problem >&2; exit 7")
        self.assertEqual((result.stdout, result.stderr, result.return_code), ("first", "problem", 7))
        results = await asyncio.gather(self.execute("sleep 0.1; printf second"), self.execute("printf third"))
        self.assertEqual([result.stdout for result in results], ["second", "third"])
        data = bytes(range(256)) * 1024
        source = self.root / "source.bin"
        source.write_bytes(data)
        await self.adapter.upload_file(source, str(self.root / "remote.bin"))
        destination = self.root / "downloaded.bin"
        await self.adapter.download_file(str(self.root / "remote.bin"), destination)
        self.assertEqual(destination.read_bytes(), data)
        directory = self.root / "source-directory"
        directory.mkdir()
        (directory / "space name.bin").write_bytes(data)
        await self.adapter.upload_dir(directory, str(self.root / "remote-directory"))
        await self.adapter.download_dir(str(self.root / "remote-directory"), self.root / "downloaded-directory")
        self.assertEqual((self.root / "downloaded-directory/space name.bin").read_bytes(), data)
        execution = asyncio.create_task(self.execute("touch started; (sleep 1; touch unexpected) & wait"))
        for _ in range(100):
            if (self.root / "started").exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue((self.root / "started").exists())
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        await asyncio.sleep(1.1)
        self.assertFalse((self.root / "unexpected").exists())
        self.assertEqual((await self.execute("printf reused")).stdout, "reused")
        self.assertEqual(self.master_count(), 1)
        self.assertEqual(self.server_log.read_text().count("Accepted publickey for "), 1)
        await self.adapter.stop()
        self.assertIsNone(self.adapter._identity_directory)

    async def test_dead_master_does_not_reconnect_or_execute_again(self):
        import json

        await self.execute("printf connected")
        arguments = json.loads(self.calls.read_text().splitlines()[0])
        control = arguments[arguments.index("-S") + 1]
        await self.command("ssh", "-F", "/dev/null", "-S", control, "-O", "exit", "harbor-eas.invalid")
        with self.assertRaises((RuntimeError, OSError)):
            await self.execute("touch must-not-run")
        self.assertEqual(self.master_count(), 1)
        self.assertFalse((self.root / "must-not-run").exists())
        self.assertEqual(self.server_log.read_text().count("Accepted publickey for "), 1)
