"""Control-plane lifecycle orchestration for `kaggle session` (`kaggle-ext`).

Lazily imports `kaggle` / `kagglesdk` only when executing remote control-plane calls
(creating private empty placeholder kernels, polling `CreateKernelSession` operations,
and cancelling interactive sessions).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import time
import uuid
from typing import Any, Iterator, Mapping

from . import accounts, session_ws, sessions

_OPERATION_ID_RE = re.compile(r"/(\d+)$")
_PROXY_ID_RE = re.compile(r"/k/(\d+)/")


@contextlib.contextmanager
def _open_authenticated_client(account_name: str) -> Iterator[tuple[str, str | None, Any]]:
    """Authenticate `account_name` inside an isolated context and yield `(username, ca_cert, client)`."""
    from kaggle.api.kaggle_api_extended import KaggleApi

    if account_name == "external":
        ctx_cm = contextlib.nullcontext("external")
    else:
        info = accounts.inspect_account_offline(account_name)
        ctx_cm = accounts.account_context(
            account_name,
            source="session",
            ignore_env_creds=bool(info.get("ready")),
        )

    with ctx_cm:
        api = KaggleApi()
        cap_out = io.StringIO()
        cap_err = io.StringIO()
        try:
            with contextlib.redirect_stdout(cap_out), contextlib.redirect_stderr(cap_err):
                api.authenticate()
        except (SystemExit, Exception) as exc:
            captured = (cap_out.getvalue() + "\n" + cap_err.getvalue()).strip()
            hint = f" ({sessions.redact_session_secrets(captured.splitlines()[0])})" if captured else ""
            if isinstance(exc, SystemExit):
                raise sessions.SessionError(
                    f"Authentication required for account {account_name!r}. "
                    f"Run `kaggle account login {account_name}` first."
                ) from exc
            raise sessions.SessionError(
                f"Authentication failed for account {account_name!r}: "
                f"{sessions.redact_session_secrets(exc)}{hint}"
            ) from exc

        username = (api.get_config_value(api.CONFIG_NAME_USER) or "").strip()
        if not username and account_name != "external":
            username = (accounts.inspect_account_offline(account_name).get("username") or "").strip()
        if not username:
            raise sessions.SessionError(
                f"Could not resolve Kaggle username for account {account_name!r}."
            )
        ca_cert = api.get_config_value(api.CONFIG_NAME_SSL_CA_CERT)
        with api.build_kaggle_client() as client:
            yield username, ca_cert, client


def _format_http_error(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        body = ""
        with contextlib.suppress(Exception):
            body = str(resp.text or "")[:400]
        safe_body = sessions.redact_session_secrets(body).strip()
        return f"HTTP {code}: {safe_body}" if safe_body else f"HTTP {code}: {sessions.redact_session_secrets(exc)}"
    return sessions.redact_session_secrets(exc)


def _extract_session_id_from_obj(obj: Any) -> int | None:
    if isinstance(obj, dict):
        for key in ("kernelSessionId", "kernel_session_id", "sessionId", "session_id"):
            val = obj.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                return val
            if isinstance(val, str) and val.strip().isdigit():
                return int(val.strip())
        for val in obj.values():
            found = _extract_session_id_from_obj(val)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _extract_session_id_from_obj(item)
            if found is not None:
                return found
    elif isinstance(obj, str):
        m = _PROXY_ID_RE.search(obj)
        if m:
            return int(m.group(1))
    return None


def _extract_session_id_from_op(op: Any) -> int | None:
    for part in (getattr(op, "metadata", None), getattr(op, "response", None)):
        found = _extract_session_id_from_obj(part)
        if found is not None:
            return found
    op_name = str(getattr(op, "name", "") or "")
    m = _OPERATION_ID_RE.search(op_name)
    if m:
        return int(m.group(1))
    return None


def _extract_proxy_credentials(op_response: Any) -> dict[str, Any]:
    if not isinstance(op_response, dict):
        return {}
    jupyter_url: str | None = None
    for key in ("jupyterUrl", "jupyter_url", "jupyterServerUrl", "jupyter_server_url", "proxyUrl", "url"):
        val = op_response.get(key)
        if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
            jupyter_url = val.strip().rstrip("/")
            break

    tokenized_url: str | None = None
    for key in ("tokenizedJupyterUrl", "tokenized_jupyter_url"):
        val = op_response.get(key)
        if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
            tokenized_url = val.strip()
            break

    proxy_token: str | None = None
    for key in ("proxyToken", "proxy_token", "token"):
        val = op_response.get(key)
        if isinstance(val, str) and val.strip():
            proxy_token = val.strip()
            break

    expires_at: str | None = None
    for key in ("tokenExpiresAt", "token_expires_at", "expiresAt", "expires_at"):
        val = op_response.get(key)
        if isinstance(val, str) and val.strip():
            expires_at = val.strip()
            break

    return {
        "jupyter_url": jupyter_url,
        "tokenized_jupyter_url": tokenized_url,
        "proxy_token": proxy_token,
        "proxy_token_expires_at": expires_at,
    }


def proxy_headers_for_record(rec: Mapping[str, Any]) -> dict[str, str]:
    tok = rec.get("proxy_token")
    if isinstance(tok, str) and tok.strip():
        return {"Authorization": f"Bearer {tok.strip()}"}
    return {}


def ensure_placeholder_kernel(
    client: Any,
    *,
    username: str,
    session_name: str,
    slug_override: str | None = None,
    enable_internet: bool = True,
    machine_shape: str | None = None,
    datasets: list[str] | None = None,
) -> str:
    """Ensure a private empty placeholder notebook exists on Kaggle without running code."""
    import requests
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiSaveKernelRequest
    from kagglesdk.kernels.types.kernels_enums import KernelExecutionType

    default_full_slug, kslug, title = sessions.placeholder_slug_and_title(username, session_name)
    owner = username
    if slug_override:
        owner, kslug = sessions.normalize_kernel_ref(slug_override, fallback=default_full_slug)
        words = re.sub(r"[^a-z0-9]+", " ", kslug.lower()).strip()
        title = words if len(words) >= 5 else f"kaggle ext {words or 'sess'}"

    target_slug = f"{owner}/{kslug}"
    if not datasets:
        greq = ApiGetKernelRequest()
        greq.user_name = owner
        greq.kernel_slug = kslug
        try:
            gresp = client.kernels.kernels_api_client.get_kernel(greq)
            if gresp and getattr(gresp, "metadata", None) is not None:
                ref = getattr(gresp.metadata, "ref", "") or target_slug
                norm_o, norm_s = sessions.normalize_kernel_ref(ref, fallback=target_slug)
                return f"{norm_o}/{norm_s}"
        except requests.exceptions.HTTPError as err:
            code = err.response.status_code if err.response is not None else None
            if code not in (403, 404):
                raise sessions.SessionError(f"Failed checking placeholder kernel {target_slug}: {_format_http_error(err)}") from err

    empty_ipynb = json.dumps(
        {
            "metadata": {
                "kernelspec": {
                    "language": "python",
                    "display_name": "Python 3",
                    "name": "python3",
                },
                "language_info": {"name": "python"},
            },
            "nbformat_minor": 4,
            "nbformat": 4,
            "cells": [
                {
                    "cell_type": "code",
                    "source": "",
                    "metadata": {},
                    "outputs": [],
                    "execution_count": None,
                }
            ],
        }
    )

    is_tpu = bool(machine_shape and machine_shape.lower().startswith("tpu"))
    is_gpu = bool(machine_shape and not is_tpu)
    sreq = ApiSaveKernelRequest()
    sreq.slug = target_slug
    sreq.new_title = title
    sreq.text = empty_ipynb
    sreq.language = "python"
    sreq.kernel_type = "notebook"
    sreq.is_private = True
    sreq.enable_gpu = is_gpu
    sreq.enable_tpu = is_tpu
    sreq.enable_internet = enable_internet
    if machine_shape:
        sreq.machine_shape = machine_shape
    if datasets:
        sreq.dataset_data_sources = list(datasets)
    sreq.kernel_execution_type = KernelExecutionType.QUICK_SAVE

    try:
        sresp = client.kernels.kernels_api_client.save_kernel(sreq)
    except requests.exceptions.HTTPError as err:
        raise sessions.SessionError(
            f"Failed creating private placeholder kernel {target_slug}: {_format_http_error(err)}"
        ) from err

    if getattr(sresp, "error", ""):
        raise sessions.SessionError(
            f"Placeholder kernel save returned error: {sessions.redact_session_secrets(sresp.error)}"
        )
    norm_o, norm_s = sessions.normalize_kernel_ref(getattr(sresp, "ref", "") or "", fallback=target_slug)
    return f"{norm_o}/{norm_s}"


def _best_effort_cancel(client: Any, kernel_session_id: int | None) -> bool:
    if kernel_session_id is None:
        return True
    from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest

    creq = ApiCancelKernelSessionRequest()
    creq.kernel_session_id = int(kernel_session_id)
    try:
        cresp = client.kernels.kernels_api_client.cancel_kernel_session(creq)
        msg = (getattr(cresp, "error_message", "") or "").strip().lower()
        return not msg or any(k in msg for k in ("not found", "not running", "already", "cancel"))
    except Exception:
        return False


def create_or_reuse_session(
    session_name: str = sessions.DEFAULT_SESSION_NAME,
    *,
    explicit_account: str | None = None,
    accelerator: str = "cpu",
    enable_internet: bool = True,
    slug_override: str | None = None,
    datasets: list[str] | None = None,
    startup_timeout: float = 300.0,
    force_new: bool = False,
) -> tuple[dict[str, Any], bool, str | None]:
    """Create a Kaggle interactive session or reuse an already-live session with the same name."""
    import requests
    from kagglesdk.kernels.types.kernels_api_service import ApiCreateKernelSessionRequest

    sessions.validate_session_name(session_name)
    machine_shape, canonical_accel, warn_msg = sessions.resolve_machine_shape(accelerator)

    if explicit_account is not None:
        acct_label = accounts.lookup_account(explicit_account, allow_create=False)
    else:
        acct_label, _ = sessions.sessions_dir_for_account(None, create=False)

    if not force_new:
        _lbl, existing = sessions.get_session(session_name, acct_label)
        if existing and isinstance(existing.get("jupyter_url"), str) and existing["jupyter_url"].strip():
            probe = session_ws.probe_proxy_liveness(
                existing["jupyter_url"],
                saved_kernel_id=existing.get("kernel_id"),
                headers=proxy_headers_for_record(existing),
                timeout=10.0,
            )
            if probe["alive"]:
                updates: dict[str, Any] = {"state": "active"}
                if probe.get("kernel_id") and probe["kernel_id"] != existing.get("kernel_id"):
                    updates["kernel_id"] = probe["kernel_id"]
                if not existing.get("client_session_id"):
                    updates["client_session_id"] = str(uuid.uuid4())
                saved = sessions.update_session_fields(session_name, account=acct_label, **updates) or existing
                return saved, True, warn_msg

    with _open_authenticated_client(acct_label) as (username, ca_cert, client):
        placeholder_slug = ensure_placeholder_kernel(
            client,
            username=username,
            session_name=session_name,
            slug_override=slug_override,
            enable_internet=enable_internet,
            machine_shape=machine_shape,
            datasets=datasets,
        )

        cs_req = ApiCreateKernelSessionRequest()
        cs_req.slug = placeholder_slug
        cs_req.language = "python"
        cs_req.kernel_type = "notebook"
        cs_req.enable_internet = bool(enable_internet)
        if machine_shape:
            cs_req.machine_shape = machine_shape

        try:
            op = client.kernels.kernels_api_client.create_kernel_session(cs_req)
        except requests.exceptions.HTTPError as err:
            raise sessions.SessionError(
                f"CreateKernelSession failed for {placeholder_slug}: {_format_http_error(err)}"
            ) from err

        ksid = _extract_session_id_from_op(op)
        op_name = str(getattr(op, "name", "") or "")
        _lbl, _ = sessions.upsert_session(
            session_name,
            {
                "username": username,
                "slug": placeholder_slug,
                "kernel_session_id": ksid,
                "state": "starting",
                "accelerator": canonical_accel,
                "machine_shape": machine_shape,
                "enable_internet": bool(enable_internet),
                "operation_name": op_name,
            },
            account=acct_label,
        )

        deadline = time.monotonic() + max(5.0, float(startup_timeout))
        while not getattr(op, "done", False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise sessions.SessionError(
                    f"Timed out after {startup_timeout:.0f}s waiting for session {session_name!r} to start "
                    f"(kernel_session_id={ksid}; run `kaggle -a {acct_label} session stop {session_name}` to cancel)."
                )
            time.sleep(min(2.5, remaining))
            try:
                op = client.common.operations_client.get_operation(name=op_name)
            except requests.exceptions.HTTPError as err:
                raise sessions.SessionError(
                    f"GetOperation failed while starting {session_name!r}: {_format_http_error(err)}"
                ) from err
            if ksid is None:
                ksid = _extract_session_id_from_op(op)
                if ksid is not None:
                    sessions.update_session_fields(session_name, account=acct_label, kernel_session_id=ksid)

        op_err = getattr(op, "error", None)
        if op_err is not None:
            cancelled = _best_effort_cancel(client, ksid)
            if cancelled:
                sessions.delete_session(session_name, account=acct_label)
            else:
                sessions.update_session_fields(session_name, account=acct_label, state="error")
            code = getattr(op_err, "code", None)
            msg = sessions.redact_session_secrets(getattr(op_err, "message", "") or "Operation failed")
            raise sessions.SessionError(f"CreateKernelSession operation failed (code {code}): {msg}")

        creds = _extract_proxy_credentials(getattr(op, "response", None))
        jupyter_url = creds.get("jupyter_url")
        if not jupyter_url:
            raise sessions.SessionError(
                "CreateKernelSession finished without returning a Jupyter proxy URL in Operation.response."
            )
        if ksid is None:
            ksid = _extract_session_id_from_obj(jupyter_url)

        extra_headers = (
            {"Authorization": f"Bearer {creds['proxy_token']}"} if creds.get("proxy_token") else None
        )
        try:
            kernel_id, _exec_state = session_ws.ensure_jupyter_kernel(
                jupyter_url,
                headers=extra_headers,
                timeout=20.0,
                ca_cert=ca_cert,
            )
        except Exception as exc:
            # Keep record so user can still `stop` or `watch/exec` once proxy finishes warming up.
            sessions.upsert_session(
                session_name,
                {
                    "username": username,
                    "slug": placeholder_slug,
                    "kernel_session_id": ksid,
                    "state": "active",
                    "accelerator": canonical_accel,
                    "machine_shape": machine_shape,
                    "enable_internet": bool(enable_internet),
                    "operation_name": op_name,
                    **creds,
                },
                account=acct_label,
            )
            raise sessions.SessionError(
                f"Session {session_name!r} started (id={ksid}) but Jupyter kernel probe failed: {exc}"
            ) from exc

        client_session_id = str(uuid.uuid4())
        _lbl, saved = sessions.upsert_session(
            session_name,
            {
                "username": username,
                "slug": placeholder_slug,
                "kernel_session_id": ksid,
                "state": "active",
                "accelerator": canonical_accel,
                "machine_shape": machine_shape,
                "enable_internet": bool(enable_internet),
                "operation_name": op_name,
                "kernel_id": kernel_id,
                "client_session_id": client_session_id,
                **creds,
            },
            account=acct_label,
        )
        return saved, False, warn_msg


def check_session_status(
    session_name: str = sessions.DEFAULT_SESSION_NAME,
    *,
    explicit_account: str | None = None,
) -> dict[str, Any]:
    """Check session liveness primarily via Jupyter proxy `/api/kernels`."""
    acct_label, rec = sessions.resolve_session_for_command(
        session_name, explicit_account, require_exists=True
    )
    assert rec is not None
    jupyter_url = rec.get("jupyter_url")
    if not isinstance(jupyter_url, str) or not jupyter_url.strip():
        return {
            **sessions.sanitize_session_record(session_name, rec, account=acct_label),
            "alive": False,
            "live_state": "missing_credentials",
            "execution_state": None,
            "probe_error": "No Jupyter proxy URL stored for this session.",
        }

    probe = session_ws.probe_proxy_liveness(
        jupyter_url,
        saved_kernel_id=rec.get("kernel_id"),
        headers=proxy_headers_for_record(rec),
        timeout=10.0,
    )
    if probe["alive"]:
        updates: dict[str, Any] = {}
        if probe.get("kernel_id") and probe["kernel_id"] != rec.get("kernel_id"):
            updates["kernel_id"] = probe["kernel_id"]
        if rec.get("state") != "active":
            updates["state"] = "active"
        if updates:
            rec = sessions.update_session_fields(session_name, account=acct_label, **updates) or rec
    elif probe["state"] == "reclaimed" and rec.get("state") == "active":
        rec = sessions.update_session_fields(session_name, account=acct_label, state="reclaimed") or rec

    return {
        **sessions.sanitize_session_record(session_name, rec, account=acct_label),
        "alive": bool(probe["alive"]),
        "live_state": probe["state"],
        "execution_state": probe["execution_state"],
        "probe_error": probe["error"],
    }


def ensure_live_session_target(
    session_name: str = sessions.DEFAULT_SESSION_NAME,
    *,
    explicit_account: str | None = None,
) -> tuple[str, dict[str, Any], str, dict[str, str]]:
    """Resolve a live session's `(account, record, kernel_id, proxy_headers)` for WebSocket/HTTP ops."""
    acct_label, rec = sessions.resolve_session_for_command(
        session_name, explicit_account, require_exists=True
    )
    assert rec is not None
    jupyter_url = rec.get("jupyter_url")
    if not isinstance(jupyter_url, str) or not jupyter_url.strip():
        raise sessions.SessionError(
            f"Session {session_name!r} has no stored Jupyter proxy URL (re-create with `kaggle session create {session_name} --force`)."
        )
    headers = proxy_headers_for_record(rec)
    try:
        kernel_id, _exec_state = session_ws.ensure_jupyter_kernel(
            jupyter_url,
            saved_kernel_id=rec.get("kernel_id"),
            headers=headers,
            timeout=20.0,
        )
    except (session_ws.SessionAuthError, session_ws.SessionLostError) as exc:
        sessions.update_session_fields(session_name, account=acct_label, state="reclaimed")
        raise sessions.SessionError(
            f"Session {session_name!r} is no longer reachable ({exc}). "
            f"Run `kaggle -a {acct_label} session stop {session_name}` or recreate it."
        ) from exc

    updates: dict[str, Any] = {}
    if kernel_id != rec.get("kernel_id"):
        updates["kernel_id"] = kernel_id
    if not rec.get("client_session_id"):
        updates["client_session_id"] = str(uuid.uuid4())
    if updates:
        rec = sessions.update_session_fields(session_name, account=acct_label, **updates) or rec
    return acct_label, rec, kernel_id, headers


def stop_session_one(
    acct_label: str,
    session_name: str,
    rec: Mapping[str, Any],
    *,
    force_local: bool = False,
) -> dict[str, Any]:
    """Cancel a running Kaggle interactive session without deleting its placeholder kernel."""
    import requests
    from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest

    ksid = rec.get("kernel_session_id")
    remote_cancelled = False
    cancel_note: str | None = None

    if ksid is not None:
        try:
            with _open_authenticated_client(acct_label) as (_user, _ca, client):
                creq = ApiCancelKernelSessionRequest()
                creq.kernel_session_id = int(ksid)
                cresp = client.kernels.kernels_api_client.cancel_kernel_session(creq)
                err_msg = (getattr(cresp, "error_message", "") or "").strip()
                if not err_msg:
                    remote_cancelled = True
                elif any(tok in err_msg.lower() for tok in ("not found", "not running", "already", "ended", "cancel")):
                    remote_cancelled = True
                    cancel_note = sessions.redact_session_secrets(err_msg)
                else:
                    raise sessions.SessionError(
                        f"CancelKernelSession({ksid}) returned error: {sessions.redact_session_secrets(err_msg)}"
                    )
        except requests.exceptions.HTTPError as err:
            code = err.response.status_code if err.response is not None else None
            if code == 404:
                remote_cancelled = True
                cancel_note = "already stopped (HTTP 404)"
            elif force_local:
                cancel_note = f"local-only removal after remote error ({_format_http_error(err)})"
            else:
                raise sessions.SessionError(
                    f"Failed cancelling session {session_name!r} (id={ksid}): {_format_http_error(err)}. "
                    "Local record preserved so you can retry; pass `--force-local` to discard locally."
                ) from err
        except sessions.SessionError as exc:
            if force_local:
                cancel_note = f"local-only removal after error ({exc})"
            else:
                raise
        except Exception as exc:
            if force_local:
                cancel_note = f"local-only removal after error ({sessions.redact_session_secrets(exc)})"
            else:
                raise sessions.SessionError(
                    f"Failed cancelling session {session_name!r} (id={ksid}): "
                    f"{sessions.redact_session_secrets(exc)}. Local record preserved for retry."
                ) from exc

    sessions.delete_session(session_name, account=acct_label)
    sanitized = sessions.sanitize_session_record(session_name, rec, account=acct_label)
    return {
        **sanitized,
        "stopped": True,
        "remote_cancelled": remote_cancelled,
        "note": cancel_note,
    }


def stop_sessions(
    *,
    session_name: str | None = None,
    stop_all: bool = False,
    explicit_account: str | None = None,
    force_local: bool = False,
) -> list[dict[str, Any]]:
    """Stop one named session or `--all` recorded sessions."""
    if stop_all:
        if session_name is not None:
            raise sessions.SessionError("Specify either a session name or `--all`, not both.")
        if explicit_account is not None:
            acct_label = accounts.lookup_account(explicit_account, allow_create=False)
            _lbl, s_map = sessions.load_sessions(acct_label)
            targets = [(acct_label, sname, rec) for sname, rec in sorted(s_map.items())]
        else:
            acct_label, s_map = sessions.load_sessions(None)
            targets = [(acct_label, sname, rec) for sname, rec in sorted(s_map.items())]
        if not targets:
            return []
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for tgt_acct, sname, rec in targets:
            try:
                results.append(stop_session_one(tgt_acct, sname, rec, force_local=force_local))
            except sessions.SessionError as exc:
                errors.append(f"{sname}: {exc}")
        if errors:
            raise sessions.SessionError("; ".join(errors))
        return results

    target_name = session_name or sessions.DEFAULT_SESSION_NAME
    acct_label, rec = sessions.resolve_session_for_command(
        target_name, explicit_account, require_exists=True
    )
    assert rec is not None
    return [stop_session_one(acct_label, target_name, rec, force_local=force_local)]
