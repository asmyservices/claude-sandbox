"""Loads and validates config.yaml."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ARG_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
BUILTIN_PLACEHOLDERS = ("home",)
RESERVED_CONTAINER_PATHS = ("/queue", "/etc/claude-code", "/home/agent", "/run/secrets")


class ConfigError(Exception):
    pass


@dataclass
class ArgSpec:
    name: str
    kind: str
    choices: Optional[List[str]] = None
    min: Optional[int] = None
    max: Optional[int] = None
    max_bytes: Optional[int] = None
    default: Any = None
    has_default: bool = False


@dataclass
class Action:
    name: str
    description: str
    command: List[str]
    args: Dict[str, ArgSpec]
    approval: bool = False
    stdin: Optional[str] = None
    timeout: Optional[int] = None


@dataclass
class Mount:
    host: Path
    container: str
    mode: str


@dataclass
class Config:
    path: Path
    root: Path
    name: str
    model: Optional[str]
    permission_mode: str
    claude_code_version: str
    container: Dict[str, Any]
    state_dir: Path
    workspace_dir: Path
    queue_dir: Path
    mounts: List[Mount]
    git: Dict[str, Any]
    broker: Dict[str, Any]
    actions: Dict[str, Action] = field(default_factory=dict)

    @property
    def inbox_dir(self) -> Path:
        return self.queue_dir / "inbox"

    @property
    def requests_dir(self) -> Path:
        return self.inbox_dir / "requests"

    @property
    def tmp_dir(self) -> Path:
        return self.inbox_dir / "tmp"

    @property
    def results_dir(self) -> Path:
        return self.queue_dir / "results"

    @property
    def pending_dir(self) -> Path:
        return self.queue_dir / "pending"

    @property
    def audit_log(self) -> Path:
        return self.queue_dir / "audit.log"

    @property
    def generated_dir(self) -> Path:
        return self.root / ".generated"

    @property
    def unit_prefix(self) -> str:
        return f"claude-sandbox-{self.name}"


DEFAULT_CONTAINER = {
    "user": "auto",
    "memory": None,
    "cpus": None,
    "apt_packages": [],
    "python_packages": [],
    "environment": {},
}
DEFAULT_PATHS = {
    "state": "./data/state",
    "workspace": "./data/workspace",
    "queue": "./data/queue",
}
DEFAULT_GIT = {
    "ssh_key": None,
    "name": "Claude Sandbox",
    "email": "claude-sandbox@localhost",
}
DEFAULT_BROKER = {
    "timeout_seconds": 60,
    "max_output_bytes": 65536,
    "max_request_bytes": 262144,
    "result_retention_days": 7,
    "approval_webhook_url": None,
}
TOP_LEVEL_KEYS = {
    "name", "model", "permission_mode", "claude_code_version", "container",
    "paths", "mounts", "git", "broker", "actions",
}


def _merge_section(raw: Any, defaults: Dict[str, Any], section: str) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{section}: must be a mapping")
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ConfigError(f"{section}: unknown key(s): {', '.join(sorted(unknown))}")
    merged = dict(defaults)
    merged.update(raw)
    return merged


def _resolve_path(value: str, root: Path) -> Path:
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute():
        path = root / path
    return Path(os.path.normpath(str(path)))


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: must be a positive integer")
    return value


def _parse_arg(action: str, name: str, raw: Any) -> ArgSpec:
    where = f"actions.{action}.args.{name}"
    if not ARG_RE.match(name) or name in BUILTIN_PLACEHOLDERS:
        raise ConfigError(f"{where}: invalid argument name")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping")
    kinds = [k for k in ("choices", "integer", "text") if k in raw]
    if len(kinds) != 1:
        raise ConfigError(f"{where}: needs exactly one of choices, integer, text")
    unknown = set(raw) - {"choices", "integer", "text", "default"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    kind = kinds[0]
    spec = ArgSpec(name=name, kind=kind)

    if kind == "choices":
        choices = raw["choices"]
        if (not isinstance(choices, list) or not choices
                or not all(isinstance(c, str) and c for c in choices)):
            raise ConfigError(f"{where}.choices: must be a non-empty list of strings")
        spec.choices = list(choices)
    elif kind == "integer":
        bounds = raw["integer"] or {}
        if not isinstance(bounds, dict) or set(bounds) - {"min", "max"}:
            raise ConfigError(f"{where}.integer: must be a mapping with min and/or max")
        for key in ("min", "max"):
            value = bounds.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ConfigError(f"{where}.integer.{key}: must be an integer")
        spec.min = bounds.get("min")
        spec.max = bounds.get("max")
    else:
        opts = raw["text"] or {}
        if not isinstance(opts, dict) or set(opts) - {"max_bytes"}:
            raise ConfigError(f"{where}.text: must be a mapping with max_bytes")
        spec.max_bytes = _positive_int(opts.get("max_bytes", 65536), f"{where}.text.max_bytes")

    if "default" in raw:
        spec.default = raw["default"]
        spec.has_default = True
    return spec


def _parse_action(name: str, raw: Any) -> Action:
    where = f"actions.{name}"
    if not ACTION_RE.match(name):
        raise ConfigError(f"{where}: action names use lowercase letters, digits and dashes")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping")
    unknown = set(raw) - {"description", "command", "args", "approval", "stdin", "timeout"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")

    command = raw.get("command")
    if (not isinstance(command, list) or not command
            or not all(isinstance(t, (str, int)) for t in command)):
        raise ConfigError(f"{where}.command: must be a non-empty list of strings")
    command = [str(t) for t in command]

    raw_args = raw.get("args") or {}
    if not isinstance(raw_args, dict):
        raise ConfigError(f"{where}.args: must be a mapping")
    args = {arg: _parse_arg(name, arg, spec) for arg, spec in raw_args.items()}

    stdin = raw.get("stdin")
    if stdin is not None:
        if stdin not in args or args[stdin].kind != "text":
            raise ConfigError(f"{where}.stdin: must name a text argument")

    used = set()
    for token in command:
        for ref in PLACEHOLDER_RE.findall(token):
            if ref in BUILTIN_PLACEHOLDERS:
                continue
            if ref not in args:
                raise ConfigError(f"{where}.command: {{{ref}}} is not a defined argument")
            if args[ref].kind == "text":
                raise ConfigError(f"{where}.command: text argument {{{ref}}} can only be used as stdin")
            used.add(ref)
    if stdin:
        used.add(stdin)
    unused = set(args) - used
    if unused:
        raise ConfigError(f"{where}: argument(s) never used: {', '.join(sorted(unused))}")

    approval = raw.get("approval", False)
    if not isinstance(approval, bool):
        raise ConfigError(f"{where}.approval: must be true or false")
    timeout = raw.get("timeout")
    if timeout is not None:
        timeout = _positive_int(timeout, f"{where}.timeout")

    action = Action(
        name=name,
        description=str(raw.get("description") or ""),
        command=command,
        args=args,
        approval=approval,
        stdin=stdin,
        timeout=timeout,
    )
    for spec in args.values():
        if spec.has_default:
            from .validation import RequestError, validate_value
            try:
                validate_value(spec, spec.default)
            except RequestError as exc:
                raise ConfigError(f"{where}.args.{spec.name}.default: {exc}")
    return action


def _parse_mounts(raw: Any, root: Path) -> List[Mount]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("mounts: must be a list")
    mounts = []
    for i, item in enumerate(raw):
        where = f"mounts[{i}]"
        if not isinstance(item, dict) or set(item) - {"host", "container", "mode"}:
            raise ConfigError(f"{where}: must be a mapping with host, container and mode")
        if not item.get("host") or not item.get("container"):
            raise ConfigError(f"{where}: host and container are required")
        container = os.path.normpath(str(item["container"]))
        if not container.startswith("/") or container == "/":
            raise ConfigError(f"{where}.container: must be an absolute path")
        for reserved in RESERVED_CONTAINER_PATHS:
            if container == reserved or container.startswith(reserved + "/"):
                raise ConfigError(f"{where}.container: {reserved} is reserved")
        mode = item.get("mode", "ro")
        if mode not in ("ro", "rw"):
            raise ConfigError(f"{where}.mode: must be ro or rw")
        mounts.append(Mount(host=_resolve_path(item["host"], root), container=container, mode=mode))
    return mounts


def load_config(path: Path) -> Config:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"{path}: not found (copy config.example.yaml to get started)")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: must be a mapping")
    raw = {k: v for k, v in raw.items() if not str(k).startswith("x-")}

    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    root = path.parent
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError("name: required; lowercase letters, digits and dashes only")

    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigError("model: must be a string")
    permission_mode = raw.get("permission_mode", "bypassPermissions")
    if permission_mode not in PERMISSION_MODES:
        raise ConfigError(f"permission_mode: must be one of {', '.join(PERMISSION_MODES)}")
    version = str(raw.get("claude_code_version", "latest"))
    if not re.match(r"^[A-Za-z0-9.\-]+$", version):
        raise ConfigError("claude_code_version: invalid version")

    container = _merge_section(raw.get("container"), DEFAULT_CONTAINER, "container")
    for key in ("apt_packages", "python_packages"):
        pkgs = container[key] or []
        if not isinstance(pkgs, list) or not all(
                isinstance(p, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9._+\-<>=\[\],]*$", p) for p in pkgs):
            raise ConfigError(f"container.{key}: must be a list of package names")
        container[key] = pkgs
    if not isinstance(container["environment"] or {}, dict):
        raise ConfigError("container.environment: must be a mapping")
    container["environment"] = {str(k): str(v) for k, v in (container["environment"] or {}).items()}
    user = str(container["user"])
    if user != "auto" and not re.match(r"^\d+(:\d+)?$", user):
        raise ConfigError('container.user: "auto" or uid[:gid]')
    container["user"] = user

    paths = _merge_section(raw.get("paths"), DEFAULT_PATHS, "paths")
    git = _merge_section(raw.get("git"), DEFAULT_GIT, "git")
    if git["ssh_key"]:
        git["ssh_key"] = _resolve_path(git["ssh_key"], root)
    broker = _merge_section(raw.get("broker"), DEFAULT_BROKER, "broker")
    for key in ("timeout_seconds", "max_output_bytes", "max_request_bytes", "result_retention_days"):
        broker[key] = _positive_int(broker[key], f"broker.{key}")

    raw_actions = raw.get("actions") or {}
    if not isinstance(raw_actions, dict):
        raise ConfigError("actions: must be a mapping")
    actions = {n: _parse_action(n, a) for n, a in raw_actions.items()}

    return Config(
        path=path,
        root=root,
        name=name,
        model=model,
        permission_mode=permission_mode,
        claude_code_version=version,
        container=container,
        state_dir=_resolve_path(paths["state"], root),
        workspace_dir=_resolve_path(paths["workspace"], root),
        queue_dir=_resolve_path(paths["queue"], root),
        mounts=_parse_mounts(raw.get("mounts"), root),
        git=git,
        broker=broker,
        actions=actions,
    )
