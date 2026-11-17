"""Per-account interactive session state, validation, and secret redaction (`kaggle-ext`).

Strictly Python 3.11+ standard library only (never imports `kaggle` or `kagglesdk`).
Stores interactive session metadata under each account's isolated `0700` directory in
`sessions.json` (`0600`) guarded by `fcntl.flock` and atomic temp-file replacement.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
import urllib.parse
from typing import Any, Mapping

from . import accounts

SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
DEFAULT_SESSION_NAME = "default"
SESSIONS_FILE_VERSION = 1

_RE_KGAT = re.compile(r"KGAT_[0-9a-fA-F]+")
_RE_KGRT = re.compile(r"KGRT_[A-Za-z0-9._-]+")
_RE_PROXY_SEG = re.compile(r"/k/(\d+)/([^/?#\s]+)")
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_RE_QS_SECRET = re.compile(r"(?i)\b(token|jwt|key|secret|auth|sig|signature)=([^&#\s]+)")
_RE_HEADER_SECRET = re.compile(
    r"(?im)^(Authorization|Cookie|Set-Cookie|X-Kaggle-[A-Za-z-]*Token):\s*([^\r\n]+)$"
)

_RETIRED_SHAPE_WARNINGS: dict[str, tuple[str, str]] = {
    "p100": (
        "NvidiaTeslaP100",
        "Accelerator 'NvidiaTeslaP100' is retired; Kaggle runs the session on default GPU (NvidiaTeslaT4).",
    ),
    "nvidiateslap100": (
        "NvidiaTeslaP100",
        "Accelerator 'NvidiaTeslaP100' is retired; Kaggle runs the session on default GPU (NvidiaTeslaT4).",
    ),
    "tpuv38": (
        "TpuV38",
        "Accelerator 'TpuV38' is retired; Kaggle runs the session on default TPU (TpuV5E8).",
    ),
    "tpu1vmv38": (
        "Tpu1VmV38",
        "Accelerator 'Tpu1VmV38' is retired; Kaggle runs the session on default TPU (TpuV5E8).",
    ),
    "tpuv232": (
        "TpuV232",
        "Accelerator 'TpuV232' is retired; Kaggle runs the session on CPU without TPU.",
    ),
    "tpuv2256": (
        "TpuV2256",
        "Accelerator 'TpuV2256' is retired; Kaggle runs the session on CPU without TPU.",
    ),
}

_SAFE_ACCELERATOR_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class SessionError(Exception):
    """Raised on invalid session configuration, missing sessions, or control-plane failures."""


# --------------------------------------------------------------------------------------
# Validation, slug normalization & secret redaction
# --------------------------------------------------------------------------------------


def validate_session_name(name: str) -> str:
    if not name or not isinstance(name, str) or ".." in name or not SESSION_NAME_RE.fullmatch(name):
        raise SessionError(
            f"Invalid session name {name!r}: must start with alphanumeric and contain "
            "only letters, digits, '.', '_', '-' (max 48 chars, no '..')."
        )
    return name


def normalize_kernel_ref(ref: str, fallback: str = "") -> tuple[str, str]:
    """Normalize `ApiSaveKernelResponse.ref` (which may start with `/code/`) into `(owner, slug)`."""
    for candidate in (ref, fallback):
        raw = (candidate or "").strip()
        if not raw:
            continue
        if raw.startswith(("http://", "https://")):
            raw = urllib.parse.urlsplit(raw).path
        raw = raw.strip("/")
        if raw.startswith("code/"):
            raw = raw[len("code/") :]
        parts = [p for p in raw.split("/") if p]
        if len(parts) >= 2:
            return parts[0], parts[1]
    raise SessionError(f"Invalid kernel reference {ref!r}: expected '<owner>/<slug>'.")


def placeholder_slug_and_title(username: str, session_name: str) -> tuple[str, str, str]:
    """Build symmetric `(full_slug, kernel_slug, title)` for the hidden private placeholder kernel."""
    if not username or not isinstance(username, str) or not username.strip():
        raise SessionError("Kaggle username is required to construct a placeholder kernel slug.")
    validate_session_name(session_name)
    safe = re.sub(r"[^a-z0-9]+", "-", session_name.lower()).strip("-") or "default"
    kernel_slug = f"kaggle-ext-s-{safe}"
    title = f"kaggle ext s {safe.replace('-', ' ')}"
    return f"{username.strip()}/{kernel_slug}", kernel_slug, title


def resolve_machine_shape(accelerator: str | None) -> tuple[str | None, str, str | None]:
    """Map user accelerator aliases to `(machine_shape, canonical_label, warning_msg)`."""
    raw = (accelerator or "").strip()
    low = raw.lower()
    if not raw or low in ("cpu", "none"):
        return None, "cpu", None
    if low in ("gpu", "t4", "t4x2", "nvidiateslat4"):
        return "NvidiaTeslaT4", "NvidiaTeslaT4", None
    if low in ("t4-highmem", "t4highmem", "nvidiateslat4highmem"):
        return "NvidiaTeslaT4Highmem", "NvidiaTeslaT4Highmem", None
    if low in ("tpu", "tpu-v5e-8", "v5e-8", "tpuv5e8"):
        return "TpuV5E8", "TpuV5E8", None
    if low in _RETIRED_SHAPE_WARNINGS:
        shape, warn = _RETIRED_SHAPE_WARNINGS[low]
        return shape, shape, warn
    if not _SAFE_ACCELERATOR_RE.fullmatch(raw):
        raise SessionError(f"Invalid accelerator specification: {accelerator!r}")
    return raw, raw, None


def redact_session_secrets(text: Any) -> str:
    """Mask Kaggle tokens, OAuth tokens, JWTs, query secrets, and `/k/<id>/<seg>` proxy paths."""
    if text is None:
        return ""
    out = str(text)
    out = _RE_KGAT.sub("KGAT_[REDACTED]", out)
    out = _RE_KGRT.sub("KGRT_[REDACTED]", out)
    out = _RE_PROXY_SEG.sub(r"/k/\1/[REDACTED]", out)
    out = _RE_JWT.sub("[REDACTED_JWT]", out)
    out = _RE_QS_SECRET.sub(r"\1=[REDACTED]", out)
    out = _RE_HEADER_SECRET.sub(r"\1: [REDACTED]", out)
    return out


def sanitize_session_record(
    name: str,
    rec: Mapping[str, Any],
    *,
    account: str | None = None,
) -> dict[str, Any]:
    """Return a secret-free representation of a stored session record."""
    raw_jurl = rec.get("jupyter_url")
    raw_tok_jurl = rec.get("tokenized_jupyter_url")
    raw_proxy_tok = rec.get("proxy_token")
    proxy_url_redacted = (
        redact_session_secrets(raw_jurl) if isinstance(raw_jurl, str) and raw_jurl.strip() else None
    )
    has_proxy_creds = bool(
        (isinstance(raw_jurl, str) and raw_jurl.strip())
        or (isinstance(raw_tok_jurl, str) and raw_tok_jurl.strip())
        or (isinstance(raw_proxy_tok, str) and raw_proxy_tok.strip())
    )
    machine_shape = rec.get("machine_shape")
    accel = rec.get("accelerator") or machine_shape or "cpu"
    return {
        "name": name,
        "account": account or rec.get("account"),
        "username": rec.get("username"),
        "slug": rec.get("slug"),
        "kernel_session_id": rec.get("kernel_session_id"),
        "state": rec.get("state") or "active",
        "accelerator": accel,
        "machine_shape": machine_shape,
        "enable_internet": rec.get("enable_internet"),
        "proxy_url_redacted": proxy_url_redacted,
        "has_proxy_credentials": has_proxy_creds,
        "proxy_token_expires_at": rec.get("proxy_token_expires_at"),
        "kernel_id": rec.get("kernel_id"),
        "client_session_id": rec.get("client_session_id"),
        "operation_name": rec.get("operation_name"),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
    }


# --------------------------------------------------------------------------------------
# Per-account path resolution, flock & atomic JSON CRUD
# --------------------------------------------------------------------------------------


def _external_sessions_dir() -> str:
    ext_cfg = accounts.external_config_dir() or "ext"
    norm = os.path.abspath(os.path.expanduser(ext_cfg))
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    return os.path.join(accounts.ext_config_dir(), "external", digest)


def sessions_dir_for_account(account: str | None = None, *, create: bool = False) -> tuple[str, str]:
    """Resolve `(account_label, storage_dir)` without mutating disk when `create=False`."""
    if account is not None:
        if account == "external":
            d = _external_sessions_dir()
            if create:
                accounts._ensure_private_dir(d)
            return "external", d
        resolved = accounts.lookup_account(account, allow_create=False)
        d = accounts.account_dir(resolved)
        if create:
            accounts._ensure_private_dir(d)
        return resolved, d

    accounts.capture_initial_env()
    ctx_acct = accounts.ACTIVE_ACCOUNT.get()
    if ctx_acct:
        d = accounts.account_dir(ctx_acct)
        if create:
            accounts._ensure_private_dir(d)
        return ctx_acct, d

    if (
        accounts.external_config_dir()
        and not accounts._cli_account_override
        and not os.environ.get("KAGGLE_ACCOUNT")
    ):
        d = _external_sessions_dir()
        if create:
            accounts._ensure_private_dir(d)
        return "external", d

    resolved = accounts.current_account()
    d = accounts.account_dir(resolved)
    if create:
        accounts._ensure_private_dir(d)
    return resolved, d


def sessions_path_for(account: str | None = None, *, create: bool = False) -> tuple[str, str]:
    label, d = sessions_dir_for_account(account, create=create)
    return label, os.path.join(d, "sessions.json")


def session_log_path(session_name: str, account: str | None = None, *, create: bool = False) -> str:
    validate_session_name(session_name)
    _label, d = sessions_dir_for_account(account, create=create)
    log_dir = os.path.join(d, "session_logs")
    if create:
        accounts._ensure_private_dir(log_dir)
    return os.path.join(log_dir, f"{session_name}.log")


@contextlib.contextmanager
def _sessions_lock(sessions_file: str):
    parent = os.path.dirname(sessions_file)
    accounts._ensure_private_dir(parent)
    lock_file = sessions_file + ".lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as fh:
        with contextlib.suppress(OSError):
            os.chmod(lock_file, 0o600)
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_sessions_doc(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {"version": SESSIONS_FILE_VERSION, "sessions": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"version": SESSIONS_FILE_VERSION, "sessions": {}}
    if not isinstance(raw, dict):
        return {"version": SESSIONS_FILE_VERSION, "sessions": {}}
    if isinstance(raw.get("sessions"), dict):
        cleaned = {
            str(k): dict(v) for k, v in raw["sessions"].items() if isinstance(k, str) and isinstance(v, dict)
        }
        return {
            "version": int(raw.get("version") or SESSIONS_FILE_VERSION),
            "sessions": cleaned,
        }
    # Tolerate flat `{session_name: {...}}` layout if encountered.
    cleaned = {
        str(k): dict(v)
        for k, v in raw.items()
        if isinstance(k, str) and k != "version" and isinstance(v, dict)
    }
    return {"version": SESSIONS_FILE_VERSION, "sessions": cleaned}


def load_sessions(account: str | None = None) -> tuple[str, dict[str, dict[str, Any]]]:
    label, path = sessions_path_for(account, create=False)
    doc = _read_sessions_doc(path)
    return label, doc["sessions"]


def get_session(session_name: str, account: str | None = None) -> tuple[str, dict[str, Any] | None]:
    validate_session_name(session_name)
    label, sessions_map = load_sessions(account)
    rec = sessions_map.get(session_name)
    return label, (dict(rec) if isinstance(rec, dict) else None)


def upsert_session(
    session_name: str,
    record: Mapping[str, Any],
    account: str | None = None,
) -> tuple[str, dict[str, Any]]:
    validate_session_name(session_name)
    label, path = sessions_path_for(account, create=True)
    with _sessions_lock(path):
        doc = _read_sessions_doc(path)
        existing = doc["sessions"].get(session_name)
        now = accounts._now_iso()
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(record)
        merged["name"] = session_name
        merged["account"] = label
        if not merged.get("created_at"):
            merged["created_at"] = now
        merged["updated_at"] = now
        if merged.get("kernel_session_id") is not None:
            merged["kernel_session_id"] = int(merged["kernel_session_id"])
        doc["sessions"][session_name] = merged
        accounts._atomic_write_json(path, doc, mode=0o600)
        return label, dict(merged)


def update_session_fields(
    session_name: str,
    account: str | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    validate_session_name(session_name)
    label, path = sessions_path_for(account, create=False)
    if not os.path.isfile(path):
        return None
    with _sessions_lock(path):
        doc = _read_sessions_doc(path)
        existing = doc["sessions"].get(session_name)
        if not isinstance(existing, dict):
            return None
        updated = dict(existing)
        updated.update(fields)
        updated["account"] = label
        updated["updated_at"] = accounts._now_iso()
        if updated.get("kernel_session_id") is not None:
            updated["kernel_session_id"] = int(updated["kernel_session_id"])
        doc["sessions"][session_name] = updated
        accounts._atomic_write_json(path, doc, mode=0o600)
        return dict(updated)


def delete_session(session_name: str, account: str | None = None) -> bool:
    validate_session_name(session_name)
    _label, path = sessions_path_for(account, create=False)
    if not os.path.isfile(path):
        return False
    with _sessions_lock(path):
        doc = _read_sessions_doc(path)
        if session_name not in doc["sessions"]:
            return False
        doc["sessions"].pop(session_name, None)
        accounts._atomic_write_json(path, doc, mode=0o600)
        return True


def scrub_account_session_secrets(account: str) -> int:
    """Remove proxy bearer URLs/tokens from `sessions.json` while keeping session IDs."""
    _label, path = sessions_path_for(account, create=False)
    if not os.path.isfile(path):
        return 0
    with _sessions_lock(path):
        doc = _read_sessions_doc(path)
        changed = 0
        now = accounts._now_iso()
        for _sname, rec in doc["sessions"].items():
            if not isinstance(rec, dict):
                continue
            had = False
            for secret_key in ("jupyter_url", "tokenized_jupyter_url", "proxy_token"):
                if secret_key in rec and rec[secret_key]:
                    rec.pop(secret_key, None)
                    had = True
            if had:
                rec["state"] = "logged_out"
                rec["updated_at"] = now
                changed += 1
        if changed:
            accounts._atomic_write_json(path, doc, mode=0o600)
        return changed


def append_session_log(session_name: str, data: bytes | str, account: str | None = None) -> str:
    """Append redacted output text to the account-isolated `0600` session log file."""
    path = session_log_path(session_name, account=account, create=True)
    text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
    safe_text = redact_session_secrets(text)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        fh.write(safe_text)
    return path


def read_session_log_tail(
    session_name: str,
    *,
    lines: int = 100,
    account: str | None = None,
) -> str:
    path = session_log_path(session_name, account=account, create=False)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return ""
    if lines <= 0:
        return content
    split_lines = content.splitlines(keepends=True)
    return "".join(split_lines[-lines:])


def _candidate_accounts_for_scan() -> list[str]:
    candidates: list[str] = []
    if os.path.isfile(accounts.registry_path()):
        reg = accounts.load_registry(bootstrap=False)
        candidates.extend(reg.get("accounts", {}).keys())
    ext_path = os.path.join(_external_sessions_dir(), "sessions.json")
    if os.path.isfile(ext_path) and "external" not in candidates:
        candidates.append("external")
    return candidates


def list_all_sessions(
    *,
    all_accounts: bool = False,
    account: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not all_accounts:
        label, s_map = load_sessions(account)
        for sname in sorted(s_map.keys()):
            rows.append(sanitize_session_record(sname, s_map[sname], account=label))
        return rows

    for acct_name in _candidate_accounts_for_scan():
        label, s_map = load_sessions(acct_name)
        for sname in sorted(s_map.keys()):
            rows.append(sanitize_session_record(sname, s_map[sname], account=label))
    return rows


def resolve_session_for_command(
    session_name: str,
    explicit_account: str | None = None,
    *,
    require_exists: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    """Resolve `(account_label, raw_record)` for a session command.

    Honors explicit `-a` / `KAGGLE_ACCOUNT` / `account_context`, and otherwise falls back
    to scanning other registered accounts if the active account has no matching session.
    """
    validate_session_name(session_name)
    if explicit_account is not None:
        acct_label, rec = get_session(session_name, explicit_account)
        if rec is None and require_exists:
            raise SessionError(
                f"Session {session_name!r} not found in account {acct_label!r} "
                f"(run `kaggle -a {acct_label} session create {session_name}`)."
            )
        return acct_label, rec

    src = accounts.current_account_source()
    primary_label, primary_rec = get_session(session_name, None)
    if primary_rec is not None or src != "default":
        if primary_rec is None and require_exists:
            raise SessionError(
                f"Session {session_name!r} not found in account {primary_label!r} "
                f"(run `kaggle -a {primary_label} session create {session_name}`)."
            )
        return primary_label, primary_rec

    matches: list[tuple[str, dict[str, Any]]] = []
    for acct_name in _candidate_accounts_for_scan():
        if acct_name == primary_label:
            continue
        lbl, rec = get_session(session_name, acct_name)
        if rec is not None:
            matches.append((lbl, rec))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        found_accts = ", ".join(m[0] for m in matches)
        raise SessionError(
            f"Session {session_name!r} exists in multiple accounts ({found_accts}); "
            "specify `-a <account>`."
        )
    if require_exists:
        raise SessionError(
            f"Session {session_name!r} not found (run `kaggle session list --all-accounts` "
            f"or `kaggle session create {session_name}`)."
        )
    return primary_label, None
