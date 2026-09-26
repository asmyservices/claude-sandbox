"""Host-side queue processor.

The container drops request files into inbox/requests/. Each run drains that
directory completely (the systemd path unit re-fires while it's non-empty),
runs allowlisted actions, and writes results/<id>.json. Actions marked
`approval: true` are parked in pending/ until `sandbox approve <id>`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Action, Config
from .validation import ID_RE, RequestError, build_argv, builtin_values, validate_request

CATALOG_NAME = "_catalog.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(message, flush=True)


def ensure_dirs(cfg: Config) -> None:
    for path in (cfg.requests_dir, cfg.tmp_dir, cfg.results_dir, cfg.pending_dir):
        path.mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_catalog(cfg: Config) -> None:
    actions = {}
    for name, action in sorted(cfg.actions.items()):
        args = {}
        for arg, spec in action.args.items():
            info: Dict[str, Any] = {"type": spec.kind}
            if spec.kind == "choices":
                info["choices"] = spec.choices
            elif spec.kind == "integer":
                info.update({k: v for k, v in (("min", spec.min), ("max", spec.max)) if v is not None})
            else:
                info["max_bytes"] = spec.max_bytes
            if spec.has_default:
                info["default"] = spec.default
            args[arg] = info
        actions[name] = {
            "description": action.description,
            "approval": action.approval,
            "timeout_seconds": action.timeout or cfg.broker["timeout_seconds"],
            "args": args,
        }
    catalog = {
        "name": cfg.name,
        "actions": actions,
        "mounts": [{"path": m.container, "mode": m.mode} for m in cfg.mounts],
    }
    _write_json_atomic(cfg.results_dir / CATALOG_NAME, catalog)


def _summarize_args(action: Optional[Action], values: Dict[str, str]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key, value in values.items():
        spec = action.args.get(key) if action else None
        if spec is not None and spec.kind == "text":
            summary[key] = {
                "bytes": len(value.encode("utf-8")),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        else:
            summary[key] = value
    return summary


def audit(cfg: Config, entry: Dict[str, Any]) -> None:
    entry = dict(entry, time=now_iso())
    with open(cfg.audit_log, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def notify(cfg: Config, text: str) -> None:
    url = cfg.broker.get("approval_webhook_url")
    if not url:
        return
    body = json.dumps({"content": text[:1900]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:  # a failed notification must not block the queue
        log(f"approval webhook failed: {exc}")


def _remove_entry(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        os.unlink(path)


def read_request_file(path: Path, max_bytes: int) -> Any:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RequestError("request is not a regular file")
        if st.st_size > max_bytes:
            raise RequestError(f"request is larger than {max_bytes} bytes")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RequestError(f"request is larger than {max_bytes} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RequestError("request is not valid JSON")


def _truncate(output: bytes, limit: int) -> str:
    text = output.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[-limit:].decode("utf-8", errors="ignore") + "\n[output truncated to last %d bytes]" % limit


def execute(cfg: Config, action: Action, values: Dict[str, str]) -> Dict[str, Any]:
    home = os.path.expanduser("~")
    argv = build_argv(action, values, builtin_values(home))
    stdin_data = values[action.stdin].encode("utf-8") if action.stdin else None
    timeout = action.timeout or cfg.broker["timeout_seconds"]
    limit = cfg.broker["max_output_bytes"]
    started = now_iso()
    result: Dict[str, Any] = {"started_at": started}
    try:
        proc = subprocess.run(
            argv,
            input=stdin_data,
            stdin=None if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=home,
        )
        result.update(
            status="ok" if proc.returncode == 0 else "error",
            exit_code=proc.returncode,
            output=_truncate(proc.stdout or b"", limit),
        )
    except subprocess.TimeoutExpired as exc:
        result.update(status="timeout", exit_code=None,
                      output=_truncate(exc.output or b"", limit) + f"\n[timed out after {timeout}s]")
    except OSError as exc:
        result.update(status="error", exit_code=None, output=f"could not run command: {exc}")
    result["finished_at"] = now_iso()
    return result


def write_result(cfg: Config, req_id: str, action_name: Optional[str], values: Dict[str, str],
                 action: Optional[Action], outcome: Dict[str, Any]) -> None:
    result = {
        "id": req_id,
        "action": action_name,
        "args": _summarize_args(action, values),
    }
    result.update(outcome)
    _write_json_atomic(cfg.results_dir / f"{req_id}.json", result)
    audit(cfg, {
        "id": req_id,
        "action": action_name,
        "args": result["args"],
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
    })


def _id_in_use(cfg: Config, req_id: str) -> bool:
    return ((cfg.results_dir / f"{req_id}.json").exists()
            or (cfg.pending_dir / f"{req_id}.json").exists())


def process_request(cfg: Config, req_id: str, data: Any) -> None:
    try:
        action, values = validate_request(cfg, data)
    except RequestError as exc:
        name = data.get("action") if isinstance(data, dict) and isinstance(data.get("action"), str) else None
        write_result(cfg, req_id, name, {}, None, {"status": "rejected", "error": str(exc)})
        log(f"{req_id}: rejected: {exc}")
        return

    if action.approval:
        _write_json_atomic(cfg.pending_dir / f"{req_id}.json", {
            "id": req_id,
            "action": action.name,
            "args": values,
            "requested_at": now_iso(),
        })
        write_result(cfg, req_id, action.name, values, action, {
            "status": "pending",
            "message": "Waiting for approval on the host (`sandbox approve " + req_id + "`).",
        })
        summary = json.dumps(_summarize_args(action, values), sort_keys=True)
        notify(cfg, f"**{cfg.name}**: approval needed for `{action.name}` {summary}\n"
                    f"Approve: `sandbox approve {req_id}` · Deny: `sandbox deny {req_id}`")
        log(f"{req_id}: {action.name} waiting for approval")
        return

    outcome = execute(cfg, action, values)
    write_result(cfg, req_id, action.name, values, action, outcome)
    log(f"{req_id}: {action.name} -> {outcome['status']}")


def handle_entry(cfg: Config, path: Path) -> None:
    name = path.name
    req_id = name[:-5] if name.endswith(".json") else None
    if req_id is None or not ID_RE.match(req_id):
        _remove_entry(path)
        audit(cfg, {"id": None, "status": "rejected", "error": f"invalid entry name {name[:80]!r}"})
        log(f"removed invalid queue entry {name[:80]!r}")
        return

    try:
        data = read_request_file(path, cfg.broker["max_request_bytes"])
    except (RequestError, OSError) as exc:
        _remove_entry(path)
        if not _id_in_use(cfg, req_id):
            write_result(cfg, req_id, None, {}, None, {"status": "rejected", "error": str(exc)})
        log(f"{req_id}: rejected: {exc}")
        return
    _remove_entry(path)

    if _id_in_use(cfg, req_id):
        audit(cfg, {"id": req_id, "status": "rejected", "error": "duplicate request id"})
        log(f"{req_id}: rejected: duplicate request id")
        return
    process_request(cfg, req_id, data)


def prune(cfg: Config) -> None:
    cutoff = time.time() - cfg.broker["result_retention_days"] * 86400
    for entry in os.scandir(cfg.results_dir):
        if entry.name == CATALOG_NAME:
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass
    stale = time.time() - 3600
    for entry in os.scandir(cfg.tmp_dir):
        try:
            if entry.stat(follow_symlinks=False).st_mtime < stale:
                _remove_entry(Path(entry.path))
        except OSError:
            pass


def run_queue(cfg: Config, max_passes: int = 100) -> int:
    ensure_dirs(cfg)
    write_catalog(cfg)
    prune(cfg)
    handled = 0
    seen = set()
    for _ in range(max_passes):
        entries = sorted(os.scandir(cfg.requests_dir), key=lambda e: e.name)
        fresh = [e for e in entries if e.name not in seen]
        if not fresh:
            break
        for entry in fresh:
            seen.add(entry.name)
            try:
                handle_entry(cfg, Path(entry.path))
            except Exception as exc:
                log(f"error handling {entry.name[:80]!r}: {exc}")
                try:
                    _remove_entry(Path(entry.path))
                except OSError:
                    pass
            handled += 1
    return handled


def list_pending(cfg: Config) -> list:
    ensure_dirs(cfg)
    items = []
    for path in sorted(cfg.pending_dir.glob("*.json")):
        try:
            items.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return items


def load_pending(cfg: Config, req_id: str) -> Dict[str, Any]:
    if not ID_RE.match(req_id):
        raise RequestError(f"invalid id {req_id!r}")
    path = cfg.pending_dir / f"{req_id}.json"
    if not path.is_file():
        raise RequestError(f"no pending request {req_id!r}")
    return json.loads(path.read_text())


def approve(cfg: Config, req_id: str) -> Dict[str, Any]:
    pending = load_pending(cfg, req_id)
    (cfg.pending_dir / f"{req_id}.json").unlink()
    try:
        action, values = validate_request(cfg, {"action": pending["action"], "args": pending["args"]})
    except RequestError as exc:
        outcome = {"status": "rejected", "error": f"no longer valid under the current config: {exc}"}
        write_result(cfg, req_id, pending.get("action"), {}, None, outcome)
        return outcome
    outcome = execute(cfg, action, values)
    outcome["approved_at"] = now_iso()
    write_result(cfg, req_id, action.name, values, action, outcome)
    return outcome


def deny(cfg: Config, req_id: str, reason: str = "") -> Dict[str, Any]:
    pending = load_pending(cfg, req_id)
    (cfg.pending_dir / f"{req_id}.json").unlink()
    action = cfg.actions.get(pending.get("action"))
    values = pending.get("args") or {}
    outcome = {"status": "denied", "error": reason or "Denied on the host."}
    write_result(cfg, req_id, pending.get("action"), values, action, outcome)
    return outcome


def main_broker(cfg: Config) -> int:
    handled = run_queue(cfg)
    if handled:
        log(f"processed {handled} queue entr{'y' if handled == 1 else 'ies'}")
    return 0
