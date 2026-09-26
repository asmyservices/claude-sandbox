"""Validates agent requests against the configured allowlist.

Everything here treats request content as untrusted: the container writes it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

from .config import PLACEHOLDER_RE, Action, ArgSpec, Config

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class RequestError(Exception):
    pass


def validate_value(spec: ArgSpec, value: Any) -> str:
    if spec.kind == "choices":
        if not isinstance(value, str) or value not in spec.choices:
            raise RequestError(f"{spec.name}: must be one of {', '.join(spec.choices)}")
        return value

    if spec.kind == "integer":
        if isinstance(value, bool):
            raise RequestError(f"{spec.name}: must be an integer")
        if isinstance(value, str) and re.fullmatch(r"-?\d{1,18}", value):
            value = int(value)
        if not isinstance(value, int):
            raise RequestError(f"{spec.name}: must be an integer")
        if spec.min is not None and value < spec.min:
            raise RequestError(f"{spec.name}: must be at least {spec.min}")
        if spec.max is not None and value > spec.max:
            raise RequestError(f"{spec.name}: must be at most {spec.max}")
        return str(value)

    if not isinstance(value, str):
        raise RequestError(f"{spec.name}: must be text")
    if len(value.encode("utf-8")) > spec.max_bytes:
        raise RequestError(f"{spec.name}: longer than {spec.max_bytes} bytes")
    return value


def validate_request(cfg: Config, data: Any) -> Tuple[Action, Dict[str, str]]:
    if not isinstance(data, dict):
        raise RequestError("request must be a JSON object")
    unknown = set(data) - {"action", "args"}
    if unknown:
        raise RequestError(f"unknown request field(s): {', '.join(sorted(unknown))}")

    name = data.get("action")
    if not isinstance(name, str) or name not in cfg.actions:
        raise RequestError(f"unknown action {name!r}; run `hostctl list` to see allowed actions")
    action = cfg.actions[name]

    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise RequestError("args must be a JSON object")
    unknown = set(args) - set(action.args)
    if unknown:
        raise RequestError(f"{name}: unknown argument(s): {', '.join(sorted(map(str, unknown)))}")

    values = {}
    for arg, spec in action.args.items():
        if arg in args:
            values[arg] = validate_value(spec, args[arg])
        elif spec.has_default:
            values[arg] = validate_value(spec, spec.default)
        else:
            raise RequestError(f"{name}: missing required argument {arg!r}")
    return action, values


def build_argv(action: Action, values: Dict[str, str], builtins: Dict[str, str]) -> list:
    lookup = dict(builtins)
    lookup.update(values)

    def replace(match: "re.Match[str]") -> str:
        return lookup[match.group(1)]

    argv = []
    for token in action.command:
        refs = PLACEHOLDER_RE.findall(token)
        if any(ref not in lookup for ref in refs):
            raise RequestError(f"{action.name}: command references an unknown placeholder")
        argv.append(PLACEHOLDER_RE.sub(replace, token))
    return argv


def builtin_values(home: str) -> Dict[str, str]:
    return {"home": home}
