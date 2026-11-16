"""CLI command added by `kaggle-ext`: `kaggle skill` (alias: `SKILL`)."""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import sys

from . import SKILL_HELP, cli_hooks, skill_doc

_WRAPPED_ATTR = "_kaggle_ext_wrapped_skill"
_OWNED_PARSER_ATTR = "_kaggle_ext_skill_parser"


def _cmd_skill(
    topic: str | None = None,
    list_topics: bool = False,
    all_topics: bool = False,
    raw: bool = False,
    export_dir: str | None = None,
    json_output: bool = False,
) -> None:
    if export_dir is not None:
        if topic is not None or list_topics or all_topics:
            print(
                "[skill] --export cannot be combined with a topic, --list, or --all.",
                file=sys.stderr,
            )
            sys.exit(2)
        result = skill_doc.export_skill_bundle(export_dir, raw=raw)
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            print(
                "[skill] Exported %d file(s) to %s"
                % (result["file_count"], result["destination"])
            )
        return

    if list_topics:
        if topic is not None or all_topics:
            print(
                "[skill] --list cannot be combined with a topic or --all.",
                file=sys.stderr,
            )
            sys.exit(2)
        items = skill_doc.list_topics(include_extension=not raw)
        if json_output:
            print(json.dumps(items, indent=2))
            return
        print("Available `kaggle skill` topics (`kaggle skill <topic>`):\n")
        for item in items:
            aliases = ", ".join(item["aliases"])  # type: ignore[arg-type]
            alias_str = f" (aliases: {aliases})" if aliases else ""
            src_tag = f" [{item['source']}]"
            print(f"  {item['topic']:<26}{src_tag:<14}{alias_str}")
            print(f"      {item['summary']}")
        return

    if all_topics:
        if topic is not None:
            print(
                "[skill] --all cannot be combined with a positional topic.",
                file=sys.stderr,
            )
            sys.exit(2)
        content = skill_doc.get_all_skill_markdown(raw=raw)
        if json_output:
            print(json.dumps({"raw": raw, "content": content}, indent=2))
        else:
            sys.stdout.write(content)
        return

    if topic is not None:
        try:
            canonical, content = skill_doc.get_topic_markdown(topic, raw=raw)
        except KeyError:
            print(
                f"[skill] Unknown topic '{topic}'. Run `kaggle skill --list` to see available topics.",
                file=sys.stderr,
            )
            sys.exit(2)
        except ValueError as exc:
            print(f"[skill] {exc}", file=sys.stderr)
            sys.exit(2)

        if json_output:
            print(json.dumps({"topic": canonical, "raw": raw, "content": content}, indent=2))
        else:
            sys.stdout.write(content)
        return

    content = skill_doc.get_skill_markdown(raw=raw)
    if json_output:
        print(
            json.dumps(
                {
                    "name": "kaggle-cli",
                    "raw": raw,
                    "content": content,
                    "topics": skill_doc.list_topics(include_extension=not raw),
                },
                indent=2,
            )
        )
    else:
        sys.stdout.write(content)


def _wrap_upstream_skill_parser(existing_parser) -> None:
    """Future-proof wrapper if an upstream `kaggle` release adds `kaggle skill`."""
    orig_func = existing_parser.get_default("func")
    if orig_func is None or getattr(orig_func, _WRAPPED_ATTR, False):
        return

    @functools.wraps(orig_func)
    def _augmented_upstream_skill(*args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = orig_func(*args, **kwargs)
        output = buf.getvalue()
        if output:
            sys.stdout.write(skill_doc.augment_skill_markdown(output))
        return result

    setattr(_augmented_upstream_skill, _WRAPPED_ATTR, True)
    existing_parser.set_defaults(func=_augmented_upstream_skill)


def attach_skill_subparser(subparsers, _cli_mod=None) -> None:
    """Attach `kaggle skill` (alias `SKILL`) to the root subparsers."""
    name_map = getattr(subparsers, "_name_parser_map", {})
    existing = name_map.get("skill")
    if existing is not None:
        if not getattr(existing, _OWNED_PARSER_ATTR, False):
            _wrap_upstream_skill_parser(existing)
        return

    parser_skill = subparsers.add_parser(
        "skill",
        aliases=["SKILL"],
        formatter_class=argparse.RawTextHelpFormatter,
        help=SKILL_HELP,
    )
    setattr(parser_skill, _OWNED_PARSER_ATTR, True)

    # CRITICAL: `kaggle.cli:main` strips only `command`, `func`, `disable_version_warning`,
    # and `debug`. Every argument `dest` below MUST match a parameter of `_cmd_skill`.
    parser_skill.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Optional reference topic (e.g. competitions, datasets, kernels, auth, account, ext)",
    )
    parser_skill.add_argument(
        "-l",
        "--list",
        dest="list_topics",
        action="store_true",
        help="List available skill reference topics",
    )
    parser_skill.add_argument(
        "--all",
        dest="all_topics",
        action="store_true",
        help="Print SKILL.md followed by all reference documents",
    )
    parser_skill.add_argument(
        "--raw",
        dest="raw",
        action="store_true",
        help="Print unaugmented upstream SKILL.md or upstream-only references",
    )
    parser_skill.add_argument(
        "--export",
        dest="export_dir",
        metavar="DIR",
        default=None,
        help="Export SKILL.md and references/ directory into DIR",
    )
    parser_skill.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output JSON (works with --list, --export, --all, topic, or default)",
    )
    parser_skill.set_defaults(func=_cmd_skill)


def register_skill_commands() -> None:
    """Register `kaggle skill` through `cli_hooks`."""
    cli_hooks.register_subparsers_hook(
        attach_skill_subparser,
        command="skill",
        choices=["skill", "SKILL"],
        help_line="skill [topic] [--list] [--all] [--export DIR]",
    )
