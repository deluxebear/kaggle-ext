"""CLI commands for multi-account management: `kaggle account` (alias: `acct`)."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
import sys
import threading
import urllib.parse
from typing import Any

from . import ACCOUNT_HELP, cli_hooks
from . import accounts

ACCOUNT_CHOICES = [
    "list",
    "ls",
    "current",
    "use",
    "switch",
    "add",
    "login",
    "logout",
    "status",
    "rename",
    "remove",
    "rm",
]

_ACCESS_TOKEN_RE = re.compile(r"^KGAT_[0-9a-fA-F]{32}$")
_DEFAULT_OAUTH_SCOPES = ["resources.admin:*"]


def _exit_error(exc: Exception | str) -> None:
    print(f"[account] {exc}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------------------
# Login strategy helpers (OAuth interactive, --url-only/--code, --token, --kaggle-json)
# --------------------------------------------------------------------------------------


def _extract_code_and_verify_state(raw_code: str, expected_state: str | None) -> str:
    text = raw_code.strip()
    if not text:
        raise accounts.AccountError("Authorization code cannot be empty.")
    if text.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(text)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params and params["code"]:
            if expected_state and "state" in params and params["state"]:
                got_state = params["state"][0]
                if got_state != expected_state:
                    raise accounts.AccountError("OAuth callback state mismatch; re-run `--url-only`.")
            return params["code"][0].strip()
    return text


def _build_api_and_client():
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    client = api.build_kaggle_client()
    return api, client


def _oauth_interactive_login(acct: str, *, no_launch_browser: bool) -> tuple[str | None, str]:
    from kagglesdk.kaggle_oauth import KaggleOAuth

    with accounts.account_context(acct, source="login", ignore_env_creds=True):
        _api, client = _build_api_and_client()
        with client:
            oauth = KaggleOAuth(client=client)
            creds = oauth.authenticate(scopes=_DEFAULT_OAUTH_SCOPES, no_launch_browser=no_launch_browser)
            username = creds.get_username() if creds is not None else None
        pending = accounts.oauth_pending_path(acct)
        if os.path.isfile(pending):
            os.unlink(pending)
    accounts.record_account_login(acct, username=username, auth_method="oauth")
    return username, "oauth"


def _start_url_only_login(acct: str) -> None:
    from kagglesdk.kaggle_oauth import KaggleOAuth

    with accounts.account_context(acct, source="login", ignore_env_creds=True):
        _api, client = _build_api_and_client()
        with client:
            http_client = client.http_client()
            redirect_uri = http_client.get_oauth_default_redirect_url()
            oauth_state = KaggleOAuth.OAuthState()
            auth_url = http_client.build_start_oauth_url(
                client_id=KaggleOAuth.OAUTH_CLIENT_ID,
                redirect_uri=redirect_uri,
                scope=_DEFAULT_OAUTH_SCOPES,
                state=oauth_state.state,
                code_challenge=oauth_state.code_challenge,
            )

    accounts._atomic_write_json(
        accounts.oauth_pending_path(acct),
        {
            "account": acct,
            "scopes": list(_DEFAULT_OAUTH_SCOPES),
            "redirect_uri": redirect_uri,
            "state": oauth_state.state,
            "code_verifier": oauth_state.code_verifier,
            "created_at": accounts._now_iso(),
        },
        mode=0o600,
    )
    print(auth_url)
    print(
        f"\n# Open the URL above in a browser, approve for '{acct}', then complete login with:\n"
        f"#   kaggle account login {acct} --code <PASTED_CODE>",
        file=sys.stderr,
    )


def _finish_code_login(acct: str, raw_code: str) -> tuple[str | None, str]:
    from kagglesdk.kaggle_creds import KaggleCredentials
    from kagglesdk.security.types.oauth_service import ExchangeOAuthTokenRequest

    pending_path = accounts.oauth_pending_path(acct)
    if not os.path.isfile(pending_path):
        raise accounts.AccountError(
            f"No pending headless OAuth login for account {acct!r} ({pending_path} missing). "
            f"Run `kaggle account login {acct} --url-only` first."
        )
    try:
        with open(pending_path, encoding="utf-8") as fh:
            pend = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise accounts.AccountError(f"Could not read pending OAuth state {pending_path}: {exc}") from exc

    code = _extract_code_and_verify_state(raw_code, pend.get("state"))
    verifier = pend.get("code_verifier")
    if not verifier:
        raise accounts.AccountError(f"Corrupt pending OAuth state in {pending_path} (missing code_verifier).")
    scopes = pend.get("scopes") or list(_DEFAULT_OAUTH_SCOPES)

    with accounts.account_context(acct, source="login", ignore_env_creds=True):
        _api, client = _build_api_and_client()
        with client:
            req = ExchangeOAuthTokenRequest()
            req.code = code
            req.code_verifier = verifier
            req.grant_type = "authorization_code"
            response = client.security.oauth_client.exchange_oauth_token(req)
            creds = KaggleCredentials(
                client=client,
                refresh_token=response.refreshToken,
                access_token=response.accessToken,
                access_token_expiration=datetime.now(timezone.utc) + timedelta(seconds=response.expires_in),
                username=response.username,
                scopes=scopes,
            )
            username = creds.introspect()
            creds.save()

    if os.path.isfile(pending_path):
        os.unlink(pending_path)
    accounts.record_account_login(acct, username=username, auth_method="oauth")
    return username, "oauth"


def _read_token_input(token: str | None, token_file: str | None, token_stdin: bool) -> str:
    raw = ""
    if token is not None:
        raw = token
    elif token_file is not None:
        expanded = os.path.abspath(os.path.expanduser(token_file))
        if not os.path.isfile(expanded):
            raise accounts.AccountError(f"Token file not found: {expanded}")
        with open(expanded, encoding="utf-8") as fh:
            raw = fh.read()
    elif token_stdin:
        raw = sys.stdin.read()

    cleaned = raw.strip()
    if not _ACCESS_TOKEN_RE.match(cleaned):
        raise accounts.AccountError(
            "Invalid Kaggle API token format (expected raw token starting with `KGAT_` followed by 32 hex chars)."
        )
    return cleaned


def _import_access_token(
    acct: str,
    *,
    token: str | None,
    token_file: str | None,
    token_stdin: bool,
    no_verify: bool,
) -> tuple[str | None, str]:
    tok = _read_token_input(token, token_file, token_stdin)
    username: str | None = None
    if not no_verify:
        with accounts.account_context(acct, source="login", ignore_env_creds=True):
            api, _client = _build_api_and_client()
            username = api._introspect_token(tok)
        if not username:
            raise accounts.AccountError("Kaggle rejected this API token (invalid or expired).")

    accounts._atomic_write_text(accounts.access_token_path(acct), tok + "\n", mode=0o600)
    accounts.record_account_login(acct, username=username, auth_method="access_token")
    return username, "access_token"


def _import_kaggle_json(acct: str, source_path: str) -> tuple[str | None, str]:
    expanded = os.path.abspath(os.path.expanduser(source_path))
    if not os.path.isfile(expanded):
        raise accounts.AccountError(f"kaggle.json file not found: {expanded}")
    try:
        with open(expanded, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise accounts.AccountError(f"Invalid JSON in {expanded}: {exc}") from exc

    if not isinstance(data, dict):
        raise accounts.AccountError(f"Expected a JSON object in {expanded}.")

    username = data.get("username")
    key = data.get("key")
    if isinstance(key, str) and key.strip().upper().startswith(("KGAT_", "KGRT_")):
        raise accounts.AccountError(
            "Found a KGAT_/KGRT_ token in `key`; pass `--token` or `--token-file` instead."
        )
    if not isinstance(username, str) or not username.strip() or not isinstance(key, str) or not key.strip():
        raise accounts.AccountError(f'{expanded} must contain non-empty "username" and "key" fields.')

    target = accounts.kaggle_json_path(acct)
    existing = accounts._read_json_dict(target) or {}
    existing.update(data)
    accounts._atomic_write_json(target, existing, mode=0o600)
    accounts.record_account_login(acct, username=username.strip(), auth_method="legacy_api_key")
    return username.strip(), "legacy_api_key"


def _run_login_mode(
    acct: str,
    *,
    no_launch_browser: bool,
    url_only: bool,
    code: str | None,
    token: str | None,
    token_file: str | None,
    token_stdin: bool,
    kaggle_json: str | None,
    no_verify: bool,
) -> tuple[bool, str | None, str | None]:
    has_token_opt = token is not None or token_file is not None or token_stdin
    selected = sum(
        [
            1 if url_only else 0,
            1 if code is not None else 0,
            1 if has_token_opt else 0,
            1 if kaggle_json is not None else 0,
        ]
    )
    if selected > 1:
        raise accounts.AccountError(
            "Specify only one login mode among --url-only, --code, --token/--token-file/--token-stdin, or --kaggle-json."
        )
    if sum([1 if token is not None else 0, 1 if token_file is not None else 0, 1 if token_stdin else 0]) > 1:
        raise accounts.AccountError("Specify only one of --token, --token-file, or --token-stdin.")

    if url_only:
        _start_url_only_login(acct)
        return False, None, None
    if code is not None:
        user, method = _finish_code_login(acct, code)
        return True, user, method
    if has_token_opt:
        user, method = _import_access_token(
            acct,
            token=token,
            token_file=token_file,
            token_stdin=token_stdin,
            no_verify=no_verify,
        )
        return True, user, method
    if kaggle_json is not None:
        user, method = _import_kaggle_json(acct, kaggle_json)
        return True, user, method

    user, method = _oauth_interactive_login(acct, no_launch_browser=no_launch_browser)
    return True, user, method


# --------------------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------------------


def _cmd_list(json_output: bool = False) -> None:
    reg = accounts.load_registry(bootstrap=True)
    active = reg.get("active")
    rows: list[dict[str, Any]] = []
    for name in reg.get("accounts", {}):
        info = accounts.inspect_account_offline(name, reg=reg)
        rows.append(
            {
                "account": name,
                "active": name == active,
                "username": info["username"],
                "auth_method": info["auth_method"],
                "ready": info["ready"],
                "has_oauth": info["has_oauth"],
                "has_access_token": info["has_access_token"],
                "has_legacy_key": info["has_legacy_key"],
                "has_pending_oauth": info["has_pending_oauth"],
                "account_dir": info["account_dir"],
            }
        )

    if json_output:
        print(json.dumps({"active": active, "accounts": rows}, indent=2))
        return

    for row in rows:
        mark = "*" if row["active"] else " "
        user = ("@" + row["username"]) if row["username"] else "-"
        if row["ready"]:
            state = f"ok ({row['auth_method']})"
        elif row["has_pending_oauth"]:
            state = "pending (--code)"
        else:
            state = "not logged in"
        print(f"{mark} {row['account']:<14} | user: {user:<22} | auth: {state}")


def _cmd_current(json_output: bool = False) -> None:
    reg = accounts.load_registry(bootstrap=True)
    try:
        acct = accounts.current_account()
    except accounts.AccountError as exc:
        _exit_error(exc)
        return

    if not json_output:
        print(acct)
        return

    info = accounts.inspect_account_offline(acct, reg=reg)
    print(
        json.dumps(
            {
                "account": acct,
                "source": accounts.current_account_source(),
                **info,
            },
            indent=2,
        )
    )


def _cmd_use(name: str) -> None:
    try:
        resolved, reg = accounts.set_active_account(name)
    except accounts.AccountError as exc:
        _exit_error(exc)
        return
    username = (reg.get("accounts", {}).get(resolved) or {}).get("username")
    suffix = f" (@{username})" if username else ""
    print(f"[account] Active account -> {resolved}{suffix}")


def _cmd_add(
    name: str,
    no_launch_browser: bool = False,
    url_only: bool = False,
    token: str | None = None,
    token_file: str | None = None,
    token_stdin: bool = False,
    kaggle_json: str | None = None,
    no_verify: bool = False,
    use: bool = True,
) -> None:
    try:
        accounts.validate_account_name(name)
        accounts.ensure_account(name)
        finished, username, method = _run_login_mode(
            name,
            no_launch_browser=no_launch_browser,
            url_only=url_only,
            code=None,
            token=token,
            token_file=token_file,
            token_stdin=token_stdin,
            kaggle_json=kaggle_json,
            no_verify=no_verify,
        )
    except Exception as exc:
        _exit_error(exc)
        return

    if use:
        accounts.set_active_account(name)

    if finished:
        user_str = f" (@{username})" if username else ""
        print(f"[account] Added & logged in '{name}'{user_str} [{method}]")
    if use:
        print(f"[account] Active account -> {name}")


def _cmd_login(
    name: str | None = None,
    no_launch_browser: bool = False,
    url_only: bool = False,
    code: str | None = None,
    token: str | None = None,
    token_file: str | None = None,
    token_stdin: bool = False,
    kaggle_json: str | None = None,
    no_verify: bool = False,
    use: bool = False,
) -> None:
    try:
        if name:
            acct = accounts.lookup_account(name, allow_create=(code is None))
        else:
            acct = accounts.current_account()
        finished, username, method = _run_login_mode(
            acct,
            no_launch_browser=no_launch_browser,
            url_only=url_only,
            code=code,
            token=token,
            token_file=token_file,
            token_stdin=token_stdin,
            kaggle_json=kaggle_json,
            no_verify=no_verify,
        )
    except Exception as exc:
        _exit_error(exc)
        return

    if use:
        accounts.set_active_account(acct)

    if not finished:
        return

    reg = accounts.load_registry(bootstrap=False)
    user_str = f" (@{username})" if username else ""
    print(f"[account] Logged in '{acct}'{user_str} [{method}]")
    if use:
        print(f"[account] Active account -> {acct}")
    elif reg.get("active") != acct:
        print(f"[account] Tip: run `kaggle -a {acct} ...` or `kaggle account use {acct}`")


def _cmd_logout(name: str | None = None, revoke: bool = False) -> None:
    try:
        reg = accounts.load_registry(bootstrap=True)
        acct = accounts.lookup_account(name, reg, allow_create=False) if name else accounts.current_account()
        if revoke:
            from kagglesdk.kaggle_creds import KaggleCredentials

            with accounts.account_context(acct, source="logout", ignore_env_creds=True):
                _api, client = _build_api_and_client()
                with client:
                    creds = KaggleCredentials.load(client=client)
                    if creds is not None:
                        creds.revoke_token("Logged out via kaggle-ext")
        removed = accounts.clear_account_credentials(acct)
    except Exception as exc:
        _exit_error(exc)
        return

    if removed:
        print(f"[account] Logged out '{acct}' (cleared {len(removed)} credential item(s)).")
    else:
        print(f"[account] '{acct}' had no stored credentials.")


class _ThreadLocalStream:
    """Thread-safe stream proxy so worker threads can silence stdout/stderr independently."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self._tls = threading.local()

    @contextlib.contextmanager
    def capture(self):
        self._tls.sink = io.StringIO()
        try:
            yield self._tls.sink
        finally:
            if hasattr(self._tls, "sink"):
                del self._tls.sink

    def write(self, data):
        target = getattr(self._tls, "sink", self._wrapped)
        return target.write(data)

    def flush(self) -> None:
        target = getattr(self._tls, "sink", self._wrapped)
        target.flush()

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


def _probe_single_account(
    acct: str,
    active_name: str | None,
    reg: dict[str, Any],
    tls_out: _ThreadLocalStream | None = None,
    tls_err: _ThreadLocalStream | None = None,
) -> dict[str, Any]:
    info = accounts.inspect_account_offline(acct, reg=reg)
    base: dict[str, Any] = {
        "account": acct,
        "active": acct == active_name,
        "username": info["username"],
        "auth_method": info["auth_method"],
        "authenticated": False,
        "gpu_quota": None,
        "tpu_quota": None,
        "error": None,
    }
    if not info["ready"]:
        base["error"] = "not logged in"
        return base

    from kaggle.api.kaggle_api_extended import KaggleApi

    with accounts.account_context(acct, source="status", ignore_env_creds=True):
        api = KaggleApi()
        try:
            out_cm = tls_out.capture() if tls_out is not None else contextlib.nullcontext()
            err_cm = tls_err.capture() if tls_err is not None else contextlib.nullcontext()
            with out_cm, err_cm:
                api.authenticate()
        except (Exception, SystemExit) as exc:
            base["error"] = f"authentication failed ({exc})"
            return base

        verified_user = api.config_values.get(api.CONFIG_NAME_USER) or info["username"]
        base["username"] = verified_user
        base["authenticated"] = True
        if verified_user and verified_user != info["username"]:
            with contextlib.suppress(Exception):
                accounts.record_account_login(acct, username=verified_user, auth_method=info["auth_method"])

        try:
            resp = api.quota_view()
            for field, attr in (("gpu_quota", "gpu_quota"), ("tpu_quota", "tpu_quota")):
                q = getattr(resp, attr, None)
                if q is not None:
                    used_h = q.time_used.total_seconds() / 3600.0
                    total_h = q.total_time_allowed.total_seconds() / 3600.0
                    base[field] = {
                        "used_hours": round(used_h, 2),
                        "remaining_hours": round(max(0.0, total_h - used_h), 2),
                        "total_hours": round(total_h, 2),
                    }
        except Exception as exc:
            base["error"] = f"quota check failed: {exc}"
    return base


def _cmd_status(json_output: bool = False) -> None:
    reg = accounts.load_registry(bootstrap=True)
    active = reg.get("active")
    names = list(reg.get("accounts", {}).keys())
    if not names:
        if json_output:
            print("[]")
        else:
            print("[account] No accounts configured.")
        return

    orig_out, orig_err = sys.stdout, sys.stderr
    tls_out = _ThreadLocalStream(orig_out)
    tls_err = _ThreadLocalStream(orig_err)
    sys.stdout = tls_out  # type: ignore[assignment]
    sys.stderr = tls_err  # type: ignore[assignment]
    try:
        with ThreadPoolExecutor(max_workers=min(6, len(names))) as pool:
            results = list(
                pool.map(lambda n: _probe_single_account(n, active, reg, tls_out, tls_err), names)
            )
    finally:
        sys.stdout = orig_out
        sys.stderr = orig_err

    if json_output:
        print(json.dumps(results, indent=2))
        return

    for row in results:
        mark = "*" if row["active"] else " "
        user = ("@" + row["username"]) if row["username"] else "-"
        if row["error"] and not row["authenticated"]:
            print(f"[{mark}{row['account']}] ({user}) | {row['error']}")
            continue
        gpu = row.get("gpu_quota")
        tpu = row.get("tpu_quota")
        gpu_str = f"{gpu['remaining_hours']:.2f}/{gpu['total_hours']:.2f}h" if gpu else "-"
        tpu_str = f"{tpu['remaining_hours']:.2f}/{tpu['total_hours']:.2f}h" if tpu else "-"
        err_suffix = f" | WARN: {row['error']}" if row.get("error") else ""
        print(
            f"[{mark}{row['account']}] ({user}) | auth: {row['auth_method']} | "
            f"GPU: {gpu_str} | TPU: {tpu_str}{err_suffix}"
        )


def _cmd_rename(old: str, new: str) -> None:
    try:
        resolved_new, reg = accounts.rename_account(old, new)
    except accounts.AccountError as exc:
        _exit_error(exc)
        return
    print(f"[account] Renamed '{old}' -> '{resolved_new}' (active: {reg.get('active')})")


def _cmd_remove(name: str, force: bool = False) -> None:
    try:
        resolved, reg = accounts.remove_account(name, force=force)
    except accounts.AccountError as exc:
        _exit_error(exc)
        return
    print(f"[account] Removed '{resolved}' (active: {reg.get('active')})")


# --------------------------------------------------------------------------------------
# Parser wiring
# --------------------------------------------------------------------------------------


def _add_login_options(parser, *, include_code: bool, use_default: bool | None) -> None:
    parser.add_argument(
        "--no-launch-browser",
        "--no-browser",
        dest="no_launch_browser",
        action="store_true",
        help="Print the OAuth URL and prompt for the verification code instead of launching a browser",
    )
    parser.add_argument(
        "--url-only",
        dest="url_only",
        action="store_true",
        help="Print the OAuth PKCE URL, save pending verifier state, and exit (finish with `--code`)",
    )
    if include_code:
        parser.add_argument(
            "--code",
            dest="code",
            default=None,
            help="Finish a headless `--url-only` OAuth flow using the pasted verification code or callback URL",
        )
    parser.add_argument(
        "--token",
        dest="token",
        default=None,
        help="Store a raw KGAT_... API token directly for this account",
    )
    parser.add_argument(
        "--token-file",
        dest="token_file",
        default=None,
        help="Read a KGAT_... API token from a file",
    )
    parser.add_argument(
        "--token-stdin",
        dest="token_stdin",
        action="store_true",
        help="Read a KGAT_... API token from stdin",
    )
    parser.add_argument(
        "--kaggle-json",
        dest="kaggle_json",
        default=None,
        help="Import legacy username/key credentials from a kaggle.json file",
    )
    parser.add_argument(
        "--no-verify",
        dest="no_verify",
        action="store_true",
        help="Skip online token introspection when storing a KGAT_ token",
    )
    if use_default is True:
        parser.add_argument(
            "--use",
            dest="use",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="Switch the global default active account to this profile (default: true)",
        )
    else:
        parser.add_argument(
            "--use",
            dest="use",
            action="store_true",
            help="Also switch the global default active account to this profile",
        )


def attach_account_subparser(subparsers, _cli_mod=None) -> None:
    """Attach `kaggle account` (`acct` alias) to the root CLI subparsers."""
    name_map = getattr(subparsers, "_name_parser_map", {})
    if "account" in name_map:
        return

    parser_acct = subparsers.add_parser(
        "account",
        aliases=["acct"],
        formatter_class=argparse.RawTextHelpFormatter,
        help=ACCOUNT_HELP,
    )
    # CRITICAL: nested subparsers MUST use `dest="command"` so `kaggle.cli:main` strips it.
    sub_acct = parser_acct.add_subparsers(title="commands", dest="command")
    sub_acct.required = True
    sub_acct.choices = list(ACCOUNT_CHOICES)

    p_list = sub_acct.add_parser("list", aliases=["ls"], help="List configured Kaggle accounts (offline)")
    p_list.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_list.set_defaults(func=_cmd_list)

    p_curr = sub_acct.add_parser("current", help="Show the currently resolved Kaggle account")
    p_curr.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_curr.set_defaults(func=_cmd_current)

    p_use = sub_acct.add_parser("use", aliases=["switch"], help="Switch the default active Kaggle account")
    p_use.add_argument("name", help="Account profile name or Kaggle username")
    p_use.set_defaults(func=_cmd_use)

    p_add = sub_acct.add_parser("add", help="Register and authenticate a new isolated Kaggle account")
    p_add.add_argument("name", help="New account profile name (e.g. work, personal)")
    _add_login_options(p_add, include_code=False, use_default=True)
    p_add.set_defaults(func=_cmd_add)

    p_login = sub_acct.add_parser(
        "login",
        help="Log in or import credentials for a Kaggle account (creates profile if needed)",
    )
    p_login.add_argument("name", nargs="?", default=None, help="Account profile name (defaults to active account)")
    _add_login_options(p_login, include_code=True, use_default=False)
    p_login.set_defaults(func=_cmd_login)

    p_logout = sub_acct.add_parser("logout", help="Clear stored credentials for an account")
    p_logout.add_argument("name", nargs="?", default=None, help="Account profile name (defaults to active account)")
    p_logout.add_argument("--revoke", dest="revoke", action="store_true", help="Revoke OAuth token on Kaggle first")
    p_logout.set_defaults(func=_cmd_logout)

    p_status = sub_acct.add_parser(
        "status", help="Concurrently verify auth and weekly GPU/TPU quota across all accounts"
    )
    p_status.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_status.set_defaults(func=_cmd_status)

    p_rename = sub_acct.add_parser("rename", help="Rename an account profile and move its directory")
    p_rename.add_argument("old", help="Existing account profile name or username")
    p_rename.add_argument("new", help="New account profile name")
    p_rename.set_defaults(func=_cmd_rename)

    p_remove = sub_acct.add_parser("remove", aliases=["rm"], help="Remove an account profile and its directory")
    p_remove.add_argument("name", help="Account profile name or username")
    p_remove.add_argument(
        "--force", dest="force", action="store_true", help="Remove even if credentials are still stored"
    )
    p_remove.set_defaults(func=_cmd_remove)


def register_account_commands() -> None:
    """Register `kaggle account` (`acct`) through `cli_hooks`."""
    cli_hooks.register_subparsers_hook(
        attach_account_subparser,
        command="account",
        choices=["account", "acct"],
        help_line="account {list, current, use, add, login, logout, status, rename, remove}",
    )
