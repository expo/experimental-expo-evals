"""One pinned Harbor CLI invocation per container; task execution stays in E2B."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_REQUEST = 1_000_000
MAX_FILE = 20 * 1024 * 1024
MAX_UPLOAD = 100 * 1024 * 1024
MAX_SECONDS = 3600
TASK_FILES = {
    "instruction.md", "task.toml", "environment/Dockerfile",
    "environment/setup.sh", "tests/test.sh", "tests/rubric.toml",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
SECRET_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "E2B_API_KEY", "EXPO_TOKEN", "TYPESAFE_API_KEY", "CALLBACK_TOKEN", "CALLBACK_URL")


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    """Redact configured secrets recursively without breaking JSON escaping."""
    tokens = sorted((os.environ[name] for name in SECRET_NAMES if os.environ.get(name)), key=len, reverse=True)
    if isinstance(value, str):
        for token in tokens:
            value = value.replace(token, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Run payload must be an object.")
    for key in ("runId", "taskId"):
        if not isinstance(payload.get(key), str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}", payload[key]):
            raise ValueError(f"Invalid {key}.")
    if not isinstance(payload.get("commitSha"), str) or not re.fullmatch(r"[a-f0-9]{40}", payload["commitSha"]):
        raise ValueError("A full Git commit SHA is required.")
    if payload.get("agent") not in ("claude-code", "codex"):
        raise ValueError("Unsupported agent.")
    if not isinstance(payload.get("model"), str) or not payload["model"].strip() or len(payload["model"]) > 200:
        raise ValueError("Invalid model.")
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != len(TASK_FILES):
        raise ValueError("The six native task files are required.")
    allowed = {f"tasks/{payload['taskId']}/{name}" for name in TASK_FILES}
    seen = set()
    for file in files:
        if not isinstance(file, dict) or file.get("path") not in allowed or file["path"] in seen:
            raise ValueError("Invalid or duplicate task file path.")
        if not isinstance(file.get("content"), str) or len(file["content"].encode()) > 200_000:
            raise ValueError("Invalid task file contents.")
        seen.add(file["path"])
    return payload


def make_config(payload):
    return {
        "job_name": payload["runId"], "n_attempts": 1, "n_concurrent_trials": 1,
        "environment": {"import_path": "e2b_environment:OneHourE2B"},
        "agents": [{"name": payload["agent"], "model_name": payload["model"].strip()}],
        "tasks": [{"path": f"tasks/{payload['taskId']}"}],
    }


def read_json(path):
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def text(path, limit=60_000):
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        with path.open("rb") as file:
            if path.stat().st_size > limit:
                file.seek(-limit, 2)
            return file.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def finite(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def content_text(value):
    if isinstance(value, str):
        return re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[image]", value)
    if isinstance(value, list):
        return "\n".join(filter(None, (content_text(item) for item in value)))
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url"):
            return "[image attachment]"
        return str(value["text"]) if "text" in value else json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def safe_output(path, root):
    """Do not publish symlinks, hidden credentials, or files outside the run."""
    try:
        parts = path.relative_to(root).parts
        return (path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
                and not any(part.startswith(".") or part in ("node_modules", "__pycache__")
                            or re.search(r"(?:^|[-_])(credentials|secrets?)(?:[._-]|$)", part, re.I) for part in parts)
                and path.stat().st_size <= MAX_FILE)
    except (OSError, ValueError):
        return False


def output_files(base, payload):
    """Bound output size; preserve screenshots, native logs/results and task snapshot."""
    job_dir = base / "jobs" / payload["runId"]
    candidates = [base / "harbor.yaml", base / "run.log", base / "revision.json"]
    candidates += [base / item["path"] for item in payload["files"]]
    if job_dir.exists():
        candidates += list(job_dir.rglob("*"))
    # Screenshots are prioritized if an agent creates more output than the cap.
    candidates.sort(key=lambda path: (path.suffix.lower() not in IMAGE_EXTENSIONS, str(path)))
    total = 0
    outputs = []
    for path in candidates:
        if not safe_output(path, base):
            continue
        relative = path.relative_to(base).as_posix()
        # Harness session dumps may contain unrelated account metadata. ATIF is canonical.
        if "/agent/sessions/" in relative:
            continue
        data = path.read_bytes()
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            # JSON-aware scrubbing preserves escaped values as well as literals.
            if path.suffix.lower() == ".json":
                try:
                    decoded = json.dumps(redact(json.loads(decoded)), ensure_ascii=False, indent=2)
                except ValueError:
                    decoded = redact(decoded)
            else:
                decoded = redact(decoded)
            data = decoded.encode()
        if total + len(data) > MAX_UPLOAD or len(outputs) >= 400:
            continue
        total += len(data)
        outputs.append((relative, data, path.suffix.lower() in IMAGE_EXTENSIONS))
    return outputs


def build_detail(base, payload, started_at, exit_code, error=None):
    job_dir = base / "jobs" / payload["runId"]
    trials = sorted(path for path in job_dir.glob("*") if path.is_dir() and not path.is_symlink())
    trial = trials[0] if trials else job_dir / "missing"
    result, job = read_json(trial / "result.json"), read_json(job_dir / "result.json")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    score = finite(rewards.get("reward"))
    if score is None:
        score = finite(rewards.get("success"))
    exception = result.get("exception_info")
    if not error and exception:
        error = content_text(exception)
    if not error and (exit_code != 0 or score is None or (job.get("stats") or {}).get("n_errored_trials", 0)):
        error = f"Harbor exited with code {exit_code} or did not produce an overall reward. Check the logs."
    details = read_json(trial / "verifier/reward-details.json")
    trajectory = read_json(trial / "agent/trajectory.json")
    steps = []
    for index, step in enumerate(trajectory.get("steps", [])[:500]):
        parts = [content_text(step.get("message"))]
        parts += [f"{call.get('function_name', '')}\n{content_text(call.get('arguments'))}" for call in step.get("tool_calls", [])]
        parts += [f"Tool result\n{content_text(observation.get('content'))}" for observation in (step.get("observation") or {}).get("results", [])]
        steps.append({"id": str(step.get("step_id", index + 1)), "role": str(step.get("source", "agent")), "text": "\n\n".join(filter(None, parts))[:40000]})
    config_paths = [base / "harbor.yaml", base / "revision.json"] + [base / file["path"] for file in payload["files"]]
    config_paths += [job_dir / "config.json", job_dir / "lock.json", trial / "config.json", trial / "lock.json", trial / "verifier/reward-details.json"]
    logs = "\n\n".join(filter(None, [text(base / "run.log"), text(job_dir / "job.log"), text(trial / "verifier/test-stdout.txt")]))[-60000:]
    try:
        duration = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
    except ValueError:
        duration = None
    return redact({
        "id": payload["runId"], "task": payload["taskId"], "agent": payload["agent"], "model": payload["model"],
        "startedAt": started_at, "status": "error" if error else "passed" if score >= 1 else "failed",
        "score": score, "durationSec": duration,
        "costUsd": finite((result.get("agent_result") or {}).get("cost_usd")),
        "criteria": [{"name": str(item.get("name", "")), "description": str(item.get("description", "")), "score": finite(item.get("value")) or 0, "probability": finite(item.get("raw"))} for item in (details.get("reward") or {}).get("criteria", [])],
        "steps": steps, "log": re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", logs), "error": error,
        "artifacts": [],
        "configFiles": [{"path": path.relative_to(base).as_posix(), "content": text(path, MAX_REQUEST)} for path in config_paths if safe_output(path, base)],
    })


class Callback:
    def __init__(self):
        self.url = os.environ["CALLBACK_URL"]
        self.token = os.environ["CALLBACK_TOKEN"]

    def send(self, payload=None, path=None, data=None):
        url = self.url + ("?path=" + quote(path, safe="") if path else "")
        body = data if path else json.dumps(redact(payload)).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/octet-stream" if path else "application/json",
            "User-Agent": "eas-simulator-evals/1.0",
            "Accept": "application/json",
        }
        # The trusted Worker sets this URL. Never accept a destination in the run payload.
        request = Request(url, data=body, headers=headers, method="PUT" if path else "POST")
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    response.read(1024)
                return
            except (URLError, TimeoutError) as error:
                if isinstance(error, HTTPError) and error.code < 500 and error.code != 429:
                    raise RuntimeError(f"Result callback rejected ({error.code}).") from None
                if attempt == 3:
                    raise RuntimeError("Could not persist results to the Worker.") from None
                time.sleep(2 ** attempt)


class Runner:
    def __init__(self, root=Path("/work")):
        self.root = root
        self.lock = threading.Lock()
        self.run_id = None
        self.digest = None
        self.status = "idle"
        self.last_error = None

    def snapshot(self):
        return {"runId": self.run_id, "status": self.status, "error": redact(self.last_error)}

    def record_error(self, message):
        self.last_error = redact(str(message))[:2000]
        self.status = "error"
        print(json.dumps({"event": "runner_error", **self.snapshot()}), flush=True)

    def validate_environment(self):
        missing = [name for name in ("RUN_ID", "CALLBACK_URL", "CALLBACK_TOKEN") if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError("Missing runner configuration: " + ", ".join(missing) + ".")

    def accept(self, payload):
        validate_payload(payload)
        try:
            self.validate_environment()
        except ValueError as error:
            self.record_error(error)
            raise
        if payload["runId"] != os.environ.get("RUN_ID"):
            raise ValueError("Run ID does not match this container.")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.run_id:
                if self.run_id != payload["runId"] or self.digest != digest:
                    raise ValueError("This container already accepted a different run.")
                return self.snapshot()
            self.run_id, self.digest, self.status = payload["runId"], digest, "running"
            self.last_error = None
            threading.Thread(target=self.execute, args=(payload,), daemon=True).start()
        return self.snapshot()

    def execute(self, payload):
        started_at, exit_code, error = now(), -1, None
        base = self.root / payload["runId"]
        callback = None
        try:
            self.validate_environment()
            callback = Callback()
            print(json.dumps({"event": "runner_execution_started", "runId": payload["runId"]}), flush=True)
            base.mkdir(parents=True, exist_ok=False)
            for file in payload["files"]:
                destination = base / file["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(file["content"])
            (base / "harbor.yaml").write_text(json.dumps(make_config(payload), indent=2) + "\n")
            (base / "revision.json").write_text(json.dumps({"commitSha": payload["commitSha"], "taskId": payload["taskId"]}) + "\n")
            callback.send({"runId": payload["runId"], "status": "running"})
            process_env = {key: value for key, value in os.environ.items() if key not in ("CALLBACK_URL", "CALLBACK_TOKEN", "RUN_ID")}
            with (base / "run.log").open("wb") as log:
                process = subprocess.Popen([os.environ.get("HARBOR_BIN", "/opt/harbor/.venv/bin/harbor"), "run", "-c", "harbor.yaml"], cwd=base, env=process_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    exit_code = process.wait(timeout=MAX_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    error = "Run exceeded the one-hour limit and was stopped. The E2B sandbox also has a one-hour lifetime."
        except Exception as exception:
            error = str(exception)
            self.record_error(error)
        if callback is None:
            return
        try:
            detail = build_detail(base, payload, started_at, exit_code, error)
            if detail["error"] and not self.last_error:
                self.record_error(detail["error"])
            for relative, data, image in output_files(base, payload):
                callback.send(path=relative, data=data)
                detail["artifacts"].append({"path": relative, "name": PurePosixPath(relative).name, "image": image})
            callback.send({"runId": payload["runId"], "status": "failed" if detail["status"] == "error" else "completed", "detail": detail})
            self.status = detail["status"]
        except Exception as exception:
            # Do not print URLs/tokens or a traceback containing request payloads.
            self.record_error(f"Result persistence failed: {exception}")


runner = Runner()


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value):
        body = json.dumps(redact(value)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = os.environ.get("CALLBACK_TOKEN", "")
        return bool(expected) and hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {expected}")

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"ok": True, **runner.snapshot()})
        if not self.authorized():
            return self.respond(401, {"error": "Unauthorized."})
        if self.path == "/status":
            return self.respond(200, runner.snapshot())
        self.respond(404, {"error": "Not found."})

    def do_POST(self):
        if not self.authorized():
            return self.respond(401, {"error": "Unauthorized."})
        if self.path != "/run":
            return self.respond(404, {"error": "Not found."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST:
                raise ValueError("Run payload is too large or empty.")
            value = runner.accept(json.loads(self.rfile.read(length)))
            self.respond(202, value)
        except (ValueError, TypeError) as error:
            self.respond(400, {"error": str(error)})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
