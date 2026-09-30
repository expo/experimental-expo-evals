"""Outbound HTTPS transport for Harbor's EAS sandbox operations."""

import concurrent.futures
import fcntl
import hashlib
import http.client
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK_BYTES = 4 * 1024 * 1024
MAX_BYTES = 256 * 1024 * 1024
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class SessionClosed(Exception):
    pass


class ProtocolError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def write_private(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def child_environment():
    # Relay credentials and EAS/model tokens must never reach the sandbox process.
    return {
        key: os.environ[key]
        for key in (
            "PATH", "TMPDIR", "LANG", "LC_ALL", "TERM", "DEVELOPER_DIR", "SDKROOT", "SUDO_USER",
        )
        if key in os.environ
    }


class Client:
    def __init__(self, config, stop, *, allow_loopback=False):
        url = config["url"].rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        loopback = allow_loopback and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (parsed.scheme != "https" and not loopback) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Worker relay URL must use HTTPS without embedded credentials")
        self.url = url
        self.token = config["token"]
        if not isinstance(self.token, str) or not self.token or "\n" in self.token or "\r" in self.token:
            raise ValueError("Invalid worker credential")
        self.deadline = min(float(config["expires_at"]), time.time() + 55 * 60)
        self.stop = stop
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None, *, limit=CHUNK_BYTES):
        delay = 0.5
        while not self.stop.is_set():
            remaining = self.deadline - time.time()
            if remaining <= 0:
                raise SessionClosed("Worker session expired")
            request = urllib.request.Request(
                self.url + path, data=data, method=method,
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json" if method == "POST" else "application/octet-stream"},
            )
            try:
                with self.opener.open(request, timeout=min(10, remaining)) as response:
                    payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise ProtocolError("Relay response exceeds transfer limit")
                return payload
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 410:
                    self.stop.set()
                    raise SessionClosed("Worker session closed") from None
                if error.code not in (408, 429) and error.code < 500:
                    self.stop.set()
                    raise ProtocolError(f"Relay rejected worker request (HTTP {error.code})") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError):
                pass
            self.stop.wait(min(delay, max(0, self.deadline - time.time())))
            delay = min(delay * 2, 5)
        raise SessionClosed("Worker stopped")

    def poll(self):
        try:
            response = json.loads(self.request("GET", "/poll"))
            requests = response["requests"]
            if not isinstance(requests, list):
                raise ValueError()
            for item in requests:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not IDENTIFIER.fullmatch(item["id"]):
                    raise ValueError()
                if type(item.get("size")) is not int or not 0 < item["size"] <= MAX_BYTES:
                    raise ValueError()
                if not isinstance(item.get("sha256"), str) or not DIGEST.fullmatch(item["sha256"]):
                    raise ValueError()
            return requests
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("Invalid relay request manifest") from None

    def download(self, item, destination):
        temporary = destination.with_suffix(".download")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        digest = hashlib.sha256()
        remaining = item["size"]
        try:
            with os.fdopen(descriptor, "wb") as output:
                for part in range((remaining + CHUNK_BYTES - 1) // CHUNK_BYTES):
                    data = self.request("GET", f'/requests/{item["id"]}?part={part}')
                    if len(data) != min(remaining, CHUNK_BYTES):
                        raise ProtocolError("Incomplete relay request")
                    output.write(data)
                    digest.update(data)
                    remaining -= len(data)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != item["sha256"]:
                raise ProtocolError("Relay request checksum mismatch")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def upload(self, identifier, source):
        digest = hashlib.sha256()
        size = source.stat().st_size
        if size > MAX_BYTES:
            raise ProtocolError("RPC response exceeds transfer limit")
        with source.open("rb") as stream:
            part = 0
            while data := stream.read(CHUNK_BYTES):
                self.request("PUT", f"/results/{identifier}?part={part}", data)
                digest.update(data)
                part += 1
        self.request("POST", f"/results/{identifier}", json.dumps({"size": size, "sha256": digest.hexdigest()}).encode())


class Worker:
    def __init__(self, config_path, *, rpc_path=None, allow_loopback=False):
        self.config_path = pathlib.Path(config_path)
        stat = self.config_path.lstat()
        if self.config_path.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError("Worker config must be private and owned by the helper user")
        config = json.loads(self.config_path.read_bytes())
        self.stop = threading.Event()
        self.client = Client(config, self.stop, allow_loopback=allow_loopback)
        self.state = self.config_path.with_suffix(self.config_path.suffix + ".state")
        self.state.mkdir(mode=0o700, exist_ok=True)
        stat = self.state.lstat()
        if self.state.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError("Worker state directory must be private")
        self.instance_lock = os.open(self.state / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.instance_lock)
            self.instance_lock = None
            raise ValueError("Another helper already owns this session") from None
        adjacent = pathlib.Path(__file__).with_name("rpc.py")
        self.rpc_path = pathlib.Path(rpc_path) if rpc_path else adjacent if adjacent.is_file() else pathlib.Path("/private/tmp/harbor-rpc.py")
        self.processes = set()
        self.lock = threading.Lock()
        self.exec_slots = threading.Semaphore(2)
        self.active = {}
        self.normal_pool = concurrent.futures.ThreadPoolExecutor(max_workers=14)
        self.cancel_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # A previous helper may have died after starting a command. Never rerun it.
        for claim in self.state.glob("*.claimed"):
            result = claim.with_suffix(".result")
            if not result.exists() and not claim.with_suffix(".done").exists():
                write_private(result, b'{"error":"Worker restarted during this operation; execution was not repeated"}')

    def path(self, identifier, suffix):
        return self.state / (identifier + suffix)

    def execute(self, identifier, request):
        result = self.path(identifier, ".result")
        temporary = self.path(identifier, ".output")
        if self.stop.is_set():
            raise SessionClosed("Worker stopped")
        with self.path(identifier, ".request").open("rb") as source:
            descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                process = subprocess.Popen(
                    [sys.executable, str(self.rpc_path)], stdin=source, stdout=output,
                    stderr=subprocess.DEVNULL, env=child_environment(), start_new_session=True,
                )
                with self.lock:
                    self.processes.add(process)
                try:
                    while process.poll() is None:
                        if self.stop.wait(0.1):
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            raise SessionClosed("Worker stopped")
                    if process.returncode != 0:
                        raise ProtocolError(f"Sandbox RPC exited with status {process.returncode}")
                finally:
                    process.wait()
                    with self.lock:
                        self.processes.discard(process)
                output.flush()
                os.fsync(output.fileno())
        if temporary.stat().st_size > MAX_BYTES:
            raise ProtocolError("RPC response exceeds transfer limit")
        try:
            if not isinstance(json.loads(temporary.read_bytes()), dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise ProtocolError("Invalid sandbox RPC response") from None
        temporary.replace(result)

    def complete(self, identifier, request):
        result = self.path(identifier, ".result")
        if not result.exists():
            try:
                if request.get("op") == "exec":
                    while not self.exec_slots.acquire(timeout=0.1):
                        if self.stop.is_set():
                            raise SessionClosed("Worker stopped")
                    try:
                        self.execute(identifier, request)
                    finally:
                        self.exec_slots.release()
                else:
                    self.execute(identifier, request)
            except (ProtocolError, OSError) as error:
                # Only transport-generated messages are exposed, never request data.
                message = str(error) if isinstance(error, ProtocolError) else "Sandbox RPC could not start"
                write_private(result, json.dumps({"error": message}).encode())
        self.client.upload(identifier, result)
        write_private(self.path(identifier, ".done"), b"acknowledged")
        for suffix in (".claimed", ".request", ".result", ".output"):
            self.path(identifier, suffix).unlink(missing_ok=True)

    def dispatch(self, item):
        identifier = item["id"]
        if identifier in self.active or self.path(identifier, ".done").exists():
            return
        request_file = self.path(identifier, ".request")
        claim = self.path(identifier, ".claimed")
        if claim.exists():
            if json.loads(claim.read_bytes()) != item:
                raise ProtocolError("Relay changed an already claimed request")
        else:
            self.client.download(item, request_file)
        try:
            request = json.loads(request_file.read_bytes()) if request_file.exists() else {}
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise ValueError()
        except (ValueError, TypeError):
            raise ProtocolError("Invalid sandbox request") from None
        is_cancel = request.get("op") == "cancel"
        count = sum(cancel == is_cancel for _, cancel in self.active.values())
        if count >= (2 if is_cancel else 14):
            return
        if not claim.exists():
            write_private(claim, json.dumps(item).encode())
        pool = self.cancel_pool if is_cancel else self.normal_pool
        self.active[identifier] = (pool.submit(self.complete, identifier, request), is_cancel)

    def run(self):
        try:
            while not self.stop.is_set():
                for identifier, (future, _) in list(self.active.items()):
                    if future.done():
                        future.result()
                        del self.active[identifier]
                for item in self.client.poll():
                    self.dispatch(item)
                self.stop.wait(1)
        finally:
            self.close()

    def close(self):
        self.stop.set()
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
        self.normal_pool.shutdown(wait=True, cancel_futures=True)
        self.cancel_pool.shutdown(wait=True, cancel_futures=True)
        if self.instance_lock is not None:
            os.close(self.instance_lock)
            self.instance_lock = None


def main():
    worker = Worker(sys.argv[1])
    for name in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(name, lambda *_: worker.stop.set())
    try:
        worker.run()
    except SessionClosed:
        pass
    except ProtocolError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
