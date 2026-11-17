"""Offline AI agent skill and reference engine for `kaggle skill`.

Bundles upstream `Kaggle/kaggle-cli/skills/` (`SKILL.md` + `references/*.md`),
which the PyPI wheel omits, and merges `kaggle-ext` multi-account, headless OAuth/token
login, and extension diagnostics documentation idempotently using stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

SKILL_DATA_DIR = Path(__file__).resolve().parent / "skill_data"
REFERENCES_DATA_DIR = SKILL_DATA_DIR / "references"

SKILL_MARKER = "## Multi-Account & Extension Management (`kaggle-ext`)"
TOPIC_EXTENSION_MARKER = "## `kaggle-ext` Extension Notes"

SKILL_FRONTMATTER_NOTE = (
    "With kaggle-ext installed, also covers isolated multi-account management "
    "(kaggle account / acct, -a / --account, KAGGLE_ACCOUNT), interactive Jupyter "
    "sessions and WebSocket execution (kaggle session / sess), agent-safe two-step "
    "headless OAuth and token login, offline diagnostics (kaggle ext), and CLI "
    "reference lookup (kaggle skill [topic])."
)

EXTENSION_SKILL_SECTION = """## Multi-Account & Extension Management (`kaggle-ext`)

This environment includes [`kaggle-ext`](https://github.com/deluxebear/kaggle-ext),
a non-intrusive extension wired via a single `.pth` lazy loader (`kaggle` and
`kagglesdk` package files remain unmodified).

### Per-Command Account Targeting

Run any native Kaggle command against a specific isolated profile without
changing the global active account:

```bash
kaggle -a work competitions list       # leading global flag
kaggle quota -a alt                    # trailing flag after subcommand
KAGGLE_ACCOUNT=work kaggle datasets list
kaggle account use work                # switch default active account
```

### Offline & Agent-Safe Inspection

Inspect accounts and wiring state offline without triggering auth failures or
printing secret keys/tokens:

```bash
kaggle account list --json
kaggle account current --json
kaggle ext doctor --json
```

Check auth validity and weekly GPU/TPU quota across all configured accounts
concurrently in one call:

```bash
kaggle account status --json
```

### Non-Interactive & Headless Login (`kaggle account login`)

Each profile stores isolated `kaggle.json`, `access_token`, and
`credentials.json` under `~/.config/kaggle-ext/accounts/<name>/` (`0700` dir,
`0600` files):

```bash
# Agent-safe two-step headless PKCE OAuth (no blocking stdin prompt)
kaggle account login work --url-only
kaggle account login work --code "<CODE_OR_REDIRECT_URL>"

# Interactive URL-paste browser flow (e.g. separate Chrome profile / incognito)
kaggle account login work --no-launch-browser

# Token or legacy kaggle.json import
kaggle account login work --token KGAT_...
kaggle account login work --token-file /path/to/token
kaggle account login work --kaggle-json /path/to/kaggle.json
```

### Interactive Sessions & WebSocket Execution (`kaggle session` / `sess`)

Start or attach to live Kaggle Jupyter sessions backed by a private empty
placeholder kernel (`QUICK_SAVE`) and execute `.py` / `.ipynb` / inline code in
memory over a pure-stdlib WebSocket without writing secrets or code to the cloud
notebook:

```bash
kaggle session create worker --accelerator NvidiaTeslaT4 --internet
kaggle session status worker --json
kaggle session exec worker -f train.ipynb --env-file .env
kaggle session watch worker --timeout 60
kaggle session logs worker -n 100
kaggle session restart worker
kaggle session stop worker
```

Run `kaggle skill session` for full hardware aliases, limits, and options.

### Extension Recovery & Skill Lookup

```bash
kaggle ext install [--link]            # re-wire .pth after `uv tool upgrade kaggle`
kaggle skill --list                    # list all CLI & extension reference topics
kaggle skill <topic>                   # print reference (e.g. `kaggle skill kernels`, `account`, `session`, `ext`)
kaggle skill --export <dir>            # export SKILL.md and references/ into <dir>
```

### Practical Kernel TPU (`TpuV5E8`) Note

When pushing TPU kernels (`accelerator: "TpuV5E8"`), prefer `"kernel_type":
"notebook"` and omit any stale `"docker_image"` / `"docker_image_pinning_type"`
pin in `kernel-metadata.json` so Kaggle does not pin an incompatible CPU image.
Note that upstream Kaggle batch `SaveKernel` runs may still fall back to a
CPU image on some accounts even when `enable_tpu=true`; verify the runtime image
in `kaggle kernels logs` if TPU hardware is critical.
"""

ACCOUNT_REFERENCE_MD = """# Multi-Account Management CLI Reference (`kaggle-ext`)

Use `kaggle account` (alias: `kaggle acct`) and the global `-a / --account` flag
to manage multiple isolated Kaggle accounts without credential collisions or
leaks between `kaggle.json`, `access_token`, and OAuth `credentials.json`.

## Prerequisites

- `kaggle-ext` active in the `kaggle` environment (`kaggle ext doctor`).
- No login required for offline inspection commands (`list`, `current`).

## Command Hierarchy

```text
kaggle account | acct
├── list | ls [--json]
├── current [--json]
├── status [--json]
├── use | switch <NAME>
├── add <NAME> [login options] [--use | --no-use]
├── login [NAME] [login options]
├── logout [NAME] [--revoke]
├── rename <OLD> <NEW>
└── remove | rm <NAME> [--force]
```

## Storage Isolation Model

Each profile lives in its own locked directory:

- Root registry: `${KAGGLE_EXT_CONFIG_DIR:-~/.config/kaggle-ext}/accounts.json`
- Profile directory: `${KAGGLE_EXT_CONFIG_DIR:-~/.config/kaggle-ext}/accounts/<name>/`
  (`0700` dir, `0600` files: `kaggle.json`, `access_token`, `credentials.json`)
- First run non-destructively imports existing `~/.kaggle/` (or XDG Kaggle)
  credentials as the `default` profile.

`kaggle-ext` isolates all four upstream credential resolution points (`KaggleApi.config_dir`,
`kagglesdk.kaggle_env.get_access_token_from_env`, `KaggleCredentials` default paths,
and `kagglesdk.kaggle_http_client._get_apikey_creds`).

## Resolution Precedence

1. Explicit CLI flag: `kaggle -a <name> ...` or `kaggle ... -a <name>`
2. Environment variable: `KAGGLE_ACCOUNT=<name>`
3. Persistent active account recorded in `accounts.json` (`kaggle account use <name>`)
4. Fallback: `default`

Profiles can be referenced by profile name (`work`) or stored Kaggle `@username`.

## `kaggle account list` (`ls`) & `kaggle account current`

Offline, zero-network inspection of registered accounts and credential presence.

```bash
kaggle account list
kaggle account list --json
kaggle account current --json
```

## `kaggle account status`

Concurrently authenticates each account inside its own `account_context` and
fetches username and weekly GPU/TPU quota (`used/total`).

```bash
kaggle account status
kaggle account status --json
```

## `kaggle account login` & `kaggle account add`

Supports five authentication flows:

1. **Browser OAuth** (including Linux Wayland `WAYLAND_DISPLAY` without `DISPLAY`):
   `kaggle account login work`
2. **Manual browser URL paste** (interactive prompt):
   `kaggle account login alt --no-launch-browser`
3. **Two-step agent-safe headless PKCE** (no blocking stdin prompt):
   ```bash
   kaggle account login alt --url-only
   kaggle account login alt --code "<CODE_OR_REDIRECT_URL>"
   ```
4. **API token (`KGAT_...`)**:
   `kaggle account login work --token KGAT_...` (or `--token-file`, `--token-stdin`)
5. **Legacy credentials file**:
   `kaggle account login work --kaggle-json /path/to/kaggle.json`

Optional flags: `--no-verify` skips post-import auth verification; `--use` makes
the target account active immediately (`add` defaults to `--use`, pass `--no-use`
to keep the current active account).

## `kaggle account use` (`switch`), `logout`, `rename`, `remove` (`rm`)

```bash
kaggle account use work
kaggle account logout alt [--revoke]
kaggle account rename old new
kaggle account remove alt [--force]
```
"""

EXT_REFERENCE_MD = """# Extension Diagnostics & Skill CLI Reference (`kaggle-ext`)

Use `kaggle ext` to inspect, diagnose, or re-wire `kaggle-ext`, and `kaggle skill`
to read or export upstream + extension CLI documentation offline.

## Command Hierarchy

```text
kaggle ext
├── doctor [--json]
├── status [--json]      # alias for doctor
├── install [--site-dir DIR] [--link] [-q]
├── uninstall [--site-dir DIR] [-q]
└── version

kaggle skill [TOPIC]
├── --list [-l] [--json]
├── --all
├── --raw
├── --export DIR [--raw]
└── --json
```

## `kaggle ext doctor` (`status`)

Offline, read-only, secret-redacted diagnostic report covering:

- `.pth` hook wiring in the `kaggle` tool `site-packages`
- Extension source path (`~/.kaggle-ext` or `KAGGLE_EXT_HOME`)
- Multi-account registry location, active account, and resolution source
- Effective auth method (`access_token`, `kaggle.json`, `oauth`), file modes, and
  Linux Wayland-only browser detection notes

```bash
kaggle ext doctor
kaggle ext doctor --json
```

## `kaggle ext install` / `uninstall` / `version`

Re-wire `.pth` in a single command after `uv tool upgrade kaggle` or `pip install -U kaggle`:

```bash
kaggle ext install          # copy source into ~/.kaggle-ext and drop kaggle_ext.pth
kaggle ext install --link   # dev mode: symlink local checkout
kaggle ext uninstall
kaggle ext version
```

If `kaggle` cannot find `.pth` at all (so `kaggle ext` is not loaded yet), run:

```bash
python3 ~/.kaggle-ext/install.py
```

## `kaggle skill` (`SKILL`)

Prints bundled `Kaggle/kaggle-cli` `SKILL.md` merged with `kaggle-ext` guidance,
fetches individual reference topics, or exports a complete standalone skill
directory:

```bash
kaggle skill                       # augmented SKILL.md
kaggle skill --raw                 # unaugmented upstream SKILL.md
kaggle skill --list                # list available topics
kaggle skill --list --json         # machine-readable topic catalog
kaggle skill kernels               # print kernels reference (+ kaggle-ext notes)
kaggle skill references/auth.md    # path-style reference lookup also works
kaggle skill account               # multi-account reference
kaggle skill session               # interactive session & WebSocket reference
kaggle skill ext                   # extension & skill reference
kaggle skill --all                 # full SKILL.md + all references
kaggle skill --export ./kaggle-cli # write SKILL.md + references/ directory
```
"""

SESSION_REFERENCE_MD = """# Interactive Sessions & WebSocket Execution Reference (`kaggle-ext`)

Use `kaggle session` (alias: `kaggle sess`) to provision live interactive Kaggle
Jupyter containers and execute Python code or `.ipynb` notebooks over WebSocket
without persisting source code or secrets into cloud kernels.

## Prerequisites & Account Limits

- Authenticated Kaggle account (`kaggle account login <name>`). OAuth or `KGAT_*`
  tokens are supported (`api.authenticate()` runs per targeted account).
- Outbound container internet (`--internet`, default on) requires Kaggle phone
  verification on the account; pass `--no-internet` if unverified.
- Kaggle enforces weekly GPU/TPU quota (~30h/week), per-account concurrency
  limits, and ~40-60 minute idle container reclamation. Check remaining quota
  with `kaggle account status --json`.

## Command Hierarchy

```text
kaggle session | sess
├── create | up | start [NAME] [--accelerator SHAPE] [--internet | --no-internet]
│                              [--slug OWNER/SLUG] [--dataset OWNER/DATASET]
│                              [--startup-timeout SEC] [--force] [--json]
├── list | ls [--all-accounts] [--json]           # offline, secret-redacted
├── status | info [NAME] [--json]                 # HTTP /api/kernels liveness probe
├── exec | run [NAME] (-c CODE | -f FILE|-) [-e KEY[=VAL]] [--env-file FILE]
│                     [-t SEC] [--interrupt-on-cancel | --no-interrupt-on-cancel] [--json]
├── logs | tail [NAME] [-n LINES] [-f] [-t SEC] [--json]
├── watch | monitor [NAME] [-t SEC] [--json]
├── restart [NAME] [--json]
├── interrupt [NAME] [--json]
└── stop | down | cancel [NAME] [--all] [--force-local] [--json]
```

## Placeholder Kernel & Isolation Model

- **Private empty placeholder**: Because Kaggle requires an existing kernel
  entity before starting an interactive session, `kaggle session create` checks or
  creates `<username>/kaggle-ext-s-<name>` via `SaveKernel` (`is_private=True`,
  `KernelExecutionType.QUICK_SAVE`) containing only a single empty code cell.
- **Zero secret persistence in the cloud**: All `--env` / `--env-file` values are
  injected over the authenticated WebSocket in a preliminary `silent=True,
  store_history=False` frame before user cells execute, leaving IPython `In[]`
  history clean and keeping `--env` secrets out of process CLI arguments.
- **Per-account state**: Session records live in `0600` `sessions.json` inside each
  account's `0700` directory (`~/.config/kaggle-ext/accounts/<acct>/sessions.json`),
  locked via `fcntl.flock`. Both `jupyterUrl` path segments (`/k/<id>/<seg>/`)
  and query tokens (`?token=...`) are redacted in `list`, `status`, `ext doctor`,
  logs, and error output.
- **Pinned account routing**: If you omit `-a <account>` and a named session does
  not exist on the default active account, `kaggle-ext` resolves it uniquely
  across configured accounts so subsequent `exec`/`status`/`stop` stay pinned to
  the session's owner account.

## Hardware Accelerator Aliases (`--accelerator`)

| CLI Value | Kaggle `machine_shape` | Notes |
|---|---|---|
| `cpu`, `none` (default) | *(unset)* | CPU container, consumes no GPU/TPU quota |
| `gpu`, `t4`, `t4x2`, `NvidiaTeslaT4` | `NvidiaTeslaT4` | Default Kaggle GPU (2x T4) |
| `t4-highmem`, `NvidiaTeslaT4Highmem` | `NvidiaTeslaT4Highmem` | High-memory T4 GPU |
| `tpu`, `tpu-v5e-8`, `TpuV5E8` | `TpuV5E8` | TPU v5e-8 (`docker_image` omitted) |

Other valid PascalCase Kaggle shapes pass through unchanged; retired shapes such
as `NvidiaTeslaP100` or `TpuV38` emit a warning to stderr.

## Liveness, Monitoring & Safe Shutdown

- `kaggle session status` probes `GET {jupyter_url}/api/kernels` directly
  (`GetKernelSessionStatus` only tracks saved batch versions and reads `COMPLETE`
  even while an interactive container is alive). Exit code `1` indicates the
  session was reclaimed or is unreachable.
- `kaggle session watch` and `kaggle session logs -f` allocate a fresh monitor
  `session_id` UUID so attaching a read-only observer never disconnects an
  in-progress `kaggle session exec` WebSocket.
- `kaggle session stop` calls `CancelKernelSession` to halt quota billing and
  removes the local `sessions.json` entry **without** deleting the remote
  placeholder kernel. If the remote cancel request fails, the local entry is
  preserved for retry unless `--force-local` is passed.
"""

_TOPIC_NOTES: dict[str, str] = {
    "auth": """- Use `kaggle account login <name>` to store isolated credentials per profile instead
  of overwriting `~/.kaggle/`.
- Supports two-step headless PKCE (`--url-only` then `--code "<CODE_OR_URL>"`),
  `--no-launch-browser`, `--token` / `--token-file` / `--token-stdin`, and
  `--kaggle-json`.
- Linux pure-Wayland sessions (`WAYLAND_DISPLAY` set without `DISPLAY` when
  `xdg-open` is available) automatically open the browser for OAuth login.
- `kaggle-ext` also fixes upstream `KaggleCredentials.access_token_has_expired()`
  so OAuth tokens refresh cleanly 5 minutes before expiry instead of misreporting
  expired tokens as valid.""",
    "configuration": """- Each profile (`kaggle account ...` or `-a <name>`) resolves `KaggleApi.config_dir`
  to `${KAGGLE_EXT_CONFIG_DIR:-~/.config/kaggle-ext}/accounts/<name>/kaggle.json`.
- `KAGGLE_CONFIG_DIR` takes explicit precedence when set by the user unless an
  active `-a` / `KAGGLE_ACCOUNT` profile override redirects the call cleanly.
- Upstream `kagglesdk` hardcoded `~/.kaggle/` lookups (`access_token`,
  `credentials.json`, `_get_apikey_creds`) are all isolated per account.""",
    "kernels": """- Run kernel commands on a specific account without switching global default:
  `kaggle -a work kernels list -m` or `kaggle k push -p ./nb -a work`.
- For interactive live Jupyter sessions and cell-by-cell WebSocket execution, use
  `kaggle session` (`kaggle sess`, documented in `kaggle skill session`).
- When pushing `TpuV5E8` workloads, prefer `"kernel_type": "notebook"` and omit
  stale `"docker_image"` / `"docker_image_pinning_type"` pins; upstream Kaggle
  batch `SaveKernel` runs may still schedule a CPU image on some accounts, so
  verify the actual image in `kaggle kernels logs`.""",
    "quota": """- Run `kaggle account status` (or `--json`) to inspect auth status and weekly
  GPU/TPU remaining quota concurrently across **all** configured accounts.
- Run `kaggle quota -a <account>` to check a single profile's upstream table/CSV.""",
}


@dataclass(frozen=True)
class TopicSpec:
    """Metadata describing a CLI reference topic."""

    name: str
    title: str
    summary: str
    aliases: tuple[str, ...]
    filename: str
    upstream: bool


TOPIC_SPECS: tuple[TopicSpec, ...] = (
    TopicSpec(
        name="competitions",
        title="Competitions",
        summary="Competition discovery, files, downloads, submissions, leaderboards, simulations, pages, topics.",
        aliases=("c", "competition"),
        filename="competitions.md",
        upstream=True,
    ),
    TopicSpec(
        name="datasets",
        title="Datasets",
        summary="Dataset search, files, downloads, metadata, create/version/status/delete, topics.",
        aliases=("d", "dataset"),
        filename="datasets.md",
        upstream=True,
    ),
    TopicSpec(
        name="kernels",
        title="Kernels",
        summary="Notebook/script discovery, metadata, push/pull, outputs, status, logs, delete.",
        aliases=("k", "kernel", "notebooks", "notebook"),
        filename="kernels.md",
        upstream=True,
    ),
    TopicSpec(
        name="models",
        title="Models",
        summary="Model records, metadata, create/get/update/delete, model topics.",
        aliases=("m", "model"),
        filename="models.md",
        upstream=True,
    ),
    TopicSpec(
        name="model_variations",
        title="Model Variations",
        summary="Create and manage framework-specific model variations.",
        aliases=("model-variations", "model_variation", "model-variation", "variations", "variation", "instances"),
        filename="model_variations.md",
        upstream=True,
    ),
    TopicSpec(
        name="model_variations_versions",
        title="Model Variation Versions",
        summary="Create, list, download, inspect, and delete model variation versions.",
        aliases=(
            "model-variations-versions",
            "model_variation_versions",
            "model-variation-versions",
            "versions",
            "version",
        ),
        filename="model_variations_versions.md",
        upstream=True,
    ),
    TopicSpec(
        name="files",
        title="Files",
        summary="Inbox uploads, resumable uploads, directory compression behavior.",
        aliases=("file", "inbox"),
        filename="files.md",
        upstream=True,
    ),
    TopicSpec(
        name="forums",
        title="Forums",
        summary="Global discussion forums, topics, and comments.",
        aliases=("f", "forum", "discussions"),
        filename="forums.md",
        upstream=True,
    ),
    TopicSpec(
        name="benchmarks",
        title="Benchmarks",
        summary="Benchmark auth/init, task push/run/status/download/log/model flows, benchmark topics.",
        aliases=("b", "benchmark"),
        filename="benchmarks.md",
        upstream=True,
    ),
    TopicSpec(
        name="configuration",
        title="Configuration",
        summary="Config file, default path, proxy, default competition, per-account isolation.",
        aliases=("config", "cfg"),
        filename="configuration.md",
        upstream=True,
    ),
    TopicSpec(
        name="auth",
        title="Authentication",
        summary="OAuth login, access token printing, revocation, token/key sources.",
        aliases=("authentication", "login", "oauth"),
        filename="auth.md",
        upstream=True,
    ),
    TopicSpec(
        name="quota",
        title="Quota",
        summary="Weekly GPU/TPU accelerator quota and multi-account status checks.",
        aliases=("quotas", "gpu", "tpu"),
        filename="quota.md",
        upstream=True,
    ),
    TopicSpec(
        name="search",
        title="Search",
        summary="Unified cross-content search over competitions, datasets, notebooks, models, users, and discussions.",
        aliases=(),
        filename="search.md",
        upstream=True,
    ),
    TopicSpec(
        name="account",
        title="Account Management (kaggle-ext)",
        summary="Isolated multi-account switching (-a/--account), headless PKCE/token login, and parallel quota status.",
        aliases=("acct", "accounts", "multi-account"),
        filename="account.md",
        upstream=False,
    ),
    TopicSpec(
        name="session",
        title="Interactive Sessions & WebSocket Execution (kaggle-ext)",
        summary="Live Kaggle Jupyter sessions, private placeholder lifecycle, RFC 6455 WebSocket execution, env injection, and monitoring.",
        aliases=("sess", "sessions", "ws", "jupyter"),
        filename="session.md",
        upstream=False,
    ),
    TopicSpec(
        name="ext",
        title="Extension & Skill CLI (kaggle-ext)",
        summary="Offline doctor diagnostics, .pth wiring recovery, and skill documentation export.",
        aliases=("extension", "doctor"),
        filename="ext.md",
        upstream=False,
    ),
)

_EXTENSION_TOPIC_DOCS: dict[str, str] = {
    "account": ACCOUNT_REFERENCE_MD,
    "session": SESSION_REFERENCE_MD,
    "ext": EXT_REFERENCE_MD,
}

_TOPIC_BY_NAME: dict[str, TopicSpec] = {spec.name: spec for spec in TOPIC_SPECS}
_TOPIC_ALIASES: dict[str, str] = {}
for _spec in TOPIC_SPECS:
    _TOPIC_ALIASES[_spec.name.lower()] = _spec.name
    for _alias in _spec.aliases:
        _TOPIC_ALIASES[_alias.lower()] = _spec.name

_EXTENSION_TREE_REPLACEMENT = """├── search
├── account | acct
│   └── list, current, status, use, add, login, logout, rename, remove
├── session | sess
│   └── create|up, list|ls, status|info, exec|run, logs|tail, watch, restart, interrupt, stop|down
├── ext {doctor, status, install, uninstall, version}
└── skill [topic] [--list] [--all] [--raw] [--export DIR]"""


def _augment_frontmatter(md_text: str) -> str:
    """Idempotently enrich YAML frontmatter `description` with `kaggle-ext` scope."""
    normalized = md_text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return md_text

    end_idx = normalized.find("\n---\n", 4)
    if end_idx == -1:
        if normalized.endswith("\n---"):
            end_idx = len(normalized) - 4
        else:
            return md_text

    header = normalized[4:end_idx]
    body = normalized[end_idx + 4 :]
    lines = header.split("\n")

    new_lines: list[str] = []
    i = 0
    updated = False
    while i < len(lines):
        line = lines[i]
        m_block = re.match(r"^description:\s*([>|][+-]?)\s*$", line)
        if m_block and not updated:
            new_lines.append(line)
            i += 1
            block_indent = "  "
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or lines[i].strip() == ""):
                stripped = lines[i].lstrip(" \t")
                if stripped:
                    block_indent = lines[i][: len(lines[i]) - len(stripped)]
                new_lines.append(lines[i])
                i += 1
            if "kaggle-ext" not in "\n".join(new_lines):
                new_lines.append(f"{block_indent}{SKILL_FRONTMATTER_NOTE}")
            updated = True
            continue

        m_inline = re.match(r"^description:\s*(.+)$", line)
        if m_inline and not updated:
            raw_val = m_inline.group(1).strip()
            if "kaggle-ext" not in raw_val:
                if (raw_val.startswith('"') and raw_val.endswith('"')) or (
                    raw_val.startswith("'") and raw_val.endswith("'")
                ):
                    quote = raw_val[0]
                    inner = raw_val[1:-1].rstrip()
                    joined = f"{inner} {SKILL_FRONTMATTER_NOTE}".strip()
                    if quote == '"':
                        joined = joined.replace('"', '\\"')
                    else:
                        joined = joined.replace("'", "''")
                    new_lines.append(f"description: {quote}{joined}{quote}")
                else:
                    new_lines.append(f"description: {raw_val} {SKILL_FRONTMATTER_NOTE}")
            else:
                new_lines.append(line)
            updated = True
            i += 1
            continue

        new_lines.append(line)
        i += 1

    return "---\n" + "\n".join(new_lines) + "\n---" + body


def _augment_command_tree(md_text: str) -> str:
    """Append extension subcommands and `-a / --account` note to `Command Tree`."""
    if "├── account | acct" in md_text:
        return md_text

    marker = "## Command Tree"
    pos = md_text.find(marker)
    if pos == -1:
        return md_text

    before = md_text[:pos]
    section_and_after = md_text[pos:]
    if "└── search" not in section_and_after:
        return md_text

    section_and_after = section_and_after.replace("└── search", _EXTENSION_TREE_REPLACEMENT, 1)
    # Add global -a / --account note right after the command tree code fence if present.
    fence_end = section_and_after.find("\n```", section_and_after.find("└── skill"))
    if fence_end != -1:
        insert_at = fence_end + 4
        global_note = (
            "\n\nGlobal `kaggle-ext` account flag: `-a <account>` / `--account <account>` "
            "(valid before or after subcommands) or env `KAGGLE_ACCOUNT=<account>`."
        )
        section_and_after = section_and_after[:insert_at] + global_note + section_and_after[insert_at:]

    return before + section_and_after


def _augment_reference_map(md_text: str) -> str:
    """Append `account` and `ext` entries plus CLI retrieval tip to `## Reference Map`."""
    if "references/account.md" in md_text:
        return md_text

    marker = "## Reference Map"
    pos = md_text.find(marker)
    if pos == -1:
        return md_text

    next_heading = md_text.find("\n## ", pos + len(marker))
    if next_heading == -1:
        section = md_text[pos:]
        tail = ""
    else:
        section = md_text[pos:next_heading]
        tail = md_text[next_heading:]

    extra_lines = (
        "\n- [Account Management (kaggle-ext)](references/account.md) - isolated multi-account switching (`-a`/`--account`), headless PKCE/token login, and parallel GPU/TPU quota checks."
        "\n- [Interactive Sessions (kaggle-ext)](references/session.md) - live Kaggle Jupyter containers, private placeholder lifecycle, RFC 6455 WebSocket execution, and monitoring."
        "\n- [Extension & Skill CLI (kaggle-ext)](references/ext.md) - offline `kaggle ext doctor` diagnostics, `.pth` wiring recovery, and `kaggle skill` lookup/export."
        "\n\nCLI shortcut: read any topic directly without locating files on disk using "
        "`kaggle skill <topic>` (e.g. `kaggle skill kernels`, `kaggle skill references/auth.md`, "
        "`kaggle skill account`, `kaggle skill session`), list all topics with `kaggle skill --list`, or write a full "
        "directory bundle with `kaggle skill --export <dir>`."
    )
    section = section.rstrip("\n") + extra_lines + "\n"
    return md_text[:pos] + section + tail


def augment_skill_markdown(base_md: str) -> str:
    """Idempotently merge `kaggle-ext` metadata and documentation into `SKILL.md`."""
    if SKILL_MARKER in base_md:
        return base_md

    enriched = _augment_frontmatter(base_md)
    enriched = _augment_command_tree(enriched)
    enriched = _augment_reference_map(enriched)
    return enriched.rstrip() + "\n\n" + EXTENSION_SKILL_SECTION.rstrip() + "\n"


def _augment_topic_markdown(topic_name: str, base_md: str) -> str:
    note = _TOPIC_NOTES.get(topic_name)
    if not note or TOPIC_EXTENSION_MARKER in base_md:
        return base_md
    return base_md.rstrip() + "\n\n" + TOPIC_EXTENSION_MARKER + "\n\n" + note.strip() + "\n"


def load_base_skill_markdown() -> str:
    """Load the bundled upstream `SKILL.md`."""
    skill_path = SKILL_DATA_DIR / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(f"Bundled upstream SKILL.md not found: {skill_path}")
    return skill_path.read_text(encoding="utf-8")


def get_skill_markdown(*, raw: bool = False) -> str:
    """Return upstream (`raw=True`) or `kaggle-ext`-augmented `SKILL.md`."""
    base_md = load_base_skill_markdown()
    return base_md if raw else augment_skill_markdown(base_md)


def resolve_topic(query: str) -> str | None:
    """Resolve user/agent topic query (`kernels`, `k`, `references/kernels.md`) to canonical name."""
    cleaned = query.strip().replace("\\", "/")
    if not cleaned:
        return None
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("references/"):
        cleaned = cleaned[len("references/") :]
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    key = cleaned.strip().lower()
    return _TOPIC_ALIASES.get(key)


def list_topics(*, include_extension: bool = True) -> list[dict[str, object]]:
    """Return metadata records for all available reference topics."""
    items: list[dict[str, object]] = []
    for spec in TOPIC_SPECS:
        if not include_extension and not spec.upstream:
            continue
        items.append(
            {
                "topic": spec.name,
                "title": spec.title,
                "summary": spec.summary,
                "aliases": list(spec.aliases),
                "file": f"references/{spec.filename}",
                "source": "upstream" if spec.upstream else "kaggle-ext",
            }
        )
    return items


def get_topic_markdown(topic: str, *, raw: bool = False) -> tuple[str, str]:
    """Return `(canonical_topic, markdown_content)` for a topic query.

    Raises:
        KeyError: if the topic name/alias is unknown.
        ValueError: if `raw=True` is requested for a `kaggle-ext`-only topic.
        FileNotFoundError: if the bundled upstream reference file is missing.
    """
    canonical = resolve_topic(topic)
    if canonical is None:
        raise KeyError(topic)
    spec = _TOPIC_BY_NAME[canonical]

    if not spec.upstream:
        if raw:
            raise ValueError(
                f"Topic '{canonical}' is provided by kaggle-ext and has no upstream --raw file."
            )
        content = _EXTENSION_TOPIC_DOCS[canonical]
        return canonical, content.rstrip() + "\n"

    ref_path = REFERENCES_DATA_DIR / spec.filename
    if not ref_path.is_file():
        raise FileNotFoundError(f"Bundled reference file not found: {ref_path}")
    content = ref_path.read_text(encoding="utf-8")
    if not raw:
        content = _augment_topic_markdown(canonical, content)
    return canonical, content.rstrip() + "\n"


def get_all_skill_markdown(*, raw: bool = False) -> str:
    """Return `SKILL.md` followed by all reference documents concatenated."""
    parts = [get_skill_markdown(raw=raw).rstrip()]
    for spec in TOPIC_SPECS:
        if raw and not spec.upstream:
            continue
        _name, doc = get_topic_markdown(spec.name, raw=raw)
        parts.append(f"\n---\n<!-- reference: references/{spec.filename} -->\n\n{doc.rstrip()}")
    return "\n".join(parts) + "\n"


def export_skill_bundle(dest_dir: str, *, raw: bool = False) -> dict[str, object]:
    """Write `SKILL.md` and `references/*.md` into `dest_dir` safely."""
    target = Path(os.path.expanduser(dest_dir)).resolve()
    refs_out = target / "references"
    refs_out.mkdir(parents=True, exist_ok=True)

    skill_path = target / "SKILL.md"
    skill_path.write_text(get_skill_markdown(raw=raw), encoding="utf-8")

    written_files: list[str] = [str(skill_path)]
    for spec in TOPIC_SPECS:
        if raw and not spec.upstream:
            continue
        _name, doc = get_topic_markdown(spec.name, raw=raw)
        out_path = refs_out / spec.filename
        out_path.write_text(doc, encoding="utf-8")
        written_files.append(str(out_path))

    return {
        "destination": str(target),
        "raw": raw,
        "file_count": len(written_files),
        "files": written_files,
    }
