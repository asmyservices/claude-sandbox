"""`./sandbox` command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List

from . import broker
from .config import Config, ConfigError, load_config
from .render import REPO_DIR, systemd_units, write_generated
from .validation import RequestError


class SandboxError(Exception):
    pass


def run(argv: List[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, **kwargs)
    except FileNotFoundError:
        raise SandboxError(f"{argv[0]} is not installed or not on PATH")
    if check and proc.returncode != 0:
        raise SandboxError(f"command failed ({proc.returncode}): {' '.join(map(str, argv))}")
    return proc


def docker_user(cfg: Config) -> str:
    if cfg.container["user"] != "auto":
        return cfg.container["user"]
    proc = run(["docker", "info", "--format", "{{json .SecurityOptions}}"],
               check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SandboxError("can't talk to Docker: " + (proc.stderr.strip() or proc.stdout.strip())
                           + "\nSee the README section on Docker access.")
    if "rootless" in proc.stdout:
        return "0:0"
    return f"{os.getuid()}:{os.getgid()}"


def compose(cfg: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    path = write_generated(cfg, docker_user(cfg))
    return run(["docker", "compose", "-f", str(path), *args], check=check)


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args], check=check)


def prepare_dirs(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ssh_dir = cfg.state_dir / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    broker.ensure_dirs(cfg)
    missing = [str(m.host) for m in cfg.mounts if not m.host.exists()]
    if missing:
        raise SandboxError("mount host path(s) don't exist: " + ", ".join(missing))
    if cfg.git["ssh_key"] and not Path(cfg.git["ssh_key"]).is_file():
        raise SandboxError(f"git.ssh_key not found: {cfg.git['ssh_key']}")
    template = REPO_DIR / "templates" / "workspace-CLAUDE.md"
    target = cfg.workspace_dir / "CLAUDE.md"
    if not target.exists():
        shutil.copyfile(template, target)


def install_units(cfg: Config) -> None:
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in systemd_units(cfg).items():
        (directory / name).write_text(content)
    systemctl("daemon-reload")
    systemctl("enable", "--now", f"{cfg.unit_prefix}-broker.path")


def linger_enabled() -> bool:
    user = os.environ.get("USER") or ""
    proc = run(["loginctl", "show-user", user, "-p", "Linger"],
               check=False, capture_output=True, text=True)
    return proc.stdout.strip() == "Linger=yes"


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    print(f"config OK: {cfg.path}")
    print(f"  name:        {cfg.name}")
    print(f"  model:       {cfg.model or '(Claude Code default)'}")
    print(f"  permissions: {cfg.permission_mode}")
    print(f"  state:       {cfg.state_dir}")
    print(f"  workspace:   {cfg.workspace_dir}")
    print(f"  queue:       {cfg.queue_dir}")
    for m in cfg.mounts:
        print(f"  mount:       {m.host} -> {m.container} ({m.mode})")
    print("  actions:")
    for name, action in sorted(cfg.actions.items()):
        flag = " [approval]" if action.approval else ""
        print(f"    {name}{flag}: {action.description}")
    return 0


def cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    run(["docker", "compose", "version"], capture_output=True)
    user = docker_user(cfg)
    prepare_dirs(cfg)
    write_generated(cfg, user)
    broker.write_catalog(cfg)
    install_units(cfg)
    print(f"broker installed: {cfg.unit_prefix}-broker.path (container user {user})")
    if not args.no_build:
        compose(cfg, "build")
    if not linger_enabled():
        print("warning: lingering is off, so the broker and a rootless Docker daemon "
              "stop when you log out. Run: loginctl enable-linger")
    print("\nNext: ./sandbox login   (one-time: log in, trust /workspace, run /remote-control once, /quit)")
    print("      ./sandbox up")
    return 0


def cmd_login(cfg: Config, args: argparse.Namespace) -> int:
    print("Starting Claude in the container. In the session:\n"
          "  1. Log in and accept the trust prompt for /workspace.\n"
          "  2. Run /remote-control once and answer y to enable it.\n"
          "  3. Run /quit.\n"
          "Then start it for real with ./sandbox up.")
    return compose(cfg, "run", "--rm", "claude", "claude", check=False).returncode


def cmd_up(cfg: Config, args: argparse.Namespace) -> int:
    compose(cfg, "up", "-d")
    print(f"running: the environment '{cfg.name}' should appear in the Claude app")
    return 0


def cmd_down(cfg: Config, args: argparse.Namespace) -> int:
    compose(cfg, "down")
    return 0


def cmd_restart(cfg: Config, args: argparse.Namespace) -> int:
    compose(cfg, "up", "-d", "--force-recreate")
    return 0


def cmd_logs(cfg: Config, args: argparse.Namespace) -> int:
    extra = ["-f"] if args.follow else []
    return compose(cfg, "logs", "--tail", str(args.lines), *extra, check=False).returncode


def cmd_shell(cfg: Config, args: argparse.Namespace) -> int:
    return compose(cfg, "exec", "claude", "bash", check=False).returncode


def cmd_upgrade(cfg: Config, args: argparse.Namespace) -> int:
    compose(cfg, "build", "--pull", "--build-arg", f"CACHEBUST={int(time.time())}")
    compose(cfg, "up", "-d")
    print("upgraded; open sessions were restarted")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    compose(cfg, "ps", check=False)
    unit = f"{cfg.unit_prefix}-broker.path"
    state = systemctl("is-active", unit, check=False)
    print(f"\nbroker ({unit}): {'active' if state.returncode == 0 else 'NOT active'}")
    pending = broker.list_pending(cfg)
    print(f"pending approvals: {len(pending)}")
    return 0


def cmd_broker(cfg: Config, args: argparse.Namespace) -> int:
    return broker.main_broker(cfg)


def cmd_pending(cfg: Config, args: argparse.Namespace) -> int:
    items = broker.list_pending(cfg)
    if not items:
        print("no pending approvals")
        return 0
    for item in items:
        action = cfg.actions.get(item.get("action"))
        summary = broker._summarize_args(action, item.get("args") or {})
        print(f"{item['id']}  {item.get('action')}  {json.dumps(summary)}  (requested {item.get('requested_at')})")
    print("\nReview with `./sandbox show <id>`, then `./sandbox approve <id>` or `./sandbox deny <id>`.")
    return 0


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    item = broker.load_pending(cfg, args.id)
    action = cfg.actions.get(item.get("action"))
    print(f"id:        {item['id']}")
    print(f"action:    {item.get('action')}")
    print(f"requested: {item.get('requested_at')}")
    if action:
        print(f"command:   {' '.join(action.command)}")
    for key, value in (item.get("args") or {}).items():
        if "\n" in str(value):
            print(f"{key}:\n----\n{value}\n----")
        else:
            print(f"{key}: {value}")
    return 0


def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    outcome = broker.approve(cfg, args.id)
    print(f"{args.id}: {outcome['status']}")
    if outcome.get("output"):
        print(outcome["output"])
    if outcome.get("error"):
        print(outcome["error"])
    return 0 if outcome["status"] == "ok" else 1


def cmd_deny(cfg: Config, args: argparse.Namespace) -> int:
    broker.deny(cfg, args.id, args.reason or "")
    print(f"{args.id}: denied")
    return 0


def cmd_uninstall(cfg: Config, args: argparse.Namespace) -> int:
    compose(cfg, "down", check=False)
    systemctl("disable", "--now", f"{cfg.unit_prefix}-broker.path", check=False)
    for name in systemd_units(cfg):
        (unit_dir() / name).unlink(missing_ok=True)
    systemctl("daemon-reload", check=False)
    print(f"removed the container and broker units; data is still in {cfg.state_dir.parent}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    default_config = os.environ.get("SANDBOX_CONFIG") or str(REPO_DIR / "config.yaml")
    parser = argparse.ArgumentParser(
        prog="sandbox", description="Run Claude Code Remote Control in a container with an allowlisted host action queue.")
    parser.add_argument("--config", default=default_config, help="path to config.yaml (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    add("check", cmd_check, "validate config.yaml and print a summary")
    p = add("setup", cmd_setup, "create directories, install the broker units and build the image")
    p.add_argument("--no-build", action="store_true", help="skip building the image")
    add("login", cmd_login, "run Claude interactively in the container to log in (one-time)")
    add("up", cmd_up, "start the container")
    add("down", cmd_down, "stop and remove the container")
    add("restart", cmd_restart, "recreate the container (ends open sessions)")
    p = add("logs", cmd_logs, "show container logs")
    p.add_argument("-n", "--lines", type=int, default=100)
    p.add_argument("-f", "--follow", action="store_true")
    add("shell", cmd_shell, "open a shell in the running container")
    add("upgrade", cmd_upgrade, "rebuild with the latest Claude Code and restart (ends open sessions)")
    add("status", cmd_status, "show container, broker and approval status")
    add("broker", cmd_broker, "process the queue once (run by the systemd unit)")
    add("pending", cmd_pending, "list requests waiting for approval")
    p = add("show", cmd_show, "show a pending request in full")
    p.add_argument("id")
    p = add("approve", cmd_approve, "approve and run a pending request")
    p.add_argument("id")
    p = add("deny", cmd_deny, "deny a pending request")
    p.add_argument("id")
    p.add_argument("--reason", help="message returned to the agent")
    add("uninstall", cmd_uninstall, "remove the container and broker units (keeps data)")
    return parser


def main(argv: List[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        return args.func(cfg, args)
    except (ConfigError, SandboxError, RequestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
