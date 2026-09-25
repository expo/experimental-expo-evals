"""Harbor environment backed by one EAS Workflows macOS VM.

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

import yaml
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentResourceCapabilities

MAX_TRANSFER_BYTES = 128 * 1024 * 1024
ROOT_ALIASES = ("app", "logs", "tests", "solution", "installed-agent", "harbor")
RPC_COMMAND = "sudo -n /private/tmp/harbor-python /private/tmp/harbor-rpc.py"
TERMINAL_STATUSES = {"SUCCESS", "FAILURE", "CANCELED"}

# One request per SSH connection. A standalone stdlib script also makes transport
# behavior testable without an EAS machine or a model invocation.
REMOTE_SCRIPT = r"""
import base64, io, json, os, pathlib, pwd, shutil, signal, subprocess, sys, tarfile, uuid

MAX_BYTES = 128 * 1024 * 1024
ALIASES = {"/" + name for name in ("app", "logs", "tests", "solution", "installed-agent", "harbor")}

def account(user=None):
    user = user if user is not None else os.environ.get("SUDO_USER", str(os.getuid()))
    return pwd.getpwuid(int(user)) if str(user).isdigit() else pwd.getpwnam(str(user))

def environment(request, identity):
    result = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL", "TERM", "DEVELOPER_DIR", "SDKROOT") if key in os.environ}
    path_file = pathlib.Path("/private/tmp/harbor-path")
    if path_file.is_file():
        result["PATH"] = path_file.read_text().strip()
    result.update(HOME=identity.pw_dir, USER=identity.pw_name, LOGNAME=identity.pw_name, SHELL="/bin/bash")
    result.update(request.get("env") or {})
    return result

def drop_privileges(identity):
    if os.geteuid() == 0:
        os.initgroups(identity.pw_name, identity.pw_gid)
        os.setgid(identity.pw_gid)
        os.setuid(identity.pw_uid)
    elif os.geteuid() != identity.pw_uid:
        raise PermissionError("Cannot select the requested sandbox user")

def bounded_file(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Transfer exceeds 128 MiB")
    return data

def unpack(data, destination):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        total = 0
        for member in archive.getmembers():
            total += member.size
            if total > MAX_BYTES or not (member.isfile() or member.isdir()):
                raise ValueError("Archive exceeds limits or contains links/special files")
        archive.extractall(destination, filter="data")

def owner(path):
    identity = account()
    if os.geteuid() == 0:
        for child in [path, *path.rglob("*")] if path.is_dir() else [path]:
            if not child.is_symlink():
                os.chown(child, identity.pw_uid, identity.pw_gid)

def reset_path(value):
    path = pathlib.Path(value)
    if str(path) in ALIASES:
        target = path.resolve()
        expected = pathlib.Path("/private/tmp/harbor") / path.name
        if target != expected:
            raise ValueError("Harbor root alias changed")
        return target
    return path

def main(request):
    operation = request["op"]
    if operation == "ready":
        ready = pathlib.Path("/private/tmp/harbor-ready").is_file()
        return {"ready": ready and all(pathlib.Path(p).resolve() == pathlib.Path("/private/tmp/harbor") / pathlib.Path(p).name for p in ALIASES)}
    if operation == "exec":
        identity = account(request.get("user"))
        command_id = str(uuid.UUID(request["id"]))
        state = pathlib.Path("/tmp/harbor-command-" + command_id)
        canceled = state.with_suffix(".canceled")
        if canceled.exists():
            return {"timeout": True}
        process = subprocess.Popen(["/bin/bash", "-c", request["command"]], cwd=request["cwd"], env=environment(request, identity), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, preexec_fn=lambda: drop_privileges(identity))
        state.write_text(str(process.pid))
        state.chmod(0o600)
        try:
            if canceled.exists():
                os.killpg(process.pid, signal.SIGKILL)
            try:
                stdout, stderr = process.communicate(timeout=request["timeout"])
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return {"timeout": True}
            return {"stdout": stdout.decode("utf-8", "replace"), "stderr": stderr.decode("utf-8", "replace"), "return_code": process.returncode}
        finally:
            state.unlink(missing_ok=True)
            canceled.unlink(missing_ok=True)
    if operation == "cancel":
        state = pathlib.Path("/tmp/harbor-command-" + str(uuid.UUID(request["id"])))
        state.with_suffix(".canceled").touch(mode=0o600)
        try:
            pid = int(state.read_text())
            os.killpg(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass
        return {}
    if operation in ("put", "put_dir"):
        data = base64.b64decode(request["data"], validate=True)
        if len(data) > MAX_BYTES:
            raise ValueError("Transfer exceeds 128 MiB")
        path = pathlib.Path(request["path"])
        if operation == "put":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(request["mode"] & 0o777)
        else:
            path.mkdir(parents=True, exist_ok=True)
            unpack(data, path)
        owner(path)
        return {}
    if operation == "get":
        return {"data": base64.b64encode(bounded_file(request["path"])).decode()}
    if operation == "get_dir":
        source = pathlib.Path(request["path"])
        if not source.is_dir():
            raise NotADirectoryError(str(source))
        buffer = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path in sorted(source.rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    continue
                total += path.stat().st_size if path.is_file() else 0
                if total > MAX_BYTES:
                    raise ValueError("Directory transfer exceeds 128 MiB")
                archive.add(path, arcname=str(path.relative_to(source)), recursive=False)
        return {"data": base64.b64encode(buffer.getvalue()).decode()}
    if operation == "reset":
        for value in request["remove"]:
            path = reset_path(value)
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        for value in request["create"]:
            reset_path(value).mkdir(parents=True, exist_ok=True)
        for value in request["chmod"]:
            reset_path(value).chmod(0o777)
        return {"stdout": "", "stderr": "", "return_code": 0}
    raise ValueError("Unknown sandbox operation")

try:
    request = json.loads(sys.stdin.buffer.read(MAX_BYTES * 2))
    response = main(request)
except Exception as error:
    # Do not include request contents or environment values in protocol errors.
    response = {"error": type(error).__name__ + ": " + str(error)}
print(json.dumps(response))
"""


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
        definition = self.environment_dir / "workflow.yml"
        if not definition.is_file():
            raise FileNotFoundError("EAS Workflows requires environment/workflow.yml")
        if not (self.environment_dir / "setup.sh").is_file():
            raise FileNotFoundError("EAS Workflows requires environment/setup.sh")
        config = yaml.safe_load(definition.read_text())
        jobs = config.get("jobs", {}) if isinstance(config, dict) else {}
        if len(jobs) != 1:
            raise ValueError(
                "An EAS sandbox workflow must contain exactly one macOS job"
            )
        self._job_key, job = next(iter(jobs.items()))
        if not isinstance(job, dict) or job.get("runs_on") not in (
            "macos-medium",
            "macos-large",
        ):
            raise ValueError("The EAS sandbox job must use macos-medium or macos-large")
        if self.task_env_config.docker_image:
            raise ValueError("EAS macOS uses workflow image, not docker_image")

    def _prepare_bundle(self) -> Path:
        uuid.UUID(self.project_id)
        self._bundle = tempfile.TemporaryDirectory(prefix="harbor-eas-")
        root = Path(self._bundle.name)
        environment = root / "environment"
        environment.mkdir()
        for name in ("workflow.yml", "setup.sh"):
            source = self.environment_dir / name
            if source.is_symlink():
                raise ValueError(f"Environment source cannot be a symlink: {name}")
            if source.is_file():
                if source.stat().st_size > 1024 * 1024:
                    raise ValueError(f"Environment source is too large: {name}")
                shutil.copy2(source, environment / name)
        workflow_dir = root / ".eas" / "workflows"
        workflow_dir.mkdir(parents=True)
        shutil.copy2(environment / "workflow.yml", workflow_dir / "harbor.yml")
        (root / "bootstrap.sh").write_bytes(
            files("eas_provider").joinpath("bootstrap.sh").read_bytes()
        )
        (root / "harbor_rpc.py").write_text(REMOTE_SCRIPT)
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
                        if job.get("key") == self._job_key
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
