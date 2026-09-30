"""Exercise outbound transport against a local relay without EAS credentials."""

import base64
import hashlib
import http.server
import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest.mock import patch

from environments.eas.worker import Client, ProtocolError, SessionClosed, Worker, child_environment


class Relay:
    def __init__(self):
        self.requests = {}
        self.parts = {}
        self.results = {}
        self.uploads = {}
        self.polls = 0
        self.fail_download_once = False
        self.drop_finalize_once = False
        self.tamper = False
        self.closed = False
        self.status = None
        self.headers = []
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, data=b""):
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_request(self):
                relay.headers.append(self.headers.get("Authorization"))
                if relay.status or relay.closed:
                    return self.answer(relay.status or 410)
                path = urllib.parse.urlsplit(self.path)
                segments = path.path.split("/")
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if path.path == "/poll":
                    relay.polls += 1
                    return self.answer(200, json.dumps({"requests": [item[0] for item in list(relay.requests.values())]}).encode())
                identifier = segments[2]
                part = int(urllib.parse.parse_qs(path.query).get("part", [0])[0])
                if self.command == "GET":
                    if relay.fail_download_once:
                        relay.fail_download_once = False
                        return self.answer(503)
                    content = relay.requests[identifier][1]
                    if relay.tamper:
                        content = b"x" * len(content)
                    start = part * 4 * 1024 * 1024
                    return self.answer(200, content[start:start + 4 * 1024 * 1024])
                if self.command == "PUT":
                    previous = relay.parts.setdefault(identifier, {}).get(part)
                    if previous is not None and previous != data:
                        return self.answer(409)
                    relay.parts[identifier][part] = data
                    return self.answer(200)
                metadata = json.loads(data)
                content = b"".join(value for _, value in sorted(relay.parts[identifier].items()))
                if len(content) != metadata["size"] or hashlib.sha256(content).hexdigest() != metadata["sha256"]:
                    return self.answer(400)
                relay.results[identifier] = json.loads(content)
                relay.uploads[identifier] = relay.uploads.get(identifier, 0) + 1
                if relay.drop_finalize_once:
                    relay.drop_finalize_once = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                return self.answer(200)

            do_GET = do_PUT = do_POST = handle_request

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def add(self, request):
        identifier = uuid.uuid4().hex
        data = json.dumps(request).encode()
        self.requests[identifier] = ({"id": identifier, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, data)
        return identifier

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harbor-https-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.relay = Relay()
        self.addCleanup(self.relay.close)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"url": self.relay.url, "token": "private-relay-token", "expires_at": time.time() + 60}))
        self.config.chmod(0o600)
        self.worker = Worker(self.config, allow_loopback=True)
        self.errors = []
        self.thread = None
        self.addCleanup(self.stop_worker)

    def start_worker(self):
        def run():
            try:
                self.worker.run()
            except SessionClosed:
                pass
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=run)
        self.thread.start()

    def stop_worker(self):
        self.worker.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
            self.assertFalse(self.thread.is_alive(), "Worker did not stop")
        self.worker.close()

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not condition():
            if self.errors:
                raise self.errors[0]
            time.sleep(0.02)
        self.assertTrue(condition())

    def command(self, command, command_id=None):
        return {"op": "exec", "id": command_id or str(uuid.uuid4()), "command": command, "cwd": str(self.root), "timeout": 20}

    def test_retries_and_duplicate_dispatch_do_not_repeat_execution(self):
        self.relay.fail_download_once = True
        self.relay.drop_finalize_once = True
        identifier = self.relay.add(self.command("printf x >> counter; printf hello; printf warning >&2; exit 7"))
        self.start_worker()
        self.wait_for(lambda: self.worker.path(identifier, ".done").exists())
        self.wait_for(lambda: self.relay.polls >= 3)
        self.assertEqual((self.root / "counter").read_text(), "x")
        self.assertEqual(self.relay.results[identifier], {"stdout": "hello", "stderr": "warning", "return_code": 7})
        self.assertEqual(self.relay.uploads[identifier], 2)
        self.assertEqual(set(self.relay.headers), {"Bearer private-relay-token"})
        self.assertFalse(self.errors)

    def test_cancellation_and_polling_continue_during_long_command(self):
        command_id = str(uuid.uuid4())
        identifier = self.relay.add(self.command("touch started; (sleep 4; touch must-not-exist) & wait", command_id))
        self.start_worker()
        self.wait_for(lambda: (self.root / "started").exists())
        polls = self.relay.polls
        cancel = self.relay.add({"op": "cancel", "id": command_id})
        self.wait_for(lambda: identifier in self.relay.results and cancel in self.relay.results)
        self.assertGreater(self.relay.polls, polls)
        self.assertNotEqual(self.relay.results[identifier].get("return_code"), 0)
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_restart_returns_uncertain_error_without_running_command(self):
        identifier = self.relay.add(self.command("touch must-not-exist"))
        item, data = self.relay.requests[identifier]
        self.worker.path(identifier, ".request").write_bytes(data)
        self.worker.path(identifier, ".claimed").write_text(json.dumps(item))
        self.worker.close()
        self.worker = Worker(self.config, allow_loopback=True)
        self.start_worker()
        self.wait_for(lambda: identifier in self.relay.results)
        self.assertIn("not repeated", self.relay.results[identifier]["error"])
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_restart_uploads_saved_result_without_reexecution(self):
        identifier = self.relay.add(self.command("touch must-not-exist"))
        item, data = self.relay.requests[identifier]
        self.worker.path(identifier, ".request").write_bytes(data)
        self.worker.path(identifier, ".claimed").write_text(json.dumps(item))
        self.worker.path(identifier, ".result").write_text('{"stdout":"saved","return_code":0}')
        self.worker.close()
        self.worker = Worker(self.config, allow_loopback=True)
        self.start_worker()
        self.wait_for(lambda: identifier in self.relay.results)
        self.assertEqual(self.relay.results[identifier]["stdout"], "saved")
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_tampered_request_never_executes(self):
        self.relay.tamper = True
        self.relay.add(self.command("touch must-not-exist"))
        self.start_worker()
        self.wait_for(lambda: bool(self.errors))
        self.assertIsInstance(self.errors[0], ProtocolError)
        self.assertIn("checksum", str(self.errors[0]))
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_file_chunks_and_environment_do_not_leak_controller_credentials(self):
        data = bytes(range(256)) * (20 * 1024)
        upload = self.relay.add({"op": "put", "path": str(self.root / "binary"), "mode": 0o644, "data": base64.b64encode(data).decode()})
        with patch.dict(os.environ, {"EXPO_TOKEN": "secret-expo", "ANTHROPIC_API_KEY": "secret-model", "RELAY_TOKEN": "secret-relay"}):
            inspect = self.relay.add(self.command("env"))
            self.start_worker()
            self.wait_for(lambda: inspect in self.relay.results and upload in self.relay.results)
        self.assertEqual((self.root / "binary").read_bytes(), data)
        download = self.relay.add({"op": "get", "path": str(self.root / "binary")})
        self.wait_for(lambda: download in self.relay.results)
        self.assertEqual(base64.b64decode(self.relay.results[download]["data"]), data)
        self.assertGreater(len(self.relay.parts[download]), 1)
        environment = self.relay.results[inspect]["stdout"]
        self.assertNotIn("secret-", environment)
        self.assertNotIn("private-relay-token", environment)
        self.assertEqual(self.worker.state.stat().st_mode & 0o777, 0o700)

    def test_session_close_terminates_running_command(self):
        self.relay.add(self.command("touch started; (sleep 4; touch must-not-exist) & wait"))
        self.start_worker()
        self.wait_for(lambda: (self.root / "started").exists())
        self.relay.closed = True
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse((self.root / "must-not-exist").exists())
        self.assertFalse(self.errors)

    def test_second_helper_cannot_execute_same_session(self):
        with self.assertRaisesRegex(ValueError, "already owns"):
            Worker(self.config, allow_loopback=True)

    def test_credentials_and_redirects_are_rejected_without_retry(self):
        self.relay.status = 403
        with self.assertRaisesRegex(ProtocolError, "403"):
            self.worker.client.poll()
        self.assertEqual(len(self.relay.headers), 1)
        self.relay.status = 302
        client = Client(json.loads(self.config.read_text()), threading.Event(), allow_loopback=True)
        with self.assertRaisesRegex(ProtocolError, "302"):
            client.poll()
        self.assertEqual(len(self.relay.headers), 2)
        with self.assertRaises(ValueError):
            Client({"url": self.relay.url, "token": "test", "expires_at": time.time() + 10}, threading.Event())
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            Worker(self.config, allow_loopback=True)
        with patch.dict(os.environ, {"EXPO_TOKEN": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret"}):
            self.assertNotIn("EXPO_TOKEN", child_environment())
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", child_environment())


if __name__ == "__main__":
    unittest.main()
