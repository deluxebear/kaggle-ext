"""CLI commands for interactive sessions: `kaggle session` (alias: `sess`)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import SESSION_HELP, cli_hooks, session_client, session_ws, sessions

SESSION_CHOICES = [
    "create",
    "up",
    "start",
    "list",
    "ls",
    "status",
    "info",
    "exec",
    "run",
    "logs",
    "tail",
    "watch",
    "monitor",
    "restart",
    "interrupt",
    "stop",
    "down",
    "cancel",
]


def _exit_error(exc: Exception | str, *, code: int = 1) -> None:
    print(f"[session] {sessions.redact_session_secrets(exc)}", file=sys.stderr)
    sys.exit(code)


def _render_watch_event_text(ev: dict[str, Any]) -> None:
    mtype = ev.get("msg_type")
    if mtype == "stream":
        text = str(ev.get("text") or "")
        if not text:
            return
        if ev.get("name") == "stderr":
            sys.stderr.write(text)
            sys.stderr.flush()
        else:
            sys.stdout.write(text)
            sys.stdout.flush()
    elif mtype in ("execute_result", "display_data"):
        text = ev.get("text")
        if isinstance(text, str) and text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()
    elif mtype == "error":
        tb = ev.get("traceback") or []
        rendered = "".join(str(line) for line in tb) if tb else f"{ev.get('ename')}: {ev.get('evalue')}\n"
        sys.stderr.write(rendered if rendered.endswith("\n") else rendered + "\n")
        sys.stderr.flush()
    elif mtype == "status":
        state = ev.get("execution_state")
        if state:
            print(f"[kernel] execution_state={state}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------------------


def _cmd_create(
    name: str = sessions.DEFAULT_SESSION_NAME,
    accelerator: str = "cpu",
    enable_internet: bool = True,
    slug_override: str | None = None,
    datasets: list[str] | None = None,
    startup_timeout: float = 300.0,
    force_new: bool = False,
    json_output: bool = False,
) -> None:
    try:
        rec, reused, warn_msg = session_client.create_or_reuse_session(
            name,
            accelerator=accelerator,
            enable_internet=enable_internet,
            slug_override=slug_override,
            datasets=datasets,
            startup_timeout=startup_timeout,
            force_new=force_new,
        )
    except Exception as exc:
        _exit_error(exc)
        return

    if warn_msg:
        print(f"[session] WARN: {warn_msg}", file=sys.stderr)

    sanitized = sessions.sanitize_session_record(name, rec)
    if json_output:
        print(json.dumps({**sanitized, "reused": reused}, indent=2))
        return

    action = "Reused live" if reused else "Started"
    print(
        f"[session] {action} session '{sanitized['name']}' "
        f"(account={sanitized['account']}, id={sanitized['kernel_session_id']}, "
        f"accel={sanitized['accelerator']}, internet={sanitized['enable_internet']})"
    )
    print(f"  placeholder : {sanitized['slug']}")
    print(f"  kernel_id   : {sanitized['kernel_id']}")
    print(f"  proxy       : {sanitized['proxy_url_redacted']}")


def _cmd_list(all_accounts: bool = False, json_output: bool = False) -> None:
    try:
        rows = sessions.list_all_sessions(all_accounts=all_accounts)
    except Exception as exc:
        _exit_error(exc)
        return

    if json_output:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("[session] No recorded sessions.")
        return

    for row in rows:
        print(
            f"{row['name']:<16} | acct={str(row.get('account') or '-'):<12} | "
            f"state={str(row.get('state') or '-'):<10} | id={str(row.get('kernel_session_id') or '-'):<10} | "
            f"accel={str(row.get('accelerator') or 'cpu'):<16} | slug={row.get('slug') or '-'}"
        )


def _cmd_status(name: str = sessions.DEFAULT_SESSION_NAME, json_output: bool = False) -> None:
    try:
        info = session_client.check_session_status(name)
    except Exception as exc:
        _exit_error(exc)
        return

    if json_output:
        print(json.dumps(info, indent=2))
    else:
        alive_str = "alive" if info["alive"] else f"not alive ({info['live_state']})"
        print(
            f"[session] '{info['name']}' (account={info['account']}, id={info['kernel_session_id']}) -> {alive_str}"
        )
        print(f"  slug            : {info['slug']}")
        print(f"  kernel_id       : {info['kernel_id']} (exec={info['execution_state'] or '-'})")
        print(f"  proxy           : {info['proxy_url_redacted'] or '-'}")
        if info.get("probe_error"):
            print(f"  probe_error     : {info['probe_error']}")

    if not info["alive"]:
        sys.exit(1)


def _cmd_exec(
    name: str = sessions.DEFAULT_SESSION_NAME,
    code: str | None = None,
    file_path: str | None = None,
    env_list: list[str] | None = None,
    env_file: str | None = None,
    timeout: float | None = None,
    interrupt_on_cancel: bool = True,
    json_output: bool = False,
) -> None:
    try:
        env_vars = session_ws.parse_env_inputs(env_list=env_list, env_file=env_file)
        cells = session_ws.load_execution_cells(code=code, file_path=file_path)
        acct_label, rec, kernel_id, headers = session_client.ensure_live_session_target(name)
    except Exception as exc:
        _exit_error(exc)
        return

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def _on_stdout(chunk: str) -> None:
        if json_output:
            out_chunks.append(chunk)
        else:
            sys.stdout.write(chunk)
            sys.stdout.flush()

    def _on_stderr(chunk: str) -> None:
        if json_output:
            err_chunks.append(chunk)
        else:
            sys.stderr.write(chunk)
            sys.stderr.flush()

    def _on_log(chunk: str) -> None:
        sessions.append_session_log(name, chunk, account=acct_label)

    try:
        res = session_ws.run_cells(
            rec["jupyter_url"],
            kernel_id,
            cells,
            client_session_id=rec.get("client_session_id"),
            env_vars=env_vars,
            headers=headers,
            timeout=timeout,
            interrupt_on_cancel=interrupt_on_cancel,
            stdout_sink=_on_stdout,
            stderr_sink=_on_stderr,
            log_sink=_on_log,
        )
    except session_ws.SessionExecutionTimeoutError as exc:
        if json_output:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": 124,
                        "session": name,
                        "account": acct_label,
                        "kernel_id": kernel_id,
                        "error": str(exc),
                        "stdout": sessions.redact_session_secrets("".join(out_chunks)),
                        "stderr": sessions.redact_session_secrets("".join(err_chunks)),
                    },
                    indent=2,
                )
            )
            sys.exit(124)
        _exit_error(exc, code=124)
        return
    except (session_ws.SessionAuthError, session_ws.SessionLostError) as exc:
        sessions.update_session_fields(name, account=acct_label, state="reclaimed")
        _exit_error(exc)
        return
    except Exception as exc:
        _exit_error(exc)
        return

    if json_output:
        print(
            json.dumps(
                {
                    "ok": res["ok"],
                    "exit_code": res["exit_code"],
                    "session": name,
                    "account": acct_label,
                    "kernel_id": kernel_id,
                    "cells_executed": res["cells_executed"],
                    "cells_total": res["cells_total"],
                    "failed_cell": res["failed_cell"],
                    "error": res["error"],
                    "stdout": sessions.redact_session_secrets("".join(out_chunks)),
                    "stderr": sessions.redact_session_secrets("".join(err_chunks)),
                },
                indent=2,
            )
        )
    elif not res["ok"] and res.get("error"):
        print(f"[session] Execution failed: {res['error']}", file=sys.stderr)

    if not res["ok"]:
        sys.exit(int(res.get("exit_code") or 1))


def _cmd_logs(
    name: str = sessions.DEFAULT_SESSION_NAME,
    lines: int = 100,
    follow: bool = False,
    timeout: float | None = None,
    json_output: bool = False,
) -> None:
    try:
        acct_label, rec = sessions.resolve_session_for_command(name, require_exists=True)
        assert rec is not None
        tail = sessions.read_session_log_tail(name, lines=lines, account=acct_label)
    except Exception as exc:
        _exit_error(exc)
        return

    if not follow:
        if json_output:
            print(json.dumps({"session": name, "account": acct_label, "lines": lines, "log": tail}, indent=2))
        elif tail:
            sys.stdout.write(tail if tail.endswith("\n") else tail + "\n")
        return

    if tail:
        if json_output:
            print(json.dumps({"msg_type": "history", "session": name, "account": acct_label, "log": tail}), flush=True)
        else:
            sys.stdout.write(tail if tail.endswith("\n") else tail + "\n")
            sys.stdout.flush()

    _watch_impl(name=name, timeout=timeout, json_output=json_output)


def _watch_impl(
    *,
    name: str,
    timeout: float | None,
    json_output: bool,
) -> None:
    try:
        acct_label, rec, kernel_id, headers = session_client.ensure_live_session_target(name)
    except Exception as exc:
        _exit_error(exc)
        return

    def _sink(ev: dict[str, Any]) -> None:
        if json_output:
            print(json.dumps(ev), flush=True)
        else:
            _render_watch_event_text(ev)

    try:
        session_ws.watch_kernel(
            rec["jupyter_url"],
            kernel_id,
            headers=headers,
            timeout=timeout,
            event_sink=_sink,
        )
    except KeyboardInterrupt:
        return
    except (session_ws.SessionAuthError, session_ws.SessionLostError) as exc:
        sessions.update_session_fields(name, account=acct_label, state="reclaimed")
        _exit_error(exc)
    except Exception as exc:
        _exit_error(exc)


def _cmd_watch(
    name: str = sessions.DEFAULT_SESSION_NAME,
    timeout: float | None = None,
    json_output: bool = False,
) -> None:
    _watch_impl(name=name, timeout=timeout, json_output=json_output)


def _cmd_restart(name: str = sessions.DEFAULT_SESSION_NAME, json_output: bool = False) -> None:
    try:
        acct_label, rec, kernel_id, headers = session_client.ensure_live_session_target(name)
        resp = session_ws.restart_kernel(rec["jupyter_url"], kernel_id, headers=headers)
    except Exception as exc:
        _exit_error(exc)
        return

    payload = {
        "session": name,
        "account": acct_label,
        "kernel_id": kernel_id,
        "restarted": True,
        "execution_state": resp.get("execution_state"),
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"[session] Restarted kernel {kernel_id} for '{name}' (account={acct_label})")


def _cmd_interrupt(name: str = sessions.DEFAULT_SESSION_NAME, json_output: bool = False) -> None:
    try:
        acct_label, rec, kernel_id, headers = session_client.ensure_live_session_target(name)
        session_ws.interrupt_kernel(rec["jupyter_url"], kernel_id, headers=headers)
    except Exception as exc:
        _exit_error(exc)
        return

    payload = {
        "session": name,
        "account": acct_label,
        "kernel_id": kernel_id,
        "interrupted": True,
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"[session] Interrupted kernel {kernel_id} for '{name}' (account={acct_label})")


def _cmd_stop(
    name: str | None = None,
    stop_all: bool = False,
    force_local: bool = False,
    json_output: bool = False,
) -> None:
    try:
        stopped = session_client.stop_sessions(
            session_name=name,
            stop_all=stop_all,
            force_local=force_local,
        )
    except Exception as exc:
        _exit_error(exc)
        return

    if json_output:
        print(json.dumps(stopped, indent=2))
        return

    if not stopped:
        print("[session] No sessions to stop.")
        return

    for item in stopped:
        note = f" ({item['note']})" if item.get("note") else ""
        print(
            f"[session] Stopped '{item['name']}' "
            f"(account={item['account']}, id={item['kernel_session_id']}, placeholder={item['slug']} kept){note}"
        )


# --------------------------------------------------------------------------------------
# Parser wiring (NOTE: NEVER bind `-a` on subcommands; `-a` is global account selector!)
# --------------------------------------------------------------------------------------


def attach_session_subparser(subparsers, _cli_mod=None) -> None:
    """Attach `kaggle session` (`sess` alias) to the root CLI subparsers."""
    name_map = getattr(subparsers, "_name_parser_map", {})
    if "session" in name_map:
        return

    parser_sess = subparsers.add_parser(
        "session",
        aliases=["sess"],
        formatter_class=argparse.RawTextHelpFormatter,
        help=SESSION_HELP,
    )
    # CRITICAL: nested subparsers MUST use `dest="command"` so `kaggle.cli:main` strips it.
    sub_sess = parser_sess.add_subparsers(title="commands", dest="command")
    sub_sess.required = True
    sub_sess.choices = list(SESSION_CHOICES)

    p_create = sub_sess.add_parser(
        "create",
        aliases=["up", "start"],
        help="Start or reuse a live interactive Kaggle Jupyter session",
    )
    p_create.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_create.add_argument(
        "--accelerator",
        dest="accelerator",
        default="cpu",
        help="Hardware shape: cpu, NvidiaTeslaT4 (t4), NvidiaTeslaT4Highmem, or TpuV5E8 (tpu)",
    )
    p_create.add_argument(
        "--internet",
        dest="enable_internet",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable outbound internet access in the container (default: true; requires phone verification)",
    )
    p_create.add_argument(
        "--slug",
        dest="slug_override",
        default=None,
        help="Optional existing placeholder kernel '<owner>/<slug>' instead of auto-creating one",
    )
    p_create.add_argument(
        "--dataset",
        dest="datasets",
        action="append",
        default=None,
        help="Attach Kaggle dataset '<owner>/<dataset-slug>' to the placeholder (repeatable)",
    )
    p_create.add_argument(
        "--startup-timeout",
        dest="startup_timeout",
        type=float,
        default=300.0,
        help="Max seconds to wait for CreateKernelSession operation completion (default: 300)",
    )
    p_create.add_argument(
        "--force",
        dest="force_new",
        action="store_true",
        help="Force creating a fresh session instead of reusing an existing live one",
    )
    p_create.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_create.set_defaults(func=_cmd_create)

    p_list = sub_sess.add_parser(
        "list",
        aliases=["ls"],
        help="List locally recorded interactive sessions (offline, secret-redacted)",
    )
    p_list.add_argument(
        "--all-accounts",
        dest="all_accounts",
        action="store_true",
        help="List recorded sessions across all configured accounts",
    )
    p_list.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_list.set_defaults(func=_cmd_list)

    p_status = sub_sess.add_parser(
        "status",
        aliases=["info"],
        help="Probe live Jupyter proxy health for a recorded session",
    )
    p_status.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_status.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_status.set_defaults(func=_cmd_status)

    p_exec = sub_sess.add_parser(
        "exec",
        aliases=["run"],
        help="Execute Python code or a .py/.ipynb file over Jupyter WebSocket",
    )
    p_exec.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_exec.add_argument("-c", "--code", dest="code", default=None, help="Inline Python snippet to execute")
    p_exec.add_argument(
        "-f",
        "--file",
        dest="file_path",
        default=None,
        help="Local .py/.ipynb file path (or '-' for stdin)",
    )
    p_exec.add_argument(
        "-e",
        "--env",
        dest="env_list",
        action="append",
        default=None,
        help="Inject environment variable KEY=VALUE or KEY from local env before running (repeatable)",
    )
    p_exec.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Read KEY=VALUE environment entries from a local file before running",
    )
    p_exec.add_argument(
        "-t",
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="Execution deadline in seconds (exits 124 on timeout)",
    )
    p_exec.add_argument(
        "--interrupt-on-cancel",
        dest="interrupt_on_cancel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Send Jupyter /interrupt on timeout (default: true)",
    )
    p_exec.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_exec.set_defaults(func=_cmd_exec)

    p_logs = sub_sess.add_parser(
        "logs",
        aliases=["tail"],
        help="Read local session execution logs or follow live kernel output",
    )
    p_logs.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_logs.add_argument(
        "-n",
        "--lines",
        dest="lines",
        type=int,
        default=100,
        help="Number of trailing lines to print (default: 100, 0 for all)",
    )
    p_logs.add_argument(
        "-f",
        "--follow",
        dest="follow",
        action="store_true",
        help="Follow live iopub output over a separate read-only WebSocket",
    )
    p_logs.add_argument(
        "-t",
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="Max seconds to follow in `--follow` mode",
    )
    p_logs.add_argument("--json", dest="json_output", action="store_true", help="Print JSON / JSONL output")
    p_logs.set_defaults(func=_cmd_logs)

    p_watch = sub_sess.add_parser(
        "watch",
        aliases=["monitor"],
        help="Attach a read-only WebSocket monitor to live kernel iopub events",
    )
    p_watch.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_watch.add_argument(
        "-t",
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="Max seconds to watch before returning cleanly",
    )
    p_watch.add_argument("--json", dest="json_output", action="store_true", help="Emit JSONL events")
    p_watch.set_defaults(func=_cmd_watch)

    p_restart = sub_sess.add_parser("restart", help="Restart the remote Jupyter kernel")
    p_restart.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_restart.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_restart.set_defaults(func=_cmd_restart)

    p_interrupt = sub_sess.add_parser("interrupt", help="Interrupt the currently executing cell")
    p_interrupt.add_argument(
        "name",
        nargs="?",
        default=sessions.DEFAULT_SESSION_NAME,
        help="Logical session name (default: 'default')",
    )
    p_interrupt.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_interrupt.set_defaults(func=_cmd_interrupt)

    p_stop = sub_sess.add_parser(
        "stop",
        aliases=["down", "cancel"],
        help="Cancel running Kaggle interactive session(s) while preserving placeholder kernel(s)",
    )
    p_stop.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Logical session name (defaults to 'default' unless --all is passed)",
    )
    p_stop.add_argument(
        "--all",
        dest="stop_all",
        action="store_true",
        help="Stop all recorded sessions for the resolved account",
    )
    p_stop.add_argument(
        "--force-local",
        dest="force_local",
        action="store_true",
        help="Remove local session entry even if remote cancellation fails",
    )
    p_stop.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    p_stop.set_defaults(func=_cmd_stop)


def register_session_commands() -> None:
    """Register `kaggle session` (`sess`) through `cli_hooks`."""
    cli_hooks.register_subparsers_hook(
        attach_session_subparser,
        command="session",
        choices=["session", "sess"],
        help_line="session {create, list, status, exec, logs, watch, restart, interrupt, stop}",
    )
