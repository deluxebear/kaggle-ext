"""`kaggle-ext` -- non-intrusive CLI extension scaffold for Kaggle/kaggle-cli.

No file belonging to `kaggle` or `kagglesdk` is edited. A single `.pth` file drops a
zero-overhead `sys.meta_path` finder (`kaggle_ext_loader.py`) that wires extension hooks
and commands only when `kaggle.api.kaggle_api_extended` or `kaggle.cli` is imported.
"""

from __future__ import annotations

import os
import sys

__version__ = "0.3.0"
REGISTERED_ATTR = "_kaggle_ext_registered"
ACCOUNT_HELP = "Manage multiple isolated Kaggle accounts and credentials (alias: acct)"
EXT_HELP = "Diagnose, repair, or inspect the kaggle-ext CLI extension"
SKILL_HELP = "Print AI agent skill documentation and command references"


def _debug_enabled() -> bool:
    return bool(os.environ.get("KAGGLE_EXT_DEBUG"))


def register(cli_mod=None, *, force: bool = False) -> bool:
    """Attach CLI hooks and extension commands to `kaggle.cli`. Never raises."""
    if cli_mod is None:
        cli_mod = sys.modules.get("kaggle.cli")

    if cli_mod is not None and getattr(cli_mod, REGISTERED_ATTR, False) and not force:
        return False

    try:
        from . import account_commands, cli_hooks, commands, skill_commands

        cli_hooks.register_multi_account_hooks()
        account_commands.register_account_commands()
        commands.register_commands()
        skill_commands.register_skill_commands()

        api_mod = sys.modules.get("kaggle.api.kaggle_api_extended")
        if api_mod is not None:
            cli_hooks.patch_api_module(api_mod, force=force)

        if cli_mod is not None:
            cli_hooks.install_cli_hooks(cli_mod, force=force)
            setattr(cli_mod, REGISTERED_ATTR, True)
        return True
    except Exception as exc:  # fail-open: never break the official kaggle CLI
        if _debug_enabled():
            import traceback

            traceback.print_exc()
        else:
            print(
                "[kaggle-ext] extension disabled this run: %r (KAGGLE_EXT_DEBUG=1 for traceback)"
                % exc,
                file=sys.stderr,
            )
        return False
