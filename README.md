# kaggle-ext — Non-Intrusive Extension for `Kaggle/kaggle-cli`

> English | [中文](README.zh-CN.md)

`kaggle-ext` extends the official [`Kaggle/kaggle-cli`](https://github.com/Kaggle/kaggle-cli) (`kaggle`) **without modifying any file inside `kaggle` or `kagglesdk`**.

Upgrading `kaggle` (`uv tool upgrade kaggle` / `pip install -U kaggle`) never produces merge conflicts. Extension source lives outside the tool venv in `~/.kaggle-ext/`, and a single one-line `kaggle_ext.pth` hook wires into the `kaggle` environment via a zero-overhead `sys.meta_path` loader.

## Requirements

- Python 3.11+ (core installer, loader, and diagnostics use only the Python standard library)
- Installed [`kaggle`](https://pypi.org/project/kaggle/) CLI (`uv tool install kaggle`)

## Installation

### One-Line Bootstrap

```sh
curl -fsSL https://raw.githubusercontent.com/deluxebear/kaggle-ext/main/install.sh | sh
```

### Local Checkout

```bash
git clone https://github.com/deluxebear/kaggle-ext
cd kaggle-ext
python3 install.py              # copy source to ~/.kaggle-ext and drop kaggle_ext.pth
python3 install.py --link       # dev mode: symlink this repo instead of copying
python3 install.py --status     # inspect detected site-packages and wiring status
python3 install.py --uninstall  # remove kaggle_ext.pth (keeps ~/.kaggle-ext intact)
```

## Available Commands

### Multi-Account Management (`kaggle account` / `acct`, `-a / --account`)

Each profile stores isolated `credentials.json`, `access_token`, and `kaggle.json` under `~/.config/kaggle-ext/accounts/<name>/` (`0700` dir, `0600` files). Existing `~/.kaggle/` credentials are imported non-destructively as `default` on first run.

```bash
# Login options (OAuth browser, manual URL, headless PKCE, API token, legacy kaggle.json)
kaggle account login work
kaggle account login alt --no-launch-browser
kaggle account login alt --url-only
kaggle account login alt --code <PASTE_CODE_OR_URL>
kaggle account login work --token KGAT_...          # or --token-file / --token-stdin
kaggle account login work --kaggle-json ~/Downloads/kaggle.json

# Switch & run any Kaggle command as a specific account
kaggle -a work competitions list
kaggle quota -a alt
KAGGLE_ACCOUNT=work kaggle datasets list
kaggle account use work

# Manage profiles
kaggle account list [--json]
kaggle account current [--json]
kaggle account status [--json]                      # concurrently checks auth + GPU/TPU quota
kaggle account add personal --use
kaggle account logout alt [--revoke]
kaggle account rename old new
kaggle account remove alt [--force]
```

### Extension Management (`kaggle ext`)

```bash
kaggle ext doctor [--json]      # inspect .pth wiring, active account, and offline auth/config state
kaggle ext status [--json]      # alias for doctor
kaggle ext install [--link]     # (re-)wire kaggle_ext.pth after upgrading kaggle
kaggle ext uninstall            # remove kaggle_ext.pth
kaggle ext version              # print extension version and environment JSON
```

`kaggle ext doctor` and `kaggle account list` work completely **offline and unauthenticated** without printing secret keys or tokens.

## How Non-Intrusive Wiring Works

1. `kaggle_ext.pth` inserts `~/.kaggle-ext` onto `sys.path` and imports `kaggle_ext_loader`.
2. `kaggle_ext_loader` installs a lightweight `sys.meta_path` `MetaPathFinder` without eagerly importing `kaggle` (so unrelated `python -c ...` invocations inside the tool venv pay zero import or auth overhead).
3. When `kaggle.api.kaggle_api_extended` loads, `cli_hooks` patches `KaggleApi.authenticate` so offline extension commands (`kaggle ext ...`) bypass login checks cleanly, while native `kaggle` commands keep 100% upstream auth behavior.
4. When `kaggle.cli` loads, `kaggle_ext.register(cli_mod)` instruments `kaggle.cli:main()`'s local `ArgumentParser` and `Help.kaggle_choices` to mount extension subcommands.
5. **Fail-open by design**: any unexpected error in `kaggle-ext` degrades gracefully without breaking the official `kaggle` CLI (set `KAGGLE_EXT_DEBUG=1` to print tracebacks).

## Roadmap

- **Phase 1 (Done)**: Non-intrusive `.pth` + `sys.meta_path` core, `install.py`/`install.sh`, `kaggle ext {doctor, status, install, uninstall, version}`.
- **Phase 2 (Done)**: Isolated multi-account management (`kaggle account` / `-a` / `KAGGLE_ACCOUNT`), headless `--url-only` / `--code` PKCE OAuth, token/kaggle.json import, and Wayland (`WAYLAND_DISPLAY`) browser-launch compatibility.
- **Phase 3 (Next)**: Agent skill documentation command (`kaggle skill`) merging upstream `Kaggle/kaggle-cli` skills with `kaggle-ext`.

## Testing

```bash
python3 -m unittest discover -s tests -v
```
