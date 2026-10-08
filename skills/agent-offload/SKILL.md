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

The brief's first line becomes the job's description in Weft and in Weft Status, so write it as a short plain summary of the task. Task jobs are listed under the target repository's project name and carry the `agent-offload-task` tag.

Save the returned task and Weft job identities. A local timeout does not cancel the task. An ambiguous submission retains its identity; use `agent-offload task resume TASK_ID` to retry with the same idempotency key. Do not resubmit with a new identity. A task whose job failed before publishing a result reports `failed_without_result` with the job log tail; fix the cause and run `task resume`, which retries the retained request as a new attempt of the same task. Submit automatically provisions the exact remote worker build before queueing, and resume provisions it again before resending, so a host update is not a manual step. If the mismatch remains, a build changed under the queued job and resume repairs it. A mismatch between this machine's installed agent-offload and the checkouts agent-host-sync deploys requires reinstalling locally (`just install` in the agent-offload checkout) or committing/reverting checkout edits, then submitting again.

The default coding writer is `openai-codex/gpt-6-luna` through OMP. Interactive user-facing Codex sessions use `openai-codex/gpt-6-astra`. `--selector` overrides the writer with any of Agent Execution's registered writers; `agent-offload task selectors --json` lists them for this installation. This is the admitted local roster, not a claim that a writer is available remotely. Submit and resume obtain fresh exact-subject Execution-owned observations on the provisioned remote build before creating a new Weft job; a retained accepted job is reconciled before probing. The agent uses `workspace-write-no-shell`: it edits the disposable snapshot but cannot execute shell commands. The optional `--check` runs separately afterward, with arguments parsed as shell quoting and no shell expansion. A failing check prevents application. Without a check, `validation` is null.

Check task-lane readiness without generating a model response:

```sh
agent-offload doctor --host studio --selector openai-codex/gpt-6-luna --json
```

Doctor emits `agent-offload.doctor/v2` with the raw Execution-owned `execution_status` envelope (facts, age, and provenance) plus `admission: {policy: "offload-task/v1", allowed, reasons}`. Admission requires current available capability, authentication, and transport; current explicit quota refusal blocks, while unknown quota is not a capacity promise. Probe failure leaves status null with a visible reason. No universal readiness flag is emitted. Without options doctor checks only the default writer. `--provider NAME` selects every admitted writer for that provider. `--selector` checks one exact writer and overrides `--provider`. `--account` is optional (default `agent`). Native `codex` remains retired and is only reported as retired when explicitly requested.

If admission refuses before an attempt first enters Weft submission, the task keeps its identity without a new job; fix the writer and run the `agent-offload task resume TASK_ID` named in the diagnostic. A refusal on the executor happens after job acceptance and publishes a terminal `failed` result without running the model. `task resume` reports that result without resubmitting; retrieve it, fix the cause, and submit a new task identity. Resume reconciles a submission that may already have reached Weft, including one interrupted mid-submit, before checking readiness, so it never queues a duplicate.

Checks must preserve ordinary source files and their permissions. Edits, additions, and deletions made by a check fail the task, even with exit zero. Excluded caches such as `__pycache__` may change.

The snapshot carries only the project directory. A `--check` run is refused when the project's `Package.swift` or `[tool.uv.sources]` path dependencies resolve outside it; without `--check` nothing builds remotely and such projects are accepted — run check-dependent projects in the workspace lane (`agent-host-sync workspace run --dependency <path> ...`). The task runs agent-execution bundled inside its pinned offload build, independently of the `agent-execution-worker` on PATH. Both package digests must match the retained request at provisioning, execution, and retrieval.

## Continue interrupted work

A task that publishes a validated partial patch can seed a fresh attempt. Continue it explicitly:

```sh
agent-offload task continue TASK_ID --timeout 3600
```

The command prints a fresh child identity and seeds only its disposable snapshot
with the parent's validated partial patch. It preserves the parent, including a
terminal result already acknowledged with `task cancel`. The local project's
editable manifest and carried census must match the original baseline, with no
added paths. Resolve local drift deliberately; continuation does not merge it.

The child inherits the parent's writer, check, host, account, and original brief.
Optional `--brief FILE` or `--prompt TEXT` adds continuation instructions.
The required `--timeout` accepts 1–7200 seconds. It starts a fresh harness pinned
to the current tool build, including when the parent predates an upgrade.
Partial edits are untrusted input, not proof of successful execution.

Use `--id CHILD` to choose the child identity. Recover interrupted submission
with `task resume CHILD`, preserving its immutable seed and parent provenance.
An accepted child's receipt can be recovered despite local drift; sending a new
job still requires the original baseline.
The child's diff spans the original baseline to the combined final state.
Requested validation and ordinary apply conflict checks still govern acceptance.
A failed harness result with validated cumulative edits can be continued again.
Setup or census failures without a validated edit snapshot are refused; continue
an earlier ancestor with retained edits, or submit a fresh task.

## Retrieve and verify

```sh
agent-offload task retrieve TASK_ID
agent-offload task diff TASK_ID
agent-offload task apply TASK_ID
```

Apply the `delegated-work-verification` skill before accepting changes. Inspect the diff, report, and check evidence. Retrieval validates the retained request, worker identity, transcript, and change bundle. Application checks every affected local file against the submitted baseline before writing and retains rollback bytes in the task journal. Resolve local conflicts deliberately; do not overwrite them to force application.

Application marks the terminal Weft job processed after saving the applied state. If acknowledgment fails, repeat `task apply TASK_ID`; this retries acknowledgment without reapplying changes. `processed_at` in task responses records acknowledgment of the current job. Retrieval, diff, status, and list do not mark jobs processed.

Edits are ordinary UTF-8 files, at most 2 MiB each and 64 MiB total. A larger project file is carried read-only: the writer and the check can read it, and the task fails if it comes back as an edit, so a repository holding a corpus or fixture still uses the task lane. Symlinks and protected metadata are rejected. Snapshot census failures return failed task artifacts; inspect their reports. Keep tasks requiring interaction, local credentials, or GUI state local.

## Cancel and deploy

```sh
agent-offload task cancel TASK_ID
agent-host-sync --account agent apply --stage tools --tool agent-offload
```

A retained task whose current attempt has never entered Weft submission can be abandoned with `task cancel`: its immutable request remains, status becomes `cancelled`, and resume cannot resurrect it. No Weft job is created. Missing custody metadata is not evidence that submission never started. A submission that may have reached Weft must be reconciled before cancellation; do not discard its custody or edit its private record. Even a not-found lookup retains attempted submission custody: an in-flight request may still be accepted. If admission refuses a resend, cancellation remains blocked; resume can recover the job once acceptance is visible. Failed or mismatched lookups also retain ambiguous custody.

Cancellation requests `weft cancel JOB_ID` only for an active job, then marks it processed once Weft confirms a terminal state. Use `task cancel` on an already-terminal task to abandon its result explicitly. A failed attempt is also marked processed before resume queues its replacement; acknowledgment failure leaves the old identity available for retry. Local and remote task-build digests must match, independently of the review-owned worker on PATH. The updater's exit code 4 means installation succeeded but existing sessions must restart; fresh Weft jobs use the updated executable.

## Other lanes

Native `agent-offload run` submission is retired. `agent-offload status JOB` and `agent-offload retrieve JOB` remain for historical report-only jobs. The `relay` commands support attached OMP sessions; relays do not provide durable task orchestration.
