#!/usr/bin/env python3
"""Install, repair, or inspect `kaggle-ext` for Kaggle/kaggle-cli.

Runs with any Python 3.11+ interpreter (standard library only). When `kaggle` is
upgraded and rebuilds its virtual environment, the extension source in `~/.kaggle-ext`
remains untouched and this script re-wires `kaggle_ext.pth` in one command:

    python3 install.py                  # install (copy source + drop kaggle_ext.pth)
    python3 install.py --status         # show wiring status and exit
    python3 install.py --uninstall      # remove kaggle_ext.pth (~/.kaggle-ext stays)
    python3 install.py --link           # dev mode: symlink repo instead of copying
    python3 install.py --site-dir DIR   # target a specific site-packages directory
    KAGGLE_EXT_DEBUG=1 ...              # verbose loader diagnostics
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kaggle_ext import inject  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="show wiring status and exit")
    ap.add_argument("--uninstall", action="store_true", help="remove kaggle_ext.pth")
    ap.add_argument(
        "--link",
        action="store_true",
        help="dev mode: symlink repo source into ~/.kaggle-ext instead of copying",
    )
    ap.add_argument("--site-dir", help="site-packages directory of the kaggle-cli environment")
    ap.add_argument("--quiet", action="store_true", help="suppress informational output")
    args = ap.parse_args()

    if args.status:
        for key, val in inject.status(site_dir=args.site_dir).items():
            print("%-18s: %s" % (key, val))
        return 0

    if args.uninstall:
        out = inject.remove(site_dir=args.site_dir)
        if not args.quiet:
            if out["removed"]:
                print("[kaggle-ext] removed %s" % out["pth"])
            else:
                print("[kaggle-ext] nothing to remove (%s)" % out["pth"])
        return 0

    rep = inject.ensure(
        site_dir=args.site_dir,
        copy_source=not args.link,
        link=args.link,
        quiet=args.quiet,
    )
    if rep.get("error"):
        print("ERROR: %s" % rep["error"], file=sys.stderr)
        return 1

    if not args.quiet:
        st = inject.status(site_dir=args.site_dir)
        state = "written" if rep.get("changed") else "already present"
        print("[kaggle-ext] .pth : %s (%s)" % (rep.get("pth"), state))
        print("[kaggle-ext] source: %s" % rep.get("source"))
        if not st.get("kaggle_here"):
            print("[kaggle-ext] WARN: %s does not contain kaggle" % st.get("site_packages"))
        print("[kaggle-ext] done. `kaggle ext doctor` works in the NEXT invocation.")
        print("[kaggle-ext] after upgrading kaggle, re-run this script if extension commands disappear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
