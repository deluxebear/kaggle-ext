"""Pure-stdlib Jupyter Proxy REST and RFC 6455 asyncio WebSocket client (`kaggle-ext`).

Zero third-party dependencies and zero imports of `kaggle` or `kagglesdk`. Implements
HTTP liveness probing, kernel management, hidden-frame env injection, cell-by-cell
execution, and concurrent `iopub` monitoring over Kaggle's `jupyter-proxy.kaggle.net`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sys
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import sessions

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024
_DEFAULT_PING_INTERVAL = 25.0
_DEFAULT_PONG_TIMEOUT = 90.0
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TITLE_RE = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)


class SessionWsError(RuntimeError):
    """Base error raised by the Kaggle Jupyter proxy HTTP / WebSocket client."""


class SessionAuthError(SessionWsError):
    """Proxy rejected credentials with HTTP 401/403."""


class SessionLostError(SessionWsError):
    """Proxy or kernel returned HTTP 404 / 502 / 503 / 504 (reclaimed or gone)."""


class SessionExecutionTimeoutError(TimeoutError):
    """Execution or watch deadline exceeded."""


# --------------------------------------------------------------------------------------
# TLS & HTTP helpers
# --------------------------------------------------------------------------------------


def build_ssl_context(ca_cert: str | None = None) -> ssl.SSLContext:
    """Create a TLS context honoring explicit `ca_cert`, `SSL_CERT_FILE`, or `REQUESTS_CA_BUNDLE`."""
    for candidate in (
        ca_cert,
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
    ):
        if candidate and os.path.isfile(candidate):
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def _join_proxy_url(base_url: str, sub_path: str) -> str:
    """Join `sub_path` onto `base_url` preserving proxy path prefixes like `/k/<id>/<seg>/proxy`."""
    parsed = urllib.parse.urlsplit(base_url.strip())
    base_path = parsed.path.rstrip("/")
    rel = sub_path.lstrip("/")
    joined_path = f"{base_path}/{rel}" if rel else (base_path or "/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, joined_path, parsed.query, ""))


def _ws_channels_url(base_url: str, kernel_id: str, client_session_id: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    ws_scheme = "wss" if parsed.scheme.lower() == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    chan_path = f"{base_path}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}/channels"
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in params if k != "session_id"]
    filtered.append(("session_id", client_session_id))
    return urllib.parse.urlunsplit(
        (ws_scheme, parsed.netloc, chan_path, urllib.parse.urlencode(filtered), "")
    )


def proxy_http_json(
    method: str,
    base_url: str,
    sub_path: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
    ca_cert: str | None = None,
) -> Any:
    """Perform an HTTP JSON request against the Jupyter proxy with redacted errors."""
    url = _join_proxy_url(base_url, sub_path)
    safe_url = sessions.redact_session_secrets(url)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "kaggle-ext-session",
    }
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    ctx = build_ssl_context(ca_cert) if url.lower().startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        err_body = ""
        with contextlib.suppress(Exception):
            err_body = err.read().decode("utf-8", errors="replace")[:300]
        safe_detail = sessions.redact_session_secrets(err_body)
        if err.code in (401, 403):
            raise SessionAuthError(f"Jupyter proxy auth failed (HTTP {err.code}) at {safe_url}") from err
        if err.code in (404, 502, 503, 504):
            raise SessionLostError(
                f"Jupyter proxy or kernel unavailable (HTTP {err.code}) at {safe_url}"
            ) from err
        raise SessionWsError(
            f"Jupyter proxy HTTP {err.code} on {method} {safe_url}: {safe_detail}"
        ) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SessionWsError(
            f"Jupyter proxy request failed ({method} {safe_url}): {sessions.redact_session_secrets(err)}"
        ) from err

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as err:
        raise SessionWsError(f"Jupyter proxy returned invalid JSON from {safe_url}: {err}") from err


def probe_proxy_liveness(
    base_url: str,
    *,
    saved_kernel_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    ca_cert: str | None = None,
) -> dict[str, Any]:
    """Probe `GET /api/kernels` on the Kaggle Jupyter proxy as primary liveness truth."""
    try:
        kernels_raw = proxy_http_json(
            "GET",
            base_url,
            "api/kernels",
            headers=headers,
            timeout=timeout,
            ca_cert=ca_cert,
        )
    except (SessionAuthError, SessionLostError) as exc:
        return {
            "alive": False,
            "state": "reclaimed",
            "kernels": [],
            "kernel_id": saved_kernel_id,
            "execution_state": None,
            "error": str(exc),
        }
    except SessionWsError as exc:
        return {
            "alive": False,
            "state": "unreachable",
            "kernels": [],
            "kernel_id": saved_kernel_id,
            "execution_state": None,
            "error": str(exc),
        }

    kernels = kernels_raw if isinstance(kernels_raw, list) else []
    chosen_id = saved_kernel_id
    exec_state: str | None = None
    matched = None
    if saved_kernel_id:
        for item in kernels:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == saved_kernel_id:
                matched = item
                break
    if matched is None:
        for item in kernels:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                matched = item
                break
    if isinstance(matched, dict):
        chosen_id = str(matched["id"]).strip()
        raw_exec = matched.get("execution_state")
        exec_state = str(raw_exec) if raw_exec is not None else None

    return {
        "alive": True,
        "state": "running",
        "kernels": kernels,
        "kernel_id": chosen_id,
        "execution_state": exec_state,
        "error": None,
    }


def ensure_jupyter_kernel(
    base_url: str,
    *,
    saved_kernel_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
    ca_cert: str | None = None,
) -> tuple[str, str]:
    """Return `(kernel_id, execution_state)` from the live proxy, spawning `python3` if empty."""
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            if saved_kernel_id:
                try:
                    info = proxy_http_json(
                        "GET",
                        base_url,
                        f"api/kernels/{urllib.parse.quote(saved_kernel_id, safe='')}",
                        headers=headers,
                        timeout=timeout,
                        ca_cert=ca_cert,
                    )
                    if isinstance(info, dict) and isinstance(info.get("id"), str) and info["id"].strip():
                        return info["id"].strip(), str(info.get("execution_state") or "idle")
                except SessionLostError:
                    pass

            listed = proxy_http_json(
                "GET",
                base_url,
                "api/kernels",
                headers=headers,
                timeout=timeout,
                ca_cert=ca_cert,
            )
            if isinstance(listed, list):
                for item in listed:
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                        return item["id"].strip(), str(item.get("execution_state") or "idle")

            created = proxy_http_json(
                "POST",
                base_url,
                "api/kernels",
                headers=headers,
                payload={"name": "python3"},
                timeout=timeout,
                ca_cert=ca_cert,
            )
            if isinstance(created, dict) and isinstance(created.get("id"), str) and created["id"].strip():
                return created["id"].strip(), str(created.get("execution_state") or "starting")
            raise SessionWsError("Jupyter proxy did not return a kernel id")
        except SessionAuthError:
            raise
        except SessionLostError as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except SessionWsError as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    assert last_err is not None
    raise last_err


def restart_kernel(
    base_url: str,
    kernel_id: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
    ca_cert: str | None = None,
) -> dict[str, Any]:
    res = proxy_http_json(
        "POST",
        base_url,
        f"api/kernels/{urllib.parse.quote(kernel_id, safe='')}/restart",
        headers=headers,
        payload={},
        timeout=timeout,
        ca_cert=ca_cert,
    )
    return res if isinstance(res, dict) else {}


def interrupt_kernel(
    base_url: str,
    kernel_id: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    ca_cert: str | None = None,
) -> dict[str, Any]:
    res = proxy_http_json(
        "POST",
        base_url,
        f"api/kernels/{urllib.parse.quote(kernel_id, safe='')}/interrupt",
        headers=headers,
        payload={},
        timeout=timeout,
        ca_cert=ca_cert,
    )
    return res if isinstance(res, dict) else {}


# --------------------------------------------------------------------------------------
# Minimal pure-stdlib RFC 6455 asyncio WebSocket & Jupyter v5.3 codec
# --------------------------------------------------------------------------------------


def _mask_bytes(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    repeated = (mask * ((n // 4) + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


class AsyncWebSocket:
    """Minimal pure-stdlib RFC 6455 asyncio WebSocket client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._write_lock = asyncio.Lock()
        self.last_rx_mono = time.monotonic()
        self.closed = False

    @classmethod
    async def connect(
        cls,
        ws_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        ca_cert: str | None = None,
    ) -> "AsyncWebSocket":
        parsed = urllib.parse.urlsplit(ws_url)
        scheme = parsed.scheme.lower()
        safe_url = sessions.redact_session_secrets(ws_url)
        if scheme not in ("ws", "wss") or not parsed.hostname:
            raise SessionWsError(f"Invalid WebSocket URL: {safe_url}")
        host = parsed.hostname
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        is_tls = scheme == "wss"
        default_port = 443 if is_tls else 80
        host_header = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_header = f"{host_header}:{port}"

        ssl_ctx = build_ssl_context(ca_cert) if is_tls else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host,
                    port,
                    ssl=ssl_ctx,
                    server_hostname=host if is_tls else None,
                    limit=131072,
                ),
                timeout=timeout,
            )
        except (OSError, TimeoutError) as exc:
            raise SessionWsError(
                f"WebSocket TCP/TLS connection failed for {safe_url}: {sessions.redact_session_secrets(exc)}"
            ) from exc

        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            req_headers = {
                "Host": host_header,
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": ws_key,
                "Sec-WebSocket-Version": "13",
                **(dict(headers) if headers else {}),
            }
            lines = [f"GET {path} HTTP/1.1"]
            lines.extend(f"{k}: {v}" for k, v in req_headers.items())
            lines.append("")
            lines.append("")
            writer.write("\r\n".join(lines).encode("ascii"))
            await asyncio.wait_for(writer.drain(), timeout=timeout)

            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
            head_text = raw_head.decode("latin-1")
            head_lines = head_text.split("\r\n")
            if not head_lines:
                raise SessionWsError("Empty WebSocket handshake response")
            status_parts = head_lines[0].split(None, 2)
            if len(status_parts) < 2 or not status_parts[1].isdigit():
                raise SessionWsError(
                    f"Invalid WebSocket handshake status line: {sessions.redact_session_secrets(head_lines[0])}"
                )
            status_code = int(status_parts[1])
            if status_code in (401, 403):
                raise SessionAuthError(
                    f"WebSocket handshake rejected (HTTP {status_code}) for {safe_url}"
                )
            if status_code in (404, 502, 503, 504):
                raise SessionLostError(
                    f"WebSocket kernel unavailable (HTTP {status_code}) for {safe_url}"
                )
            if status_code != 101:
                raise SessionWsError(
                    f"WebSocket handshake failed (HTTP {status_code}): {sessions.redact_session_secrets(head_lines[0])}"
                )

            resp_headers: dict[str, str] = {}
            for line in head_lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    resp_headers[k.strip().lower()] = v.strip()
            expected_accept = base64.b64encode(
                hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if resp_headers.get("sec-websocket-accept") != expected_accept:
                raise SessionWsError("WebSocket Sec-WebSocket-Accept verification failed")
            return cls(reader, writer)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise SessionWsError("WebSocket connection is closed.")
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        header.extend(mask)
        frame = bytes(header) + _mask_bytes(payload, mask)
        async with self._write_lock:
            self.writer.write(frame)
            await self.writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(0x1, text.encode("utf-8"))

    async def send_ping(self, data: bytes = b"") -> None:
        await self._send_frame(0x9, data[:125])

    async def send_pong(self, data: bytes = b"") -> None:
        await self._send_frame(0xA, data[:125])

    async def _read_single_frame(self) -> tuple[bool, int, bytes]:
        head = await self.reader.readexactly(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self.reader.readexactly(8), "big")
        if length > _MAX_WS_MESSAGE_BYTES:
            raise SessionWsError(f"WebSocket frame too large ({length} bytes)")
        mask_key = await self.reader.readexactly(4) if masked else b""
        payload = await self.reader.readexactly(length) if length else b""
        if masked and payload:
            payload = _mask_bytes(payload, mask_key)
        self.last_rx_mono = time.monotonic()
        return fin, opcode, payload

    async def recv_message(self) -> tuple[int, bytes]:
        frag_opcode: int | None = None
        chunks: list[bytes] = []
        total_len = 0
        while True:
            fin, opcode, payload = await self._read_single_frame()
            if opcode == 0x8:
                self.closed = True
                raise EOFError("WebSocket closed by server")
            if opcode == 0x9:
                with contextlib.suppress(Exception):
                    await self.send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                if frag_opcode is None:
                    raise SessionWsError("Unexpected WebSocket continuation frame")
                total_len += len(payload)
                if total_len > _MAX_WS_MESSAGE_BYTES:
                    raise SessionWsError("Fragmented WebSocket message exceeded max size")
                chunks.append(payload)
                if fin:
                    return frag_opcode, b"".join(chunks)
                continue
            if not fin:
                frag_opcode = opcode
                total_len = len(payload)
                chunks = [payload]
                continue
            return opcode, payload

    async def close(self) -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._send_frame(0x8, (1000).to_bytes(2, "big")), timeout=1.0)
        self.closed = True
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)


def encode_jupyter_msg(
    channel: str,
    msg_type: str,
    content: dict[str, Any],
    session_id: str,
) -> tuple[str, str]:
    msg_id = str(uuid.uuid4())
    msg = {
        "header": {
            "msg_id": msg_id,
            "username": "username",
            "session": session_id,
            "date": datetime.now(timezone.utc).isoformat(),
            "msg_type": msg_type,
            "version": "5.3",
        },
        "msg_id": msg_id,
        "msg_type": msg_type,
        "parent_header": {},
        "metadata": {},
        "content": content,
        "buffers": [],
        "channel": channel,
    }
    return msg_id, json.dumps(msg, ensure_ascii=False)


def decode_jupyter_ws_frame(opcode: int, payload: bytes) -> dict[str, Any]:
    if opcode == 0x1:
        return json.loads(payload.decode("utf-8"))
    if opcode == 0x2:
        if len(payload) < 8:
            raise ValueError("Invalid binary Jupyter WS frame")
        nbufs = int.from_bytes(payload[:4], "big")
        if nbufs < 1 or len(payload) < 4 * (nbufs + 1):
            raise ValueError("Invalid binary Jupyter WS buffer count")
        json_start = int.from_bytes(payload[4:8], "big")
        json_stop = int.from_bytes(payload[8:12], "big") if nbufs >= 2 else len(payload)
        if not (0 <= json_start <= json_stop <= len(payload)):
            raise ValueError("Invalid binary Jupyter WS offsets")
        return json.loads(payload[json_start:json_stop].decode("utf-8"))
    raise ValueError(f"Unsupported WebSocket opcode: {opcode}")


async def connect_ws(
    ws_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    ca_cert: str | None = None,
) -> AsyncWebSocket:
    """Connect RFC 6455 WebSocket, falling back to Kaggle `Origin` only on HTTP 403."""
    base_headers = dict(headers) if headers else {}
    try:
        return await AsyncWebSocket.connect(
            ws_url,
            headers=base_headers,
            timeout=timeout,
            ca_cert=ca_cert,
        )
    except SessionAuthError as err:
        if "HTTP 403" in str(err) and "Origin" not in base_headers:
            with_origin = {**base_headers, "Origin": "https://www.kaggle.com"}
            return await AsyncWebSocket.connect(
                ws_url,
                headers=with_origin,
                timeout=timeout,
                ca_cert=ca_cert,
            )
        raise


# --------------------------------------------------------------------------------------
# Input parsing: `--env` / `--env-file` & `.py` / `.ipynb` / `-c` / stdin
# --------------------------------------------------------------------------------------


def _unquote_env_value(val: str) -> str:
    stripped = val.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        inner = stripped[1:-1]
        if stripped[0] == '"':
            inner = (
                inner.replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return inner
    return stripped


def parse_env_inputs(
    env_list: list[str] | None = None,
    env_file: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parse `--env-file` and `--env KEY[=VAL]` into an isolated dict without touching argv."""
    src_env = os.environ if environ is None else environ
    parsed: dict[str, str] = {}

    if env_file is not None:
        fpath = os.path.abspath(os.path.expanduser(env_file))
        if not os.path.isfile(fpath):
            raise sessions.SessionError(f"Env file not found: {fpath}")
        try:
            lines = Path(fpath).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise sessions.SessionError(f"Could not read env file {fpath}: {exc}") from exc
        for lineno, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export ") and len(line) > 7 and line[7:].lstrip():
                line = line[7:].lstrip()
            if "=" not in line:
                raise sessions.SessionError(f"Invalid line {lineno} in env file {fpath}: expected KEY=VALUE.")
            key, raw_val = line.split("=", 1)
            key = key.strip()
            if not _ENV_KEY_RE.fullmatch(key):
                raise sessions.SessionError(f"Invalid environment variable name {key!r} at {fpath}:{lineno}.")
            parsed[key] = _unquote_env_value(raw_val)

    for item in env_list or []:
        if "=" in item:
            key, val = item.split("=", 1)
            key = key.strip()
            if not _ENV_KEY_RE.fullmatch(key):
                raise sessions.SessionError(f"Invalid environment variable name {key!r}.")
            parsed[key] = val
        else:
            key = item.strip()
            if not _ENV_KEY_RE.fullmatch(key):
                raise sessions.SessionError(f"Invalid environment variable name {key!r}.")
            if key not in src_env:
                raise sessions.SessionError(
                    f"Environment variable {key!r} passed to `--env` is not set in local environment."
                )
            parsed[key] = src_env[key]

    return parsed


def _cells_from_notebook_json(raw_text: str, origin: str) -> list[tuple[str, str]]:
    try:
        nb = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise sessions.SessionError(f"Invalid notebook JSON in {origin}: {exc}") from exc
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise sessions.SessionError(f"Expected notebook object with 'cells' list in {origin}.")
    cells: list[tuple[str, str]] = []
    for idx, cell in enumerate(nb["cells"], start=1):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(str(part) for part in source)
        elif not isinstance(source, str):
            source = str(source)
        if not source.strip():
            continue
        title_m = _TITLE_RE.search(source)
        label = (
            title_m.group(1).strip()
            if title_m
            else (str(cell.get("id")).strip() if cell.get("id") else f"cell-{idx}")
        )
        cells.append((label, source))
    return cells


def load_execution_cells(
    *,
    code: str | None = None,
    file_path: str | None = None,
    stdin_stream=None,
) -> list[tuple[str, str]]:
    """Load executable `(label, source)` cells from `-c`, a `.py`/`.ipynb` file, or `-` stdin."""
    if code is not None and file_path is not None:
        raise sessions.SessionError("Specify only one of `--code` (`-c`) or `--file` (`-f`).")
    if code is None and file_path is None:
        raise sessions.SessionError("Provide either `-c <CODE>` or `-f <FILE|->`.")

    if code is not None:
        return [("inline", code)]

    assert file_path is not None
    if file_path == "-":
        stream = sys.stdin if stdin_stream is None else stdin_stream
        raw = stream.read()
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            try:
                maybe_nb = json.loads(stripped)
            except json.JSONDecodeError:
                maybe_nb = None
            if isinstance(maybe_nb, dict) and isinstance(maybe_nb.get("cells"), list):
                return _cells_from_notebook_json(stripped, "<stdin>")
        return [("stdin", raw)]

    resolved = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.isfile(resolved):
        raise sessions.SessionError(f"Execution file not found: {resolved}")
    try:
        raw = Path(resolved).read_text(encoding="utf-8")
    except OSError as exc:
        raise sessions.SessionError(f"Could not read execution file {resolved}: {exc}") from exc

    if resolved.lower().endswith(".ipynb"):
        return _cells_from_notebook_json(raw, resolved)
    return [(os.path.basename(resolved), raw)]


# --------------------------------------------------------------------------------------
# Cell execution & read-only iopub watcher
# --------------------------------------------------------------------------------------


def _emit_chunk(
    text: str,
    *,
    is_err: bool,
    stdout_sink: Callable[[str], None] | None,
    stderr_sink: Callable[[str], None] | None,
    log_sink: Callable[[str], None] | None,
) -> None:
    if not text:
        return
    if log_sink is not None:
        log_sink(text)
    sink = stderr_sink if is_err else stdout_sink
    if sink is not None:
        sink(text)


async def _execute_single_request(
    ws: AsyncWebSocket,
    *,
    code: str,
    client_session_id: str,
    silent: bool,
    store_history: bool,
    deadline: float | None,
    ping_interval: float,
    pong_timeout: float,
    stdout_sink: Callable[[str], None] | None,
    stderr_sink: Callable[[str], None] | None,
    log_sink: Callable[[str], None] | None,
) -> tuple[str, str | None]:
    msg_id, frame = encode_jupyter_msg(
        "shell",
        "execute_request",
        {
            "code": code,
            "silent": silent,
            "store_history": store_history,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
        client_session_id,
    )
    await ws.send_text(frame)

    shell_reply: dict[str, Any] | None = None
    iopub_idle = False
    error_summary: str | None = None
    next_ping_at = time.monotonic() + ping_interval
    recv_task: asyncio.Task[tuple[int, bytes]] | None = None

    try:
        while not (shell_reply is not None and iopub_idle):
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise SessionExecutionTimeoutError("Execution timed out before kernel finished.")
            if (now - ws.last_rx_mono) > pong_timeout:
                raise SessionWsError("WebSocket connection timed out (no pong/frame received).")
            if now >= next_ping_at:
                await ws.send_ping()
                next_ping_at = now + ping_interval

            wakeups = [
                max(0.05, next_ping_at - time.monotonic()),
                max(0.05, (ws.last_rx_mono + pong_timeout) - time.monotonic()),
            ]
            if deadline is not None:
                wakeups.append(max(0.0, deadline - time.monotonic()))
            wait_sec = max(0.05, min(wakeups))

            if recv_task is None:
                recv_task = asyncio.create_task(ws.recv_message())
            done, _ = await asyncio.wait({recv_task}, timeout=wait_sec)
            if not done:
                continue

            opcode, payload = recv_task.result()
            recv_task = None
            msg = decode_jupyter_ws_frame(opcode, payload)
            channel = msg.get("channel") or "iopub"
            header = msg.get("header") or {}
            parent = msg.get("parent_header") or {}
            msg_type = msg.get("msg_type") or header.get("msg_type") or ""
            content = msg.get("content") or {}

            if channel == "iopub" and msg_type == "status":
                exec_state = content.get("execution_state")
                if exec_state in ("dead", "restarting"):
                    err_txt = f"RuntimeError: Kernel {exec_state} before execution finished.\n"
                    _emit_chunk(
                        err_txt,
                        is_err=True,
                        stdout_sink=stdout_sink,
                        stderr_sink=stderr_sink,
                        log_sink=log_sink,
                    )
                    raise SessionLostError(err_txt.strip())

            if parent.get("msg_id") != msg_id:
                continue

            if channel == "shell" and msg_type == "execute_reply":
                shell_reply = msg
                status = str(content.get("status") or "ok")
                if status in ("error", "abort") and error_summary is None:
                    ename = content.get("ename") or "Error"
                    evalue = content.get("evalue") or status
                    error_summary = sessions.redact_session_secrets(f"{ename}: {evalue}")
            elif channel == "iopub":
                if msg_type == "stream":
                    text = str(content.get("text") or "")
                    is_err = content.get("name") == "stderr"
                    if not silent:
                        _emit_chunk(
                            text,
                            is_err=is_err,
                            stdout_sink=stdout_sink,
                            stderr_sink=stderr_sink,
                            log_sink=log_sink,
                        )
                elif msg_type in ("display_data", "execute_result"):
                    data = content.get("data") or {}
                    plain = data.get("text/plain")
                    if isinstance(plain, str) and plain and not silent:
                        rendered = plain if plain.endswith("\n") else plain + "\n"
                        _emit_chunk(
                            rendered,
                            is_err=False,
                            stdout_sink=stdout_sink,
                            stderr_sink=stderr_sink,
                            log_sink=log_sink,
                        )
                elif msg_type == "error":
                    ename = content.get("ename") or "Error"
                    evalue = content.get("evalue") or ""
                    error_summary = sessions.redact_session_secrets(f"{ename}: {evalue}")
                    tb = content.get("traceback") or []
                    rendered_err = (
                        "".join(str(line) for line in tb) + "\n"
                        if tb
                        else f"{ename}: {evalue}\n"
                    )
                    _emit_chunk(
                        rendered_err,
                        is_err=True,
                        stdout_sink=stdout_sink,
                        stderr_sink=stderr_sink,
                        log_sink=log_sink,
                    )
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    iopub_idle = True

        reply_status = str((shell_reply.get("content") or {}).get("status") or "ok")
        return reply_status, error_summary
    finally:
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
            with contextlib.suppress(BaseException):
                await recv_task


async def run_cells_async(
    base_url: str,
    kernel_id: str,
    cells: list[tuple[str, str]],
    *,
    client_session_id: str | None = None,
    env_vars: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    interrupt_on_cancel: bool = True,
    ca_cert: str | None = None,
    ping_interval: float = _DEFAULT_PING_INTERVAL,
    pong_timeout: float = _DEFAULT_PONG_TIMEOUT,
    stdout_sink: Callable[[str], None] | None = None,
    stderr_sink: Callable[[str], None] | None = None,
    log_sink: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute `(label, code)` cells sequentially over a single Kaggle Jupyter WebSocket."""
    sid = client_session_id or str(uuid.uuid4())
    ws_url = _ws_channels_url(base_url, kernel_id, sid)
    connect_to = min(30.0, timeout) if timeout is not None and timeout > 0 else 30.0
    deadline = (time.monotonic() + timeout) if (timeout is not None and timeout > 0) else None

    ws = await connect_ws(ws_url, headers=headers, timeout=connect_to, ca_cert=ca_cert)
    cells_executed = 0
    try:
        if env_vars:
            prelude = (
                "import os as _ke_os\n"
                f"_ke_os.environ.update({dict(env_vars)!r})\n"
                "del _ke_os"
            )
            env_status, env_err = await _execute_single_request(
                ws,
                code=prelude,
                client_session_id=sid,
                silent=True,
                store_history=False,
                deadline=deadline,
                ping_interval=ping_interval,
                pong_timeout=pong_timeout,
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
                log_sink=None,  # Never write secret env prelude errors/details unredacted.
            )
            if env_status != "ok":
                return {
                    "ok": False,
                    "exit_code": 1,
                    "cells_executed": 0,
                    "cells_total": len(cells),
                    "failed_cell": "<env-prelude>",
                    "error": env_err or f"Environment injection failed ({env_status})",
                    "client_session_id": sid,
                }

        for label, src in cells:
            status, err_summary = await _execute_single_request(
                ws,
                code=src,
                client_session_id=sid,
                silent=False,
                store_history=True,
                deadline=deadline,
                ping_interval=ping_interval,
                pong_timeout=pong_timeout,
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
                log_sink=log_sink,
            )
            cells_executed += 1
            if status in ("error", "abort"):
                return {
                    "ok": False,
                    "exit_code": 1,
                    "cells_executed": cells_executed,
                    "cells_total": len(cells),
                    "failed_cell": label,
                    "error": err_summary or f"Cell {label!r} returned status {status}",
                    "client_session_id": sid,
                }

        return {
            "ok": True,
            "exit_code": 0,
            "cells_executed": cells_executed,
            "cells_total": len(cells),
            "failed_cell": None,
            "error": None,
            "client_session_id": sid,
        }
    except SessionExecutionTimeoutError:
        if interrupt_on_cancel:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    interrupt_kernel,
                    base_url,
                    kernel_id,
                    headers=headers,
                    timeout=10.0,
                    ca_cert=ca_cert,
                )
        raise
    except EOFError as exc:
        raise SessionLostError(f"WebSocket closed while executing: {exc}") from exc
    finally:
        await ws.close()


def run_cells(*args, **kwargs) -> dict[str, Any]:
    """Synchronous wrapper around `run_cells_async()`."""
    return asyncio.run(run_cells_async(*args, **kwargs))


async def watch_kernel_async(
    base_url: str,
    kernel_id: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    ca_cert: str | None = None,
    ping_interval: float = _DEFAULT_PING_INTERVAL,
    pong_timeout: float = _DEFAULT_PONG_TIMEOUT,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    max_events: int | None = None,
) -> dict[str, Any]:
    """Attach a read-only monitor WebSocket with a fresh `session_id` to stream `iopub` events."""
    monitor_session_id = str(uuid.uuid4())
    ws_url = _ws_channels_url(base_url, kernel_id, monitor_session_id)
    connect_to = min(30.0, timeout) if timeout is not None and timeout > 0 else 30.0
    deadline = (time.monotonic() + timeout) if (timeout is not None and timeout > 0) else None

    ws = await connect_ws(ws_url, headers=headers, timeout=connect_to, ca_cert=ca_cert)
    events_seen = 0
    stop_reason = "eof"
    next_ping_at = time.monotonic() + ping_interval
    recv_task: asyncio.Task[tuple[int, bytes]] | None = None
    try:
        while True:
            if max_events is not None and events_seen >= max_events:
                stop_reason = "max_events"
                break
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                stop_reason = "timeout"
                break
            if (now - ws.last_rx_mono) > pong_timeout:
                raise SessionWsError("Watch WebSocket timed out waiting for heartbeat/frames.")
            if now >= next_ping_at:
                await ws.send_ping()
                next_ping_at = now + ping_interval

            wakeups = [
                max(0.05, next_ping_at - time.monotonic()),
                max(0.05, (ws.last_rx_mono + pong_timeout) - time.monotonic()),
            ]
            if deadline is not None:
                wakeups.append(max(0.0, deadline - time.monotonic()))
            wait_sec = max(0.05, min(wakeups))

            if recv_task is None:
                recv_task = asyncio.create_task(ws.recv_message())
            done, _ = await asyncio.wait({recv_task}, timeout=wait_sec)
            if not done:
                continue

            try:
                opcode, payload = recv_task.result()
            except EOFError:
                recv_task = None
                stop_reason = "eof"
                break
            recv_task = None
            msg = decode_jupyter_ws_frame(opcode, payload)
            channel = msg.get("channel") or "iopub"
            if channel != "iopub":
                continue

            header = msg.get("header") or {}
            msg_type = str(msg.get("msg_type") or header.get("msg_type") or "")
            content = msg.get("content") or {}
            if msg_type not in ("status", "stream", "execute_input", "execute_result", "display_data", "error"):
                continue

            event: dict[str, Any] = {
                "msg_type": msg_type,
                "parent_msg_id": (msg.get("parent_header") or {}).get("msg_id"),
                "date": header.get("date"),
            }
            if msg_type == "status":
                event["execution_state"] = content.get("execution_state")
            elif msg_type == "stream":
                event["name"] = content.get("name") or "stdout"
                event["text"] = sessions.redact_session_secrets(content.get("text") or "")
            elif msg_type == "execute_input":
                event["execution_count"] = content.get("execution_count")
                event["code"] = sessions.redact_session_secrets(content.get("code") or "")
            elif msg_type in ("execute_result", "display_data"):
                data = content.get("data") or {}
                plain = data.get("text/plain")
                if isinstance(plain, str):
                    event["text"] = sessions.redact_session_secrets(plain)
            elif msg_type == "error":
                event["ename"] = content.get("ename") or "Error"
                event["evalue"] = sessions.redact_session_secrets(content.get("evalue") or "")
                tb = content.get("traceback") or []
                event["traceback"] = [sessions.redact_session_secrets(line) for line in tb]

            events_seen += 1
            if event_sink is not None:
                event_sink(event)
            if msg_type == "status" and content.get("execution_state") == "dead":
                stop_reason = "kernel_dead"
                break
        return {
            "monitor_session_id": monitor_session_id,
            "events_seen": events_seen,
            "stop_reason": stop_reason,
        }
    finally:
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
            with contextlib.suppress(BaseException):
                await recv_task
        await ws.close()


def watch_kernel(*args, **kwargs) -> dict[str, Any]:
    """Synchronous wrapper around `watch_kernel_async()`."""
    return asyncio.run(watch_kernel_async(*args, **kwargs))
