"""OpenAI-compatible HTTPS transport. No credentials in persisted requests."""

import json
import os
import stat
import ssl
import subprocess
import threading
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class CallFailure(RuntimeError):
    def __init__(self, category, status=None, transport_kind=None):
        super().__init__(category)
        self.category = category
        self.status = status
        self.transport_kind = transport_kind


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authorization header to an unexpected host.
        return None


def load_key(env_name, key_file=None):
    if key_file:
        path = Path(key_file).absolute()
        if path.resolve() != path:
            raise PermissionError("credential symlinks are not allowed")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError(
                    "credential file must be owned by this user with mode 600"
                )
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(16385)
            key = raw.decode("utf-8").removesuffix("\n")
        finally:
            os.close(fd)
    else:
        key = os.environ.get(env_name, "")
    if not key or len(key) > 16384 or any(c.isspace() for c in key):
        raise CallFailure("credential_missing_or_malformed")
    return key


class APIClient:
    def __init__(self, config, key):
        self.config = config
        self._key = key
        self._start_lock = threading.Lock()
        self._last_start = 0.0
        self._stop_event = None

    def bind_stop_event(self, event):
        """Bind a campaign's stop signal; in-flight requests cannot be recalled."""
        self._stop_event = event

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self._key, "[REDACTED]")
        if isinstance(value, list):
            return [self.redact(x) for x in value]
        if isinstance(value, dict):
            return {self.redact(k): self.redact(v) for k, v in value.items()}
        return value

    def _request(self, endpoint, payload=None):
        with self._start_lock:
            delay = max(0, 1.0 - (time.monotonic() - self._last_start))
            if self._stop_event is None:
                time.sleep(delay)
            elif self._stop_event.wait(delay):
                raise CallFailure("paused")
            self._last_start = time.monotonic()
        if self.config.transport == "curl":
            return self._curl_request(endpoint, payload)
        request = Request(
            self.config.base_url.rstrip("/") + "/" + endpoint,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + self._key,
                "Content-Type": "application/json",
                "User-Agent": "infix-dag-builder/0.1",
            },
        )
        handlers = [NoRedirect()]
        if self.config.tls_max_version == "TLSv1.2":
            # Explicit per-run compatibility setting, never disable verification.
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.maximum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(["http/1.1"])
            handlers.append(HTTPSHandler(context=context))
        try:
            with build_opener(*handlers).open(
                request, timeout=self.config.timeout_seconds
            ) as response:
                raw = response.read(16_000_001)
        except HTTPError as error:
            code = error.code
            error.close()
            category = (
                "authentication"
                if code in (401, 403)
                else "rate_limit"
                if code == 429
                else "uncertain_remote_state"
                if code >= 500 or code == 408
                else "http_error"
            )
            raise CallFailure(category, code) from None
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            # Preserve only an exception class, never URLs, headers or raw text.
            # A timeout/disconnect may occur after billing; retry policy is explicit.
            cause = error.reason if isinstance(error, URLError) else error
            raise CallFailure(
                "uncertain_remote_state", transport_kind=type(cause).__name__
            ) from None
        if len(raw) > 16_000_000:
            raise CallFailure("response_too_large")
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeError):
            return {
                "_transport_error": "invalid_http_json",
                "_raw_text": self.redact(raw.decode("utf-8", errors="replace")),
            }
        if not isinstance(response, dict):
            raise CallFailure("invalid_http_json")
        return self.redact(response)

    def _curl_request(self, endpoint, payload):
        """Optional native HTTPS transport; secrets only on stdin, never argv.

        Disable curlrc, redirects, retries and insecure TLS options. Keep the same
        response parser/contract downstream; transport changes no model controls.
        """
        settings = [
            "url = " + json.dumps(self.config.base_url.rstrip("/") + "/" + endpoint),
            "header = " + json.dumps("Authorization: Bearer " + self._key),
            'header = "Content-Type: application/json"',
            'user-agent = "infix-dag-builder/0.1"',
        ]
        if payload is not None:
            settings.append("data-binary = " + json.dumps(json.dumps(payload)))
        command = ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--proto", "=https",
                   "--max-redirs", "0", "--connect-timeout", "30", "--max-time", str(self.config.timeout_seconds),
                   "--max-filesize", "16000000", "--write-out", "\n%{http_code}", "--config", "-"]
        if self.config.tls_max_version == "TLSv1.2":
            command.extend(["--tlsv1.2", "--tls-max", "1.2"])
        try:
            process = subprocess.run(command, input="\n".join(settings).encode(), capture_output=True,
                                     timeout=self.config.timeout_seconds + 10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CallFailure("uncertain_remote_state", transport_kind=type(error).__name__) from None
        if process.returncode:
            category = "response_too_large" if process.returncode == 63 else "uncertain_remote_state"
            raise CallFailure(category, transport_kind="curl_exit_" + str(process.returncode))
        raw, _, code = process.stdout.rpartition(b"\n")
        if len(raw) > 16_000_000:
            raise CallFailure("response_too_large")
        if len(code) != 3 or not code.isdigit():
            raise CallFailure("uncertain_remote_state", transport_kind="invalid_curl_status")
        status = int(code)
        if not 200 <= status < 300:
            category = ("authentication" if status in (401, 403) else "rate_limit" if status == 429
                        else "uncertain_remote_state" if status >= 500 or status == 408 else "http_error")
            raise CallFailure(category, status)
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeError):
            return {"_transport_error": "invalid_http_json",
                    "_raw_text": self.redact(raw.decode("utf-8", errors="replace"))}
        if not isinstance(response, dict):
            raise CallFailure("invalid_http_json")
        return self.redact(response)

    def complete(self, payload):
        return self._request("chat/completions", payload)

    def probe(self):
        response = self._request("models")
        ids = [
            row["id"]
            for row in response.get("data", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        ]
        return {
            "requested_model": self.config.model,
            "listed": self.config.model in ids,
            "matching_model_ids": [x for x in ids if "deepseek" in x.lower()],
        }
