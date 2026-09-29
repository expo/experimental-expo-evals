"""Harbor environment for one EAS Workflows macOS VM.

Harbor stays on the controller. The EAS CLI supplies authenticated SSH; commands
and file contents travel over its stdin, never through workflow inputs or argv.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
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


class _SSHConnectionError(RuntimeError):
    """The relay rejected a connection before any request was submitted."""

    def __init__(self, message: str, master=None):
        super().__init__(message)
        self.master = master


class EASWorkflowEnvironment(BaseEnvironment):
    def __init__(
        self, *args, project_id: str | None = None, stage: str = "production",
        ssh_idle_timeout_sec: int = 600, **kwargs
    ):
        if stage not in {"production", "staging"}:
            raise ValueError("EAS stage must be production or staging")
        if type(ssh_idle_timeout_sec) is not int or not 0 <= ssh_idle_timeout_sec <= 3600:
            raise ValueError("ssh_idle_timeout_sec must be an integer from 0 to 3600")
        self.stage = stage
        self.ssh_idle_timeout_sec = ssh_idle_timeout_sec
        suffix = "_STAGING" if stage == "staging" else ""
        self._token_variable = "EXPO_TOKEN" + suffix
        self._project_variable = "EAS_PROJECT_ID" + suffix
        self.project_id = project_id or os.environ.get(self._project_variable, "")
        self.workflow_run_id: str | None = None
        self.workflow_job_id: str | None = None
        self._bundle: tempfile.TemporaryDirectory | None = None
        self._identity_directory: tempfile.TemporaryDirectory | None = None
        self._master_directory: tempfile.TemporaryDirectory | None = None
        self._master_process: asyncio.subprocess.Process | None = None
        self._master_readers: list[asyncio.Task] = []
        self._master_diagnostics = bytearray()
        self._master_lock = asyncio.Lock()
        self._ready = False
        self._requests_submitted = False
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "eas-workflows"

    @classmethod
    def preflight(cls) -> None:
        # Harbor calls this before construction, without environment kwargs.
        # The selected stage and project are checked in start(), before upload.
        if not any(os.environ.get(name) for name in ("EXPO_TOKEN", "EXPO_TOKEN_STAGING")):
            raise SystemExit("EAS Workflows requires EXPO_TOKEN or EXPO_TOKEN_STAGING")
        for command in ("eas", "ssh", "ssh-keygen", "git"):
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
        if not self.project_id:
            raise ValueError(
                f"EAS {self.stage} requires project_id or {self._project_variable}"
            )
        uuid.UUID(self.project_id)
        self._bundle = tempfile.TemporaryDirectory(prefix="harbor-eas-")
        root = Path(self._bundle.name).resolve()
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

    def _diagnostic(self, message: str, *details: str) -> str:
        values = list(self._persistent_env.values())
        values += [
            value
            for name, value in os.environ.items()
            if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        ]
        def clean(text: str) -> str:
            # Strip terminal formatting before redacting, including OSC titles.
            text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]", "", text)
            text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
            for value in values:
                if len(value) >= 6:
                    text = text.replace(value, "[redacted]")
            return text.strip()

        summary = clean(message)
        context = [value for detail in details if (value := clean(detail))]
        if not context:
            return summary if len(summary) <= 2000 else summary[:500] + "\n...\n" + summary[-1495:]
        # Keep the cause first and reserve space for each stream's latest error.
        summary = summary[:500]
        budget = (2000 - len(summary) - len(context)) // len(context)
        return "\n".join([summary, *(value[-budget:] for value in context)])

    async def _prepare_identity(self) -> None:
        # This is deliberately separate from the directory uploaded to EAS.
        # The relay accepts this disposable SSH key alongside its session token;
        # a hosted controller must not depend on someone's personal ~/.ssh keys.
        self._identity_directory = tempfile.TemporaryDirectory(prefix="harbor-eas-ssh-")
        await self._process(
            [
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "harbor",
                "-f", str(Path(self._identity_directory.name) / "identity"),
            ],
            timeout=30,
        )

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

    def _cli_environment(self) -> dict[str, str]:
        token = os.environ.get(self._token_variable)
        if not token:
            raise ValueError(f"EAS {self.stage} requires {self._token_variable}")
        # Do not inherit EXPO_STAGING/EXPO_LOCAL from the controller: a run's
        # explicit stage is authoritative, including when both tokens exist.
        result = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if key in os.environ
        }
        result.update(
            EXPO_TOKEN=token,
            CI="1",
            EAS_NO_VCS="1",
            EAS_DISABLE_AUTO_FINGERPRINT="1",
        )
        if self.stage == "staging":
            result["EXPO_STAGING"] = "1"
        if self._bundle:
            # Node's cwd resolves macOS /var -> /private/var. EAS computes the
            # uploaded project subdirectory relative to this path, so it must
            # use the same canonical root rather than a controller-only path.
            result["EAS_PROJECT_ROOT"] = str(Path(self._bundle.name).resolve())
        return result

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
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=self._bundle.name if self._bundle else None,
            env=self._cli_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
        except BaseException as error:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if isinstance(error, TimeoutError):
                raise TimeoutError(
                    f"{Path(arguments[0]).name} {arguments[1]} timed out after {timeout:g}s"
                ) from error
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

    def _ssh_control_arguments(self) -> list[str]:
        if not self._master_directory:
            raise RuntimeError("EAS SSH master has not started")
        return [
            "ssh", "-F", "/dev/null", "-S", str(Path(self._master_directory.name) / "mux"),
            "-o", "ControlMaster=no", "-o", "ProxyCommand=false", "-o", "BatchMode=yes",
        ]

    async def _ensure_master(self, *, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                async with self._master_lock:
                    if self._master_process and self._master_process.returncode is None:
                        return
                    if self._ready or self._requests_submitted:
                        raise RuntimeError(self._diagnostic(
                            "EAS SSH connection was lost; submitted requests will not be replayed: "
                            + self._master_diagnostics.decode("utf-8", "replace")
                        ))
                    await self._close_master()
                    # A short absolute path stays below macOS's Unix socket limit.
                    # EAS's token-bearing SSH config lives here until master exit.
                    self._master_directory = tempfile.TemporaryDirectory(prefix="heas-", dir="/tmp")
                    self._master_diagnostics.clear()
                    try:
                        self._master_process = await asyncio.create_subprocess_exec(
                            "eas", "workflow:ssh", self.workflow_job_id,
                            "-M", "-N", "-S", str(Path(self._master_directory.name) / "mux"),
                            "-i", str(Path(self._identity_directory.name) / "identity"),
                            "-o", "ControlPersist=no", "-o", "IdentitiesOnly=yes",
                            "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
                            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                            cwd=self._bundle.name if self._bundle else None,
                            env={**self._cli_environment(), "TMPDIR": self._master_directory.name},
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            start_new_session=True,
                        )

                        async def collect(stream) -> None:
                            while chunk := await stream.read(8192):
                                self._master_diagnostics.extend(chunk)
                                del self._master_diagnostics[:-2000]

                        self._master_readers = [
                            asyncio.create_task(collect(self._master_process.stdout)),
                            asyncio.create_task(collect(self._master_process.stderr)),
                        ]
                        while True:
                            if self._master_process.returncode is not None:
                                await asyncio.gather(*self._master_readers)
                                message = self._diagnostic(
                                    "EAS SSH master exited before readiness: "
                                    + self._master_diagnostics.decode("utf-8", "replace")
                                )
                                if "upstream unavailable" in message.lower():
                                    raise _SSHConnectionError(message, self._master_process)
                                raise RuntimeError(message)
                            if (Path(self._master_directory.name) / "mux").exists():
                                _, _, code = await self._process(
                                    [*self._ssh_control_arguments(), "-O", "check", "harbor-eas.invalid"],
                                    timeout=5, check=False,
                                )
                                if code == 0:
                                    return
                            await asyncio.sleep(0.05)
                    except BaseException:
                        await self._close_master()
                        raise
        except TimeoutError as error:
            raise TimeoutError(self._diagnostic(
                "EAS SSH master did not become ready before the connection timeout: "
                + self._master_diagnostics.decode("utf-8", "replace")
            )) from error

    async def _close_master(self) -> None:
        process = self._master_process
        try:
            if process and process.returncode is None:
                try:
                    _, _, code = await self._process(
                        [*self._ssh_control_arguments(), "-O", "exit", "harbor-eas.invalid"],
                        timeout=5, check=False,
                    )
                    if code == 0:
                        await asyncio.wait_for(process.wait(), 5)
                except (TimeoutError, OSError, RuntimeError):
                    pass
        finally:
            # Cancellation during startup/shutdown must not orphan the master.
            try:
                if process and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                if self._master_readers:
                    await asyncio.gather(*self._master_readers, return_exceptions=True)
            finally:
                self._master_process = None
                self._master_readers = []
                if self._master_directory:
                    self._master_directory.cleanup()
                    self._master_directory = None

    async def _rpc(self, request: dict, *, timeout: float = 180) -> dict:
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            try:
                return await self._rpc_once(request, timeout=max(0, deadline - time.monotonic()))
            except _SSHConnectionError as error:
                attempt += 1
                async with self._master_lock:
                    if self._ready or self._requests_submitted or attempt == 3:
                        raise
                    if self._master_process is error.master:
                        await self._close_master()
                if deadline - time.monotonic() <= attempt:
                    raise
                self.logger.warning("EAS SSH connection failed before readiness; retrying: %s", error)
                await asyncio.sleep(attempt)

    async def _rpc_once(self, request: dict, *, timeout: float) -> dict:
        if not self.workflow_job_id:
            raise RuntimeError("EAS sandbox has not started")
        if not self._identity_directory:
            raise RuntimeError("EAS sandbox SSH identity has not been prepared")
        nonce = uuid.uuid4().hex
        payload = json.dumps(request).encode()
        if len(payload) > MAX_TRANSFER_BYTES * 2:
            raise ValueError("RPC request exceeds frame limit")
        deadline = time.monotonic() + timeout
        await self._ensure_master(timeout=timeout)
        master = self._master_process
        process = await asyncio.create_subprocess_exec(
            *self._ssh_control_arguments(), "-tt", "harbor-eas.invalid",
            cwd=self._bundle.name if self._bundle else None,
            env=self._cli_environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        prefix = f"HARBOR_RPC {nonce} ".encode()
        diagnostics = bytearray()
        preamble = bytearray()

        async def collect_stderr() -> None:
            while chunk := await process.stderr.read(8192):
                diagnostics.extend(chunk)
                del diagnostics[:-2000]

        stderr_task = asyncio.create_task(collect_stderr())

        def diagnostic(error: object) -> str:
            return self._diagnostic(
                str(error),
                preamble.decode("utf-8", "replace"),
                diagnostics.decode("utf-8", "replace"),
                self._master_diagnostics.decode("utf-8", "replace"),
            )

        async def frame(expected: str) -> bytes:
            while True:
                line = await process.stdout.readline()
                if not line:
                    # SSH may close stdout before its final stderr arrives.
                    # Keep this wait inside the RPC deadline; the collector
                    # must survive timeout cancellation so cleanup can drain it.
                    await asyncio.shield(stderr_task)
                    raise RuntimeError("EAS SSH closed before the RPC response")
                header = line.rstrip(b"\r\n")
                if not header.startswith(prefix):
                    preamble.extend(line)
                    del preamble[:-2000]
                    continue
                parts = header[len(prefix):].split()
                if len(parts) != 2 or parts[0] != expected.encode():
                    raise RuntimeError(f"EAS RPC ended before {expected}")
                length = int(parts[1])
                if not 0 <= length <= MAX_TRANSFER_BYTES * 2:
                    raise ValueError("RPC response exceeds frame limit")
                if expected != "RESULT" and length != 0:
                    raise ValueError("Invalid RPC control frame")
                return await process.stdout.readexactly(length)

        submitted = False
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                # Only this public bootstrap command goes through the shell.
                # EXIT makes a missing helper fail promptly during image setup.
                process.stdin.write(
                    (
                        f"{RPC_COMMAND} --framed {nonce}; "
                        f"printf '\\nHARBOR_RPC {nonce} EXIT 0\\n'\n"
                    ).encode()
                )
                await process.stdin.drain()
                await frame("READY")
                # Mark before write: even a partial write must never be replayed.
                submitted = True
                if request.get("op") != "ready":
                    self._requests_submitted = True
                process.stdin.write(
                    f"HARBOR_RPC {nonce} REQUEST {len(payload)}\n".encode() + payload
                )
                await process.stdin.drain()
                response = json.loads(await frame("RESULT"))
                await frame("DONE")
        except TimeoutError as error:
            raise TimeoutError(diagnostic(
                f"EAS SSH RPC timed out after {timeout:.3g}s"
            )) from error
        except (RuntimeError, ValueError, asyncio.IncompleteReadError) as error:
            message = diagnostic(error)
            if not submitted and "upstream unavailable" in message.lower():
                raise _SSHConnectionError(message, master) from error
            raise RuntimeError(message) from error
        finally:
            # This local mux client owns only its session channel. Closing it
            # leaves the shared SSH transport and other commands alive.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
            await stderr_task
        if response.get("error"):
            raise RuntimeError(self._diagnostic(f"EAS sandbox: {response['error']}"))
        if request.get("op") == "ready" and response.get("ready"):
            self._ready = True
        return response

    async def start(self, force_build: bool = False) -> None:
        if self.workflow_run_id:
            raise RuntimeError("EAS sandbox is already started")
        self._ready = False
        self._requests_submitted = False
        self._cli_environment()
        try:
            self._prepare_bundle()
            await self._prepare_identity()
            # Resolve the existing project's owner/slug into this temporary app
            # config. --id never creates a project; --no-icon avoids mutations.
            await self._link_project()
            # Upload only this generated source bundle. Never submit the user's
            # repository or retry an ambiguous workflow creation response.
            started = await self._cli(
                "workflow:run", ".eas/workflows/harbor.yml", "--ssh",
                "--ssh-idle-timeout", str(self.ssh_idle_timeout_sec),
            )
            self.workflow_run_id = started["id"]
            self.logger.info("EAS %s workflow: %s", self.stage, self.workflow_run_id)
            deadline = time.monotonic() + self.task_env_config.build_timeout_sec
            last_error: str | None = None
            while time.monotonic() < deadline:
                state = await self._cli("workflow:view", self.workflow_run_id)
                if state.get("status") in TERMINAL_STATUSES:
                    raise RuntimeError(
                        self._startup_failure(state)
                        + (f"; last SSH readiness failure: {last_error}" if last_error else "")
                    )
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
                    except (RuntimeError, json.JSONDecodeError, TimeoutError) as error:
                        diagnostic = self._diagnostic(str(error))
                        if diagnostic != last_error:
                            self.logger.warning("EAS SSH readiness: %s", diagnostic)
                        last_error = diagnostic
                await asyncio.sleep(3)
            raise TimeoutError(
                "EAS workflow did not become ready before build_timeout_sec"
                + (f": {last_error}" if last_error else "")
            )
        except BaseException:
            await asyncio.shield(self.stop(delete=True))
            raise

    async def stop(self, delete: bool = True) -> None:
        try:
            try:
                await self._close_master()
            finally:
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
            if self._identity_directory and not self.workflow_run_id:
                self._identity_directory.cleanup()
                self._identity_directory = None

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
