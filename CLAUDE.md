# claude-sandbox

Runs Claude Code Remote Control inside a Docker container. The host is only
reachable through an allowlisted action queue. `README.md` covers usage and
the security model; read it before changing behaviour.

## Layout

- `sandbox`: CLI entry point. Checks the Python version and PyYAML, then
  calls `sandboxlib.cli.main`.
- `sandboxlib/config.py`: loads and validates `config.yaml` into dataclasses.
  All config errors are `ConfigError` with a `section.key: problem` message.
- `sandboxlib/validation.py`: validates requests from the container and
  builds argv.
- `sandboxlib/broker.py`: the host-side queue processor, approvals, audit
  log and the catalog the agent reads (`results/_catalog.json`).
- `sandboxlib/render.py`: generates `.generated/compose.yaml`,
  `.generated/managed-settings.json` and the systemd units.
- `image/`: the Dockerfile and `hostctl`, the client baked into the image.
- `templates/workspace-CLAUDE.md`: copied into the agent's `/workspace`
  on setup, if one isn't there already.

## Security rules

These are the point of the project. Don't weaken them without saying so
explicitly in the change.

- **Requests are untrusted input.** Anything under `queue/inbox/` is
  written by the container. Open request files with
  `O_NOFOLLOW | O_NONBLOCK`, accept only regular files, enforce
  `max_request_bytes`, and validate ids with `ID_RE` before using them in
  paths.
- **No shell, ever.** Actions run as fixed argv lists via `subprocess.run`
  without `shell=True`. Placeholders are only filled from validated values.
  `text` arguments only go to stdin.
- **The broker must drain `inbox/requests/` on every run**, including junk
  entries. The systemd path unit uses `DirectoryNotEmpty`, so anything left
  behind makes it re-fire in a loop.
- **The container gets no write access to host-side state.** `results/` is
  mounted read-only. `pending/`, `audit.log`, `config.yaml` and
  `.generated/` aren't mounted at all, except `managed-settings.json`, which
  is read-only.
- **Never mount the Docker socket** or give the container host PID or
  network namespaces, extra capabilities or privileged mode.
- **Approvals are re-validated** against the current config when approved.

## Compatibility

- The host side runs on the system Python, **3.8 or newer**, with only the
  standard library plus PyYAML. No `match`, no `str.removeprefix`, no
  `X | Y` type unions. Keep `from __future__ import annotations` at the top
  of modules.
- `image/hostctl` runs inside the container and uses only the standard
  library.

## Tests

```bash
python3 -m unittest discover -s tests -t tests
```

Add tests for any change to validation or the broker, especially hostile
inputs: symlinks, FIFOs, directories, oversized files, bad ids, injection
attempts. The tests don't need Docker. Run them before committing.

## Conventions

- New config keys need a default in `config.py`, a line in
  `config.example.yaml`, and a row in the README's configuration table.
- New CLI commands go in `build_parser()` in `cli.py` and in the README's
  command table.
- Errors shown to users are plain sentences with the fix where possible,
  e.g. "can't talk to Docker … See the README section on Docker access."
