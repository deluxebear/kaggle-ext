#!/bin/sh
# Bootstrap installer for kaggle-ext.
#
# One-line install (any POSIX shell, no git required):
#
#     curl -fsSL https://raw.githubusercontent.com/deluxebear/kaggle-ext/main/install.sh | sh
#
# Env overrides:
#     KAGGLE_EXT_REPO=owner/repo    source repository   (default: deluxebear/kaggle-ext)
#     KAGGLE_EXT_REF=main           branch or tag       (default: main)
#     KAGGLE_EXT_HOME=~/.kaggle-ext install destination (default: ~/.kaggle-ext)
#
# What it does: fetch the repository tarball, unpack it into a temp dir, and run
# `python3 install.py`. That copies the extension to ~/.kaggle-ext and writes a single
# `.pth` file (kaggle_ext.pth) into the kaggle-cli environment's site-packages.
set -eu

REPO="${KAGGLE_EXT_REPO:-deluxebear/kaggle-ext}"
REF="${KAGGLE_EXT_REF:-main}"

say() { printf '[kaggle-ext] %s\n' "$*"; }
die() { printf '[kaggle-ext] ERROR: %s\n' "$*" >&2; exit 1; }

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        die "need curl or wget to download the source"
    fi
}

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
[ -n "$PY" ] || die "python3 not found on PATH (https://www.python.org/)"

if ! command -v kaggle >/dev/null 2>&1; then
    say "WARN: 'kaggle' not found on PATH."
    say "      install it first:  uv tool install kaggle"
    say "      continuing anyway -- kaggle_ext.pth activates once kaggle exists."
fi

command -v tar >/dev/null 2>&1 || die "tar not found on PATH"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/kaggle-ext.XXXXXX")" || die "cannot create temp dir"
trap 'rm -rf "$TMP"' EXIT INT TERM

TARBALL="${TMP}/src.tar.gz"
URL="https://github.com/${REPO}/archive/refs/heads/${REF}.tar.gz"
say "downloading ${REPO}@${REF} ..."
fetch "$URL" > "$TARBALL" || die "download failed: ${URL}"
[ -s "$TARBALL" ] || die "downloaded tarball is empty: ${URL}"

tar -xzf "$TARBALL" -C "$TMP" || die "extract failed (${TARBALL})"

SRC_DIR=""
for d in "$TMP"/*/; do
    [ -f "${d}install.py" ] && SRC_DIR="$d" && break
done
[ -n "$SRC_DIR" ] || die "install.py not found in the tarball"

cd "$SRC_DIR"
"$PY" install.py || die "install.py failed (re-run with KAGGLE_EXT_DEBUG=1 for details)"

cat <<'EOF'

[kaggle-ext] installed. next steps:
  1. kaggle ext doctor           # verify the .pth wiring & offline auth/config summary
  2. kaggle ext version          # print extension version and site-packages wiring JSON
EOF
