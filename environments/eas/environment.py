"""Deferred Harbor environment prototype for one EAS Workflows macOS VM.

Harbor stays on the controller. The EAS CLI supplies authenticated SSH; commands
and file contents travel over its stdin, never through workflow inputs or argv.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import signal
import tarfile
import tempfile
import time
import uuid
from importlib.resources import files
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentResourceCapabilities

MAX_TRANSFER_BYTES = 128 * 1024 * 1024
RPC_COMMAND = "sudo -n /private/tmp/harbor-python /private/tmp/harbor-rpc.py"
TERMINAL_STATUSES = {"SUCCESS", "FAILURE", "CANCELED"}


class EASWorkflowEnvironment(BaseEnvironment):
    def __init__(self, *args, project_id: str | None = None, **kwargs):
        self.project_id = project_id or os.environ.get("EAS_PROJECT_ID", "")
        self.workflow_run_id: str | None = None
        self.workflow_job_id: str | None = None
        self._bundle: tempfile.TemporaryDirectory | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "eas-workflows"

    @classmethod
    def preflight(cls) -> None:
        for name in ("EXPO_TOKEN", "EAS_PROJECT_ID"):
            if not os.environ.get(name):
                raise SystemExit(f"EAS Workflows requires {name}")
        for command in ("eas", "ssh", "git"):
            if not shutil.which(command):
                raise SystemExit(f"EAS Workflows requires {command} on PATH")

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # The workflow selects a fixed macOS resource class, not numeric limits.
        return EnvironmentResourceCapabilities()

    def _validate_definition(self) -> None:
        if not (self.environment_dir / "setup.sh").is_file():
            raise FileNotFoundError("EAS Workflows requires environment/setup.sh")
        if self.task_env_config.docker_image:
            raise ValueError("EAS macOS uses the provider workflow image, not docker_image")

    def _prepare_bundle(self) -> Path:
        uuid.UUID(self.project_id)
        self._bundle = tempfile.TemporaryDirectory(prefix="harbor-eas-")
        root = Path(self._bundle.name)
        environment = root / "environment"
        environment.mkdir()
        setup = self.environment_dir / "setup.sh"
        if setup.is_symlink():
            raise ValueError("Environment source cannot be a symlink: setup.sh")
        if setup.stat().st_size > 1024 * 1024:
            raise ValueError("Environment source is too large: setup.sh")
        shutil.copy2(setup, environment / "setup.sh")
        resources = files(__package__)
        workflow_dir = root / ".eas" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "harbor.yml").write_bytes(
            resources.joinpath("workflow.yml").read_bytes()
        )
        (root / "bootstrap.sh").write_bytes(resources.joinpath("bootstrap.sh").read_bytes())
        (root / "harbor_rpc.py").write_bytes(resources.joinpath("rpc.py").read_bytes())
        (root / "app.json").write_text(
            json.dumps(
                {
                    "expo": {
                        "name": "Harbor sandbox",
                        "slug": "harbor-sandbox",
                        "extra": {"eas": {"projectId": self.project_id}},
                    }
                }
            )
        )
        (root / "package.json").write_text(
            json.dumps({"name": "harbor-sandbox", "private": True, "version": "1.0.0"})
        )
        (root / ".gitignore").write_text(".env\n.env.*\n")
        return root

    def _diagnostic(self, message: str) -> str:
        values = list(self._persistent_env.values())
        values += [
            value
            for name, value in os.environ.items()
            if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        ]
        for value in values:
            if len(value) >= 6:
                message = message.replace(value, "[redacted]")
        return message[-2000:]

    def _startup_failure(self, state: dict) -> str:
        errors = []
        for job in state.get("jobs", []):
            for error in [
                *(job.get("errors") or []),
                *((job.get("turtleJobRun") or {}).get("errors") or []),
            ]:
                errors.append(
                    ": ".join(
                        str(error[key])
                        for key in ("title", "message")
                        if error.get(key)
                    )
                )
        details = "; ".join(filter(None, errors))
        return self._diagnostic(
            f"EAS workflow ended before readiness: {state.get('status')}"
            + (f" — {details}" if details else "")
        )

    async def _process(
        self,
        arguments: list[str],
        *,
        timeout: float,
        data: bytes | None = None,
        check: bool = True,
    ) -> tuple[bytes, bytes, int]:
        # Authentication stays on the controller. Never inherit model/judge keys
        # into the EAS CLI; environment variables are applied by the remote RPC.
        process_env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "EXPO_TOKEN")
            if key in os.environ
        }
        process_env.update(CI="1", EAS_NO_VCS="1", EAS_DISABLE_AUTO_FINGERPRINT="1")
        if self._bundle:
            process_env["EAS_PROJECT_ROOT"] = self._bundle.name
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=self._bundle.name if self._bundle else None,
            env=process_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
        except BaseException:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise
        if check and process.returncode:
            detail = self._diagnostic(stderr.decode("utf-8", "replace").strip())
            raise RuntimeError(
                f"EAS {arguments[1]} exited {process.returncode}: {detail}"
            )
        return stdout, stderr, process.returncode

    async def _cli(self, *arguments: str, timeout: float = 180) -> dict:
        stdout, _, _ = await self._process(
            ["eas", *arguments, "--non-interactive", "--json"], timeout=timeout
        )
        return json.loads(stdout)

    async def _link_project(self) -> None:
        project = await self._cli(
            "init", "--id", self.project_id, "--force", "--no-icon"
        )
        # EAS init can skip its config rewrite when the minimal source bundle has
        # no expo dependency. Its JSON still contains the verified remote values.
        if not all(
            isinstance(project.get(key), str) and project[key]
            for key in ("owner", "slug")
        ):
            raise RuntimeError(
                "EAS did not return the selected project's owner and slug"
            )
        config_path = Path(self._bundle.name) / "app.json"
        config = json.loads(config_path.read_text())
        config["expo"].update(owner=project["owner"], slug=project["slug"])
        config_path.write_text(json.dumps(config))

    async def _rpc(self, request: dict, *, timeout: float = 180) -> dict:
        if not self.workflow_job_id:
            raise RuntimeError("EAS sandbox has not started")
        stdout, _, _ = await self._process(
            ["eas", "workflow:ssh", self.workflow_job_id, RPC_COMMAND],
            timeout=timeout,
            data=json.dumps(request).encode(),
        )
        response = json.loads(stdout)
        if response.get("error"):
            raise RuntimeError(self._diagnostic(f"EAS sandbox: {response['error']}"))
        return response

    async def start(self, force_build: bool = False) -> None:
        if self.workflow_run_id:
            raise RuntimeError("EAS sandbox is already started")
        try:
            self._prepare_bundle()
            # Resolve the existing project's owner/slug into this temporary app
            # config. --id never creates a project; --no-icon avoids mutations.
            await self._link_project()
            # Upload only this generated source bundle. Never submit the user's
            # repository or retry an ambiguous workflow creation response.
            started = await self._cli(
                "workflow:run", ".eas/workflows/harbor.yml", "--ssh"
            )
            self.workflow_run_id = started["id"]
            self.logger.info("EAS workflow: %s", self.workflow_run_id)
            deadline = time.monotonic() + self.task_env_config.build_timeout_sec
            while time.monotonic() < deadline:
                state = await self._cli("workflow:view", self.workflow_run_id)
                if state.get("status") in TERMINAL_STATUSES:
                    raise RuntimeError(self._startup_failure(state))
                job = next(
                    (
                        job
                        for job in state.get("jobs", [])
                        if job.get("key") == "sandbox"
                    ),
                    None,
                )
                if job and job.get("status") == "IN_PROGRESS":
                    self.workflow_job_id = job["id"]
                    # SSH may be ready before bootstrap has finished. Probe is
                    # read-only and may be retried; commands/files never are.
                    try:
                        if (
                            await self._rpc(
                                {"op": "ready"},
                                timeout=min(330, max(1, deadline - time.monotonic())),
                            )
                        ).get("ready"):
                            return
                    except (RuntimeError, json.JSONDecodeError):
                        pass
                await asyncio.sleep(3)
            raise TimeoutError(
                "EAS workflow did not become ready before build_timeout_sec"
            )
        except BaseException:
            await asyncio.shield(self.stop(delete=True))
            raise

    async def stop(self, delete: bool = True) -> None:
        try:
            if self.workflow_run_id:
                run_id = self.workflow_run_id
                await self._process(
                    ["eas", "workflow:cancel", run_id, "--non-interactive"], timeout=60
                )
                # eas workflow:cancel can exit zero after a server-side error.
                for _ in range(10):
                    state = await self._cli("workflow:view", run_id, timeout=30)
                    if state.get("status") in TERMINAL_STATUSES:
                        self.workflow_run_id = None
                        self.workflow_job_id = None
                        break
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(
                        f"EAS workflow cancellation was not confirmed: {run_id}"
                    )
        finally:
            if self._bundle and not self.workflow_run_id:
                self._bundle.cleanup()
                self._bundle = None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        timeout = timeout_sec if timeout_sec is not None else 3600
        command_id = str(uuid.uuid4())
        try:
            response = await self._rpc(
                {
                    "op": "exec",
                    "id": command_id,
                    "command": command,
                    "cwd": cwd or self.task_env_config.workdir or "/app",
                    "env": self._merge_env(env),
                    "user": self._resolve_user(user),
                    "timeout": timeout,
                },
                timeout=timeout + 60,
            )
        except (asyncio.CancelledError, TimeoutError):
            # Closing local SSH does not kill a detached remote process group.
            # Cancel that command while retaining the VM for Harbor's verifier.
            try:
                await asyncio.shield(
                    self._rpc({"op": "cancel", "id": command_id}, timeout=30)
                )
            except (OSError, RuntimeError, ValueError, TimeoutError):
                await asyncio.shield(self.stop(delete=True))
            raise
        if response.get("timeout"):
            raise TimeoutError("EAS sandbox command timed out")
        result = ExecResult(**response)
        callback = self._output_callback()
        if callback:
            for name in ("stdout", "stderr"):
                if text := getattr(result, name):
                    await callback(text, name)
        return result

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        path = Path(source_path)
        if path.stat().st_size > MAX_TRANSFER_BYTES:
            raise ValueError("Transfer exceeds 128 MiB")
        data = self._bounded(path.read_bytes())
        await self._rpc(
            {
                "op": "put",
                "path": target_path,
                "data": base64.b64encode(data).decode(),
                "mode": path.stat().st_mode,
            }
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            total = 0
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Directory uploads cannot contain symlinks")
                if path.is_file() or path.is_dir():
                    total += path.stat().st_size if path.is_file() else 0
                    if total > MAX_TRANSFER_BYTES:
                        raise ValueError("Transfer exceeds 128 MiB")
                    archive.add(
                        path, arcname=str(path.relative_to(source)), recursive=False
                    )
        data = self._bounded(buffer.getvalue())
        await self._rpc(
            {
                "op": "put_dir",
                "path": target_dir,
                "data": base64.b64encode(data).decode(),
            },
            timeout=600,
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        response = await self._rpc({"op": "get", "path": source_path}, timeout=600)
        data = self._bounded(base64.b64decode(response["data"], validate=True))
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        response = await self._rpc({"op": "get_dir", "path": source_dir}, timeout=600)
        data = self._bounded(base64.b64decode(response["data"], validate=True))
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            if sum(member.size for member in members) > MAX_TRANSFER_BYTES or any(
                not (member.isfile() or member.isdir()) for member in members
            ):
                raise ValueError("Invalid sandbox directory archive")
            archive.extractall(target, filter="data")

    @staticmethod
    def _bounded(data: bytes) -> bytes:
        if len(data) > MAX_TRANSFER_BYTES:
            raise ValueError("Transfer exceeds 128 MiB")
        return data

    async def reset_dirs(
        self, *, remove_dirs, create_dirs, chmod_dirs=None
    ) -> ExecResult:
        response = await self._rpc(
            {
                "op": "reset",
                "remove": list(map(str, remove_dirs)),
                "create": list(map(str, create_dirs)),
                "chmod": list(map(str, chmod_dirs or [])),
            }
        )
        return ExecResult(**response)

    async def empty_dirs(self, dirs, *, chmod: bool = True) -> ExecResult | None:
        if not dirs:
            return None
        return await self.reset_dirs(
            remove_dirs=dirs, create_dirs=dirs, chmod_dirs=dirs if chmod else []
        )
