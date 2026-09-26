# claude-sandbox

Run a persistent [Claude Code](https://claude.com/claude-code) Remote Control
session on a Linux server, inside a Docker container. The agent sees only the
directories you choose. It reaches the host only through a short list of
actions you allow in a YAML file, and the risky ones wait for your approval.

You get an agent you can open from the Claude mobile app or claude.ai/code at
any time. It can restart your services or read their logs when you've allowed
that, but it can't touch the rest of the server.

## How it works

```
 Claude app / claude.ai/code
            │
            ▼
┌──────────── container ─────────────┐        ┌─────────────── host ───────────────┐
│ claude remote-control              │        │                                     │
│   works in /workspace              │        │  systemd path unit watches the queue│
│   hostctl restart service=my-bot ──┼─ file ─┼─▶ broker: check the allowlist ──────┼─▶ systemctl --user restart my-bot
│                                  ◀─┼─ file ─┼── result written back               │
└────────────────────────────────────┘        │  approval actions wait for          │
                                              │  ./sandbox approve <id>             │
                                              └─────────────────────────────────────┘
```

1. **The container** runs `claude remote-control`. It sees `/workspace`,
   its own home directory (login and settings), any host directories you
   mount, and the queue folders. Nothing else from the host.
2. **The agent asks for host actions** with `hostctl <action> name=value`.
   That drops a small JSON file into the queue.
3. **A systemd path unit** on the host notices the file and runs the
   **broker**. The broker checks the request against the `actions` in
   `config.yaml`, runs the fixed command with no shell, and writes the result
   back for `hostctl` to pick up.
4. **Actions marked `approval: true`** are held until you run
   `./sandbox approve <id>` (or `deny`). You can also have a Discord webhook
   ping you when one arrives.
5. **Every request** is recorded in an audit log on the host.

## Security model

What the container **can** do:

- Read and write `/workspace` and its own home directory.
- Read (or write, if you choose) the host directories listed under `mounts`.
- Use the network.
- Ask for the actions in `config.yaml`, with argument values from the lists
  and ranges you defined.

What it **can't** do:

- See or change anything else on the host: other files, processes, services,
  your crontab or SSH keys.
- Change the allowlist, its own Claude settings, or results. Those are
  host-only or mounted read-only.
- Run arbitrary commands through the broker. Commands are fixed argv lists
  with no shell. Arguments are chosen from lists or bounded integers. Free
  text can only be piped to stdin.
- Get privileges back inside the container. All Linux capabilities are
  dropped and `no-new-privileges` is set. On rootless Docker, root in the
  container is your unprivileged user on the host.

The broker treats every request as untrusted. It ignores symlinks, FIFOs and
directories, rejects oversized or malformed files, and rejects unknown actions,
arguments or values.

### Things to watch

- **Code the host runs is still a way in.** If you mount a directory that
  cron or a service runs code from as `rw`, the agent can run code on the
  host by editing it. The pattern this repo is built around avoids that: the
  agent works on its own clones in `/workspace` and pushes, and a `deploy`
  action that needs approval pulls the change into the live checkout.
  Approving a deploy or a crontab change means trusting that change.
- **Secrets in mounted directories are visible.** If a mounted directory
  contains `.env` files or tokens, the agent can read them.
- **The network is open.** The container can reach the internet and your
  LAN. Limit it with Docker networking or a firewall if you need to.
- **Docker access is root access.** Being in the `docker` group gives a user
  root on the host. Rootless Docker avoids that; see below.

## Requirements

- Linux with systemd, where your user can run user services
  (`systemctl --user`)
- Docker 20.10+ with the Compose v2 plugin (`docker compose`). Rootless is
  recommended.
- Python 3.8+ with PyYAML on the host (`apt install python3-yaml`)
- A Claude account that can use Claude Code

## Docker access

`./sandbox` needs your user to be able to run `docker`. Pick one:

**Rootless Docker (recommended).** Docker runs as your user, so the
container never has real root on the host. Root inside the container is
your own unprivileged user outside it. It runs alongside a normal system
Docker without affecting it.

### Setting up rootless Docker

These are the steps used on an Ubuntu 20.04 host with Docker 28 installed
from Docker's apt repository. Only step 2 needs root.

1. **Check your user has a subordinate ID range.** Rootless Docker maps the
   container's users into this range. Most distributions create it when the
   user is added:

   ```bash
   grep "^$USER:" /etc/subuid /etc/subgid
   # /etc/subuid:you:296608:65536
   # /etc/subgid:you:296608:65536
   ```

   If either line is missing, an admin can add one with
   `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 <user>`,
   using a range that doesn't overlap anyone else's.

2. **Install `uidmap`** (as root). It provides `newuidmap` and
   `newgidmap`:

   ```bash
   sudo apt-get install -y uidmap
   ```

3. **Install rootless Docker for your user** (as you, no sudo). The tool
   comes with Docker's packages. `check` lists anything still missing;
   `install` sets up a `docker.service` user unit, starts it, and creates a
   `rootless` CLI context:

   ```bash
   dockerd-rootless-setuptool.sh check
   dockerd-rootless-setuptool.sh install
   ```

   If `dockerd-rootless-setuptool.sh` isn't found, install Docker's
   `docker-ce-rootless-extras` package.

4. **Start it at boot and use it by default:**

   ```bash
   systemctl --user enable --now docker
   loginctl enable-linger
   docker context use rootless
   ```

   Lingering starts your user services at boot and keeps them running
   after you log out. Some tools ignore Docker contexts. If one can't find
   Docker, also set
   `export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock`.

5. **Check it:**

   ```bash
   docker info --format '{{json .SecurityOptions}}'   # should include "name=rootless"
   docker run --rm hello-world
   ```

Rootless Docker can only enforce per-container memory and CPU limits on
cgroup v2. Check with `docker info --format '{{.CgroupVersion}}'`. On
cgroup v1, leave out `container.memory` and `container.cpus`, and see
[Limiting the whole account](#limiting-the-whole-account) instead.

**The `docker` group.** Simpler, but anyone in that group is effectively
root on the host:

```bash
sudo usermod -aG docker "$USER"        # then log out and back in
```

With `container.user: auto` (the default), `./sandbox` detects which one you
have and picks the container user to match.

## Limiting the whole account

You can cap the CPU, memory and number of processes for everything a user
account runs. Running the sandbox under a dedicated account makes this
especially useful. It's also the only way to limit resources with rootless
Docker on cgroup v1.

systemd puts each user's services and logins in a slice named
`user-<uid>.slice`. With rootless Docker, the Docker daemon and every
container live inside that slice, along with your other user services and
SSH sessions. Cron jobs don't: they run under the system's `cron.service`,
so they aren't covered.

Set limits as root. They're saved and survive reboots:

```bash
uid=$(id -u <user>)

# cgroup v2 (docker info shows CgroupVersion 2)
sudo systemctl set-property user-$uid.slice MemoryMax=6G CPUQuota=200% TasksMax=4096

# cgroup v1
sudo systemctl set-property user-$uid.slice MemoryLimit=6G CPUQuota=200% TasksMax=4096
```

- `CPUQuota=200%` means at most two CPU cores' worth of time.
- `MemoryMax` / `MemoryLimit` is a hard cap. When it's reached, the kernel
  kills processes inside the slice.
- `TasksMax` caps processes and threads.

Check the limits took effect:

```bash
systemctl show user-$uid.slice -p MemoryMax -p MemoryLimit -p CPUQuotaPerSecUSec -p TasksMax
cat /sys/fs/cgroup/memory/user.slice/user-$uid.slice/memory.limit_in_bytes   # cgroup v1
cat /sys/fs/cgroup/user.slice/user-$uid.slice/memory.max                     # cgroup v2
```

To see where a process sits, run `cat /proc/<pid>/cgroup`. Remove the limits
with `sudo systemctl revert user-$uid.slice`.

The limit is shared by everything in the slice. If the agent uses up the
memory, your other services under that account can be the ones killed.
Running the sandbox under its own account avoids that.

## Quick start

```bash
git clone <this repo> ~/claude-sandbox
cd ~/claude-sandbox
cp config.example.yaml config.yaml
$EDITOR config.yaml       # at least: name, and the services/projects in the actions
./sandbox check           # validate the config
./sandbox setup           # create data dirs, install the broker, build the image
./sandbox login           # one-time, see below
./sandbox up              # start it
```

`./sandbox login` opens an interactive Claude session in the container. In
it:

1. Log in, and accept the trust prompt for `/workspace`.
2. Run `/remote-control` once and answer `y` when asked to enable Remote
   Control.
3. Run `/quit`.

The container runs Claude without a terminal, so it can't answer these
prompts itself. Doing them once here saves the answers in the container's
home directory (`paths.state`).

After `./sandbox up`, the environment shows up in the Claude app under the
`name` you set.
Setup warns you if lingering is off. Run `loginctl enable-linger` so the
broker and a rootless Docker daemon keep running after you log out and start
at boot. The container restarts on its own (`restart: unless-stopped`).

## Configuration

Everything lives in `config.yaml`. `config.example.yaml` is a complete,
commented example. Relative paths are relative to the config file. Top-level
keys starting with `x-` are ignored, which is handy for YAML anchors such as
a shared list of service names.

| Key | Default | What it does |
|---|---|---|
| `name` | required | Environment name in the Claude app. Also names the container and systemd units. |
| `model` | Claude Code default | Default model for new sessions. |
| `permission_mode` | `bypassPermissions` | Claude's permission mode inside the container. |
| `claude_code_version` | `latest` | npm version of Claude Code to install. |
| `container.user` | `auto` | `auto`, or `uid:gid`. |
| `container.memory`, `container.cpus` | unlimited | Resource limits. Rootless Docker needs cgroup v2 for these. |
| `container.apt_packages`, `container.python_packages` | `[]` | Extra packages baked into the image. |
| `container.environment` | `{}` | Extra environment variables. |
| `paths.state` | `./data/state` | The container's home: Claude login, settings, history. |
| `paths.workspace` | `./data/workspace` | `/workspace` in the container. |
| `paths.queue` | `./data/queue` | Queue, results, pending approvals and audit log. |
| `mounts` | `[]` | Host directories to mount: `host`, `container`, `mode` (`ro` or `rw`, default `ro`). |
| `git.ssh_key` | none | Private key for git over SSH, mounted read-only. |
| `git.name`, `git.email` | | Commit identity inside the container. |
| `broker.timeout_seconds` | `60` | Time limit per action. |
| `broker.max_output_bytes` | `65536` | Output kept per result (the end is kept). |
| `broker.max_request_bytes` | `262144` | Largest request file accepted. |
| `broker.result_retention_days` | `7` | Results older than this are deleted. |
| `broker.approval_webhook_url` | none | Discord webhook pinged when a request needs approval. |

### Actions

Each entry under `actions` is something the agent may ask for:

```yaml
actions:
  logs:
    description: Show a service's recent log lines.
    command: [journalctl, --user, -u, "{service}", -n, "{lines}", --no-pager]
    args:
      service: {choices: [my-bot, my-site]}
      lines: {integer: {min: 1, max: 1000}, default: 100}

  crontab-install:
    description: Replace the host user's crontab.
    command: [crontab, "-"]
    args:
      content: {text: {max_bytes: 20000}}
    stdin: content
    approval: true
```

- **`command`** is the exact argv. It never goes through a shell, so pipes,
  `&&` and globs have no effect. `{name}` is replaced with a validated
  argument. `{home}` is your home directory.
- **`args`**: every argument has exactly one type:
  - `choices: [...]`: must be one of these strings
  - `integer: {min, max}`: a whole number, bounds optional
  - `text: {max_bytes}`: free text, only allowed as `stdin`

  Add `default:` to make an argument optional. Every argument must be used,
  and every placeholder must be an argument.
- **`stdin`** names a text argument to pipe into the command.
- **`approval: true`** holds the request until you approve it.
- **`timeout`** overrides `broker.timeout_seconds` for this action.

`./sandbox check` rejects typos, unknown keys, placeholders without an
argument, and text used anywhere but stdin. After changing actions, run
`./sandbox setup --no-build` (or just wait for the next request) so the agent
sees the new list.

## Using it from the agent's side

`./sandbox setup` puts a `CLAUDE.md` in the workspace explaining this to the
agent. The commands are:

```bash
hostctl list                                 # allowed actions, arguments and mounts
hostctl status service=my-bot
hostctl logs service=my-bot lines=200
hostctl crontab-install content=@/workspace/crontab.txt   # @path sends a file
hostctl result <id>                          # check a request later
```

`hostctl` exits with the action's result: 0 for success, 1 for failed,
rejected, denied or timed out, 3 for waiting for approval, and 4 for no
result yet.

## Approvals

```bash
./sandbox pending          # list waiting requests
./sandbox show <id>        # everything in the request, including text content
./sandbox approve <id>     # run it now and send the result to the agent
./sandbox deny <id> --reason "not during the event"
```

Requests are checked again against the current config when you approve
them, so removing an action also blocks anything still waiting for it. To
get pinged, set `broker.approval_webhook_url` to a Discord webhook URL.

## Commands

| Command | What it does |
|---|---|
| `./sandbox check` | Validate `config.yaml` and print a summary. |
| `./sandbox setup [--no-build]` | Create data directories, write generated files, install and start the broker units, build the image. Safe to run again. |
| `./sandbox login` | Run Claude interactively in the container to log in, trust `/workspace` and enable Remote Control. |
| `./sandbox up` / `down` | Start / stop the container. |
| `./sandbox restart` | Recreate the container. Ends open sessions. |
| `./sandbox upgrade` | Rebuild with the newest Claude Code and restart. Ends open sessions. |
| `./sandbox status` | Container state, broker state, number of pending approvals. |
| `./sandbox logs [-f] [-n N]` | Container logs. |
| `./sandbox shell` | A shell in the running container, for debugging. |
| `./sandbox pending` / `show` / `approve` / `deny` | Approvals. |
| `./sandbox broker` | Process the queue once. The systemd unit runs this; you rarely need it. |
| `./sandbox uninstall` | Remove the container and broker units. Data is kept. |

`--config PATH` (or `SANDBOX_CONFIG`) points at a different config file,
so you can run several sandboxes from one checkout.

## Files

```
claude-sandbox/
├── sandbox                    # the CLI
├── sandboxlib/                # config loading, validation, broker, rendering
├── image/Dockerfile           # the container image
├── image/hostctl              # the agent's client for the queue
├── templates/workspace-CLAUDE.md
├── config.example.yaml
├── tests/
│
├── config.yaml                # yours (git-ignored)
├── .generated/                # compose.yaml and managed settings (git-ignored)
└── data/                      # git-ignored
    ├── state/                 # container home: Claude login, settings, history
    ├── workspace/             # /workspace
    └── queue/
        ├── inbox/requests/    # agent writes requests here
        ├── inbox/tmp/         # agent writes here first, then renames
        ├── results/           # broker writes results (read-only in the container)
        ├── pending/           # requests waiting for approval (host only)
        └── audit.log          # one JSON line per request (host only)
```

The broker is two systemd user units: `claude-sandbox-<name>-broker.path`
watches `inbox/requests`, and `claude-sandbox-<name>-broker.service` drains
it. See them with `systemctl --user status 'claude-sandbox-*'` and their
logs with `journalctl --user -u claude-sandbox-<name>-broker.service`.

The container's Claude settings (model and permission mode) are written to
`.generated/managed-settings.json` and mounted read-only at
`/etc/claude-code/managed-settings.json`, so the agent can't change them.

## Upgrading

```bash
./sandbox upgrade
```

This rebuilds the image with the newest Claude Code and recreates the
container, which ends any open sessions. Pin a version with
`claude_code_version` if you'd rather upgrade on purpose. Running
`claude --upgrade` inside the container doesn't stick, because the image is
rebuilt from scratch.

## Moving from a host-run Remote Control service

If you already run `claude remote-control` directly on the host as a
systemd user service:

1. Set up and start the sandbox as above, with a different `name`, and check
   it appears in the app.
2. Stop the old service: `systemctl --user disable --now <old-service>`.
3. Move anything the agent needs into the sandbox: clone repositories into
   `data/workspace`, carry over useful notes from the old `CLAUDE.md`, and
   add the actions it relied on to `config.yaml`.

## Troubleshooting

- **`can't talk to Docker`**: your user can't reach the Docker daemon. See
  [Docker access](#docker-access). With rootless Docker, check
  `docker context use rootless`.
- **`hostctl` says the action list is missing**: the broker hasn't run yet.
  Run `./sandbox setup --no-build`.
- **Requests never get a result**: check
  `systemctl --user status claude-sandbox-<name>-broker.path` and the
  service's journal. If lingering is off, user units stop when you log out.
- **The environment doesn't appear in the app**: check `./sandbox logs`.
  Usually the one-time login steps weren't finished. Run `./sandbox login`,
  and make sure you ran `/remote-control` once before `/quit`. Then run
  `./sandbox restart`.
- **Files in `data/` are owned by an odd uid**: set `container.user`
  explicitly. On rootless Docker it should be `0:0`, which maps to you.
- **Errors about memory or CPU limits on rootless Docker**: rootless Docker
  can only enforce limits on cgroup v2. If `docker info` shows
  `Cgroup Version: 1`, remove `container.memory` and `container.cpus`.

## Development

```bash
python3 -m unittest discover -s tests -t tests
```

The tests cover config validation, the rendered Compose file and units,
the broker (including hostile queue entries), approvals, and a round trip
through `hostctl`. They don't need Docker.
