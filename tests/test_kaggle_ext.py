"""Hermetic unit and end-to-end integration tests for `kaggle-ext`.

All tests isolate filesystem state inside `TemporaryDirectory` via `HOME`,
`XDG_CONFIG_HOME`, `KAGGLE_CONFIG_DIR`, `KAGGLE_EXT_HOME`, and `KAGGLE_EXT_SOURCE`.
Real `~/.kaggle`, `~/.config/kaggle`, `~/.kaggle-ext`, and real `uv` tool
`site-packages` are never read or modified.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.server
import importlib
import importlib.util
import io
import json
import os
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kaggle_ext  # noqa: E402
from kaggle_ext import (  # noqa: E402
    account_commands,
    accounts,
    cli_hooks,
    commands,
    diagnostics,
    inject,
    session_client,
    session_commands,
    session_ws,
    sessions,
    skill_commands,
    skill_doc,
)


def _clean_env(
    *,
    home: Path,
    ext_home: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "KAGGLE_EXT_HOME": str(ext_home),
        "KAGGLE_EXT_CONFIG_DIR": str(home / ".config" / "kaggle-ext"),
        "PYTHONNOUSERSITE": "1",
    }
    for key in (
        "KAGGLE_API_TOKEN",
        "KAGGLE_USERNAME",
        "KAGGLE_KEY",
        "KAGGLE_CONFIG_DIR",
        "KAGGLE_ACCOUNT",
        "KAGGLE_EXT_SOURCE",
        "KAGGLE_EXT_DEBUG",
        "KAGGLE_EXT_API_ENDPOINT",
        "DISPLAY",
        "WAYLAND_DISPLAY",
    ):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def _real_kaggle_python() -> str | None:
    """Locate the Python interpreter running the installed `kaggle` CLI, if present."""
    for py in inject._interpreters_to_try():
        try:
            proc = subprocess.run(
                [py, "-c", "import importlib.util; print(bool(importlib.util.find_spec('kaggle')))"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0 and proc.stdout.strip() == "True":
                return py
        except Exception:
            continue
    return None


class InjectAndInstallerTests(unittest.TestCase):
    def test_pth_contents_single_line_import_and_fail_open(self) -> None:
        body = inject.pth_contents()
        import_lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(import_lines), 1)
        self.assertTrue(import_lines[0].startswith("import os, sys; exec("))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _clean_env(home=tmp_path / "home", ext_home=tmp_path / "missing-ext-home")
            proc = subprocess.run(
                [sys.executable, "-c", import_lines[0]],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")

    def test_ensure_link_status_and_remove_hermetic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ext_home = tmp_path / "ext-home"
            site_dir = tmp_path / "site"
            site_dir.mkdir()
            (site_dir / "kaggle").mkdir()
            (site_dir / "kaggle" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")

            old_ext_home = os.environ.get("KAGGLE_EXT_HOME")
            os.environ["KAGGLE_EXT_HOME"] = str(ext_home)
            try:
                rep1 = inject.ensure(site_dir=str(site_dir), copy_source=True, quiet=True)
                self.assertTrue(rep1["changed"])
                self.assertTrue((ext_home / "kaggle_ext" / "__init__.py").is_file())
                self.assertTrue((ext_home / "kaggle_ext_loader.py").is_file())
                self.assertTrue((site_dir / inject.PTH_NAME).is_file())

                # Second run must be idempotent.
                rep2 = inject.ensure(site_dir=str(site_dir), copy_source=True, quiet=True)
                self.assertFalse(rep2["changed"])

                st = inject.status(site_dir=str(site_dir))
                self.assertTrue(st["kaggle_here"])
                self.assertEqual(st["kaggle_cli_version"], "9.9.9")
                self.assertTrue(st["pth_present"])

                # Switch to --link dev mode and verify symlinks.
                rep_link = inject.ensure(site_dir=str(site_dir), copy_source=False, link=True, quiet=True)
                self.assertTrue(rep_link["linked"])
                self.assertTrue((ext_home / "kaggle_ext").is_symlink())
                self.assertTrue((ext_home / "kaggle_ext_loader.py").is_symlink())

                removed = inject.remove(site_dir=str(site_dir))
                self.assertTrue(removed["removed"])
                self.assertFalse((site_dir / inject.PTH_NAME).exists())
                # Source remains intact outside site-packages.
                self.assertTrue((ext_home / "kaggle_ext_loader.py").exists())
            finally:
                if old_ext_home is None:
                    os.environ.pop("KAGGLE_EXT_HOME", None)
                else:
                    os.environ["KAGGLE_EXT_HOME"] = old_ext_home

    def test_install_py_cli_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            ext_home = tmp_path / "ext-home"
            site_dir = tmp_path / "site"
            site_dir.mkdir()
            env = _clean_env(home=home, ext_home=ext_home)

            install_py = str(REPO_ROOT / "install.py")
            p_link = subprocess.run(
                [sys.executable, install_py, "--link", "--site-dir", str(site_dir)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(p_link.returncode, 0, p_link.stderr)
            self.assertTrue((ext_home / "kaggle_ext").is_symlink())
            self.assertTrue((site_dir / inject.PTH_NAME).is_file())

            p_status = subprocess.run(
                [sys.executable, install_py, "--status", "--site-dir", str(site_dir)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(p_status.returncode, 0, p_status.stderr)
            self.assertIn("pth_present       : True", p_status.stdout)

            p_uninst = subprocess.run(
                [sys.executable, install_py, "--uninstall", "--site-dir", str(site_dir)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(p_uninst.returncode, 0, p_uninst.stderr)
            self.assertFalse((site_dir / inject.PTH_NAME).exists())


class DiagnosticsTests(unittest.TestCase):
    def test_empty_home_does_not_create_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "fresh-home"
            home.mkdir()
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
            rep = diagnostics.inspect_kaggle_auth_offline(
                environ=env, home_dir=str(home), platform_name="linux"
            )
            self.assertEqual(rep["config_dir"], str(home / ".config" / "kaggle"))
            self.assertEqual(rep["config_dir_source"], "XDG_CONFIG_HOME")
            self.assertEqual(rep["effective_auth_method"], "none")
            self.assertFalse((home / ".kaggle").exists())
            self.assertFalse((home / ".config" / "kaggle").exists())

    def test_legacy_json_permissions_and_secret_redaction(self) -> None:
        secret_key = "0123456789abcdef0123456789abcdef"
        secret_token = "KGAT_fedcba9876543210fedcba9876543210"
        secret_refresh = "KGRT_super_secret_refresh_token"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            dot_kaggle = home / ".kaggle"
            dot_kaggle.mkdir(parents=True)

            kj = dot_kaggle / "kaggle.json"
            kj.write_text(
                json.dumps({"username": "alice", "key": secret_key, "competition": "titanic"}),
                encoding="utf-8",
            )
            os.chmod(kj, 0o644)

            at = dot_kaggle / "access_token"
            at.write_text(secret_token + "\n", encoding="utf-8")

            oc = dot_kaggle / "credentials.json"
            oc.write_text(
                json.dumps(
                    {
                        "username": "alice-oauth",
                        "refresh_token": secret_refresh,
                        "access_token": "opaque-access-token",
                        "access_token_expiration": "2099-01-01T00:00:00+00:00",
                        "scopes": ["resources.admin:*"],
                    }
                ),
                encoding="utf-8",
            )

            rep = diagnostics.inspect_kaggle_auth_offline(
                environ={"HOME": str(home), "WAYLAND_DISPLAY": "wayland-1"},
                home_dir=str(home),
                platform_name="linux",
            )
            serialized = json.dumps(rep)
            self.assertNotIn(secret_key, serialized)
            self.assertNotIn(secret_token, serialized)
            self.assertNotIn(secret_refresh, serialized)
            self.assertNotIn("opaque-access-token", serialized)

            self.assertEqual(rep["config_dir_source"], "~/.kaggle")
            self.assertTrue(rep["kaggle_json"]["world_readable"])
            self.assertTrue(rep["kaggle_json"]["has_key"])
            self.assertEqual(rep["kaggle_json"]["extra_config"], {"competition": "titanic"})
            self.assertTrue(rep["access_token_file"]["valid_format"])
            self.assertEqual(rep["effective_auth_method"], "access_token")
            self.assertEqual(rep["effective_auth_source"], str(at))
            self.assertFalse(rep["oauth_credentials"]["access_token_expired"])
            self.assertTrue(rep["display"]["wayland_only_oauth_quirk"])

            os.chmod(kj, 0o600)
            rep2 = diagnostics.inspect_kaggle_auth_offline(
                environ={"HOME": str(home)}, home_dir=str(home), platform_name="linux"
            )
            self.assertFalse(rep2["kaggle_json"]["world_readable"])
            self.assertEqual(rep2["kaggle_json"]["mode"], oct(stat.S_IMODE(0o600)))


class CliHooksUnitTests(unittest.TestCase):
    def test_extract_top_level_command_skips_root_flags(self) -> None:
        self.assertEqual(cli_hooks.extract_top_level_command(["kaggle", "ext", "doctor"]), "ext")
        self.assertEqual(
            cli_hooks.extract_top_level_command(["kaggle", "-W", "--debug", "ext", "version"]),
            "ext",
        )
        self.assertEqual(
            cli_hooks.extract_top_level_command(["kaggle", "-a", "work", "ext", "doctor"]),
            "ext",
        )
        self.assertEqual(
            cli_hooks.extract_top_level_command(["kaggle", "--account=personal", "datasets", "list"]),
            "datasets",
        )
        self.assertIsNone(cli_hooks.extract_top_level_command(["kaggle", "--help"]))
        self.assertIsNone(cli_hooks.extract_top_level_command(["kaggle", "-v"]))

    def test_synthetic_kaggle_cli_and_api_hooking(self) -> None:
        auth_calls: list[str] = []
        dispatched: dict[str, object] = {}

        class FakeHelp:
            kaggle_choices = ["search"]
            kaggle = "Use one of:\nsearch"

        class FakeApi:
            _authenticated = False
            config_dir = "/tmp/fake-kaggle"
            config = "/tmp/fake-kaggle/kaggle.json"
            config_values: dict[str, str] = {}

            def _invoked_as_cli(self) -> bool:
                return True

            def _command_allows_logged_out(self, api_command: str) -> bool:
                return False

            def read_config_environment(self, config_data=None):
                return dict(config_data or {})

            def authenticate(self) -> None:
                auth_calls.append("called")
                raise SystemExit(1)

        fake_api = FakeApi()

        def fake_search_cli(query: str) -> None:
            dispatched["search"] = query

        def fake_main() -> None:
            parser = fake_cli_mod.argparse.ArgumentParser()
            subparsers = parser.add_subparsers(title="commands", help=FakeHelp.kaggle, dest="command")
            subparsers.required = True
            subparsers.choices = FakeHelp.kaggle_choices
            sp_search = subparsers.add_parser("search")
            sp_search.add_argument("query")
            sp_search.set_defaults(func=fake_search_cli)

            args = parser.parse_args()
            command_args = dict(vars(args))
            del command_args["func"]
            del command_args["command"]
            if not fake_api._authenticated:
                fake_api.authenticate()
            args.func(**command_args)

        fake_api_mod = types.SimpleNamespace(KaggleApi=FakeApi)
        fake_cli_mod = types.SimpleNamespace(argparse=argparse, Help=FakeHelp, main=fake_main, api=fake_api)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_patch = _clean_env(home=tmp_path / "home", ext_home=tmp_path / "ext")
            with mock.patch.dict(os.environ, env_patch, clear=False):
                accounts.clear_startup_override(reset_initial_env=True)
                cli_hooks.patch_api_module(fake_api_mod, force=True)
                commands.register_commands()
                account_commands.register_account_commands()
                cli_hooks.install_cli_hooks(fake_cli_mod, force=True)

                self.assertIn("ext", FakeHelp.kaggle_choices)
                self.assertIn("account", FakeHelp.kaggle_choices)
                self.assertIn("ext {", FakeHelp.kaggle)

                old_argv = sys.argv
                buf = io.StringIO()
                captured_tags: list[str | None] = []

                def _add_custom_flag(parser) -> None:
                    parser.add_argument("--custom-tag", dest="custom_tag", default=None)

                def _consume_custom_flag(args) -> None:
                    captured_tags.append(getattr(args, "custom_tag", None))
                    if hasattr(args, "custom_tag"):
                        delattr(args, "custom_tag")

                cli_hooks.register_root_parser_hook(_add_custom_flag)
                cli_hooks.register_parsed_args_hook(_consume_custom_flag)
                try:
                    sys.argv = ["kaggle", "-a", "default", "--custom-tag=work", "ext", "version"]
                    with contextlib.redirect_stdout(buf):
                        fake_cli_mod.main()
                    self.assertEqual(auth_calls, [])
                    self.assertTrue(fake_api._authenticated)
                    self.assertEqual(captured_tags, ["work"])
                    ver_payload = json.loads(buf.getvalue())
                    self.assertEqual(ver_payload["extension"], kaggle_ext.__version__)

                    # Native command still hits upstream authenticate().
                    fake_api._authenticated = False
                    sys.argv = ["kaggle", "search", "titanic"]
                    with self.assertRaises(SystemExit) as ctx:
                        fake_cli_mod.main()
                    self.assertEqual(ctx.exception.code, 1)
                    self.assertEqual(auth_calls, ["called"])
                finally:
                    sys.argv = old_argv
                    accounts.clear_startup_override(reset_initial_env=True)
                    if _add_custom_flag in cli_hooks._root_parser_hooks:
                        cli_hooks._root_parser_hooks.remove(_add_custom_flag)
                    if _consume_custom_flag in cli_hooks._parsed_args_hooks:
                        cli_hooks._parsed_args_hooks.remove(_consume_custom_flag)


class MultiAccountUnitTests(unittest.TestCase):
    def _isolated_environ(self, tmp_path: Path) -> dict[str, str]:
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        return _clean_env(home=home, ext_home=tmp_path / "ext-home")

    def test_non_destructive_legacy_bootstrap_and_crud(self) -> None:
        secret_key = "0123456789abcdef0123456789abcdef"
        secret_token = "KGAT_0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_patch = self._isolated_environ(tmp_path)
            home = Path(env_patch["HOME"])
            dot_kaggle = home / ".kaggle"
            dot_kaggle.mkdir(parents=True)
            (dot_kaggle / "kaggle.json").write_text(
                json.dumps({"username": "legacy_user", "key": secret_key, "competition": "titanic"}),
                encoding="utf-8",
            )
            (dot_kaggle / "access_token").write_text(secret_token + "\n", encoding="utf-8")

            with mock.patch.dict(os.environ, env_patch, clear=False):
                accounts.clear_startup_override(reset_initial_env=True)
                try:
                    reg = accounts.load_registry(bootstrap=True)
                    self.assertEqual(reg["active"], "default")
                    self.assertIn("default", reg["accounts"])
                    self.assertEqual(reg["accounts"]["default"]["username"], "legacy_user")
                    # Original ~/.kaggle files remain untouched.
                    self.assertTrue((dot_kaggle / "kaggle.json").is_file())
                    self.assertTrue((dot_kaggle / "access_token").is_file())

                    # Lookup by profile name or @username works.
                    self.assertEqual(accounts.lookup_account("@LEGACY_USER"), "default")
                    accounts.ensure_account("work", username="work_user", auth_method="oauth")
                    resolved, _ = accounts.set_active_account("work")
                    self.assertEqual(resolved, "work")
                    self.assertEqual(accounts.current_account(), "work")

                    # Rename & guarded remove.
                    accounts.rename_account("work", "team")
                    self.assertEqual(accounts.current_account(), "team")
                    accounts.record_account_login("team", username="work_user", auth_method="legacy_api_key")
                    accounts._atomic_write_json(
                        accounts.kaggle_json_path("team"),
                        {"username": "work_user", "key": secret_key, "proxy": "http://127.0.0.1:8080"},
                    )
                    with self.assertRaises(accounts.AccountError):
                        accounts.remove_account("team", force=False)

                    # logout clears secrets but preserves non-secret config.
                    removed = accounts.clear_account_credentials("team")
                    self.assertTrue(removed)
                    saved_kj = json.loads(Path(accounts.kaggle_json_path("team")).read_text(encoding="utf-8"))
                    self.assertEqual(saved_kj, {"proxy": "http://127.0.0.1:8080"})

                    # Now unguarded remove succeeds and falls back active -> default.
                    accounts.remove_account("team", force=False)
                    self.assertEqual(accounts.current_account(), "default")

                    # Secret redaction in `account list --json`.
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        account_commands._cmd_list(json_output=True)
                    out = buf.getvalue()
                    self.assertNotIn(secret_key, out)
                    self.assertNotIn(secret_token, out)
                    payload = json.loads(out)
                    self.assertEqual(payload["active"], "default")
                finally:
                    accounts.clear_startup_override(reset_initial_env=True)

    def test_extract_account_from_argv_leading_trailing_and_equals(self) -> None:
        self.assertEqual(
            accounts.extract_account_from_argv(["kaggle", "-a", "alt", "quota"]),
            ("alt", ["kaggle", "quota"], False),
        )
        self.assertEqual(
            accounts.extract_account_from_argv(["kaggle", "quota", "-a", "alt"]),
            ("alt", ["kaggle", "quota"], False),
        )
        self.assertEqual(
            accounts.extract_account_from_argv(["kaggle", "competitions", "list", "--account=work"]),
            ("work", ["kaggle", "competitions", "list"], False),
        )
        self.assertEqual(
            accounts.extract_account_from_argv(["kaggle", "search", "--", "-a"]),
            (None, ["kaggle", "search", "--", "-a"], False),
        )
        self.assertEqual(
            accounts.extract_account_from_argv(["kaggle", "-a"]),
            (None, ["kaggle", "-a"], True),
        )

    def test_token_and_kaggle_json_login_helpers(self) -> None:
        tok = "KGAT_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_patch = self._isolated_environ(tmp_path)
            src_kj = tmp_path / "dl-kaggle.json"
            src_kj.write_text(json.dumps({"username": "alt_user", "key": key}), encoding="utf-8")

            with mock.patch.dict(os.environ, env_patch, clear=False):
                accounts.clear_startup_override(reset_initial_env=True)
                try:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        account_commands._cmd_login(name="tokacct", token=tok, no_verify=True, use=False)
                        account_commands._cmd_login(name="alt", kaggle_json=str(src_kj), use=True)
                    self.assertEqual(accounts.current_account(), "alt")
                    self.assertEqual(
                        Path(accounts.access_token_path("tokacct")).read_text(encoding="utf-8").strip(),
                        tok,
                    )
                    self.assertEqual(accounts.inspect_account_offline("alt")["username"], "alt_user")
                finally:
                    accounts.clear_startup_override(reset_initial_env=True)

    @unittest.skipIf(
        importlib.util.find_spec("kagglesdk") is None or importlib.util.find_spec("kaggle") is None,
        "kaggle/kagglesdk not installed in this Python interpreter",
    )
    def test_four_credential_paths_and_headless_pkce_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_patch = self._isolated_environ(tmp_path)
            home = Path(env_patch["HOME"])
            dot_kaggle = home / ".kaggle"
            dot_kaggle.mkdir(parents=True)
            # Plant host ~/.kaggle credentials that must NEVER leak into `alt`.
            (dot_kaggle / "kaggle.json").write_text(
                json.dumps({"username": "host_leak", "key": "11111111111111111111111111111111"}),
                encoding="utf-8",
            )
            (dot_kaggle / "access_token").write_text(
                "KGAT_11111111111111111111111111111111\n", encoding="utf-8"
            )

            with mock.patch.dict(os.environ, env_patch, clear=False):
                accounts.clear_startup_override(reset_initial_env=True)
                try:
                    from kagglesdk.kaggle_creds import KaggleCredentials
                    from kagglesdk.kaggle_env import get_access_token_from_env
                    from kagglesdk.kaggle_http_client import KaggleHttpClient
                    import kagglesdk.kaggle_http_client as http_client_mod
                    from kaggle.api.kaggle_api_extended import KaggleApi

                    api_mod = importlib.import_module("kaggle.api.kaggle_api_extended")
                    cli_hooks.patch_api_module(api_mod, force=True)
                    accounts.ensure_account("alt")

                    # 1. Start two-step headless --url-only for `alt`.
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                        account_commands._cmd_login(name="alt", url_only=True)
                    auth_url = buf.getvalue().strip()
                    self.assertIn("/api/v1/oauth2/authorize", auth_url)
                    pending = json.loads(Path(accounts.oauth_pending_path("alt")).read_text(encoding="utf-8"))

                    # Verify host ~/.kaggle did not leak into `alt` during account_context.
                    with accounts.account_context("alt", source="test", ignore_env_creds=True):
                        api = KaggleApi()
                        self.assertEqual(api.config_dir, accounts.account_dir("alt"))
                        self.assertEqual(api.config, accounts.kaggle_json_path("alt"))
                        self.assertEqual(get_access_token_from_env(), (None, None))
                        self.assertIsNone(http_client_mod._get_apikey_creds())

                        # Verify HTTP client does not pick up host_leak.
                        hc = KaggleHttpClient()
                        hc._try_fill_auth()
                        self.assertFalse(hc._signed_in)

                    # 2. Finish --code login with mocked OAuth token exchange + introspect.
                    fake_exchange = types.SimpleNamespace(
                        refreshToken="KGRT_fake_refresh_alt",
                        accessToken="fake_access_alt",
                        expires_in=3600,
                        username="alt_oauth_user",
                    )
                    fake_introspect = types.SimpleNamespace(active=True, username="alt_oauth_user")
                    with mock.patch(
                        "kagglesdk.security.services.oauth_service.OAuthClient.exchange_oauth_token",
                        return_value=fake_exchange,
                    ) as m_ex, mock.patch(
                        "kagglesdk.security.services.oauth_service.OAuthClient.introspect_token",
                        return_value=fake_introspect,
                    ):
                        cb_url = f"https://www.kaggle.com/account/api/oauth/token?code=MYCODE123&state={pending['state']}"
                        with contextlib.redirect_stdout(io.StringIO()):
                            account_commands._cmd_login(name="alt", code=cb_url)
                        req_sent = m_ex.call_args[0][0]
                        self.assertEqual(req_sent.code, "MYCODE123")
                        self.assertEqual(req_sent.code_verifier, pending["code_verifier"])

                    self.assertFalse(Path(accounts.oauth_pending_path("alt")).exists())
                    self.assertTrue(Path(accounts.credentials_path("alt")).is_file())
                    self.assertFalse((dot_kaggle / "credentials.json").exists())

                    with accounts.account_context("alt", source="test", ignore_env_creds=True):
                        with KaggleApi().build_kaggle_client() as client:
                            loaded = KaggleCredentials.load(client=client)
                            self.assertIsNotNone(loaded)
                            self.assertEqual(loaded.get_username(), "alt_oauth_user")

                            # Verify upstream `- timedelta(minutes=30)` expiration bug is patched.
                            from datetime import datetime, timedelta, timezone

                            loaded._access_token_expiration = datetime.now(timezone.utc) - timedelta(minutes=10)
                            self.assertTrue(loaded.access_token_has_expired())

                    # 3. Verify `kaggle account status` authenticates inside account_context even though
                    #    "account" is an offline extension command in sys.argv!
                    seen_auth_headers: list[str] = []

                    def _fake_get_quota(client_self, req):
                        hc = client_self._client
                        hc._init_session()
                        seen_auth_headers.append(
                            f"Bearer {getattr(hc._session.auth, 'token', '')}"
                        )
                        return types.SimpleNamespace(
                            gpu_quota=types.SimpleNamespace(
                                time_used=timedelta(seconds=36),
                                total_time_allowed=timedelta(hours=30),
                            ),
                            tpu_quota=types.SimpleNamespace(
                                time_used=timedelta(hours=0),
                                total_time_allowed=timedelta(hours=20),
                            ),
                        )

                    old_argv = sys.argv
                    try:
                        sys.argv = ["kaggle", "account", "status"]
                        status_buf = io.StringIO()
                        text_buf = io.StringIO()
                        with mock.patch(
                            "kagglesdk.security.services.oauth_service.OAuthClient.introspect_token",
                            return_value=fake_introspect,
                        ), mock.patch(
                            "kagglesdk.kernels.services.kernels_api_service.KernelsApiClient.get_accelerator_quota_statistics",
                            autospec=True,
                            side_effect=_fake_get_quota,
                        ):
                            with contextlib.redirect_stdout(status_buf):
                                account_commands._cmd_status(json_output=True)
                            with contextlib.redirect_stdout(text_buf):
                                account_commands._cmd_status(json_output=False)
                        rows = {r["account"]: r for r in json.loads(status_buf.getvalue())}
                        self.assertTrue(rows["alt"]["authenticated"])
                        self.assertIsNone(rows["alt"]["error"])
                        self.assertEqual(rows["alt"]["gpu_quota"]["remaining_hours"], 29.99)
                        self.assertEqual(rows["alt"]["tpu_quota"]["remaining_hours"], 20.0)
                        self.assertIn("GPU: 29.99/30.00h", text_buf.getvalue())
                        self.assertIn("Bearer fake_access_alt", seen_auth_headers)
                    finally:
                        sys.argv = old_argv
                finally:
                    accounts.clear_startup_override(reset_initial_env=True)


class SkillDocTests(unittest.TestCase):
    """Unit tests for bundled skill assets, Markdown merging, and topic resolution."""

    def test_bundled_skill_and_all_upstream_references_load(self) -> None:
        base_md = skill_doc.load_base_skill_markdown()
        self.assertIn("name: kaggle-cli", base_md)
        self.assertNotIn(skill_doc.SKILL_MARKER, base_md)

        upstream_topics = skill_doc.list_topics(include_extension=False)
        self.assertEqual(len(upstream_topics), 13)
        all_topics = skill_doc.list_topics(include_extension=True)
        self.assertEqual(len(all_topics), 16)

        for entry in upstream_topics:
            canonical, raw_md = skill_doc.get_topic_markdown(str(entry["topic"]), raw=True)
            self.assertEqual(canonical, entry["topic"])
            self.assertTrue(raw_md.strip())
            self.assertNotIn(skill_doc.TOPIC_EXTENSION_MARKER, raw_md)

    def test_augment_skill_markdown_frontmatter_variants_and_idempotency(self) -> None:
        base_md = skill_doc.load_base_skill_markdown()
        augmented = skill_doc.augment_skill_markdown(base_md)
        self.assertIn(skill_doc.SKILL_MARKER, augmented)
        self.assertIn("kaggle-ext", augmented.split("\n---\n", 1)[0])
        self.assertIn("├── account | acct", augmented)
        self.assertIn("├── session | sess", augmented)
        self.assertIn("references/account.md", augmented)
        self.assertIn("references/session.md", augmented)
        self.assertIn("references/ext.md", augmented)

        # Idempotent on second call.
        self.assertEqual(skill_doc.augment_skill_markdown(augmented), augmented)

        # Single-line quoted YAML frontmatter `description: "..."` variant.
        inline_md = (
            '---\nname: kaggle-cli\ndescription: "Upstream Kaggle skill."\n---\n\n'
            "# Kaggle CLI\n\n## Command Tree\n\n```text\nkaggle\n└── search\n```\n\n"
            "## Reference Map\n\n- [Search](references/search.md)\n"
        )
        enriched_inline = skill_doc.augment_skill_markdown(inline_md)
        fm_header = enriched_inline.split("\n---\n", 1)[0]
        self.assertIn('description: "Upstream Kaggle skill. With kaggle-ext installed', fm_header)
        self.assertIn(skill_doc.SKILL_MARKER, enriched_inline)
        self.assertEqual(skill_doc.augment_skill_markdown(enriched_inline), enriched_inline)

    def test_topic_resolution_aliases_paths_and_errors(self) -> None:
        expected = {
            "competitions": "competitions",
            "c": "competitions",
            "competitions.md": "competitions",
            "references/competitions.md": "competitions",
            "./references/kernels.md": "kernels",
            "k": "kernels",
            "config": "configuration",
            "model-variations-versions": "model_variations_versions",
            "acct": "account",
            "references/account.md": "account",
            "sess": "session",
            "references/session.md": "session",
            "ext": "ext",
        }
        for query, want in expected.items():
            self.assertEqual(skill_doc.resolve_topic(query), want, query)

        self.assertIsNone(skill_doc.resolve_topic("definitely_unknown_topic"))

        canon, kernels_md = skill_doc.get_topic_markdown("references/kernels.md", raw=False)
        self.assertEqual(canon, "kernels")
        self.assertIn(skill_doc.TOPIC_EXTENSION_MARKER, kernels_md)
        self.assertIn("TpuV5E8", kernels_md)

        canon_acct, acct_md = skill_doc.get_topic_markdown("acct", raw=False)
        self.assertEqual(canon_acct, "account")
        self.assertIn("--url-only", acct_md)

        canon_sess, sess_md = skill_doc.get_topic_markdown("sess", raw=False)
        self.assertEqual(canon_sess, "session")
        self.assertIn("QUICK_SAVE", sess_md)

        with self.assertRaises(KeyError):
            skill_doc.get_topic_markdown("nonexistent_topic")

        with self.assertRaises(ValueError):
            skill_doc.get_topic_markdown("account", raw=True)

        with self.assertRaises(ValueError):
            skill_doc.get_topic_markdown("session", raw=True)

        with self.assertRaises(ValueError):
            skill_doc.get_topic_markdown("ext", raw=True)

    def test_export_skill_bundle_and_inject_copies_skill_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_full = tmp_path / "export-full"
            rep_full = skill_doc.export_skill_bundle(str(bundle_full), raw=False)
            self.assertEqual(rep_full["file_count"], 17)  # SKILL.md + 16 references
            self.assertIn(skill_doc.SKILL_MARKER, (bundle_full / "SKILL.md").read_text(encoding="utf-8"))
            self.assertTrue((bundle_full / "references" / "kernels.md").is_file())
            self.assertTrue((bundle_full / "references" / "account.md").is_file())
            self.assertTrue((bundle_full / "references" / "session.md").is_file())
            self.assertTrue((bundle_full / "references" / "ext.md").is_file())

            bundle_raw = tmp_path / "export-raw"
            rep_raw = skill_doc.export_skill_bundle(str(bundle_raw), raw=True)
            self.assertEqual(rep_raw["file_count"], 14)  # SKILL.md + 13 upstream references
            self.assertNotIn(skill_doc.SKILL_MARKER, (bundle_raw / "SKILL.md").read_text(encoding="utf-8"))
            self.assertFalse((bundle_raw / "references" / "account.md").exists())

            # Verify inject.ensure(..., copy_source=True) copies `kaggle_ext/skill_data/`.
            ext_home = tmp_path / "copied-ext-home"
            site_dir = tmp_path / "site"
            site_dir.mkdir()
            with mock.patch.dict(
                os.environ,
                {"KAGGLE_EXT_HOME": str(ext_home), "KAGGLE_EXT_SOURCE": str(REPO_ROOT)},
            ):
                res = inject.ensure(site_dir=str(site_dir), copy_source=True, link=False, quiet=True)
                self.assertIsNone(res.get("error"))
            self.assertTrue((ext_home / "kaggle_ext" / "skill_data" / "SKILL.md").is_file())
            self.assertTrue(
                (ext_home / "kaggle_ext" / "skill_data" / "references" / "kernels.md").is_file()
            )

    def test_future_upstream_skill_subparser_is_wrapped_idempotently(self) -> None:
        parser = argparse.ArgumentParser(prog="kaggle")
        subparsers = parser.add_subparsers(dest="command")
        upstream_skill = subparsers.add_parser("skill")
        upstream_skill.set_defaults(
            func=lambda: sys.stdout.write(skill_doc.load_base_skill_markdown())
        )

        skill_commands.attach_skill_subparser(subparsers)
        skill_commands.attach_skill_subparser(subparsers)  # idempotent second attach

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upstream_skill.get_default("func")()
        out = buf.getvalue()
        self.assertIn("name: kaggle-cli", out)
        self.assertIn(skill_doc.SKILL_MARKER, out)


class RealKaggleCliIntegrationTests(unittest.TestCase):
    """End-to-end hermetic subprocess tests against the installed Kaggle CLI."""

    REAL_PY = _real_kaggle_python()

    @unittest.skipIf(REAL_PY is None, "kaggle package not installed in any detected Python environment")
    def test_pth_meta_path_integration_with_real_kaggle_cli(self) -> None:
        assert self.REAL_PY is not None
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            ext_home = tmp_path / "ext-home"
            site_dir = tmp_path / "site"
            site_dir.mkdir()
            boot_dir = tmp_path / "boot"
            boot_dir.mkdir()
            # uv tool venvs disable user site; sitecustomize.py exercises site.addsitedir(.pth) hermetically.
            (boot_dir / "sitecustomize.py").write_text(
                f"import site\nsite.addsitedir({str(site_dir)!r})\n",
                encoding="utf-8",
            )

            env = _clean_env(
                home=home,
                ext_home=ext_home,
                extra={"PYTHONPATH": str(boot_dir)},
            )

            p_inst = subprocess.run(
                [self.REAL_PY, str(REPO_ROOT / "install.py"), "--site-dir", str(site_dir)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_inst.returncode, 0, p_inst.stderr)
            self.assertTrue((site_dir / inject.PTH_NAME).is_file())

            # 1. Non-kaggle Python invocation must NOT eagerly import kaggle.
            p_lazy = subprocess.run(
                [
                    self.REAL_PY,
                    "-c",
                    "import sys; assert 'kaggle' not in sys.modules; "
                    "assert any(getattr(f, '_kaggle_ext_finder', False) for f in sys.meta_path)",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(p_lazy.returncode, 0, p_lazy.stderr)

            # 2. Unauthenticated `python -m kaggle ext doctor` and `--json` succeed without auth errors.
            p_doc_txt = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "ext", "doctor"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_doc_txt.returncode, 0, p_doc_txt.stderr)
            self.assertNotIn("Authentication required", p_doc_txt.stdout)
            self.assertNotIn("Authentication required", p_doc_txt.stderr)
            self.assertIn(f"kaggle-ext v{kaggle_ext.__version__}", p_doc_txt.stdout)
            self.assertIn("registered here : yes", p_doc_txt.stdout)

            p_doc = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "ext", "doctor", "--json"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_doc.returncode, 0, p_doc.stderr)
            self.assertNotIn("Authentication required", p_doc.stdout)
            self.assertNotIn("Authentication required", p_doc.stderr)
            doc = json.loads(p_doc.stdout)
            self.assertTrue(doc["registered"])
            self.assertEqual(doc["extension"], kaggle_ext.__version__)
            self.assertEqual(doc["kaggle_auth"]["effective_auth_method"], "none")
            self.assertTrue(doc["kaggle_cli_version"])

            # 3. `python -m kaggle ext version` returns valid JSON.
            p_ver = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "ext", "version"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_ver.returncode, 0, p_ver.stderr)
            ver = json.loads(p_ver.stdout)
            self.assertEqual(ver["extension"], kaggle_ext.__version__)

            # 4. `python -m kaggle --help` displays `ext`, `account`, `skill`, and `-a, --account`.
            p_help = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "--help"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_help.returncode, 0, p_help.stderr)
            self.assertIn("ext", p_help.stdout)
            self.assertIn("account", p_help.stdout)
            self.assertIn("session", p_help.stdout)
            self.assertIn("skill", p_help.stdout)
            self.assertIn("--account", p_help.stdout)

            # 4b. Unauthenticated `kaggle skill` / `SKILL` / `--list` / topic / `--all` / `--export`.
            p_skill = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill.returncode, 0, p_skill.stderr)
            self.assertNotIn("Authentication required", p_skill.stdout + p_skill.stderr)
            self.assertIn("name: kaggle-cli", p_skill.stdout)
            self.assertIn(skill_doc.SKILL_MARKER, p_skill.stdout)

            p_skill_raw = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "SKILL", "--raw"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill_raw.returncode, 0, p_skill_raw.stderr)
            self.assertIn("name: kaggle-cli", p_skill_raw.stdout)
            self.assertNotIn(skill_doc.SKILL_MARKER, p_skill_raw.stdout)

            p_skill_list = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill", "--list", "--json"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill_list.returncode, 0, p_skill_list.stderr)
            topic_names = {item["topic"] for item in json.loads(p_skill_list.stdout)}
            self.assertTrue({"kernels", "competitions", "account", "session", "ext"}.issubset(topic_names))

            p_skill_kernels = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill", "references/kernels.md"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill_kernels.returncode, 0, p_skill_kernels.stderr)
            self.assertIn("# Kernels CLI Reference", p_skill_kernels.stdout)
            self.assertIn(skill_doc.TOPIC_EXTENSION_MARKER, p_skill_kernels.stdout)

            p_skill_acct = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill", "account"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill_acct.returncode, 0, p_skill_acct.stderr)
            self.assertIn("# Multi-Account Management CLI Reference", p_skill_acct.stdout)

            p_skill_all = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill", "--all"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill_all.returncode, 0, p_skill_all.stderr)
            self.assertIn(skill_doc.SKILL_MARKER, p_skill_all.stdout)
            self.assertIn("<!-- reference: references/competitions.md -->", p_skill_all.stdout)
            self.assertIn("<!-- reference: references/account.md -->", p_skill_all.stdout)

            export_dest = tmp_path / "exported-skill"
            p_skill_export = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill", "--export", str(export_dest)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_skill_export.returncode, 0, p_skill_export.stderr)
            self.assertTrue((export_dest / "SKILL.md").is_file())
            self.assertTrue((export_dest / "references" / "kernels.md").is_file())
            self.assertTrue((export_dest / "references" / "account.md").is_file())

            p_skill_bad = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "skill", "no_such_topic"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(p_skill_bad.returncode, 0)
            self.assertIn("[skill] Unknown topic", p_skill_bad.stderr)

            # 5. Native unauthenticated `python -m kaggle config view` preserves upstream exit(1) auth failure.
            p_cfg = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "config", "view"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(p_cfg.returncode, 0)
            self.assertIn("Authentication required", p_cfg.stdout + p_cfg.stderr)

            # 6. `python -m kaggle account login --help` exposes all login options cleanly.
            p_login_help = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "account", "login", "--help"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_login_help.returncode, 0, p_login_help.stderr)
            for opt in ("--no-launch-browser", "--url-only", "--code", "--token", "--kaggle-json"):
                self.assertIn(opt, p_login_help.stdout)

            # 7. Import two isolated accounts and verify `-a` before & after subcommands routes cleanly.
            kj_main = tmp_path / "main-kaggle.json"
            kj_alt = tmp_path / "alt-kaggle.json"
            kj_main.write_text(
                json.dumps(
                    {
                        "username": "user_main",
                        "key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "competition": "comp-main",
                    }
                ),
                encoding="utf-8",
            )
            kj_alt.write_text(
                json.dumps(
                    {
                        "username": "user_alt",
                        "key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "competition": "comp-alt",
                    }
                ),
                encoding="utf-8",
            )
            p_imp_main = subprocess.run(
                [
                    self.REAL_PY,
                    "-m",
                    "kaggle",
                    "account",
                    "login",
                    "main_acct",
                    "--kaggle-json",
                    str(kj_main),
                    "--use",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_imp_main.returncode, 0, p_imp_main.stderr)

            p_imp_alt = subprocess.run(
                [
                    self.REAL_PY,
                    "-m",
                    "kaggle",
                    "account",
                    "login",
                    "alt",
                    "--kaggle-json",
                    str(kj_alt),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_imp_alt.returncode, 0, p_imp_alt.stderr)

            p_list = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "account", "list", "--json"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_list.returncode, 0, p_list.stderr)
            list_data = json.loads(p_list.stdout)
            self.assertEqual(list_data["active"], "main_acct")
            names = [row["account"] for row in list_data["accounts"]]
            self.assertIn("main_acct", names)
            self.assertIn("alt", names)

            # Active account (`main_acct`) vs leading `-a alt` vs trailing `-a alt`.
            p_view_main = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "config", "view"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_view_main.returncode, 0, p_view_main.stderr)
            self.assertIn("username: user_main", p_view_main.stdout)
            self.assertIn("competition: comp-main", p_view_main.stdout)

            for argv in (
                [self.REAL_PY, "-m", "kaggle", "-a", "alt", "config", "view"],
                [self.REAL_PY, "-m", "kaggle", "config", "view", "-a", "alt"],
            ):
                p_view_alt = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(p_view_alt.returncode, 0, p_view_alt.stderr)
                self.assertIn("username: user_alt", p_view_alt.stdout)
                self.assertIn("competition: comp-alt", p_view_alt.stdout)

            # Unknown account fails cleanly with `[account]` error and NO misleading auth prompt.
            p_bad = subprocess.run(
                [self.REAL_PY, "-m", "kaggle", "-a", "nonexistent", "config", "view"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(p_bad.returncode, 0)
            self.assertIn("[account] Unknown account", p_bad.stderr)
            self.assertNotIn("Authentication required", p_bad.stdout + p_bad.stderr)


# --------------------------------------------------------------------------------------
# Phase 4: Interactive Session & Loopback HTTP/WebSocket Harness Tests
# --------------------------------------------------------------------------------------


def _ws_encode_server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header.extend(n.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(n.to_bytes(8, "big"))
    return bytes(header) + payload


def _ws_read_client_frame(rfile) -> tuple[int, bytes] | None:
    head = rfile.read(2)
    if not head or len(head) < 2:
        return None
    b0, b1 = head[0], head[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = int.from_bytes(rfile.read(2), "big")
    elif length == 127:
        length = int.from_bytes(rfile.read(8), "big")
    mask_key = rfile.read(4) if masked else b""
    payload = rfile.read(length) if length else b""
    if masked and payload:
        repeated = (mask_key * ((length // 4) + 1))[:length]
        payload = (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(
            length, "big"
        )
    return opcode, payload


class _LoopbackClusterState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proxy_port = 0
        self.proxy_secret = "secretseg1234567890"
        self.query_secret = "querysecret999"
        self.next_ksid = 370558104
        self.kernels: dict[str, dict[str, object]] = {}
        self.save_calls: list[dict[str, object]] = []
        self.create_session_calls: list[dict[str, object]] = []
        self.cancel_calls: list[int] = []
        self.delete_calls: int = 0
        self.fail_cancel_code: int | None = None
        self.ops: dict[str, dict[str, object]] = {}
        self.live_sessions: dict[int, dict[str, object]] = {}
        self.ws_clients: dict[int, dict[str, tuple[socket.socket, threading.Lock]]] = {}
        self.ws_connect_history: list[tuple[int, str]] = []
        self.ws_connected_event = threading.Event()
        self.session_id_kicks: int = 0
        self.restarts: int = 0
        self.interrupts: int = 0
        self.interrupt_event = threading.Event()

    def register_ws(self, ksid: int, sid: str, sock: socket.socket, wlock: threading.Lock) -> None:
        with self.lock:
            bucket = self.ws_clients.setdefault(ksid, {})
            old = bucket.get(sid)
            if old is not None:
                self.session_id_kicks += 1
                with contextlib.suppress(OSError):
                    old[0].close()
            bucket[sid] = (sock, wlock)
            self.ws_connect_history.append((ksid, sid))
            self.ws_connected_event.set()

    def unregister_ws(self, ksid: int, sid: str, sock: socket.socket) -> None:
        with self.lock:
            bucket = self.ws_clients.get(ksid, {})
            cur = bucket.get(sid)
            if cur is not None and cur[0] is sock:
                bucket.pop(sid, None)

    def broadcast_iopub(self, ksid: int, msg_dict: dict[str, object]) -> None:
        frame = _ws_encode_server_frame(0x1, json.dumps(msg_dict).encode("utf-8"))
        with self.lock:
            targets = list(self.ws_clients.get(ksid, {}).values())
        for sock, wlock in targets:
            with wlock:
                with contextlib.suppress(OSError):
                    sock.sendall(frame)

    def close_all_ws(self) -> None:
        with self.lock:
            socks = [s for b in self.ws_clients.values() for s, _lk in b.values()]
            self.ws_clients.clear()
        for sock in socks:
            with contextlib.suppress(OSError):
                sock.close()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _LoopbackKaggleCluster:
    """Self-contained loopback Kaggle Proto-RPC + Jupyter Proxy HTTP/WebSocket server."""

    def __init__(self) -> None:
        self.state = _LoopbackClusterState()
        self._rpc_server: _ThreadingHTTPServer | None = None
        self._proxy_server: _ThreadingHTTPServer | None = None
        self._rpc_thread: threading.Thread | None = None
        self._proxy_thread: threading.Thread | None = None
        self.rpc_url = ""
        self.proxy_port = 0

    def __enter__(self) -> "_LoopbackKaggleCluster":
        state = self.state

        class ProxyHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, obj: object) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _parse_proxy_route(self) -> tuple[int, str, dict[str, list[str]]] | None:
                parsed = urllib.parse.urlsplit(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if (
                    len(parts) < 4
                    or parts[0] != "k"
                    or not parts[1].isdigit()
                    or parts[2] != state.proxy_secret
                    or parts[3] != "proxy"
                ):
                    self._send_json(404, {"error": "not found"})
                    return None
                ksid = int(parts[1])
                with state.lock:
                    sess = state.live_sessions.get(ksid)
                if sess is None or not sess.get("alive"):
                    self._send_json(404, {"error": "session gone"})
                    return None
                sub = "/" + "/".join(parts[4:])
                qs = urllib.parse.parse_qs(parsed.query)
                return ksid, sub, qs

            def do_GET(self) -> None:
                route = self._parse_proxy_route()
                if route is None:
                    return
                ksid, sub, qs = route
                with state.lock:
                    sess = state.live_sessions[ksid]
                    kid = str(sess["kernel_id"])
                if sub == "/api/status":
                    self._send_json(200, {"connections": 0, "kernels": 1})
                    return
                if sub == "/api/kernels":
                    self._send_json(
                        200,
                        [{"id": kid, "name": "python3", "execution_state": "idle", "connections": 0}],
                    )
                    return
                if sub == f"/api/kernels/{kid}":
                    self._send_json(
                        200,
                        {"id": kid, "name": "python3", "execution_state": "idle", "connections": 0},
                    )
                    return
                if sub == f"/api/kernels/{kid}/channels":
                    if (self.headers.get("Upgrade") or "").lower() != "websocket":
                        self._send_json(400, {"error": "upgrade required"})
                        return
                    ws_key = self.headers.get("Sec-WebSocket-Key") or ""
                    accept = base64.b64encode(
                        hashlib.sha1((ws_key + session_ws._WS_GUID).encode("ascii")).digest()
                    ).decode("ascii")
                    self.send_response(101, "Switching Protocols")
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.wfile.flush()
                    self.close_connection = True
                    sid = (qs.get("session_id") or [f"anon-{time.time()}"])[0]
                    self._serve_ws(ksid, sid, sess)
                    return
                self._send_json(404, {"error": "unknown route"})

            def do_POST(self) -> None:
                route = self._parse_proxy_route()
                if route is None:
                    return
                ksid, sub, _qs = route
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    _ = self.rfile.read(length)
                with state.lock:
                    sess = state.live_sessions[ksid]
                    kid = str(sess["kernel_id"])
                if sub == "/api/kernels":
                    self._send_json(
                        201,
                        {"id": kid, "name": "python3", "execution_state": "idle", "connections": 0},
                    )
                    return
                if sub == f"/api/kernels/{kid}/restart":
                    with state.lock:
                        state.restarts += 1
                        sess["env"].clear()
                    self._send_json(
                        200,
                        {"id": kid, "name": "python3", "execution_state": "idle", "connections": 0},
                    )
                    return
                if sub == f"/api/kernels/{kid}/interrupt":
                    with state.lock:
                        state.interrupts += 1
                        state.interrupt_event.set()
                    self._send_json(200, {})
                    return
                self._send_json(404, {"error": "unknown POST route"})

            def _serve_ws(self, ksid: int, sid: str, sess: dict[str, object]) -> None:
                wlock = threading.Lock()
                sock = self.connection
                state.register_ws(ksid, sid, sock, wlock)
                try:
                    while True:
                        frame = _ws_read_client_frame(self.rfile)
                        if frame is None:
                            break
                        opcode, payload = frame
                        if opcode == 0x8:
                            with wlock:
                                with contextlib.suppress(OSError):
                                    sock.sendall(_ws_encode_server_frame(0x8, (1000).to_bytes(2, "big")))
                            break
                        if opcode == 0x9:
                            with wlock:
                                with contextlib.suppress(OSError):
                                    sock.sendall(_ws_encode_server_frame(0xA, payload[:125]))
                            continue
                        if opcode != 0x1:
                            continue
                        msg = json.loads(payload.decode("utf-8"))
                        if msg.get("msg_type") != "execute_request":
                            continue
                        self._handle_execute_request(ksid, sock, wlock, sess, msg)
                except (OSError, ValueError):
                    pass
                finally:
                    state.unregister_ws(ksid, sid, sock)

            def _handle_execute_request(
                self,
                ksid: int,
                sock: socket.socket,
                wlock: threading.Lock,
                sess: dict[str, object],
                req: dict[str, object],
            ) -> None:
                parent_hdr = req.get("header") or {}
                parent_id = parent_hdr.get("msg_id") if isinstance(parent_hdr, dict) else ""
                content = req.get("content") or {}
                code = str(content.get("code") or "")
                silent = bool(content.get("silent", False))
                store_history = bool(content.get("store_history", True))

                state.broadcast_iopub(
                    ksid,
                    {
                        "channel": "iopub",
                        "msg_type": "status",
                        "header": {"msg_type": "status"},
                        "parent_header": {"msg_id": parent_id},
                        "content": {"execution_state": "busy"},
                    },
                )

                out_buf = io.StringIO()
                err_buf = io.StringIO()
                exec_status = "ok"
                ename = ""
                evalue = ""

                fake_os = types.SimpleNamespace(environ=sess["env"])
                fake_sys = types.SimpleNamespace(stdout=out_buf, stderr=err_buf, version=sys.version)

                def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
                    if name == "os":
                        return fake_os
                    if name == "sys":
                        return fake_sys
                    return __import__(name, globals, locals, fromlist, level)

                def _fake_print(*args, **kwargs):
                    sep = kwargs.get("sep", " ")
                    end = kwargs.get("end", "\n")
                    target = kwargs.get("file") or out_buf
                    target.write(sep.join(str(a) for a in args) + end)

                safe_builtins = dict(__builtins__) if isinstance(__builtins__, dict) else dict(vars(__builtins__))
                safe_builtins["__import__"] = _fake_import
                safe_builtins["print"] = _fake_print

                if store_history:
                    sess["In"].append(code)
                exec_ns = {"__builtins__": safe_builtins, "In": sess["In"], "os": fake_os, "sys": fake_sys}

                try:
                    if "# SLEEP_UNTIL_INTERRUPT" in code:
                        state.interrupt_event.clear()
                        if state.interrupt_event.wait(timeout=10.0):
                            raise KeyboardInterrupt("Interrupted by user")
                        raise TimeoutError("Mock sleep finished")
                    exec(compile(code, "<cell>", "exec"), exec_ns, exec_ns)
                except BaseException as exc:
                    exec_status = "error"
                    ename = type(exc).__name__
                    evalue = str(exc)

                if not silent:
                    out_txt = out_buf.getvalue()
                    if out_txt:
                        state.broadcast_iopub(
                            ksid,
                            {
                                "channel": "iopub",
                                "msg_type": "stream",
                                "header": {"msg_type": "stream"},
                                "parent_header": {"msg_id": parent_id},
                                "content": {"name": "stdout", "text": out_txt},
                            },
                        )
                    err_txt = err_buf.getvalue()
                    if err_txt:
                        state.broadcast_iopub(
                            ksid,
                            {
                                "channel": "iopub",
                                "msg_type": "stream",
                                "header": {"msg_type": "stream"},
                                "parent_header": {"msg_id": parent_id},
                                "content": {"name": "stderr", "text": err_txt},
                            },
                        )

                if exec_status == "error":
                    state.broadcast_iopub(
                        ksid,
                        {
                            "channel": "iopub",
                            "msg_type": "error",
                            "header": {"msg_type": "error"},
                            "parent_header": {"msg_id": parent_id},
                            "content": {
                                "ename": ename,
                                "evalue": evalue,
                                "traceback": [f"{ename}: {evalue}"],
                            },
                        },
                    )

                state.broadcast_iopub(
                    ksid,
                    {
                        "channel": "iopub",
                        "msg_type": "status",
                        "header": {"msg_type": "status"},
                        "parent_header": {"msg_id": parent_id},
                        "content": {"execution_state": "idle"},
                    },
                )

                reply = {
                    "channel": "shell",
                    "msg_type": "execute_reply",
                    "header": {"msg_type": "execute_reply"},
                    "parent_header": {"msg_id": parent_id},
                    "content": (
                        {"status": "ok"}
                        if exec_status == "ok"
                        else {"status": "error", "ename": ename, "evalue": evalue}
                    ),
                }
                with wlock:
                    with contextlib.suppress(OSError):
                        sock.sendall(_ws_encode_server_frame(0x1, json.dumps(reply).encode("utf-8")))

        class RpcHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, obj: object) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _caller_username(self) -> str:
                auth = self.headers.get("Authorization") or ""
                if "alt" in auth or "bbbb" in auth:
                    return "user_alt"
                return "user_main"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")

                if self.path == "/v1/security.OAuthService/IntrospectToken":
                    tok = str(data.get("token") or "")
                    uname = "user_alt" if "bbbb" in tok else "user_main"
                    self._send_json(200, {"active": True, "username": uname})
                    return

                if self.path == "/v1/users.AccountService/GenerateAccessToken":
                    rtok = str(data.get("refreshToken") or "")
                    uname = "user_alt" if "alt" in rtok else "user_main"
                    self._send_json(
                        200,
                        {
                            "token": "KGAT_00000000000000000000000000000001",
                            "expiresIn": 3600,
                            "userName": uname,
                            "userId": 101,
                        },
                    )
                    return

                if self.path == "/v1/kernels.KernelsApiService/GetKernel":
                    key = f"{data.get('userName')}/{data.get('kernelSlug')}"
                    with state.lock:
                        kinfo = state.kernels.get(key)
                    if kinfo is None:
                        self._send_json(404, {"code": 404, "message": "not found"})
                        return
                    self._send_json(
                        200,
                        {
                            "metadata": {"id": kinfo["kernelId"], "ref": f"/code/{key}", "title": kinfo["title"]},
                            "blob": {"source": kinfo["text"], "language": "python", "kernelType": "notebook"},
                        },
                    )
                    return

                if self.path == "/v1/kernels.KernelsApiService/SaveKernel":
                    slug = str(data.get("slug") or "")
                    with state.lock:
                        state.save_calls.append(data)
                        kid = 145548000 + len(state.save_calls)
                        state.kernels[slug] = {
                            "kernelId": kid,
                            "title": data.get("newTitle", ""),
                            "text": data.get("text", ""),
                        }
                    self._send_json(
                        200,
                        {
                            "ref": f"/code/{slug}",
                            "url": f"https://www.kaggle.com/code/{slug}",
                            "versionNumber": 1,
                            "kernelId": kid,
                        },
                    )
                    return

                if self.path == "/v1/kernels.KernelsApiService/CreateKernelSession":
                    with state.lock:
                        state.create_session_calls.append(data)
                        ksid = state.next_ksid
                        state.next_ksid += 1
                        op_name = f"operations/create-kernel-session/{ksid}"
                        state.ops[op_name] = {"ksid": ksid, "polls": 0}
                        state.live_sessions[ksid] = {
                            "alive": True,
                            "kernel_id": f"kid-{ksid}",
                            "env": {},
                            "In": [""],
                        }
                    self._send_json(
                        200,
                        {
                            "name": op_name,
                            "done": False,
                            "metadata": {"kernelSessionId": ksid},
                        },
                    )
                    return

                if self.path == "/v1/common.OperationsService/GetOperation":
                    op_name = str(data.get("name") or "")
                    with state.lock:
                        entry = state.ops.get(op_name)
                        if entry is None:
                            self._send_json(404, {"code": 404, "message": "operation not found"})
                            return
                        entry["polls"] = int(entry["polls"]) + 1
                        polls = int(entry["polls"])
                        ksid = int(entry["ksid"])
                    if polls < 2:
                        # Mirror real Kaggle behavior: metadata becomes null on subsequent GetOperation polls!
                        self._send_json(200, {"name": op_name, "done": False, "metadata": None})
                        return
                    self._send_json(
                        200,
                        {
                            "name": op_name,
                            "done": True,
                            "metadata": None,
                            "response": {
                                "jupyterUrl": f"http://127.0.0.1:{state.proxy_port}/k/{ksid}/{state.proxy_secret}/proxy",
                                "tokenizedJupyterUrl": f"http://127.0.0.1:{state.proxy_port}/?token={state.query_secret}",
                            },
                        },
                    )
                    return

                if self.path == "/v1/kernels.KernelsApiService/GetKernelSessionStatus":
                    # Mirror real Kaggle behavior: returns COMPLETE even while interactive session is alive!
                    self._send_json(200, {"status": "COMPLETE"})
                    return

                if self.path == "/v1/kernels.KernelsApiService/CancelKernelSession":
                    ksid = data.get("kernelSessionId")
                    if not isinstance(ksid, int) or isinstance(ksid, bool):
                        self._send_json(400, {"code": 400, "message": "kernelSessionId must be int"})
                        return
                    with state.lock:
                        state.cancel_calls.append(ksid)
                        if state.fail_cancel_code is not None:
                            self._send_json(
                                state.fail_cancel_code,
                                {"code": state.fail_cancel_code, "message": "transient cancel failure"},
                            )
                            return
                        if ksid in state.live_sessions:
                            state.live_sessions[ksid]["alive"] = False
                    self._send_json(200, {})
                    return

                if self.path == "/v1/kernels.KernelsApiService/DeleteKernel":
                    with state.lock:
                        state.delete_calls += 1
                    self._send_json(200, {})
                    return

                self._send_json(404, {"code": 404, "message": f"unmocked RPC {self.path}"})

        self._proxy_server = _ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        self.proxy_port = self._proxy_server.server_address[1]
        state.proxy_port = self.proxy_port
        self._proxy_thread = threading.Thread(target=self._proxy_server.serve_forever, daemon=True)
        self._proxy_thread.start()

        self._rpc_server = _ThreadingHTTPServer(("127.0.0.1", 0), RpcHandler)
        rpc_port = self._rpc_server.server_address[1]
        self.rpc_url = f"http://127.0.0.1:{rpc_port}"
        self._rpc_thread = threading.Thread(target=self._rpc_server.serve_forever, daemon=True)
        self._rpc_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.state.close_all_ws()
        if self._rpc_server is not None:
            self._rpc_server.shutdown()
            self._rpc_server.server_close()
        if self._proxy_server is not None:
            self._proxy_server.shutdown()
            self._proxy_server.server_close()


class SessionUnitTests(unittest.TestCase):
    def test_stdlib_only_core_imports_without_kaggle_packages(self) -> None:
        code = (
            "import sys; "
            "sys.meta_path = [f for f in sys.meta_path]; "
            "import kaggle_ext.inject, kaggle_ext.diagnostics, kaggle_ext.accounts, "
            "kaggle_ext.sessions, kaggle_ext.session_ws, kaggle_ext.skill_doc; "
            "assert 'kaggle' not in sys.modules and 'kagglesdk' not in sys.modules"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(home=Path(tmp) / "h", ext_home=Path(tmp) / "e", extra={"PYTHONPATH": str(REPO_ROOT)})
            proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_validation_slug_hardware_redaction_and_inputs(self) -> None:
        self.assertEqual(sessions.validate_session_name("gpu-worker_1.dev"), "gpu-worker_1.dev")
        for bad in ("", "../evil", "-leading", "a" * 49):
            with self.assertRaises(sessions.SessionError):
                sessions.validate_session_name(bad)

        self.assertEqual(sessions.normalize_kernel_ref("/code/deluxebear/my-nb"), ("deluxebear", "my-nb"))
        self.assertEqual(
            sessions.normalize_kernel_ref("https://www.kaggle.com/code/alice/demo-1"),
            ("alice", "demo-1"),
        )

        full_slug, kslug, title = sessions.placeholder_slug_and_title("alice", "Train Worker 1".replace(" ", "-"))
        self.assertEqual(full_slug, "alice/kaggle-ext-s-train-worker-1")
        self.assertEqual(kslug, "kaggle-ext-s-train-worker-1")
        self.assertEqual(title, "kaggle ext s train worker 1")

        self.assertEqual(sessions.resolve_machine_shape("cpu"), (None, "cpu", None))
        self.assertEqual(sessions.resolve_machine_shape("t4"), ("NvidiaTeslaT4", "NvidiaTeslaT4", None))
        self.assertEqual(sessions.resolve_machine_shape("tpu"), ("TpuV5E8", "TpuV5E8", None))
        shape, _lbl, warn = sessions.resolve_machine_shape("p100")
        self.assertEqual(shape, "NvidiaTeslaP100")
        self.assertIsNotNone(warn)

        raw_secret_txt = (
            "url=https://kkb-production.jupyter-proxy.kaggle.net/k/370558104/secretseg1234567890/proxy "
            "alt=https://kkb-production.jupyter-proxy.kaggle.net/?token=querysecret999 "
            "tok=KGAT_0123456789abcdef0123456789abcdef rt=KGRT_abc123 "
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig123"
        )
        redacted = sessions.redact_session_secrets(raw_secret_txt)
        for leak in ("secretseg1234567890", "querysecret999", "0123456789abcdef0123456789abcdef", "KGRT_abc123", "sig123"):
            self.assertNotIn(leak, redacted)
        self.assertIn("/k/370558104/[REDACTED]/proxy", redacted)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_f = tmp_path / ".env"
            env_f.write_text("# comment\nexport FOO='bar baz'\nQUOTED=\"hello\\nworld\"\n", encoding="utf-8")
            parsed_env = session_ws.parse_env_inputs(
                env_list=["BAR=123", "FROM_ENV"],
                env_file=str(env_f),
                environ={"FROM_ENV": "env_ok"},
            )
            self.assertEqual(parsed_env, {"FOO": "bar baz", "QUOTED": "hello\nworld", "BAR": "123", "FROM_ENV": "env_ok"})

            nb_path = tmp_path / "sample.ipynb"
            nb_path.write_text(
                json.dumps(
                    {
                        "cells": [
                            {"cell_type": "markdown", "source": "# ignore"},
                            {"cell_type": "code", "source": ["# @title Init\n", "x = 1"]},
                            {"cell_type": "code", "source": "   "},
                            {"cell_type": "code", "source": "%%bash\necho hi", "id": "c3"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            cells = session_ws.load_execution_cells(file_path=str(nb_path))
            self.assertEqual(cells, [("Init", "# @title Init\nx = 1"), ("c3", "%%bash\necho hi")])
            stdin_nb = session_ws.load_execution_cells(file_path="-", stdin_stream=io.StringIO(nb_path.read_text(encoding="utf-8")))
            self.assertEqual(len(stdin_nb), 2)

    def test_account_logout_scrubs_session_secrets_and_remove_guards_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_patch = _clean_env(home=tmp_path / "home", ext_home=tmp_path / "ext")
            with mock.patch.dict(os.environ, env_patch, clear=False):
                accounts.clear_startup_override(reset_initial_env=True)
                try:
                    accounts.ensure_account("worker_acct", username="u_worker", auth_method="access_token")
                    sessions.upsert_session(
                        "s1",
                        {
                            "username": "u_worker",
                            "slug": "u_worker/kaggle-ext-s-s1",
                            "kernel_session_id": 999111,
                            "jupyter_url": "https://kkb-production.jupyter-proxy.kaggle.net/k/999111/secretseg/proxy",
                            "tokenized_jupyter_url": "https://kkb-production.jupyter-proxy.kaggle.net/?token=qsec",
                            "proxy_token": "ptok",
                        },
                        account="worker_acct",
                    )
                    with self.assertRaises(accounts.AccountError) as ctx:
                        accounts.remove_account("worker_acct", force=False)
                    self.assertIn("session stop --all", str(ctx.exception))

                    accounts.clear_account_credentials("worker_acct")
                    _lbl, rec = sessions.get_session("s1", account="worker_acct")
                    assert rec is not None
                    self.assertEqual(rec["kernel_session_id"], 999111)
                    self.assertEqual(rec["state"], "logged_out")
                    self.assertNotIn("jupyter_url", rec)
                    self.assertNotIn("tokenized_jupyter_url", rec)
                    self.assertNotIn("proxy_token", rec)

                    accounts.remove_account("worker_acct", force=True)
                finally:
                    accounts.clear_startup_override(reset_initial_env=True)


class SessionLoopbackIntegrationTests(unittest.TestCase):
    REAL_PY = RealKaggleCliIntegrationTests.REAL_PY

    @unittest.skipIf(REAL_PY is None, "kaggle package not installed in any detected Python environment")
    def test_end_to_end_session_lifecycle_ws_exec_watch_and_redaction(self) -> None:
        assert self.REAL_PY is not None
        with tempfile.TemporaryDirectory() as tmp, _LoopbackKaggleCluster() as cluster:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            ext_home = tmp_path / "ext-home"
            site_dir = tmp_path / "site"
            site_dir.mkdir()
            boot_dir = tmp_path / "boot"
            boot_dir.mkdir()
            (boot_dir / "sitecustomize.py").write_text(
                f"import site\nsite.addsitedir({str(site_dir)!r})\n",
                encoding="utf-8",
            )
            env = _clean_env(
                home=home,
                ext_home=ext_home,
                extra={
                    "PYTHONPATH": str(boot_dir),
                    "KAGGLE_EXT_API_ENDPOINT": cluster.rpc_url,
                },
            )

            p_inst = subprocess.run(
                [self.REAL_PY, str(REPO_ROOT / "install.py"), "--site-dir", str(site_dir)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_inst.returncode, 0, p_inst.stderr)

            def run_k(args: list[str], extra_env: dict[str, str] | None = None, timeout: int = 30):
                run_env = dict(env)
                if extra_env:
                    run_env.update(extra_env)
                return subprocess.run(
                    [self.REAL_PY, "-m", "kaggle", *args],
                    env=run_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )

            # 1. Offline unauthenticated `session list` works cleanly.
            p_empty_ls = run_k(["session", "list", "--json"])
            self.assertEqual(p_empty_ls.returncode, 0, p_empty_ls.stderr)
            self.assertEqual(json.loads(p_empty_ls.stdout), [])

            # 2. Configure two isolated accounts (`main_acct` OAuth + `alt` KGAT_ token).
            accounts_cfg = home / ".config" / "kaggle-ext"
            (accounts_cfg / "accounts" / "main_acct").mkdir(parents=True)
            (accounts_cfg / "accounts" / "alt").mkdir(parents=True)
            (accounts_cfg / "accounts" / "main_acct" / "credentials.json").write_text(
                json.dumps(
                    {
                        "refresh_token": "KGRT_main_refresh",
                        "access_token": "KGAT_11111111111111111111111111111111",
                        "access_token_expiration": "2000-01-01T00:00:00+00:00",  # forces refresh via GenerateAccessToken
                        "username": "user_main",
                        "scopes": ["resources.admin:*"],
                    }
                ),
                encoding="utf-8",
            )
            (accounts_cfg / "accounts.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active": "main_acct",
                        "accounts": {
                            "main_acct": {"username": "user_main", "auth_method": "oauth"},
                            "alt": {"username": "user_alt", "auth_method": "access_token"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            p_tok_alt = run_k(
                ["account", "login", "alt", "--token", "KGAT_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]
            )
            self.assertEqual(p_tok_alt.returncode, 0, p_tok_alt.stderr)

            # 3. Create GPU session `worker` on active account `main_acct`.
            p_create = run_k(["session", "create", "worker", "--accelerator", "t4", "--json"])
            self.assertEqual(p_create.returncode, 0, p_create.stderr)
            created = json.loads(p_create.stdout)
            self.assertFalse(created["reused"])
            self.assertEqual(created["account"], "main_acct")
            self.assertEqual(created["slug"], "user_main/kaggle-ext-s-worker")
            self.assertEqual(created["machine_shape"], "NvidiaTeslaT4")
            self.assertEqual(created["kernel_session_id"], 370558104)
            self.assertNotIn(cluster.state.proxy_secret, p_create.stdout)
            self.assertNotIn(cluster.state.query_secret, p_create.stdout)

            # Verify SaveKernel sent private QUICK_SAVE empty notebook (`source == ""` str).
            self.assertEqual(len(cluster.state.save_calls), 1)
            sc = cluster.state.save_calls[0]
            self.assertTrue(sc["isPrivate"])
            self.assertEqual(sc["kernelExecutionType"], "QUICK_SAVE")
            nb_saved = json.loads(str(sc["text"]))
            self.assertIsInstance(nb_saved["cells"][0]["source"], str)
            self.assertEqual(nb_saved["cells"][0]["source"], "")

            # Verify sessions.json mode is 0600.
            sess_file = accounts_cfg / "accounts" / "main_acct" / "sessions.json"
            self.assertEqual(stat.S_IMODE(os.stat(sess_file).st_mode), 0o600)

            # 4. Re-run create -> reuses live session without calling CreateKernelSession again.
            p_reuse = run_k(["session", "create", "worker", "--json"])
            self.assertEqual(p_reuse.returncode, 0, p_reuse.stderr)
            self.assertTrue(json.loads(p_reuse.stdout)["reused"])
            self.assertEqual(len(cluster.state.create_session_calls), 1)

            # 5. Create TPU session `tpu_job` on `alt` (`--no-internet`).
            p_alt_create = run_k(
                ["-a", "alt", "session", "create", "tpu_job", "--accelerator", "tpu", "--no-internet", "--json"]
            )
            self.assertEqual(p_alt_create.returncode, 0, p_alt_create.stderr)
            alt_created = json.loads(p_alt_create.stdout)
            self.assertEqual(alt_created["account"], "alt")
            self.assertEqual(alt_created["machine_shape"], "TpuV5E8")
            self.assertFalse(alt_created["enable_internet"])
            cs_alt = cluster.state.create_session_calls[1]
            self.assertEqual(cs_alt["machineShape"], "TpuV5E8")
            self.assertNotIn("dockerImage", cs_alt)

            # Pinned-account auto-routing: `session status tpu_job` without `-a alt` resolves `alt`!
            p_tpu_st = run_k(["sess", "status", "tpu_job", "--json"])
            self.assertEqual(p_tpu_st.returncode, 0, p_tpu_st.stderr)
            tpu_st = json.loads(p_tpu_st.stdout)
            self.assertEqual(tpu_st["account"], "alt")
            self.assertTrue(tpu_st["alive"])

            # 6. Status check on `worker` returns alive even though RPC GetKernelSessionStatus returns COMPLETE.
            p_st = run_k(["session", "status", "worker", "--json"])
            self.assertEqual(p_st.returncode, 0, p_st.stderr)
            self.assertTrue(json.loads(p_st.stdout)["alive"])

            # 7. Exec with `--env` / `--env-file` hidden injection + check `len(In) == 2`.
            env_file = tmp_path / "secrets.env"
            env_file.write_text("SECRET_FROM_FILE=ENV_FILE_SECRET\n", encoding="utf-8")
            p_exec = run_k(
                [
                    "session",
                    "exec",
                    "worker",
                    "--env-file",
                    str(env_file),
                    "-e",
                    "LOCAL_SECRET",
                    "-c",
                    "import os; print('EXEC_OUT', os.environ.get('SECRET_FROM_FILE'), os.environ.get('LOCAL_SECRET'), len(In))",
                ],
                extra_env={"LOCAL_SECRET": "LOCAL_VAL_99"},
            )
            self.assertEqual(p_exec.returncode, 0, p_exec.stderr)
            self.assertIn("EXEC_OUT ENV_FILE_SECRET LOCAL_VAL_99 2", p_exec.stdout)

            # Exec .ipynb where cell 2 fails -> cell 3 must NOT run and exit code must be 1.
            nb_file = tmp_path / "fail_nb.ipynb"
            nb_file.write_text(
                json.dumps(
                    {
                        "cells": [
                            {"cell_type": "code", "source": "print('CELL1_OK')"},
                            {"cell_type": "code", "source": "raise ValueError('boom_cell_2')"},
                            {"cell_type": "code", "source": "print('CELL3_SHOULD_NOT_RUN')"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            p_nb = run_k(["session", "exec", "worker", "-f", str(nb_file), "--json"])
            self.assertEqual(p_nb.returncode, 1)
            nb_res = json.loads(p_nb.stdout)
            self.assertFalse(nb_res["ok"])
            self.assertEqual(nb_res["cells_executed"], 2)
            self.assertIn("CELL1_OK", nb_res["stdout"])
            self.assertNotIn("CELL3_SHOULD_NOT_RUN", nb_res["stdout"])
            self.assertIn("boom_cell_2", nb_res["stderr"])

            # Exec timeout -> exit code 124 and /interrupt triggered.
            p_to = run_k(["session", "exec", "worker", "-c", "# SLEEP_UNTIL_INTERRUPT", "-t", "0.4"])
            self.assertEqual(p_to.returncode, 124)
            self.assertGreaterEqual(cluster.state.interrupts, 1)

            # 8. Concurrent `watch` receives live iopub output without kicking `exec`.
            cluster.state.ws_connected_event.clear()
            watch_proc = subprocess.Popen(
                [self.REAL_PY, "-m", "kaggle", "session", "watch", "worker", "-t", "3", "--json"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(cluster.state.ws_connected_event.wait(timeout=10.0))
                p_ping = run_k(["session", "exec", "worker", "-c", "print('WATCH_PING')"])
                self.assertEqual(p_ping.returncode, 0, p_ping.stderr)
                w_out, w_err = watch_proc.communicate(timeout=10)
                self.assertEqual(watch_proc.returncode, 0, w_err)
                self.assertIn("WATCH_PING", w_out)
                self.assertEqual(cluster.state.session_id_kicks, 0)
            finally:
                if watch_proc.poll() is None:
                    watch_proc.kill()
                    watch_proc.communicate()

            # 9. Offline logs + restart.
            p_logs = run_k(["session", "logs", "worker", "-n", "20"])
            self.assertEqual(p_logs.returncode, 0, p_logs.stderr)
            self.assertIn("WATCH_PING", p_logs.stdout)

            p_rst = run_k(["session", "restart", "worker", "--json"])
            self.assertEqual(p_rst.returncode, 0, p_rst.stderr)
            self.assertEqual(cluster.state.restarts, 1)

            # 10. Secret redaction across list, status, and `ext doctor`.
            p_list_all = run_k(["session", "list", "--all-accounts", "--json"])
            self.assertEqual(p_list_all.returncode, 0, p_list_all.stderr)
            list_rows = json.loads(p_list_all.stdout)
            self.assertEqual({(r["account"], r["name"]) for r in list_rows}, {("main_acct", "worker"), ("alt", "tpu_job")})

            p_doc_json = run_k(["ext", "doctor", "--json"])
            self.assertEqual(p_doc_json.returncode, 0, p_doc_json.stderr)
            doc_data = json.loads(p_doc_json.stdout)
            self.assertEqual(doc_data["sessions"]["count"], 1)

            p_doc_txt = run_k(["ext", "doctor"])
            self.assertEqual(p_doc_txt.returncode, 0, p_doc_txt.stderr)
            self.assertIn("sessions        : 1 recorded (worker", p_doc_txt.stdout)

            combined_outputs = "\n".join(
                [p_list_all.stdout, p_tpu_st.stdout, p_st.stdout, p_doc_json.stdout, p_doc_txt.stdout]
            )
            for secret_val in (cluster.state.proxy_secret, cluster.state.query_secret, "KGRT_main_refresh", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"):
                self.assertNotIn(secret_val, combined_outputs)

            # 11. Stop preserves local state on transient HTTP 500, deletes on success, never deletes placeholder kernel.
            cluster.state.fail_cancel_code = 500
            p_stop_fail = run_k(["session", "stop", "worker"])
            self.assertNotEqual(p_stop_fail.returncode, 0)
            self.assertIn("worker", json.loads(sess_file.read_text(encoding="utf-8"))["sessions"])

            cluster.state.fail_cancel_code = None
            p_stop_ok = run_k(["session", "stop", "worker", "--json"])
            self.assertEqual(p_stop_ok.returncode, 0, p_stop_ok.stderr)
            self.assertNotIn("worker", json.loads(sess_file.read_text(encoding="utf-8"))["sessions"])
            self.assertEqual(cluster.state.delete_calls, 0)

            p_dead_st = run_k(["session", "status", "worker"])
            self.assertNotEqual(p_dead_st.returncode, 0)

            p_stop_alt = run_k(["-a", "alt", "session", "stop", "--all", "--json"])
            self.assertEqual(p_stop_alt.returncode, 0, p_stop_alt.stderr)
            self.assertEqual(len(cluster.state.cancel_calls), 3)  # 1 failed + 2 succeeded
            self.assertEqual(cluster.state.delete_calls, 0)


if __name__ == "__main__":
    unittest.main()
