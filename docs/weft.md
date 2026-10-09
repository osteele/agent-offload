# Using agent-offload with Weft

Tasks run as [Weft](https://github.com/osteele/weft) jobs; the relay does not
use Weft. This page lists what a task needs from Weft and how to check each
part before submitting.

## The host

Set the host up for Weft first, following Weft's Quick Start (`weft host setup
HOST`). Then confirm that it runs jobs:

```sh
weft run --host HOST -m "Smoke test" 'printf "hello from weft\n"'
weft status --wait JOB_ID
```

agent-offload uses that host in two ways:

- **Weft runs the job** as whichever account Weft logs in as on the host.
- **agent-offload copies its worker** to the same account, with
  `ssh ACCOUNT@HOST`, before the first task on each installed version. Set
  `[tasks] account` (or pass `--account`) when that account is not your local
  user name. The account needs Python 3.11 or later (or uv) and Bun, and
  credentials for the model that writes the code. `agent-offload doctor --host
  HOST` checks the credentials.

## What a task asks of Weft

**A snapshot of the project.** The agent edits a copy of the files Weft would
sync. agent-offload asks `weft sync inspect PROJECT --json` for Weft's
exclusions, so the same files are left out of the task's baseline. To keep a
directory out of the snapshot, list it in the project's `.weft.toml`:

```toml
[sync]
exclude_dirs = ["data", "/harness/src"]
```

A leading `/` anchors a path at the project root; otherwise the name matches
at any depth. Files outside the project directory never travel, so a `--check`
is refused when the project depends on a path outside itself.

**A job submitted without waiting.** `task submit` calls `weft run --no-sync`,
which records the job and returns; Weft's daemon then sends it to the host on
its next pass. Expect about a minute before the agent starts, and keep the
local Weft daemon running. The job is tagged `agent-offload-task` and listed
under the project's name, so `weft job list` and Weft Status show it beside the
project's other jobs. Its wall-time limit is the task's fixed timeout plus 17
minutes for setup and the check — or, for a renewable budget, the ceiling of
the budget's finite cap plus 17 minutes, never just the initial amount.

**Idempotent submission.** Every attempt carries an idempotency key.
`task resume` asks `weft job lookup --idempotency-key KEY` whether a submission
that timed out was recorded anyway, and `task cancel` uses `weft job fence
--idempotency-key KEY` to settle one that may or may not exist. Both need a
Weft build that has these commands; check with `weft job fence --help`.

**Results as artifacts.** The worker writes its result to
`.agent-execution/offload/TASK_ID.json` in the job's working directory, and agent-offload reads it with
`weft artifact cat`. `task status` uses `weft job inspect`, a job that ended
without a result shows the tail of `weft job log`, and applying, cancelling,
or resuming a task marks its job processed with `weft job mark-processed`.

## Inspecting a task's job directly

```sh
weft job inspect JOB_ID --json
weft job log JOB_ID -n 50
weft job kill JOB_ID
```

Prefer `agent-offload task cancel TASK_ID` to killing the job: it also records
the task's disposition.
