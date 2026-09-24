---
name: agent-offload
description: Run coding agents on Studio instead of this machine, through OMP. Durable tasks edit a Weft snapshot and return a diff to verify and apply; attached relays run the agent remotely against the local live tree; historical report jobs remain retrievable. Use when delegating coding, repair, or analysis work to run on Studio, and for submitting, monitoring, retrieving, or applying such work.
---

# Agent Offload

Use the task lane when a repository brief can stand alone and the work should survive the requesting session. Agent Offload owns task identity, retained baselines, result retrieval, and application. Agent Execution owns the model harness and execution evidence. Weft owns snapshots, job lifecycle, and artifact transport. Agent Dispatch exposes the task CLI in its Remote coding sheet.

## Submit and monitor

Write a brief naming the repository scope, desired behavior, exclusions, and expected verification:

```sh
agent-offload task submit PROJECT --brief BRIEF --host studio --check 'just check'
agent-offload task list
agent-offload task status TASK_ID
weft status --wait JOB_ID
```

Save the returned task and Weft job identities. A local timeout does not cancel the task. An ambiguous submission retains its identity; use `agent-offload task resume TASK_ID` to retry with the same idempotency key. Do not resubmit with a new identity. A task whose job failed before publishing a result reports `failed_without_result` with the job log tail; fix the cause (usually a worker version mismatch, fixed by updating the host) and run `task resume`, which retries the retained request as a new attempt of the same task.

The default writer is `openai-codex/gpt-6-sol` through OMP. Use `--selector openai-codex/gpt-6-luna` for cheaper bounded work. Reserve `openai-codex/gpt-6-astra` for explicit, rare largest-model escalations. `--selector` accepts Agent Execution's registered writers; `agent-offload task selectors --json` lists them for this installation. The agent uses `workspace-write-no-shell`: it edits the disposable snapshot but cannot execute shell commands. The optional `--check` runs separately afterward, with arguments parsed as shell quoting and no shell expansion. A failing check prevents application. Without a check, `validation` is null.

Checks must preserve ordinary source files and their permissions. Edits, additions, and deletions made by a check fail the task, even with exit zero. Excluded caches such as `__pycache__` may change.

## Retrieve and verify

```sh
agent-offload task retrieve TASK_ID
agent-offload task diff TASK_ID
agent-offload task apply TASK_ID
```

Apply the `delegated-work-verification` skill before accepting changes. Inspect the diff, report, and check evidence. Retrieval validates the retained request, worker identity, transcript, and change bundle. Application checks every affected local file against the submitted baseline before writing and retains rollback bytes in the task journal. Resolve local conflicts deliberately; do not overwrite them to force application.

Tasks support ordinary UTF-8 files, at most 2 MiB each and 64 MiB total. Symlinks and protected metadata are rejected. Snapshot census failures return failed task artifacts; inspect their reports. Keep tasks requiring interaction, local credentials, or GUI state local.

## Cancel and deploy

```sh
agent-offload task cancel TASK_ID
agent-host-sync --account agent apply --stage tools --tool agent-offload
```

Cancellation uses `weft cancel JOB_ID`. Local and remote package source digests must match. The updater's exit code 4 means installation succeeded but existing sessions must restart; fresh Weft jobs use the updated executable.

## Other lanes

Native `agent-offload run` submission is retired. `agent-offload status JOB` and `agent-offload retrieve JOB` remain for historical report-only jobs. The `relay` commands support attached OMP sessions; relays do not provide durable task orchestration.
