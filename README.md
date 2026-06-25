# agent-ledger

A tiny claim board for agents that share a workspace.

If you run cron agents, subagents, or a few terminals against the same repo, they can step on each other. Two jobs pick the same task. One rewrites a file while another is still reading it. Nobody meant to break anything; they just had no shared state.

`agent-ledger` is a small SQLite-backed ledger for that shared state.

Before an agent starts work, it claims a task:

```bash
ledger claim "reindex wallets"
```

If nobody else holds it, the command prints a claim id and exits `0`:

```text
7fd9a3c91b22
```

If another agent already holds that task, it exits `1`:

```text
held by nightly-agent (last seen 12s ago)
```

When the job finishes, release it:

```bash
ledger done 7fd9a3c91b22
```

That's it. No server. No daemon. No dependency outside Python's standard library.

## Why this exists

Most agent setups start simple:

- a cron job checks a feed
- a coding agent edits files
- a second agent runs tests
- a cleanup script moves artifacts

Individually, each one is fine. Together, they need one boring thing: a way to say "I'm working on this, don't touch it." This project is that boring thing.

SQLite gives us process-safe writes, so concurrent claims are atomic. If twenty agents try to claim the same task at once, exactly one wins.

## Install

Clone it and put `ledger.py` somewhere on your path:

```bash
git clone https://github.com/YOURNAME/agent-ledger.git
cd agent-ledger
chmod +x ledger.py
sudo ln -s "$PWD/ledger.py" /usr/local/bin/ledger
```

Or just run it directly:

```bash
python3 ledger.py claim "sync prices"
```

## Usage

### Claim a task

```bash
ledger claim "sync prices"
```

### Claim with context

```bash
ledger claim "sync prices" --note "hourly coingecko pull"
```

### Wait instead of failing immediately

```bash
ledger claim "sync prices" --wait 60
```

This waits up to 60 seconds for the current holder to finish.

### Keep a long job alive

```bash
ledger beat 7fd9a3c91b22
```

A beat updates the claim's heartbeat. Long-running agents should call this every few minutes.

### Release a finished claim

```bash
ledger done 7fd9a3c91b22
```

### Drop a claim without marking it done

```bash
ledger drop 7fd9a3c91b22
```

### See active claims

```bash
ledger ls
```

Example:

```text
ID            OWNER                 HELD   TASK
7fd9a3c91b22  nightly-agent         4m     sync prices
14cf81b2ef2a  reviewer              38s    review PR 12
```

### See recent history

```bash
ledger log
```

### Reap dead claims

```bash
ledger gc
```

Claims whose owner has not sent a beat within the TTL are marked expired and can be claimed again.

## Environment

`agent-ledger` is configured with environment variables:

```bash
LEDGER_DB=/path/to/.agent-ledger.db
LEDGER_OWNER=agent-name
LEDGER_TTL=900
```

Defaults:

- `LEDGER_DB`: `./.agent-ledger.db`
- `LEDGER_OWNER`: `$AGENT_NAME`, otherwise `hostname:pid`
- `LEDGER_TTL`: `900` seconds

Use one database per workspace if you want claims scoped to that repo. Use a shared absolute path if several directories should coordinate.

## Shell pattern

A practical pattern for scripts:

```bash
CLAIM=$(ledger claim "build docs" --wait 30) || exit 0
trap 'ledger drop "$CLAIM"' EXIT

# do the work
npm run build

ledger done "$CLAIM"
trap - EXIT
```

If another process already owns `build docs`, this script exits quietly. If the script crashes halfway through, the trap drops the claim. If the whole process dies before the trap runs, the TTL eventually expires it.

## Python pattern

```python
import os
import subprocess

os.environ["LEDGER_OWNER"] = "price-agent"

claim = subprocess.run(
    ["ledger", "claim", "sync prices"],
    text=True,
    capture_output=True,
)

if claim.returncode != 0:
    raise SystemExit(0)

claim_id = claim.stdout.strip()
try:
    # do work here
    subprocess.check_call(["ledger", "done", claim_id])
except Exception:
    subprocess.call(["ledger", "drop", claim_id])
    raise
```

## What it is not

This is not a scheduler, queue, lock service, or workflow engine.

It does not decide what should run. It only records who is currently working on what, and makes that claim atomic.

That narrow scope is intentional. Small tools survive contact with real projects.

## License

MIT
