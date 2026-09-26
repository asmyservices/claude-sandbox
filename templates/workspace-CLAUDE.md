# Working in this sandbox

You're running inside a Docker container, not on the host. You can do
whatever you need inside the container. The host is only reachable in the
ways described here.

## What you can see

- `/workspace` (this directory) persists across restarts. Clone
  repositories here and work on them here.
- Some host directories may be mounted too. `hostctl list` shows which ones
  and whether they're read-only.
- Nothing else from the host is visible: no host processes, services,
  crontab or files outside the mounts.

## Doing things on the host

The host runs a small set of allowlisted actions for you, such as restarting
a service or reading its logs. Use `hostctl`:

```bash
hostctl list                                    # allowed actions and arguments
hostctl status service=my-bot
hostctl logs service=my-bot lines=200
hostctl crontab-install content=@/workspace/crontab.txt
hostctl result <id>                             # check an earlier request
```

- Arguments are `name=value`. `name=@path` sends the contents of a file.
- Output and exit status are the action's own. Exit code 3 means the request
  is waiting for approval; 4 means no result yet.
- Actions marked "needs approval" wait until a person on the host approves
  them. Tell the user what you queued and the request id, then carry on or
  check back later with `hostctl result <id>`. Don't queue the same request
  again while it's waiting.
- If an action you need isn't in the list, say so and ask the user to add it
  to the host's `config.yaml`. Don't try to reach the host another way.

## Changing code the host runs

Services and cron jobs on the host run from the host's own checkouts, not
from `/workspace`. To change one:

1. Clone the repository into `/workspace`, make the change and test it here.
2. Commit and push it.
3. Queue the `deploy` action for that project (it needs approval), then use
   `restart` if a service needs to pick up the change.

## Restarts and upgrades

Upgrading Claude Code or restarting this container ends every open session.
That happens on the host (`./sandbox upgrade`), not from in here.
