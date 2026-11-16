"""Loaded by `kaggle_ext.pth` at interpreter startup inside the kaggle-cli environment.

CRITICAL: Do NOT import `kaggle` or `kaggle.cli` eagerly here! `kaggle/__init__.py`
instantiates `KaggleApi()` and calls `api.authenticate()` at import time, which would
slow down every `python -c ...` invocation in this environment and trigger config-dir
creation or auth prompts unnecessarily.

Instead, install a zero-overhead `sys.meta_path` MetaPathFinder watching only
`kaggle.api.kaggle_api_extended` and `kaggle.cli`:
1. Before `kaggle.api.kaggle_api_extended` executes: run registered pre-API hooks.
2. After `kaggle.api.kaggle_api_extended` executes (and BEFORE `kaggle/__init__.py`
   constructs `api = KaggleApi()`): patch `KaggleApi.authenticate` so offline
   extension commands (`kaggle ext ...`) never fail or hit the network when logged out.
3. After `kaggle.cli` executes: call `kaggle_ext.register(cli_mod)` to wire hooks and
   extension subcommands onto the CLI parser built inside `kaggle.cli:main()`.

Fail-open by design: any exception is silent unless `KAGGLE_EXT_DEBUG=1`.
"""

from __future__ import annotations

import importlib.abc
import os
import sys

_TARGETS = frozenset({"kaggle.api.kaggle_api_extended", "kaggle.cli"})


def _debug() -> bool:
    return bool(os.environ.get("KAGGLE_EXT_DEBUG"))


def _warn(msg: str) -> None:
    if _debug():
        sys.stderr.write(msg + "\n")


class _LoaderWrapper(importlib.abc.Loader):
    """Wrap an import spec's original loader to run pre/post hooks cleanly."""

    def __init__(self, spec, orig_loader, fullname: str) -> None:
        self._spec = spec
        self._orig = orig_loader
        self._fullname = fullname

    def create_module(self, spec):
        if hasattr(self._orig, "create_module"):
            return self._orig.create_module(spec)
        return None

    def exec_module(self, module) -> None:
        # Restore original loader immediately so importlib/pkgutil inspection sees SourceFileLoader.
        self._spec.loader = self._orig
        module.__loader__ = self._orig

        if self._fullname == "kaggle.api.kaggle_api_extended":
            try:
                from kaggle_ext import cli_hooks

                cli_hooks.run_pre_api_hooks()
            except Exception as exc:
                _warn("[kaggle-ext] pre-api hook skipped: %r" % (exc,))

        self._orig.exec_module(module)

        if self._fullname == "kaggle.api.kaggle_api_extended":
            try:
                from kaggle_ext import cli_hooks

                cli_hooks.patch_api_module(module)
            except Exception as exc:
                _warn("[kaggle-ext] api patch skipped: %r" % (exc,))
        elif self._fullname == "kaggle.cli":
            try:
                import kaggle_ext

                if kaggle_ext.register(module) and _debug():
                    sys.stderr.write("[kaggle-ext] registered kaggle-ext v%s\n" % kaggle_ext.__version__)
            except Exception as exc:
                _warn("[kaggle-ext] cli register skipped: %r" % (exc,))

    def __getattr__(self, name: str):
        return getattr(self._orig, name)


class _KaggleExtFinder(importlib.abc.MetaPathFinder):
    """MetaPathFinder that intercepts only `kaggle.api.kaggle_api_extended` and `kaggle.cli`."""

    _kaggle_ext_finder = True

    def __init__(self) -> None:
        self._busy: set[str] = set()

    def find_spec(self, fullname: str, path, target=None):
        if fullname not in _TARGETS or fullname in self._busy:
            return None
        self._busy.add(fullname)
        try:
            for finder in sys.meta_path:
                if finder is self:
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None and spec.loader is not None and not isinstance(spec.loader, _LoaderWrapper):
                    spec.loader = _LoaderWrapper(spec, spec.loader, fullname)
                    return spec
            return None
        finally:
            self._busy.discard(fullname)


def install_import_hook() -> bool:
    """Install `_KaggleExtFinder` idempotently and catch up if modules are already imported."""
    if not any(getattr(f, "_kaggle_ext_finder", False) for f in sys.meta_path):
        sys.meta_path.insert(0, _KaggleExtFinder())

    api_mod = sys.modules.get("kaggle.api.kaggle_api_extended")
    if api_mod is not None:
        from kaggle_ext import cli_hooks

        cli_hooks.patch_api_module(api_mod)

    cli_mod = sys.modules.get("kaggle.cli")
    if cli_mod is not None:
        import kaggle_ext

        kaggle_ext.register(cli_mod)

    return True


try:
    install_import_hook()
except Exception as exc:
    _warn("[kaggle-ext] import hook skipped: %r" % (exc,))
