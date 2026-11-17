"""CLI commands added by `kaggle-ext`: `kaggle ext {doctor, status, install, uninstall, version}`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import EXT_HELP, __version__, accounts, cli_hooks, diagnostics, inject

EXT_CHOICES = ["doctor", "status", "install", "uninstall", "version"]


def _cmd_doctor(json_output: bool = False) -> None:
    st = inject.status()
    reg_exists = os.path.isfile(accounts.registry_path())
    reg = accounts.load_registry(bootstrap=False)
    acct = accounts.current_account() if reg_exists else accounts.DEFAULT_ACCOUNT
    acct_src = accounts.current_account_source() if reg_exists else "default"
    acct_names = list(reg.get("accounts", {}).keys()) if reg_exists else []
    acct_dir = accounts.account_dir(acct) if reg_exists and os.path.isdir(accounts.account_dir(acct)) else None

    auth = diagnostics.inspect_kaggle_auth_offline(account_dir_override=acct_dir)
    sess_diag = diagnostics.inspect_sessions_offline(acct_dir)
    registered = inject.reload_ok()
    multi_account = {
        "registry_path": accounts.registry_path(),
        "registry_present": reg_exists,
        "account": acct,
        "source": acct_src,
        "accounts": acct_names,
        "account_dir": acct_dir,
    }
    payload = {
        "extension": __version__,
        "registered": registered,
        **st,
        "multi_account": multi_account,
        "kaggle_auth": auth,
        "sessions": sess_diag,
    }
    if json_output:
        print(json.dumps(payload, indent=2, default=str))
        return

    print("kaggle-ext v%s" % __version__)
    print("  registered here : %s" % ("yes" if registered else "no (fresh process needed)"))
    print("  kaggle shim     : %s" % st["kaggle_shim"])
    print("  interpreter     : %s" % st["python"])
    kaggle_ver = st["kaggle_cli_version"] or "unknown"
    here_note = "" if st["kaggle_here"] else "  <- kaggle NOT here"
    print("  site-packages   : %s (kaggle %s)%s" % (st["site_packages"], kaggle_ver, here_note))
    print("  hook (.pth)     : %s  [%s]" % (st["pth"], "ok" if st["pth_present"] else "MISSING"))
    print("  extension source: %s" % (st["source"] or "MISSING"))
    if not st["pth_present"]:
        print(
            "\n  fix: python3 %s/install.py   (or `kaggle ext install`)"
            % (st["source"] or "~/.kaggle-ext")
        )

    acct_summary = ", ".join(acct_names) if acct_names else "uninitialized (runs on first account use)"
    print("  accounts file   : %s (%d account(s): %s)" % (accounts.registry_path(), len(acct_names), acct_summary))
    print("  active account  : %s (source: %s)" % (acct, acct_src))
    print("  config dir      : %s (via %s)" % (auth["config_dir"], auth["config_dir_source"]))
    eff_src = auth["effective_auth_source"]
    eff_str = (
        "%s (%s)" % (auth["effective_auth_method"], eff_src)
        if eff_src
        else auth["effective_auth_method"]
    )
    print("  effective auth  : %s" % eff_str)

    kj = auth["kaggle_json"]
    if kj["present"]:
        print(
            "  kaggle.json     : present (mode: %s, user: %s, key: %s)"
            % (kj["mode"] or "?", kj["username"] or "unset", "present" if kj["has_key"] else "missing")
        )
        if kj["world_readable"]:
            print("  WARN: %s is readable by others; run `chmod 600 %s`" % (kj["path"], kj["path"]))
    else:
        print("  kaggle.json     : absent (%s)" % kj["path"])

    at = auth["access_token_file"]
    if at["present"]:
        print(
            "  access_token    : present (%s, format: %s)"
            % (at["path"], "valid KGAT_" if at["valid_format"] else "INVALID")
        )
    else:
        print("  access_token    : absent (%s)" % at["path"])

    oc = auth["oauth_credentials"]
    if oc["present"]:
        exp_note = ""
        if oc["access_token_expired"] is True:
            exp_note = ", access_token expired"
        elif oc["access_token_expired"] is False:
            exp_note = ", access_token active"
        print(
            "  oauth creds     : present (user: %s, refresh_token: %s%s)"
            % (
                oc["username"] or "unknown",
                "present" if oc["has_refresh_token"] else "missing",
                exp_note,
            )
        )
    else:
        print("  oauth creds     : absent (%s)" % oc["path"])

    if auth["display"]["wayland_only_oauth_quirk"]:
        print(
            "  NOTE: WAYLAND_DISPLAY is set without DISPLAY; upstream `kaggle auth login` "
            "may fall back to manual URL mode unless DISPLAY is set."
        )

    if sess_diag["present"]:
        if sess_diag["valid_json"] is False:
            print("  sessions        : present (%s, INVALID JSON)" % sess_diag["path"])
        else:
            names = [str(item["name"]) for item in sess_diag["sessions"]]
            summary = ", ".join(names) if names else "none"
            print(
                "  sessions        : %d recorded (%s, mode: %s)"
                % (sess_diag["count"], summary, sess_diag["mode"] or "?")
            )
            if sess_diag["world_readable"]:
                print(
                    "  WARN: %s is readable by others; run `chmod 600 %s`"
                    % (sess_diag["path"], sess_diag["path"])
                )
    else:
        print("  sessions        : none (%s)" % (sess_diag["path"] or "uninitialized"))


def _cmd_install(site_dir: str | None = None, link: bool = False, quiet: bool = False) -> None:
    rep = inject.ensure(site_dir=site_dir, copy_source=not link, link=link, quiet=quiet)
    if rep.get("error"):
        print("ERROR: %s" % rep["error"], file=sys.stderr)
        sys.exit(1)
    if not quiet:
        state = "updated" if rep.get("changed") else "already present"
        print("[kaggle-ext] .pth : %s (%s)" % (rep.get("pth"), state))
        print("[kaggle-ext] source: %s" % rep.get("source"))
        print("[kaggle-ext] extension commands are active in the NEXT `kaggle` invocation.")


def _cmd_uninstall(site_dir: str | None = None, quiet: bool = False) -> None:
    out = inject.remove(site_dir=site_dir)
    if quiet:
        return
    if out["removed"]:
        print("[kaggle-ext] removed %s" % out["pth"])
    else:
        print("[kaggle-ext] nothing to remove (%s)" % out["pth"])


def _cmd_version() -> None:
    st = inject.status()
    print(json.dumps({"extension": __version__, **st}, indent=2, default=str))


def attach_ext_subparser(subparsers, _cli_mod=None) -> None:
    """Attach `kaggle ext {doctor, status, install, uninstall, version}` to root subparsers."""
    name_map = getattr(subparsers, "_name_parser_map", {})
    if "ext" in name_map:
        return

    parser_ext = subparsers.add_parser(
        "ext",
        formatter_class=argparse.RawTextHelpFormatter,
        help=EXT_HELP,
    )
    # CRITICAL: `kaggle.cli:main` strips only `command`, `func`, `disable_version_warning`, `debug`.
    # Every nested subparser group MUST use `dest="command"` so nothing leaks into `args.func(**command_args)`.
    subparsers_ext = parser_ext.add_subparsers(title="commands", dest="command")
    subparsers_ext.required = True
    subparsers_ext.choices = list(EXT_CHOICES)

    parser_doctor = subparsers_ext.add_parser(
        "doctor",
        formatter_class=argparse.RawTextHelpFormatter,
        help="Diagnose kaggle-ext injection and offline Kaggle auth/config state",
    )
    parser_doctor.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print diagnostics as JSON",
    )
    parser_doctor.set_defaults(func=_cmd_doctor)

    parser_status = subparsers_ext.add_parser(
        "status",
        formatter_class=argparse.RawTextHelpFormatter,
        help="Alias for `kaggle ext doctor`",
    )
    parser_status.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print diagnostics as JSON",
    )
    parser_status.set_defaults(func=_cmd_doctor)

    parser_install = subparsers_ext.add_parser(
        "install",
        formatter_class=argparse.RawTextHelpFormatter,
        help="(Re-)inject kaggle_ext.pth into the kaggle CLI environment",
    )
    parser_install.add_argument(
        "--site-dir",
        dest="site_dir",
        required=False,
        help="Target site-packages directory containing kaggle",
    )
    parser_install.add_argument(
        "--link",
        dest="link",
        action="store_true",
        help="Dev mode: symlink repo source into ~/.kaggle-ext instead of copying",
    )
    parser_install.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        help="Suppress non-error output",
    )
    parser_install.set_defaults(func=_cmd_install)

    parser_uninstall = subparsers_ext.add_parser(
        "uninstall",
        formatter_class=argparse.RawTextHelpFormatter,
        help="Remove kaggle_ext.pth from the kaggle CLI environment",
    )
    parser_uninstall.add_argument(
        "--site-dir",
        dest="site_dir",
        required=False,
        help="Target site-packages directory containing kaggle",
    )
    parser_uninstall.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        help="Suppress non-error output",
    )
    parser_uninstall.set_defaults(func=_cmd_uninstall)

    parser_version = subparsers_ext.add_parser(
        "version",
        formatter_class=argparse.RawTextHelpFormatter,
        help="Print kaggle-ext version and environment wiring JSON",
    )
    parser_version.set_defaults(func=_cmd_version)


def register_commands() -> None:
    """Register `kaggle ext` through `cli_hooks`."""
    cli_hooks.register_subparsers_hook(
        attach_ext_subparser,
        command="ext",
        choices=["ext"],
        help_line="ext {" + ", ".join(EXT_CHOICES) + "}",
    )
