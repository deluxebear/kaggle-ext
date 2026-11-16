"""Injection manager for `kaggle-ext` (Python standard library only).

Importable from any Python 3.11+ interpreter (inside or outside the `kaggle` tool
environment). Nothing here edits a file owned by `kaggle` or `kagglesdk`: we drop
exactly one `.pth` file (`kaggle_ext.pth`) into the target environment's
`site-packages`. That `.pth` puts `~/.kaggle-ext` (or `$KAGGLE_EXT_HOME`) on `sys.path`
and imports `kaggle_ext_loader`, which installs a lightweight `sys.meta_path` hook
without eagerly importing `kaggle`.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

PACKAGE = "kaggle_ext"
LOADER = "kaggle_ext_loader.py"
PTH_NAME = "kaggle_ext.pth"
TOOL_NAME = "kaggle"

# site.py executes every .pth line starting with 'import' on a SINGLE line.
# Keep literal '\n' out of _SNIPPET; use chr(10) so single-line flattening stays valid Python.
_SNIPPET = """
try:
    here = os.path.expanduser(os.environ.get('KAGGLE_EXT_SOURCE') or os.environ.get('KAGGLE_EXT_HOME') or '~/.kaggle-ext')
    if os.path.isdir(here) and here not in sys.path:
        sys.path.insert(0, here)
    import kaggle_ext_loader
except Exception as _exc:
    if os.environ.get('KAGGLE_EXT_DEBUG'):
        sys.stderr.write('[kaggle-ext] loader skipped: ' + repr(_exc) + chr(10))
"""


def install_root() -> str:
    """Return the external extension installation directory (`~/.kaggle-ext` by default)."""
    return os.path.abspath(os.path.expanduser(os.environ.get("KAGGLE_EXT_HOME") or "~/.kaggle-ext"))


def _version() -> str:
    try:
        import kaggle_ext as _m

        return getattr(_m, "__version__", "?")
    except Exception:
        return "unknown"


def comment_line() -> str:
    """Header comment placed inside the generated `.pth` file."""
    return "# kaggle-ext (%s): non-intrusive extension for %s" % (_version(), TOOL_NAME)


def pth_contents() -> str:
    """Build the single-line `import ...` `.pth` payload executed by Python's `site.py`."""
    line = (
        _SNIPPET.strip("\n")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )
    return (
        "# %s\n"
        "# Managed file: regenerate with `kaggle ext install` or `python3 %s/install.py`\n"
        'import os, sys; exec(compile("%s", \'%s\', \'exec\'))'
        % (comment_line().lstrip("# "), install_root(), line, PTH_NAME)
    )


def _site_packages_of(interpreter: str) -> str | None:
    """Return `site.getsitepackages()[0]` for `interpreter`, or `None` if probing fails."""
    try:
        out = (
            subprocess.run(
                [interpreter, "-c", "import site; print(site.getsitepackages()[0])"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            .stdout.strip()
            .splitlines()
        )
        return out[-1] if out else None
    except Exception:
        return None


def _which(name: str) -> str | None:
    """Locate an executable on `PATH`."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _interpreters_to_try() -> list[str]:
    """Candidate Python interpreters, starting with the one in the `kaggle` shim shebang."""
    out: list[str] = []
    shim = _which("kaggle")
    if shim:
        try:
            with open(shim, "rb") as fh:
                first = fh.read(512).split(b"\n", 1)[0].decode("utf-8", "replace")
            if first.startswith("#!"):
                shebang = first[2:].strip()
                if " " not in shebang or shebang.startswith("/"):
                    out.append(shebang.split()[0])
        except OSError:
            pass
    here = os.path.realpath(sys.executable)
    if here not in [os.path.realpath(p) for p in out]:
        out.append(here)
    return out


def candidate_site_dirs() -> list[str]:
    """Return plausible `site-packages` directories in priority order."""
    dirs: list[str] = []

    def add(path: str | None) -> None:
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)

    for py in _interpreters_to_try():
        add(_site_packages_of(py))

    home = os.path.expanduser("~")
    for pattern in (
        home + "/.local/share/uv/tools/" + TOOL_NAME + "/lib/python*/site-packages",
        home + "/.local/pipx/venvs/" + TOOL_NAME + "/lib/python*/site-packages",
        "/usr/local/lib/python3*/site-packages",
        "/usr/lib/python3*/dist-packages",
    ):
        for found in sorted(glob.glob(pattern), reverse=True):
            add(found)

    for path in getattr(sys, "getsitepackages", lambda: [])():
        add(path)
    return dirs


def target_site_dir() -> str | None:
    """Locate the `site-packages` directory containing the `kaggle` package."""
    cands = candidate_site_dirs()
    for directory in cands:
        if os.path.isdir(os.path.join(directory, TOOL_NAME)):
            return directory
    return cands[0] if cands else None


def _has_package_and_loader(directory: str) -> bool:
    return os.path.isdir(os.path.join(directory, PACKAGE)) and os.path.isfile(
        os.path.join(directory, LOADER)
    )


def source_dir() -> str | None:
    """Locate a directory containing both `kaggle_ext/` and `kaggle_ext_loader.py`."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_candidate = os.path.dirname(here)
    env_src = os.environ.get("KAGGLE_EXT_SOURCE")
    for base in (env_src, repo_candidate, here, install_root()):
        if not base:
            continue
        expanded = os.path.abspath(os.path.expanduser(base))
        if _has_package_and_loader(expanded):
            return expanded
        if os.path.basename(expanded) == PACKAGE:
            parent = os.path.dirname(expanded)
            if _has_package_and_loader(parent):
                return parent
    return None


def pth_path(site_dir: str | None = None) -> str | None:
    """Return the path where `kaggle_ext.pth` should live."""
    resolved = site_dir or target_site_dir()
    return os.path.join(resolved, PTH_NAME) if resolved else None


def link_source(src: str | None = None, root: str | None = None) -> str:
    """Symlink `kaggle_ext/` and `kaggle_ext_loader.py` from `src` into `root` for dev mode."""
    resolved_src = os.path.abspath(os.path.expanduser(src)) if src else source_dir()
    if not resolved_src or not _has_package_and_loader(resolved_src):
        raise FileNotFoundError("cannot locate kaggle_ext source (run from the repo checkout)")

    resolved_root = os.path.abspath(os.path.expanduser(root)) if root else install_root()
    if os.path.realpath(resolved_src) == os.path.realpath(resolved_root):
        return resolved_root

    os.makedirs(resolved_root, exist_ok=True)
    for rel in (PACKAGE, LOADER):
        src_item = os.path.join(resolved_src, rel)
        dst_item = os.path.join(resolved_root, rel)
        if os.path.islink(dst_item) or os.path.isfile(dst_item):
            os.unlink(dst_item)
        elif os.path.isdir(dst_item):
            shutil.rmtree(dst_item)
        os.symlink(src_item, dst_item)
    return resolved_root


def _copy_source(src: str, root: str) -> None:
    os.makedirs(root, exist_ok=True)
    dst_pkg = os.path.join(root, PACKAGE)
    if os.path.islink(dst_pkg) or os.path.isfile(dst_pkg):
        os.unlink(dst_pkg)
    elif os.path.isdir(dst_pkg):
        shutil.rmtree(dst_pkg)
    shutil.copytree(
        os.path.join(src, PACKAGE),
        dst_pkg,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    dst_loader = os.path.join(root, LOADER)
    if os.path.islink(dst_loader):
        os.unlink(dst_loader)
    shutil.copy2(os.path.join(src, LOADER), dst_loader)


def ensure(
    site_dir: str | None = None,
    *,
    copy_source: bool = True,
    link: bool = False,
    quiet: bool = False,
) -> dict:
    """Wire `kaggle-ext` into the `kaggle` environment idempotently."""
    resolved_site = site_dir or target_site_dir()
    src = source_dir()
    root = install_root()
    report: dict = {
        "site_dir": resolved_site,
        "source": src,
        "install_root": root,
        "installed": [],
        "changed": False,
        "linked": False,
    }
    if not resolved_site:
        report["error"] = "could not find the site-packages of the kaggle-cli environment"
        return report

    if link:
        if not src:
            report["error"] = "cannot locate kaggle_ext source to link"
            return report
        link_source(src=src, root=root)
        report["linked"] = True
        report["source"] = root
        report["installed"].append(root)
    elif copy_source and src and os.path.realpath(src) != os.path.realpath(root):
        _copy_source(src, root)
        report["source"] = root
        report["installed"].append(root)

    path = pth_path(resolved_site)
    body = pth_contents()
    old = None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
    if path and old != body:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        report["changed"] = True
        report["installed"].append(path)
    report["pth"] = path

    if not quiet:
        for item in report["installed"]:
            print("[kaggle-ext] wrote %s" % item)
    return report


def remove(site_dir: str | None = None) -> dict:
    """Remove `kaggle_ext.pth` from `site_dir` (keeps `~/.kaggle-ext` intact)."""
    resolved_site = site_dir or target_site_dir()
    path = pth_path(resolved_site)
    out = {"pth": path, "removed": False}
    if path and (os.path.islink(path) or os.path.exists(path)):
        os.unlink(path)
        out["removed"] = True
    return out


def _kaggle_cli_version(site_dir: str | None = None) -> str | None:
    """Inspect installed `kaggle.__version__` without eagerly importing `kaggle`."""
    loaded = sys.modules.get("kaggle")
    if loaded is not None:
        return getattr(loaded, "__version__", None)
    if not site_dir:
        return None
    init_py = os.path.join(site_dir, TOOL_NAME, "__init__.py")
    if not os.path.isfile(init_py):
        return None
    try:
        with open(init_py, encoding="utf-8") as fh:
            text = fh.read(4096)
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        return match.group(1) if match else None
    except OSError:
        return None


def status(site_dir: str | None = None) -> dict:
    """Return non-mutating status of the extension wiring and `kaggle` environment."""
    resolved_site = site_dir or target_site_dir()
    path = pth_path(resolved_site)
    root = install_root()
    return {
        "extension_version": _version(),
        "source": source_dir(),
        "install_root": root if os.path.isdir(root) else None,
        "site_packages": resolved_site,
        "kaggle_here": bool(resolved_site and os.path.isdir(os.path.join(resolved_site, TOOL_NAME))),
        "kaggle_cli_version": _kaggle_cli_version(resolved_site),
        "pth": path,
        "pth_present": bool(path and os.path.exists(path)),
        "kaggle_shim": _which("kaggle"),
        "python": sys.executable,
    }


def reload_ok() -> bool:
    """True when `kaggle_ext` has registered onto `kaggle.cli` in this process."""
    try:
        cli_mod = sys.modules.get("kaggle.cli")
        return bool(cli_mod is not None and getattr(cli_mod, "_kaggle_ext_registered", False))
    except Exception:
        return False
