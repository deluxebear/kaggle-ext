"""CLI & KaggleApi hook registry for `kaggle-ext`.

All extension features (multi-account isolation, `kaggle skill`, headless/Wayland
OAuth improvements) register through the four extension points here rather than adding
ad-hoc monkey patches across modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import functools
import json
import os
import shutil
import sys
from typing import Callable

from . import accounts

OFFLINE_EXTENSION_COMMANDS: set[str] = {"ext", "account", "acct", "skill", "SKILL"}

_pre_api_hooks: list[Callable[[], None]] = []
_root_parser_hooks: list[Callable[[object], None]] = []
_subparsers_hooks: list[Callable[[object, object], None]] = []
_parsed_args_hooks: list[Callable[[object], None]] = []
_help_entries: dict[str, tuple[list[str], str]] = {}

_PRE_COMMAND_FLAGS_WITH_VALUE = frozenset({"-a", "--account"})
_PRE_COMMAND_FLAGS_NO_VALUE = frozenset({"-W", "--no-warn", "--debug"})
_ACCOUNT_OPTION_DEST = "_kaggle_ext_account"


def _append_unique(registry: list, fn: Callable) -> None:
    if fn not in registry:
        registry.append(fn)


def register_pre_api_hook(fn: Callable[[], None]) -> None:
    """Register a callback executed before `kaggle.api.kaggle_api_extended` loads."""
    _append_unique(_pre_api_hooks, fn)


def register_root_parser_hook(fn: Callable[[object], None]) -> None:
    """Register a callback receiving the root `ArgumentParser` before `parse_args()`."""
    _append_unique(_root_parser_hooks, fn)


def register_subparsers_hook(
    fn: Callable[[object, object], None],
    *,
    command: str | None = None,
    choices: list[str] | tuple[str, ...] = (),
    help_line: str = "",
) -> None:
    """Register a callback receiving `(subparsers, cli_mod)` to attach top-level commands."""
    _append_unique(_subparsers_hooks, fn)
    if command:
        choice_list = list(choices) if choices else [command]
        _help_entries[command] = (choice_list, help_line)


def register_parsed_args_hook(fn: Callable[[object], None]) -> None:
    """Register a callback receiving `args` right after `parse_args()` returns."""
    _append_unique(_parsed_args_hooks, fn)


def run_pre_api_hooks() -> None:
    """Run all registered pre-API hooks."""
    for hook in list(_pre_api_hooks):
        hook()


def _positional_tokens(argv: list[str] | None = None) -> list[str]:
    """Return positional command tokens starting from the top-level subcommand."""
    tokens = list((argv if argv is not None else sys.argv)[1:])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-h", "--help", "-v", "--version"):
            return []
        if tok in _PRE_COMMAND_FLAGS_NO_VALUE:
            i += 1
            continue
        if tok in _PRE_COMMAND_FLAGS_WITH_VALUE:
            i += 2
            continue
        if any(tok.startswith(flag + "=") for flag in _PRE_COMMAND_FLAGS_WITH_VALUE):
            i += 1
            continue
        if tok.startswith("-a") and len(tok) > 2 and not tok.startswith("--"):
            i += 1
            continue
        if tok == "--":
            return tokens[i + 1 :]
        if tok.startswith("-"):
            i += 1
            continue
        return tokens[i:]
    return []


def extract_top_level_command(argv: list[str] | None = None) -> str | None:
    """Return the top-level subcommand token from `argv`, skipping known root flags.

    Returns `None` if a root `-h`/`--help`/`-v`/`--version` flag appears before any
    subcommand or if no positional command token exists.
    """
    pos = _positional_tokens(argv)
    return pos[0] if pos else None


def is_offline_extension_invocation(argv: list[str] | None = None) -> bool:
    """True when the CLI is invoking an extension command that works without Kaggle login."""
    cmd = extract_top_level_command(argv)
    return bool(cmd and cmd in OFFLINE_EXTENSION_COMMANDS)


def _is_cli_argv() -> bool:
    """Check whether `sys.argv` / call stack represents `kaggle` CLI or `python -m kaggle`."""
    prog = (sys.argv[0] if sys.argv else "").replace("\\", "/")
    if os.path.splitext(os.path.basename(prog))[0] == "kaggle":
        return True
    if prog.endswith("/kaggle/__main__.py"):
        return True
    if not sys.argv or sys.argv[0] != "-m":
        return False
    getframe = getattr(sys, "_getframe", None)
    if getframe is None:
        return False
    try:
        frame = getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "_run_module_as_main":
                mod_name = frame.f_locals.get("mod_name")
                if mod_name in ("kaggle", "kaggle.__main__", "kaggle.cli"):
                    return True
            frame = frame.f_back
    except ValueError:
        pass
    return False


def _is_kaggle_cli_process(api_instance) -> bool:
    """True when running as `kaggle` console script or `python -m kaggle`."""
    invoked_fn = getattr(api_instance, "_invoked_as_cli", None)
    if callable(invoked_fn) and invoked_fn():
        return True
    return _is_cli_argv()


def _allows_account_autocreate(argv: list[str] | None = None) -> bool:
    pos = _positional_tokens(argv)
    if not pos:
        return False
    if pos[0] in ("account", "acct"):
        return True
    return pos[0] == "auth" and len(pos) > 1 and pos[1] == "login"


def prepare_multi_account_pre_api() -> None:
    """Pre-API hook: resolve `-a` / `KAGGLE_ACCOUNT` / active account before `KaggleApi` loads."""
    accounts.capture_initial_env()
    if _is_cli_argv():
        acct_flag, cleaned_argv, _missing = accounts.extract_account_from_argv(sys.argv)
        allow_create = _allows_account_autocreate(cleaned_argv)
        accounts.activate_startup_account(acct_flag, allow_create=allow_create)
    elif os.environ.get("KAGGLE_ACCOUNT") or accounts.external_config_dir() is None:
        accounts.activate_startup_account(None, allow_create=False)


# --------------------------------------------------------------------------------------
# Descriptors & patches for KaggleApi + kagglesdk
# --------------------------------------------------------------------------------------


class _DynamicConfigDirDescriptor:
    def __init__(self, fallback: str) -> None:
        self._fallback = fallback

    def __get__(self, instance, owner=None):
        if instance is not None and "config_dir" in instance.__dict__:
            return instance.__dict__["config_dir"]
        acct_dir = accounts.active_account_dir()
        if acct_dir:
            return acct_dir
        return self._fallback

    def __set__(self, instance, value) -> None:
        instance.__dict__["config_dir"] = value


class _DynamicConfigFileDescriptor:
    def __init__(self, fallback: str) -> None:
        self._fallback = fallback

    def __get__(self, instance, owner=None):
        if instance is not None and "config" in instance.__dict__:
            return instance.__dict__["config"]
        acct_dir = accounts.active_account_dir()
        if acct_dir:
            cfg_file = getattr(owner, "config_file", "kaggle.json") if owner is not None else "kaggle.json"
            return os.path.join(acct_dir, cfg_file)
        return self._fallback

    def __set__(self, instance, value) -> None:
        instance.__dict__["config"] = value


class _DynamicCredsFileDescriptor:
    def __init__(self, fallback: str) -> None:
        self._fallback = fallback

    def __get__(self, instance, owner=None):
        if instance is not None and "DEFAULT_CREDENTIALS_FILE" in instance.__dict__:
            return instance.__dict__["DEFAULT_CREDENTIALS_FILE"]
        acct_dir = accounts.active_account_dir()
        if acct_dir:
            return os.path.join(acct_dir, "credentials.json")
        return self._fallback

    def __set__(self, instance, value) -> None:
        instance.__dict__["DEFAULT_CREDENTIALS_FILE"] = value


def _read_plain_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            val = fh.read().strip()
        return val or None
    except OSError:
        return None


def _patch_kagglesdk(api_mod, *, force: bool = False) -> None:
    """Patch `kagglesdk` credential & browser touchpoints for per-account isolation."""
    try:
        import kagglesdk
        import kagglesdk.kaggle_creds as kaggle_creds_mod
        import kagglesdk.kaggle_env as kaggle_env_mod
        import kagglesdk.kaggle_http_client as http_client_mod
        import kagglesdk.kaggle_oauth as kaggle_oauth_mod
    except ImportError:
        return

    # 1. Patch get_access_token_from_env across all module bindings.
    if force or not getattr(kaggle_env_mod, "_kaggle_ext_patched", False):
        orig_get_token = getattr(
            kaggle_env_mod,
            "_kaggle_ext_orig_get_access_token",
            kaggle_env_mod.get_access_token_from_env,
        )

        @functools.wraps(orig_get_token)
        def _wrapped_get_access_token_from_env():
            acct_dir = accounts.active_account_dir()
            if acct_dir is None:
                return orig_get_token()
            if not accounts.should_ignore_env_creds():
                is_notebook = getattr(kaggle_env_mod, "is_in_kaggle_notebook", lambda: False)
                if is_notebook() or os.environ.get("KAGGLE_API_TOKEN"):
                    tok, src = orig_get_token()
                    if tok:
                        return tok, src
            for fname in ("access_token", "access_token.txt"):
                fpath = os.path.join(acct_dir, fname)
                tok = _read_plain_file(fpath)
                if tok:
                    return tok, fpath
            return None, None

        kaggle_env_mod._kaggle_ext_orig_get_access_token = orig_get_token  # type: ignore[attr-defined]
        kaggle_env_mod._kaggle_ext_patched = True  # type: ignore[attr-defined]
        for target_mod in (kaggle_env_mod, kagglesdk, http_client_mod, api_mod):
            if hasattr(target_mod, "get_access_token_from_env"):
                setattr(target_mod, "get_access_token_from_env", _wrapped_get_access_token_from_env)

    # 2. Patch KaggleCredentials load/save/delete (bypasses definition-time __defaults__).
    creds_cls = getattr(kaggle_creds_mod, "KaggleCredentials", None)
    if creds_cls is not None and (force or not getattr(creds_cls, "_kaggle_ext_patched", False)):
        orig_default_file = getattr(
            creds_cls, "_kaggle_ext_orig_default_file", creds_cls.DEFAULT_CREDENTIALS_FILE
        )
        raw_load = creds_cls.__dict__.get("load")
        orig_load_fn = getattr(
            creds_cls,
            "_kaggle_ext_orig_load_fn",
            raw_load.__func__ if isinstance(raw_load, classmethod) else creds_cls.load.__func__,
        )
        orig_save = getattr(creds_cls, "_kaggle_ext_orig_save", creds_cls.save)
        orig_delete = getattr(creds_cls, "_kaggle_ext_orig_delete", creds_cls.delete)
        orig_has_expired = getattr(
            creds_cls,
            "_kaggle_ext_orig_access_token_has_expired",
            getattr(creds_cls, "access_token_has_expired", None),
        )

        def _resolve_creds_path(file_path: str | None) -> tuple[str, bool]:
            acct_dir = accounts.active_account_dir()
            is_default = file_path is None or (
                os.path.expanduser(file_path) == os.path.expanduser(orig_default_file)
            )
            if acct_dir is None:
                return (file_path if file_path is not None else orig_default_file), is_default
            if is_default:
                return os.path.join(acct_dir, "credentials.json"), True
            return file_path, False

        @classmethod
        def _wrapped_load(cls, client, file_path: str | None = None):
            resolved, _ = _resolve_creds_path(file_path)
            return orig_load_fn(cls, client, file_path=resolved)

        @functools.wraps(orig_save)
        def _wrapped_save(self, file_path: str | None = None) -> None:
            resolved, is_default = _resolve_creds_path(file_path)
            orig_save(self, file_path=resolved)
            if is_default and accounts.active_account_dir() is not None:
                try:
                    username = self.get_username() if hasattr(self, "get_username") else None
                    if username:
                        accounts.record_account_login(
                            accounts.current_account(), username=username, auth_method="oauth"
                        )
                except Exception:
                    pass

        @functools.wraps(orig_delete)
        def _wrapped_delete(self, file_path: str | None = None) -> None:
            resolved, _ = _resolve_creds_path(file_path)
            orig_delete(self, file_path=resolved)

        creds_cls._kaggle_ext_orig_default_file = orig_default_file
        creds_cls._kaggle_ext_orig_load_fn = orig_load_fn
        creds_cls._kaggle_ext_orig_save = orig_save
        creds_cls._kaggle_ext_orig_delete = orig_delete
        creds_cls.DEFAULT_CREDENTIALS_FILE = _DynamicCredsFileDescriptor(orig_default_file)
        creds_cls.load = _wrapped_load
        creds_cls.save = _wrapped_save
        creds_cls.delete = _wrapped_delete
        if orig_has_expired is not None:

            @functools.wraps(orig_has_expired)
            def _wrapped_access_token_has_expired(self) -> bool:
                exp = getattr(self, "_access_token_expiration", None)
                if not exp:
                    return True
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                return exp <= datetime.now(timezone.utc) + timedelta(minutes=5)

            creds_cls._kaggle_ext_orig_access_token_has_expired = orig_has_expired
            creds_cls.access_token_has_expired = _wrapped_access_token_has_expired
        creds_cls._kaggle_ext_patched = True

    # 3. Patch KaggleHttpClient._get_apikey_creds (hardcoded ~/.kaggle/kaggle.json).
    if force or not getattr(http_client_mod, "_kaggle_ext_apikey_patched", False):
        orig_get_apikey = getattr(
            http_client_mod, "_kaggle_ext_orig_get_apikey_creds", http_client_mod._get_apikey_creds
        )

        @functools.wraps(orig_get_apikey)
        def _wrapped_get_apikey_creds():
            acct_dir = accounts.active_account_dir()
            if acct_dir is None:
                return orig_get_apikey()
            kj_path = os.path.join(acct_dir, "kaggle.json")
            if not os.path.exists(kj_path):
                return None
            try:
                with open(kj_path, encoding="utf-8") as fh:
                    raw = fh.read()
                if not raw or not raw.strip():
                    return None
                data = json.loads(raw)
                return data["username"], data["key"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                return None

        http_client_mod._kaggle_ext_orig_get_apikey_creds = orig_get_apikey  # type: ignore[attr-defined]
        http_client_mod._get_apikey_creds = _wrapped_get_apikey_creds
        http_client_mod._kaggle_ext_apikey_patched = True  # type: ignore[attr-defined]

    # 4. Patch KaggleOAuth._can_open_browser for Linux Wayland desktops.
    oauth_cls = getattr(kaggle_oauth_mod, "KaggleOAuth", None)
    if oauth_cls is not None and (force or not getattr(oauth_cls, "_kaggle_ext_browser_patched", False)):
        orig_can_open = getattr(oauth_cls, "_kaggle_ext_orig_can_open_browser", oauth_cls._can_open_browser)

        @staticmethod
        def _wrapped_can_open_browser():
            if orig_can_open():
                return True
            if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
                return bool(shutil.which("xdg-open"))
            return False

        oauth_cls._kaggle_ext_orig_can_open_browser = orig_can_open
        oauth_cls._can_open_browser = _wrapped_can_open_browser
        oauth_cls._kaggle_ext_browser_patched = True


def patch_api_module(api_mod, *, force: bool = False) -> None:
    """Patch `KaggleApi` and `kagglesdk` for offline commands and multi-account isolation."""
    _patch_kagglesdk(api_mod, force=force)

    kaggle_api_cls = getattr(api_mod, "KaggleApi", None)
    if kaggle_api_cls is None:
        return
    if getattr(kaggle_api_cls, "_kaggle_ext_api_patched", False) and not force:
        return

    # Dynamic config_dir/config descriptors & per-instance config_values.
    if not getattr(kaggle_api_cls, "_kaggle_ext_config_patched", False) or force:
        orig_dir = getattr(kaggle_api_cls, "_kaggle_ext_orig_config_dir", kaggle_api_cls.config_dir)
        orig_cfg = getattr(kaggle_api_cls, "_kaggle_ext_orig_config_file", kaggle_api_cls.config)
        orig_init = getattr(kaggle_api_cls, "_kaggle_ext_orig_init", getattr(kaggle_api_cls, "__init__", None))

        kaggle_api_cls._kaggle_ext_orig_config_dir = orig_dir
        kaggle_api_cls._kaggle_ext_orig_config_file = orig_cfg
        kaggle_api_cls.config_dir = _DynamicConfigDirDescriptor(orig_dir)
        kaggle_api_cls.config = _DynamicConfigFileDescriptor(orig_cfg)

        if orig_init is not None:

            @functools.wraps(orig_init)
            def _wrapped_init(self, *args, **kwargs):
                orig_init(self, *args, **kwargs)
                if "config_values" not in self.__dict__:
                    self.config_values = {}

            kaggle_api_cls._kaggle_ext_orig_init = orig_init
            kaggle_api_cls.__init__ = _wrapped_init

        orig_read_env = getattr(
            kaggle_api_cls,
            "_kaggle_ext_orig_read_config_env",
            getattr(kaggle_api_cls, "read_config_environment", None),
        )
        if orig_read_env is not None:

            @functools.wraps(orig_read_env)
            def _wrapped_read_config_env(self, config_data=None):
                if not accounts.should_ignore_env_creds():
                    return orig_read_env(self, config_data=config_data)
                base = dict(config_data) if config_data is not None else {}
                env_only = orig_read_env(self, config_data=None)
                for secret_k in ("username", "key", "api_token", "token"):
                    env_only.pop(secret_k, None)
                base.update(env_only)
                return base

            kaggle_api_cls._kaggle_ext_orig_read_config_env = orig_read_env
            kaggle_api_cls.read_config_environment = _wrapped_read_config_env

        kaggle_api_cls._kaggle_ext_config_patched = True

    orig_auth = getattr(kaggle_api_cls, "_kaggle_ext_orig_authenticate", kaggle_api_cls.authenticate)
    orig_allows = getattr(
        kaggle_api_cls,
        "_kaggle_ext_orig_allows_logged_out",
        getattr(kaggle_api_cls, "_command_allows_logged_out", None),
    )

    @functools.wraps(orig_auth)
    def _wrapped_authenticate(self, *args, **kwargs):
        if (
            accounts.ACTIVE_ACCOUNT.get() is None
            and _is_kaggle_cli_process(self)
            and (is_offline_extension_invocation() or accounts.get_pending_account_error() is not None)
        ):
            self._authenticated = True
            return None
        res = orig_auth(self, *args, **kwargs)
        self._kaggle_ext_authenticated_account = accounts.active_account_dir()
        return res

    kaggle_api_cls._kaggle_ext_orig_authenticate = orig_auth
    kaggle_api_cls.authenticate = _wrapped_authenticate

    if orig_allows is not None:

        @functools.wraps(orig_allows)
        def _wrapped_allows_logged_out(self, api_command: str, *args, **kwargs):
            if is_offline_extension_invocation():
                return True
            pos = _positional_tokens(sys.argv)
            cleaned_cmd = " ".join(pos) if pos else api_command
            return orig_allows(self, cleaned_cmd, *args, **kwargs)

        kaggle_api_cls._kaggle_ext_orig_allows_logged_out = orig_allows
        kaggle_api_cls._command_allows_logged_out = _wrapped_allows_logged_out

    kaggle_api_cls._kaggle_ext_api_patched = True


# --------------------------------------------------------------------------------------
# Root ArgumentParser + subparsers hooks
# --------------------------------------------------------------------------------------


def _attach_account_root_option(parser) -> None:
    if any(opt == "--account" for act in getattr(parser, "_actions", []) for opt in getattr(act, "option_strings", [])):
        return
    parser.add_argument(
        "-a",
        "--account",
        dest=_ACCOUNT_OPTION_DEST,
        metavar="ACCOUNT",
        default=None,
        help="Kaggle account profile name or username (overrides KAGGLE_ACCOUNT)",
    )


def register_multi_account_hooks() -> None:
    """Register multi-account pre-API and root parser hooks idempotently."""
    register_pre_api_hook(prepare_multi_account_pre_api)
    register_root_parser_hook(_attach_account_root_option)


def _update_help_metadata(cli_mod) -> None:
    """Idempotently append registered extension choices and summary lines to `Help`."""
    help_cls = getattr(cli_mod, "Help", None)
    if help_cls is None:
        return

    choices = getattr(help_cls, "kaggle_choices", None)
    if isinstance(choices, list):
        for _cmd, (cmd_choices, _line) in _help_entries.items():
            for item in cmd_choices:
                if item not in choices:
                    choices.append(item)

    help_text = getattr(help_cls, "kaggle", None)
    if isinstance(help_text, str):
        for _cmd, (_choices, line) in _help_entries.items():
            if line and line not in help_text:
                help_text = help_text.rstrip("\n") + "\n" + line
        help_cls.kaggle = help_text


def install_cli_hooks(cli_mod, *, force: bool = False) -> None:
    """Wrap `kaggle.cli.main` to intercept the local `ArgumentParser` and `subparsers`."""
    register_multi_account_hooks()
    _update_help_metadata(cli_mod)

    orig_main = getattr(cli_mod, "main", None)
    if orig_main is None:
        return
    if getattr(orig_main, "_kaggle_ext_wrapped", False) and not force:
        return
    base_main = getattr(orig_main, "_kaggle_ext_orig", orig_main)

    @functools.wraps(base_main)
    def _wrapped_main(*args, **kwargs):
        _update_help_metadata(cli_mod)
        orig_arg_parser_cls = cli_mod.argparse.ArgumentParser
        captured: dict[str, object] = {"parser": None, "subparsers": None}

        def _parser_factory(*p_args, **p_kwargs):
            # Restore before calling orig_arg_parser_cls because stdlib ArgumentParser.__init__
            # resolves `super(ArgumentParser, self)` via the module global `ArgumentParser`.
            cli_mod.argparse.ArgumentParser = orig_arg_parser_cls
            is_root = captured["parser"] is None and p_kwargs.get("add_help", True) is not False
            try:
                parser = orig_arg_parser_cls(*p_args, **p_kwargs)
            finally:
                if captured["parser"] is None and not is_root:
                    cli_mod.argparse.ArgumentParser = _parser_factory
            if is_root:
                captured["parser"] = parser
                _instrument_root_parser(parser, captured, cli_mod)
            return parser

        cli_mod.argparse.ArgumentParser = _parser_factory
        try:
            return base_main(*args, **kwargs)
        finally:
            cli_mod.argparse.ArgumentParser = orig_arg_parser_cls

    _wrapped_main._kaggle_ext_orig = base_main  # type: ignore[attr-defined]
    _wrapped_main._kaggle_ext_wrapped = True  # type: ignore[attr-defined]
    cli_mod.main = _wrapped_main


def _instrument_root_parser(parser, captured: dict[str, object], cli_mod) -> None:
    orig_add_subparsers = parser.add_subparsers
    orig_parse_args = parser.parse_args

    def _wrapped_add_subparsers(*sp_args, **sp_kwargs):
        subparsers = orig_add_subparsers(*sp_args, **sp_kwargs)
        if captured["subparsers"] is None:
            captured["subparsers"] = subparsers
        return subparsers

    def _wrapped_parse_args(*pa_args, **pa_kwargs):
        for hook in list(_root_parser_hooks):
            hook(parser)

        subparsers = captured.get("subparsers")
        if subparsers is not None:
            for hook in list(_subparsers_hooks):
                hook(subparsers, cli_mod)
            choices = getattr(subparsers, "choices", None)
            name_map = getattr(subparsers, "_name_parser_map", None)
            if isinstance(choices, list) and isinstance(name_map, dict):
                for key in name_map:
                    if key not in choices:
                        choices.append(key)

        if pa_args:
            raw_tokens = list(pa_args[0])
            has_explicit = True
        elif "args" in pa_kwargs and pa_kwargs["args"] is not None:
            raw_tokens = list(pa_kwargs["args"])
            has_explicit = True
        else:
            raw_tokens = list(sys.argv[1:])
            has_explicit = False

        prog = sys.argv[0] if sys.argv else "kaggle"
        acct_flag, cleaned_argv, missing_val = accounts.extract_account_from_argv([prog, *raw_tokens])
        cleaned_tokens = cleaned_argv[1:]

        if missing_val:
            parsed = orig_parse_args(*pa_args, **pa_kwargs)
        elif has_explicit:
            new_kwargs = dict(pa_kwargs)
            if pa_args:
                parsed = orig_parse_args(cleaned_tokens, *pa_args[1:], **new_kwargs)
            else:
                new_kwargs["args"] = cleaned_tokens
                parsed = orig_parse_args(**new_kwargs)
        else:
            new_kwargs = dict(pa_kwargs)
            new_kwargs["args"] = cleaned_tokens
            parsed = orig_parse_args(**new_kwargs)

        top_cmd = extract_top_level_command(cleaned_argv)
        allow_create = _allows_account_autocreate(cleaned_argv)
        if acct_flag is not None or accounts.get_pending_account_error() is not None:
            accounts.activate_startup_account(acct_flag, allow_create=allow_create)

        pending_err = accounts.get_pending_account_error()
        if pending_err is not None:
            print(f"[account] {pending_err}", file=sys.stderr)
            sys.exit(1)

        if hasattr(parsed, _ACCOUNT_OPTION_DEST):
            delattr(parsed, _ACCOUNT_OPTION_DEST)

        for hook in list(_parsed_args_hooks):
            hook(parsed)

        cli_api = getattr(cli_mod, "api", None)
        if cli_api is not None and top_cmd not in OFFLINE_EXTENSION_COMMANDS:
            target_dir = accounts.active_account_dir()
            if getattr(cli_api, "_kaggle_ext_authenticated_account", None) != target_dir:
                cli_api._authenticated = False
                cli_api.config_values = {}

        return parsed

    parser.add_subparsers = _wrapped_add_subparsers
    parser.parse_args = _wrapped_parse_args


# Ensure pre-API hooks are ready immediately when loader imports `cli_hooks`.
register_multi_account_hooks()
