"""Offline, read-only, secret-free inspection of Kaggle CLI config & credentials.

Strictly Python standard library only. Never creates directories or writes files, and
never returns raw API tokens, keys, or OAuth refresh/access tokens.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import stat
import sys
from typing import Mapping

_ACCESS_TOKEN_RE = re.compile(r"^KGAT_[0-9a-fA-F]{32}$")
_SAFE_CONFIG_KEYS = ("path", "competition", "proxy", "ssl_ca_cert")


def resolve_kaggle_config_dir(
    environ: Mapping[str, str] | None = None,
    home_dir: str | None = None,
    platform_name: str | None = None,
) -> tuple[str, str]:
    """Mirror `KaggleApi.config_dir` precedence without creating any directory."""
    env = os.environ if environ is None else environ
    home = os.path.abspath(os.path.expanduser(home_dir or env.get("HOME") or "~"))
    plat = sys.platform if platform_name is None else platform_name

    explicit = env.get("KAGGLE_CONFIG_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit)), "KAGGLE_CONFIG_DIR"

    dot_kaggle = os.path.join(home, ".kaggle")
    if plat.startswith("linux") and not os.path.exists(dot_kaggle):
        xdg_home = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return os.path.abspath(os.path.join(os.path.expanduser(xdg_home), "kaggle")), "XDG_CONFIG_HOME"

    return dot_kaggle, "~/.kaggle"


def _inspect_kaggle_json(config_dir: str) -> dict:
    path = os.path.join(config_dir, "kaggle.json")
    info: dict = {
        "path": path,
        "present": os.path.isfile(path),
        "mode": None,
        "world_readable": False,
        "valid_json": None,
        "username": None,
        "has_key": False,
        "extra_config": {},
    }
    if not info["present"]:
        return info

    try:
        st_mode = os.stat(path).st_mode
        info["mode"] = oct(stat.S_IMODE(st_mode))
        if os.name != "nt":
            info["world_readable"] = bool(st_mode & (stat.S_IROTH | stat.S_IRGRP))
    except OSError:
        pass

    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
        data = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        info["valid_json"] = False
        return info

    if not isinstance(data, dict):
        info["valid_json"] = False
        return info

    info["valid_json"] = True
    username = data.get("username")
    if isinstance(username, str) and username:
        info["username"] = username
    key = data.get("key")
    info["has_key"] = isinstance(key, str) and bool(key.strip())
    info["extra_config"] = {
        k: data[k] for k in _SAFE_CONFIG_KEYS if k in data and data[k] not in (None, "")
    }
    return info


def _inspect_access_token_files(token_dir: str) -> dict:
    """Inspect `access_token{,.txt}` inside `token_dir`."""
    candidates = [
        os.path.join(token_dir, "access_token"),
        os.path.join(token_dir, "access_token.txt"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError:
                return {"path": path, "present": True, "valid_format": False, "empty": None}
            return {
                "path": path,
                "present": True,
                "valid_format": bool(_ACCESS_TOKEN_RE.match(token)),
                "empty": not bool(token),
            }
    return {"path": candidates[0], "present": False, "valid_format": None, "empty": None}


def _inspect_oauth_credentials(creds_dir: str) -> dict:
    """Inspect `credentials.json` inside `creds_dir` used by `KaggleCredentials`."""
    path = os.path.join(creds_dir, "credentials.json")
    info: dict = {
        "path": path,
        "present": os.path.isfile(path),
        "valid_json": None,
        "username": None,
        "has_refresh_token": False,
        "access_token_expiration": None,
        "access_token_expired": None,
        "scopes": [],
    }
    if not info["present"]:
        return info

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        info["valid_json"] = False
        return info

    if not isinstance(data, dict):
        info["valid_json"] = False
        return info

    info["valid_json"] = True
    username = data.get("username")
    if isinstance(username, str) and username:
        info["username"] = username
    refresh = data.get("refresh_token")
    info["has_refresh_token"] = isinstance(refresh, str) and bool(refresh.strip())

    scopes = data.get("scopes")
    if isinstance(scopes, list):
        info["scopes"] = [str(s) for s in scopes]

    exp_raw = data.get("access_token_expiration")
    if isinstance(exp_raw, str) and exp_raw:
        info["access_token_expiration"] = exp_raw
        try:
            exp_dt = datetime.fromisoformat(exp_raw)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            info["access_token_expired"] = exp_dt <= datetime.now(timezone.utc)
        except ValueError:
            info["access_token_expired"] = None
    return info


def _resolve_env_token_valid(env: Mapping[str, str]) -> bool | None:
    raw = env.get("KAGGLE_API_TOKEN")
    if not raw:
        return None
    token_str = raw.strip()
    if os.path.isfile(token_str):
        try:
            with open(token_str, encoding="utf-8") as fh:
                token_str = fh.read().strip()
        except OSError:
            return False
    return bool(_ACCESS_TOKEN_RE.match(token_str))


def inspect_kaggle_auth_offline(
    environ: Mapping[str, str] | None = None,
    home_dir: str | None = None,
    platform_name: str | None = None,
    account_dir_override: str | None = None,
) -> dict:
    """Return a secret-free summary of local Kaggle config, credentials, and environment."""
    env = os.environ if environ is None else environ
    home = os.path.abspath(os.path.expanduser(home_dir or env.get("HOME") or "~"))
    plat = sys.platform if platform_name is None else platform_name

    if account_dir_override:
        config_dir = os.path.abspath(os.path.expanduser(account_dir_override))
        config_dir_source = "kaggle-ext account"
        token_dir = config_dir
        creds_dir = config_dir
    else:
        config_dir, config_dir_source = resolve_kaggle_config_dir(env, home_dir=home, platform_name=plat)
        dot_kaggle = os.path.join(home, ".kaggle")
        token_dir = dot_kaggle
        creds_dir = dot_kaggle

    kaggle_json = _inspect_kaggle_json(config_dir)
    access_token_file = _inspect_access_token_files(token_dir)
    oauth_creds = _inspect_oauth_credentials(creds_dir)

    env_flags = {
        "KAGGLE_API_TOKEN": bool(env.get("KAGGLE_API_TOKEN")),
        "KAGGLE_USERNAME": bool(env.get("KAGGLE_USERNAME")),
        "KAGGLE_KEY": bool(env.get("KAGGLE_KEY")),
        "KAGGLE_CONFIG_DIR": bool(env.get("KAGGLE_CONFIG_DIR")),
        "KAGGLE_ACCOUNT": bool(env.get("KAGGLE_ACCOUNT")),
    }
    env_token_valid = _resolve_env_token_valid(env)

    # Effective auth precedence mirrors KaggleApi.authenticate():
    # 1. access_token (KAGGLE_API_TOKEN env or ~/.kaggle/access_token)
    # 2. legacy_api_key (KAGGLE_USERNAME+KAGGLE_KEY or kaggle.json username+key)
    # 3. oauth (~/.kaggle/credentials.json with refresh_token)
    # 4. none
    effective_method = "none"
    effective_source = None
    if env_flags["KAGGLE_API_TOKEN"] and env_token_valid:
        effective_method = "access_token"
        effective_source = "KAGGLE_API_TOKEN"
    elif access_token_file["present"] and access_token_file["valid_format"]:
        effective_method = "access_token"
        effective_source = access_token_file["path"]
    elif (
        (env_flags["KAGGLE_USERNAME"] or kaggle_json["username"])
        and (env_flags["KAGGLE_KEY"] or kaggle_json["has_key"])
    ):
        effective_method = "legacy_api_key"
        effective_source = (
            "env" if (env_flags["KAGGLE_USERNAME"] and env_flags["KAGGLE_KEY"]) else kaggle_json["path"]
        )
    elif oauth_creds["present"] and oauth_creds["has_refresh_token"]:
        effective_method = "oauth"
        effective_source = oauth_creds["path"]

    display_set = bool(env.get("DISPLAY"))
    wayland_set = bool(env.get("WAYLAND_DISPLAY"))
    wayland_quirk = plat.startswith("linux") and wayland_set and not display_set

    return {
        "config_dir": config_dir,
        "config_dir_source": config_dir_source,
        "kaggle_json": kaggle_json,
        "access_token_file": access_token_file,
        "oauth_credentials": oauth_creds,
        "env": env_flags,
        "env_token_valid_format": env_token_valid,
        "effective_auth_method": effective_method,
        "effective_auth_source": effective_source,
        "display": {
            "platform": plat,
            "display_set": display_set,
            "wayland_display_set": wayland_set,
            "wayland_only_oauth_quirk": wayland_quirk,
        },
    }
