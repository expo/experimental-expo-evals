"""Controller-side requests to the per-run HTTPS bridge."""

import asyncio
import json
from http.client import HTTPException
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY = 256 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(value: str, *, local: bool = False) -> str:
    url = urlsplit(value)
    loopback = local and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (url.scheme != "https" and not loopback) or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("EAS bridge URL must use HTTPS, or HTTP on loopback for the controller")
    return value.rstrip("/")


class Bridge:
    def __init__(self, url: str, token: str):
        self.url = validate_url(url, local=True)
        if len(token) < 32:
            raise ValueError("EAS bridge requires a controller token")
        self.token = token

    def _request(self, method: str, path: str, data: bytes | None, timeout: float):
        request = Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with build_opener(NoRedirect).open(request, timeout=timeout) as response:
                content = response.read(MAX_BODY + 1)
                if len(content) > MAX_BODY:
                    raise ValueError("EAS bridge response exceeds 256 MiB")
                return response.status, content
        except HTTPError as error:
            # Error bodies may include upstream diagnostics; never echo credentials or URLs.
            status = error.code
            error.close()
            if status in {408, 429} or status >= 500:
                raise URLError(f"bridge status {status}") from None
            raise RuntimeError(f"EAS bridge rejected {method} request ({status})") from None

    async def request(self, method: str, path: str, data=None, *, timeout: float = 30):
        body = json.dumps(data).encode() if data is not None else None
        if body is not None and len(body) > MAX_BODY:
            raise ValueError("EAS bridge request exceeds 256 MiB")
        deadline = time.monotonic() + timeout
        delay = 0.25
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("EAS HTTPS bridge request timed out")
            try:
                return await asyncio.to_thread(self._request, method, path, body, min(20, remaining))
            except (URLError, TimeoutError, ConnectionError, OSError, HTTPException):
                if time.monotonic() >= deadline:
                    raise TimeoutError("EAS HTTPS bridge request timed out") from None
                await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
                delay = min(2, delay * 2)
