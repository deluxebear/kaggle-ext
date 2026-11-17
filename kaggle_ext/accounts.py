"""Multi-account registry, isolated storage paths, and runtime context for `kaggle-ext`.

Strictly Python 3.11+ standard library only at module import time so `install.py`,
`kaggle ext doctor`, and offline `kaggle account list/current` never depend on or
trigger upstream `kaggle` / `kagglesdk` imports or network calls.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import shutil
import sys
import threading
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover (non-POSIX)
    fcntl = None  # type: ignore[assignment]

ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
_ACCESS_TOKEN_RE = re.compile(r"^KGAT_[0-9a-fA-F]{32}$")
DEFAULT_ACCOUNT = "default"
REGISTRY_VERSION = 1

ACTIVE_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kaggle_ext_active_account", default=None
)
ACTIVE_SOURCE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kaggle_ext_active_source", default=None
)
IGNORE_ENV_CREDS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kaggle_ext_ignore_env_creds", default=False
)

_initial_env_captured = False
_initial_external_config_dir: str | None = None
_cli_account_override: str | None = None
_cli_account_source: str | None = None
_pending_account_error: Exception | None = None


class AccountError(Exception):
    """Raised on invalid account names, unknown profiles, or guarded removals."""


# --------------------------------------------------------------------------------------
# Paths & validation
# --------------------------------------------------------------------------------------


def ext_config_dir() -> str:
    """Return base directory for `kaggle-ext` multi-account config & isolated profiles."""
    explicit = os.environ.get("KAGGLE_EXT_CONFIG_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return os.path.abspath(os.path.join(os.path.expanduser(xdg), "kaggle-ext"))


def registry_path() -> str:
    return os.path.join(ext_config_dir(), "accounts.json")


def accounts_dir() -> str:
    return os.path.join(ext_config_dir(), "accounts")


def account_dir(name: str) -> str:
    validate_account_name(name)
    return os.path.join(accounts_dir(), name)


def credentials_path(name: str) -> str:
    return os.path.join(account_dir(name), "credentials.json")


def access_token_path(name: str) -> str:
    return os.path.join(account_dir(name), "access_token")


def access_token_txt_path(name: str) -> str:
    return os.path.join(account_dir(name), "access_token.txt")


def kaggle_json_path(name: str) -> str:
    return os.path.join(account_dir(name), "kaggle.json")


def oauth_pending_path(name: str) -> str:
    return os.path.join(account_dir(name), "oauth_pending.json")


def sessions_file_path(name: str) -> str:
    return os.path.join(account_dir(name), "sessions.json")


def validate_account_name(name: str) -> str:
    if not name or not isinstance(name, str) or ".." in name or not ACCOUNT_RE.fullmatch(name):
        raise AccountError(
            f"Invalid account name {name!r}: must start with alphanumeric and contain "
            "only letters, digits, '.', '_', '@', '-' (max 64 chars, no '..')."
        )
    return name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------------------
# Atomic private I/O & registry locking
# --------------------------------------------------------------------------------------


def _ensure_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _atomic_write_text(path: str, content: str, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    if parent:
        _ensure_private_dir(parent)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _atomic_write_json(path: str, data: Any, mode: int = 0o600) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n", mode=mode)


def _copy_private_file(src: str, dst: str) -> bool:
    if not os.path.isfile(src) or os.path.exists(dst):
        return False
    with open(src, encoding="utf-8") as fh:
        content = fh.read()
    _atomic_write_text(dst, content, mode=0o600)
    return True


@contextlib.contextmanager
def _registry_lock() -> Iterator[None]:
    path = registry_path()
    _ensure_private_dir(os.path.dirname(path))
    lock_file = path + ".lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        pass


# --------------------------------------------------------------------------------------
# Non-destructive legacy bootstrap & registry CRUD
# --------------------------------------------------------------------------------------


def _read_json_dict(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _legacy_sources() -> tuple[str, str]:
    """Return `(legacy_dot_kaggle, legacy_kaggle_json_dir)` without creating dirs."""
    dot_kaggle = os.path.abspath(os.path.expanduser("~/.kaggle"))
    json_dir = dot_kaggle
    if sys.platform.startswith("linux") and not os.path.exists(dot_kaggle):
        xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
        json_dir = os.path.abspath(os.path.join(os.path.expanduser(xdg), "kaggle"))
    return dot_kaggle, json_dir


def _seed_default_from_legacy() -> tuple[str | None, str | None]:
    """Copy existing legacy Kaggle credentials into `accounts/default` non-destructively."""
    dot_kaggle, json_dir = _legacy_sources()
    _ensure_private_dir(account_dir(DEFAULT_ACCOUNT))

    _copy_private_file(os.path.join(dot_kaggle, "credentials.json"), credentials_path(DEFAULT_ACCOUNT))
    if not _copy_private_file(os.path.join(dot_kaggle, "access_token"), access_token_path(DEFAULT_ACCOUNT)):
        _copy_private_file(os.path.join(dot_kaggle, "access_token.txt"), access_token_path(DEFAULT_ACCOUNT))
    _copy_private_file(os.path.join(json_dir, "kaggle.json"), kaggle_json_path(DEFAULT_ACCOUNT))

    info = inspect_account_offline(DEFAULT_ACCOUNT)
    return info.get("username"), info.get("auth_method")


def _default_registry_struct(username: str | None = None, auth_method: str | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "version": REGISTRY_VERSION,
        "active": DEFAULT_ACCOUNT,
        "accounts": {
            DEFAULT_ACCOUNT: {
                "username": username,
                "auth_method": auth_method,
                "created_at": now,
                "updated_at": now,
            }
        },
    }


def _normalize_registry(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _default_registry_struct()
    accounts_map = data.get("accounts")
    if not isinstance(accounts_map, dict) or not accounts_map:
        return _default_registry_struct()
    active = data.get("active")
    if not isinstance(active, str) or active not in accounts_map:
        active = next(iter(accounts_map.keys()))
    return {
        "version": int(data.get("version") or REGISTRY_VERSION),
        "active": active,
        "accounts": accounts_map,
    }


def load_registry(*, bootstrap: bool = True) -> dict[str, Any]:
    """Load `accounts.json`, optionally bootstrapping `default` from legacy `~/.kaggle`."""
    path = registry_path()
    if os.path.isfile(path):
        loaded = _read_json_dict(path)
        if loaded is not None:
            return _normalize_registry(loaded)

    if not bootstrap:
        return _default_registry_struct()

    with _registry_lock():
        if os.path.isfile(path):
            loaded = _read_json_dict(path)
            if loaded is not None:
                return _normalize_registry(loaded)
        username, auth_method = _seed_default_from_legacy()
        reg = _default_registry_struct(username=username, auth_method=auth_method)
        _atomic_write_json(path, reg, mode=0o600)
        return reg


def _save_registry(reg: dict[str, Any]) -> None:
    _atomic_write_json(registry_path(), _normalize_registry(reg), mode=0o600)


def lookup_account(
    identifier: str,
    reg: dict[str, Any] | None = None,
    *,
    allow_create: bool = False,
) -> str:
    """Resolve a profile name or Kaggle `@username` to a registered profile name."""
    if not identifier or not isinstance(identifier, str):
        raise AccountError("Account name cannot be empty.")

    loaded = reg if reg is not None else load_registry()
    accounts_map = loaded.get("accounts", {})
    if identifier in accounts_map:
        return identifier

    needle = identifier.lstrip("@").strip().lower()
    matches = [
        name
        for name, meta in accounts_map.items()
        if isinstance(meta, dict)
        and isinstance(meta.get("username"), str)
        and meta["username"].lstrip("@").strip().lower() == needle
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AccountError(
            f"Username {identifier!r} matches multiple profiles ({', '.join(matches)}); "
            "specify the profile name instead."
        )

    if allow_create:
        validate_account_name(identifier)
        ensure_account(identifier)
        return identifier

    known = ", ".join(accounts_map.keys()) or "(none)"
    raise AccountError(f"Unknown account {identifier!r}. Configured accounts: {known}")


def ensure_account(
    name: str,
    *,
    username: str | None = None,
    auth_method: str | None = None,
) -> dict[str, Any]:
    """Create or update an account entry in `accounts.json` and ensure its directory exists."""
    validate_account_name(name)
    load_registry(bootstrap=True)
    with _registry_lock():
        reg = load_registry(bootstrap=False)
        accounts_map = reg.setdefault("accounts", {})
        now = _now_iso()
        entry = accounts_map.get(name)
        if not isinstance(entry, dict):
            entry = {
                "username": username,
                "auth_method": auth_method,
                "created_at": now,
                "updated_at": now,
            }
            accounts_map[name] = entry
        else:
            if username is not None:
                entry["username"] = username
            if auth_method is not None:
                entry["auth_method"] = auth_method
            entry["updated_at"] = now
        _ensure_private_dir(account_dir(name))
        _save_registry(reg)
        return reg


def record_account_login(name: str, *, username: str | None, auth_method: str | None) -> dict[str, Any]:
    """Update `username` and `auth_method` after successful authentication."""
    return ensure_account(name, username=username, auth_method=auth_method)


def set_active_account(identifier: str) -> tuple[str, dict[str, Any]]:
    """Switch the default active profile in `accounts.json`."""
    load_registry(bootstrap=True)
    with _registry_lock():
        reg = load_registry(bootstrap=False)
        resolved = lookup_account(identifier, reg, allow_create=False)
        reg["active"] = resolved
        entry = reg["accounts"].get(resolved)
        if isinstance(entry, dict):
            entry["updated_at"] = _now_iso()
        _save_registry(reg)
        return resolved, reg


def rename_account(old_identifier: str, new_name: str) -> tuple[str, dict[str, Any]]:
    """Rename an existing account profile and move its isolated directory."""
    validate_account_name(new_name)
    load_registry(bootstrap=True)
    with _registry_lock():
        reg = load_registry(bootstrap=False)
        old_name = lookup_account(old_identifier, reg, allow_create=False)
        if old_name == new_name:
            return old_name, reg
        accounts_map = reg["accounts"]
        if new_name in accounts_map:
            raise AccountError(f"Target account {new_name!r} already exists.")

        old_dir = account_dir(old_name)
        new_dir = account_dir(new_name)
        if os.path.exists(new_dir):
            raise AccountError(f"Target directory already exists: {new_dir}")
        if os.path.exists(old_dir):
            _ensure_private_dir(accounts_dir())
            shutil.move(old_dir, new_dir)
        else:
            _ensure_private_dir(new_dir)

        meta = accounts_map.pop(old_name)
        if isinstance(meta, dict):
            meta["updated_at"] = _now_iso()
        accounts_map[new_name] = meta
        if reg.get("active") == old_name:
            reg["active"] = new_name
        _save_registry(reg)
        return new_name, reg


def clear_account_credentials(name: str) -> list[str]:
    """Remove stored secrets for `name` while preserving non-secret `kaggle.json` config."""
    validate_account_name(name)
    removed: list[str] = []
    for path in (
        credentials_path(name),
        access_token_path(name),
        access_token_txt_path(name),
        oauth_pending_path(name),
    ):
        if os.path.isfile(path) or os.path.islink(path):
            os.unlink(path)
            removed.append(path)

    kj_path = kaggle_json_path(name)
    kj_data = _read_json_dict(kj_path)
    if kj_data is not None:
        had_secret = any(k in kj_data for k in ("key", "token", "api_token", "apiToken"))
        for secret_key in ("key", "token", "api_token", "apiToken", "username"):
            kj_data.pop(secret_key, None)
        if kj_data:
            _atomic_write_json(kj_path, kj_data, mode=0o600)
        elif os.path.isfile(kj_path):
            os.unlink(kj_path)
        if had_secret:
            removed.append(kj_path)

    load_registry(bootstrap=True)
    with _registry_lock():
        reg = load_registry(bootstrap=False)
        entry = reg.get("accounts", {}).get(name)
        if isinstance(entry, dict):
            entry["auth_method"] = None
            entry["updated_at"] = _now_iso()
            _save_registry(reg)

    with contextlib.suppress(Exception):
        from . import sessions

        sessions.scrub_account_session_secrets(name)
    return removed


def remove_account(identifier: str, *, force: bool = False) -> tuple[str, dict[str, Any]]:
    """Remove an account profile and its isolated directory."""
    load_registry(bootstrap=True)
    with _registry_lock():
        reg = load_registry(bootstrap=False)
        resolved = lookup_account(identifier, reg, allow_create=False)
        info = inspect_account_offline(resolved, reg=reg)
        if info["ready"] and not force:
            raise AccountError(
                f"Account {resolved!r} still has stored credentials ({info['auth_method']}). "
                "Re-run with `--force` to delete it and its credentials."
            )
        if not force:
            from . import sessions

            _lbl, active_sessions = sessions.load_sessions(resolved)
            with_ids = sorted(
                sname
                for sname, srec in active_sessions.items()
                if isinstance(srec, dict) and srec.get("kernel_session_id")
            )
            if with_ids:
                raise AccountError(
                    f"Account {resolved!r} still has recorded interactive session(s) "
                    f"({', '.join(with_ids)}). Stop them with "
                    f"`kaggle -a {resolved} session stop --all` or re-run with `--force`."
                )

        acct_path = account_dir(resolved)
        if os.path.exists(acct_path):
            shutil.rmtree(acct_path)

        accounts_map = reg["accounts"]
        accounts_map.pop(resolved, None)
        if not accounts_map:
            _ensure_private_dir(account_dir(DEFAULT_ACCOUNT))
            reg = _default_registry_struct()
        elif reg.get("active") == resolved:
            reg["active"] = next(iter(accounts_map.keys()))

        _save_registry(reg)
        return resolved, reg


# --------------------------------------------------------------------------------------
# Secret-free offline inspection
# --------------------------------------------------------------------------------------


def inspect_account_offline(name: str, *, reg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect a single account's isolated files offline without returning secrets."""
    validate_account_name(name)
    meta = {}
    if reg is not None:
        raw_meta = reg.get("accounts", {}).get(name)
        if isinstance(raw_meta, dict):
            meta = raw_meta

    at_path = access_token_path(name)
    at_txt_path = access_token_txt_path(name)
    token_file = at_path if os.path.isfile(at_path) else (at_txt_path if os.path.isfile(at_txt_path) else None)
    has_access_token = False
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as fh:
                has_access_token = bool(_ACCESS_TOKEN_RE.match(fh.read().strip()))
        except OSError:
            has_access_token = False

    kj_data = _read_json_dict(kaggle_json_path(name))
    kj_user = kj_data.get("username") if kj_data and isinstance(kj_data.get("username"), str) else None
    kj_has_key = bool(kj_data and isinstance(kj_data.get("key"), str) and kj_data["key"].strip())
    has_legacy = bool(kj_user and kj_has_key)

    creds_data = _read_json_dict(credentials_path(name))
    creds_user = creds_data.get("username") if creds_data and isinstance(creds_data.get("username"), str) else None
    has_oauth = bool(
        creds_data and isinstance(creds_data.get("refresh_token"), str) and creds_data["refresh_token"].strip()
    )

    auth_method: str | None = None
    if has_access_token:
        auth_method = "access_token"
    elif has_legacy:
        auth_method = "legacy_api_key"
    elif has_oauth:
        auth_method = "oauth"
    elif isinstance(meta.get("auth_method"), str):
        auth_method = meta.get("auth_method")

    username = creds_user or kj_user or (meta.get("username") if isinstance(meta.get("username"), str) else None)
    ready = has_access_token or has_legacy or has_oauth

    return {
        "account": name,
        "account_dir": account_dir(name),
        "username": username,
        "auth_method": auth_method,
        "ready": ready,
        "has_oauth": has_oauth,
        "has_access_token": has_access_token,
        "has_legacy_key": has_legacy,
        "has_pending_oauth": os.path.isfile(oauth_pending_path(name)),
    }


# --------------------------------------------------------------------------------------
# Argv parsing & active account context
# --------------------------------------------------------------------------------------


def capture_initial_env() -> None:
    """Capture external `KAGGLE_CONFIG_DIR` once before `kaggle-ext` overrides it."""
    global _initial_env_captured, _initial_external_config_dir
    if not _initial_env_captured:
        val = os.environ.get("KAGGLE_CONFIG_DIR")
        _initial_external_config_dir = val if val else None
        _initial_env_captured = True


def external_config_dir() -> str | None:
    capture_initial_env()
    return _initial_external_config_dir


def extract_account_from_argv(argv: list[str] | None = None) -> tuple[str | None, list[str], bool]:
    """Extract `-a <acct>` / `--account <acct>` / `--account=<acct>` from `argv`.

    Returns `(account_value, cleaned_argv, missing_value_error)`.
    """
    raw = list(sys.argv if argv is None else argv)
    if not raw:
        return None, raw, False

    cleaned = [raw[0]]
    account_val: str | None = None
    missing_value = False
    tokens = raw[1:]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            cleaned.extend(tokens[i:])
            break
        if tok in ("-a", "--account"):
            if i + 1 < len(tokens):
                account_val = tokens[i + 1]
                i += 2
                continue
            missing_value = True
            cleaned.append(tok)
            i += 1
            continue
        if tok.startswith("--account="):
            account_val = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.startswith("-a") and len(tok) > 2 and not tok.startswith("--"):
            account_val = tok[2:]
            i += 1
            continue
        cleaned.append(tok)
        i += 1
    return account_val, cleaned, missing_value


def activate_startup_account(identifier: str | None, *, allow_create: bool = False) -> str | None:
    """Resolve and set process-level account override for the current CLI run."""
    global _cli_account_override, _cli_account_source, _pending_account_error
    capture_initial_env()
    _pending_account_error = None
    env_acct = os.environ.get("KAGGLE_ACCOUNT")
    chosen = identifier if identifier is not None else (env_acct or None)
    source = "flag" if identifier is not None else ("env" if env_acct else None)

    if chosen is not None:
        try:
            reg = load_registry(bootstrap=True)
            resolved = lookup_account(chosen, reg, allow_create=allow_create)
            _cli_account_override = resolved
            _cli_account_source = source
            _ensure_private_dir(account_dir(resolved))
            if _initial_external_config_dir is None:
                os.environ["KAGGLE_CONFIG_DIR"] = account_dir(resolved)
            return resolved
        except AccountError as exc:
            _pending_account_error = exc
            return None

    _cli_account_override = None
    _cli_account_source = None
    if _initial_external_config_dir is None:
        reg = load_registry(bootstrap=True)
        resolved = reg["active"]
        _ensure_private_dir(account_dir(resolved))
        os.environ["KAGGLE_CONFIG_DIR"] = account_dir(resolved)
        return resolved
    return None


def get_pending_account_error() -> Exception | None:
    return _pending_account_error


def clear_startup_override(*, reset_initial_env: bool = False) -> None:
    """Reset process-level CLI override (primarily for tests)."""
    global _cli_account_override, _cli_account_source, _pending_account_error
    global _initial_env_captured, _initial_external_config_dir
    _cli_account_override = None
    _cli_account_source = None
    _pending_account_error = None
    if reset_initial_env:
        _initial_env_captured = False
        _initial_external_config_dir = None


@contextlib.contextmanager
def account_context(
    name: str,
    *,
    source: str = "context",
    ignore_env_creds: bool = False,
) -> Iterator[str]:
    """Temporarily route credential & config operations to account `name`."""
    validate_account_name(name)
    _ensure_private_dir(account_dir(name))
    tok_acct = ACTIVE_ACCOUNT.set(name)
    tok_src = ACTIVE_SOURCE.set(source)
    tok_ign = IGNORE_ENV_CREDS.set(ignore_env_creds)
    try:
        yield name
    finally:
        IGNORE_ENV_CREDS.reset(tok_ign)
        ACTIVE_SOURCE.reset(tok_src)
        ACTIVE_ACCOUNT.reset(tok_acct)


def current_account() -> str:
    """Resolve the active account profile name (`context` > `-a` > `KAGGLE_ACCOUNT` > `active`)."""
    ctx_acct = ACTIVE_ACCOUNT.get()
    if ctx_acct:
        return ctx_acct
    if _cli_account_override:
        return _cli_account_override
    env_acct = os.environ.get("KAGGLE_ACCOUNT")
    if env_acct:
        reg = load_registry(bootstrap=True)
        return lookup_account(env_acct, reg, allow_create=False)
    reg = load_registry(bootstrap=True)
    return reg["active"]


def current_account_source() -> str:
    if ACTIVE_ACCOUNT.get():
        return ACTIVE_SOURCE.get() or "context"
    if _cli_account_override:
        return _cli_account_source or "flag"
    if os.environ.get("KAGGLE_ACCOUNT"):
        return "env"
    return "default"


def should_ignore_env_creds() -> bool:
    return bool(IGNORE_ENV_CREDS.get())


def active_account_dir() -> str | None:
    """Return isolated dir for the active account, or `None` if external `KAGGLE_CONFIG_DIR` applies."""
    capture_initial_env()
    ctx_acct = ACTIVE_ACCOUNT.get()
    if ctx_acct:
        d = account_dir(ctx_acct)
        _ensure_private_dir(d)
        return d
    if _initial_external_config_dir and not _cli_account_override and not os.environ.get("KAGGLE_ACCOUNT"):
        return None
    acct = current_account()
    d = account_dir(acct)
    _ensure_private_dir(d)
    return d
