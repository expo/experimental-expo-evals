"""Execute one sandbox operation from JSON stdin on the EAS worker."""

import base64
import io
import json
import os
import pathlib
import pwd
import shutil
import signal
import subprocess
import sys
import tarfile
import uuid

MAX_BYTES = 128 * 1024 * 1024
ALIASES = {
    "/" + name
    for name in ("app", "logs", "tests", "solution", "installed-agent", "harbor", "skills")
}


def account(user=None):
    user = user if user is not None else os.environ.get("SUDO_USER", str(os.getuid()))
    return pwd.getpwuid(int(user)) if str(user).isdigit() else pwd.getpwnam(str(user))


def environment(request, identity):
    result = {
        key: os.environ[key]
        for key in (
            "PATH",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "TERM",
            "DEVELOPER_DIR",
            "SDKROOT",
        )
        if key in os.environ
    }
    path_file = pathlib.Path("/private/tmp/harbor-path")
    if path_file.is_file():
        result["PATH"] = path_file.read_text().strip()
    result.update(
        HOME=identity.pw_dir,
        USER=identity.pw_name,
        LOGNAME=identity.pw_name,
        SHELL="/bin/bash",
    )
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
                raise ValueError(
                    "Archive exceeds limits or contains links/special files"
                )
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
        return {
            "ready": ready
            and all(
                pathlib.Path(p).resolve()
                == pathlib.Path("/private/tmp/harbor") / pathlib.Path(p).name
                for p in ALIASES
            )
        }
    if operation == "exec":
        identity = account(request.get("user"))
        command_id = str(uuid.UUID(request["id"]))
        state = pathlib.Path("/tmp/harbor-command-" + command_id)
        canceled = state.with_suffix(".canceled")
        if canceled.exists():
            return {"timeout": True}
        process = subprocess.Popen(
            ["/bin/bash", "-c", request["command"]],
            cwd=request["cwd"],
            env=environment(request, identity),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=lambda: drop_privileges(identity),
        )
        try:
            state.write_text(str(process.pid))
            state.chmod(0o600)
            if canceled.exists():
                os.killpg(process.pid, signal.SIGKILL)
            try:
                stdout, stderr = process.communicate(timeout=request["timeout"])
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return {"timeout": True}
            return {
                "stdout": stdout.decode("utf-8", "replace"),
                "stderr": stderr.decode("utf-8", "replace"),
                "return_code": process.returncode,
            }
        except BaseException:
            # The shell may have exited while descendants still hold its pipes.
            # On interruption, kill the group even if its leader has exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
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
                archive.add(
                    path, arcname=str(path.relative_to(source)), recursive=False
                )
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


def response(request):
    try:
        return main(request)
    except Exception as error:
        # Do not include request contents or environment values in protocol errors.
        return {"error": type(error).__name__ + ": " + str(error)}


def interrupted(signum, _frame):
    # Terminating the helper must also stop detached command process groups.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    for name in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(name, interrupted)
    data = sys.stdin.buffer.read(MAX_BYTES * 2 + 1)
    if len(data) > MAX_BYTES * 2:
        result = {"error": "RPC request exceeds 256 MiB"}
    else:
        try:
            result = response(json.loads(data))
        except (ValueError, TypeError):
            result = {"error": "Invalid RPC request"}
    encoded = json.dumps(result).encode()
    if len(encoded) > MAX_BYTES * 2:
        encoded = b'{"error":"RPC response exceeds 256 MiB"}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
