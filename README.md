# agent-offload

Agent-offload provides durable remote coding tasks and an attached read-only relay. Agent-execution owns the constrained worker and execution evidence; Weft owns scheduling, source snapshots, job lifecycle, and artifact storage. Agent-offload owns task requests, retained baselines, result validation, and explicit application of returned changes.

## Durable coding tasks

```sh
agent-offload task submit /path/to/project --brief brief.txt --host studio --check 'just check'
agent-offload task list
agent-offload task status TASK_ID
agent-offload task retrieve TASK_ID
agent-offload task diff TASK_ID
agent-offload task apply TASK_ID
agent-offload task cancel TASK_ID
```

Submit returns a task identity and Weft job identity without waiting for the agent. Requests and accepted job bindings live under `~/.local/state/agent-offload/tasks` (override with `AGENT_OFFLOAD_STATE`). JSON responses use `agent-offload.task/v1`, `agent-offload.tasks/v1`, and `agent-offload.task-result/v1`. Consumers use this CLI rather than reading the registry. Agent Dispatch's **Remote coding** control uses the same API.

Before queueing, provisioning may wait up to 45 minutes for another update to release the target account's sources. Agent-offload allows one hour for that wait and the worker build; the task's execution timeout applies separately.

`task status` and `task list` expose Weft's observed lifecycle as `job_status`. For jobs that completed or ended `failed`, `dead`, or `killed`, `status` comes from the validated task result, including `failed` and `validation_failed`. If that result is unavailable or invalid, `status` is `unknown` with a diagnostic; job completion alone does not establish task success. A job that ended `failed`, `dead`, or `killed` without publishing a result, such as a worker that refused to start because its installed version does not match, has `status` `failed_without_result`; its diagnostic carries the exit code and the tail of the job log. A canceled job keeps Weft's `canceled` status, and resume does not resubmit it.

Each task runs against a Weft source snapshot with `workspace-write-no-shell`. The retained baseline holds the files that snapshot carries: files matching the source exclusions Weft reports (`weft sync inspect --json`), such as `testdata/`, `bin/`, and `CLAUDE.md`, are left out of both. The snapshot carries only the project directory, so a `--check` run is refused when the project's `Package.swift` or `[tool.uv.sources]` path dependencies resolve outside it; without `--check` nothing builds remotely and such projects are accepted. Validate check-dependent projects in the workspace lane with `agent-host-sync workspace run --dependency <path> ...`. The worker refuses symlinks, so submit refuses a project containing a symlink that the snapshot carries: one whose target stays inside the project and that no source exclusion drops. Weft replaces a link pointing outside the project with its target's content, so such links are accepted unless that content holds a file over the 2 MiB task file limit. The agent can read and edit project files but cannot run shell commands. `--check` supplies an optional, caller-chosen validation command executed after the agent finishes; its arguments use shell quoting without shell expansion. A failing check prevents application. Without a check, the result's `validation` field is null. The default coding writer is `openai-codex/gpt-6-luna`. Interactive user-facing Codex sessions use `openai-codex/gpt-6-astra`. `--selector` overrides the writer with any identity registered by agent-execution; `agent-offload task selectors --json` lists them with the default, under schema `agent-offload.task-selectors/v1`. The default agent timeout is 1800 seconds; `--timeout` accepts 1–7200 seconds.

An empty project is valid: the task starts with an empty baseline and can create files. A census error still stops submission before queueing.

Snapshot exclusions apply to baseline files, symlink checks, and content reached through external links. For example, `.weft.toml` with `[sync] exclude_dirs = ["/harness/src"]` excludes that root-relative path and its descendants while preserving `nested/harness/src`. Patterns without a leading `/` match path components at any depth.

A project file over the 2 MiB task file limit is carried, not refused. It stays out of the editable baseline and travels in the snapshot, so the writer and the `--check` command can read it; the request records its size and permissions. A result that returns it as an edit is refused, and so is a worker that changed it, since the protocol moves whole file contents and cannot carry one that large. Resume compares those sizes and permissions alongside the baseline.

Checks must preserve ordinary source files, including their permissions. A check that edits, creates, or deletes source files publishes `validation_failed` even if it exits successfully, with the writer's evidence and a diagnostic in the validation record. Excluded cache directories such as `__pycache__` and `.pytest_cache` may change.

A timed-out check publishes `validation_failed` with captured output and a timeout diagnostic. The worker exits unsuccessfully after publishing the result, so it remains available for retrieval and cannot be applied.

Worker setup and snapshot census failures publish a failed result with a diagnostic report and no changes. `task cancel` cancels an active Weft job; for an already-terminal job, it acknowledges the disposition without requesting another cancellation. A retained task proven never to have entered Weft submission can be canceled without contacting Weft, preserving its immutable request and preventing resume. For an attempted or missing-marker submission, `task cancel` derives the current attempt's key and calls public `weft job fence --idempotency-key KEY --json` in the retained project context. Only a valid `weft.job.fence.v1` fenced=true answer permits jobless closure. A returned receipt is durably bound before cancellation or acknowledgment. Lookup absence and failed, malformed, or interrupted fence calls never release custody; retry `task cancel` or use `task resume` to recover an accepted job. This path requires Weft's fence API on the submitting machine.

Retrieval validates task identity, retained request and worker digests, the writer's tool policy and transcript, and every changed file's baseline. Apply checks all destination files before writing any of them and refuses local conflicts, symlink paths, and protected agent or VCS metadata. Returned changes must be UTF-8 text; individual files are limited to 2 MiB and the source manifest to 10000 files and 64 MiB. Existing binary source files can be read but cannot be returned as edits. Application keeps a backup in the task journal and does not commit or deploy project changes.

If Weft cannot download a result, the diagnostic names the task and job and includes an exact `task retrieve` retry command. Retry retrieval with that same task ID. For a completed job, or a failed job whose artifact is reported ready, task status stays `unknown` while the result is unreadable. `task resume` does not submit another attempt in this case. Check output is stored in the validated result artifact, so job logs cannot substitute for it.

Successful application, confirmed cancellation or abandonment through `task cancel`, and resuming a failed attempt mark the disposed Weft job processed. Status, list, retrieval, and diff do not. The task response exposes `processed_at` for its disposition: acknowledgment of an accepted job, local closure of a proven never-submitted attempt, or closure after Weft's final-no-job fence. If acknowledgment fails after application, the applied marker and edits remain durable; repeat `task apply` to retry only the acknowledgment, without touching project files. A failed attempt's acknowledgment must succeed before resume queues a replacement. A job still running is not marked processed; repeat the disposition command after it finishes.

Tasks in the same registry serialize applications to a canonical project path. A competing application is refused until the current application finishes; retrying then checks its baseline against the updated files.

Before creating a Weft job, `task submit` runs `agent-host-sync ensure` for the selected host and account (default `agent`). This mirrors the local checkouts and provisions an immutable content-addressed worker build; the task record retains its absolute `bin_dir` and build ID, and Weft launches that build's worker by absolute path. The ensured worker's agent-offload and bundled agent-execution digests must match the dispatch exactly. If they do not, submission stops before a job exists: the local installed agent-offload may have been built from different bytes than the checkouts being deployed. Reinstall it with `just install` in the agent-offload checkout, or commit/revert checkout edits, then resubmit. This provisioning does not update PATH or a host's current build. Older task records without a pinned path are provisioned when resumed.

Submit passes `--no-sync`, so `weft run` returns once the job is recorded and Weft's daemon pushes it to the host on its next pass; a task can therefore wait about a minute before it starts. If `weft run` still exceeds its 180-second budget, submit asks `weft job lookup --idempotency-key` whether the job was recorded, binds it if so, and submits once more if not. A lookup that fails or answers for another key leaves the submission unresolved. If submission is interrupted or unresolved, use `agent-offload task resume TASK_ID`; it always resumes under the same idempotency identity, preventing a second job. A changed baseline still refuses resume. Resuming a `failed_without_result` task submits the retained request again as a new attempt with its own idempotency key; the task keeps its identity and lists the earlier jobs in `prior_job_ids`. Resume provisions the retained matching worker build before resending, repairing a build that changed or was collected while a job was queued. If the submitting machine's agent-offload or agent-execution has changed since submission, resume refuses a failed job's resubmission; submit a new task for the same brief. `--id` lets a caller choose the identity before starting the command. Submission failures retain that identity and never fall back to local execution. Cancellation operates on an accepted Weft job, closes a proven never-submitted task, or obtains an atomic key-fence result for an unresolved submission.

Tasks execute agent-execution inside their pinned offload build. The separately installed `agent-execution-worker` may belong to a different agent-review release; changing that release does not change a task's execution code. Provisioning, worker execution, and result retrieval each enforce the task's exact package digests. Automatic provisioning leaves the host's live tool and PATH unchanged.

### Continue partial work

A published `failed` or `validation_failed` result with a validated edit snapshot can seed a fresh task:

```sh
agent-offload task continue PARENT --timeout 3600 --prompt "Finish the remaining work"
```

The command prints the child identity. `--timeout` is required and accepts 1–7200 seconds.
Use `--brief FILE` instead of `--prompt TEXT`, or omit both to retain the parent's
requirements. `--id CHILD` selects a new identity without overwriting an existing task.
The child inherits the parent's writer, check, host, and account.

Continuation validates the retained parent result and applies its partial patch only
inside the disposable worker snapshot. The local project's full editable manifest
and carried-file census must still match the original baseline, including the path
set. Local changes refuse continuation; there is no automatic merge.

The parent remains unchanged, including its acknowledgment state. A failed parent
already acknowledged with `task cancel` is eligible. The child records the parent
task, job, request digest, and canonical result digest without copying the transcript.
It starts a fresh harness on the current tool build, so a parent produced before an
upgrade can be continued. It does not resume the parent's model conversation.

Recover an interrupted child submission with `agent-offload task resume CHILD`.
The immutable child request retains its seed and provenance through retries.
Receipt recovery can bind an already accepted child despite local changes; a new
submission still requires the original baseline.
The returned patch spans the original baseline to the combined final state.
Requested checks run on that final state, and only a successful result can be
applied through the ordinary conflict checks and rollback journal. A failed harness
result with validated cumulative edits can itself be continued. Setup or census
failures that publish no validated edit snapshot are refused; continue an earlier
ancestor with retained edits, or submit a fresh task.

## Historical report jobs

The historical lane validates and retrieves results from report-only
Codex jobs submitted through Weft. New native Codex submissions are retired:
`run`, direct submission, and remote worker execution refuse the provider before
launching it.

The historical report lane's replacement for grounded reads is the OMP Anthropic route. Durable coding tasks use the separate workspace-writer policy described above.

## Historical jobs

Inspect or retrieve an existing job:

```sh
agent-offload status wj1234
agent-offload retrieve wj1234
```

Retrieval validates the result envelope and report artifact before displaying
either. By default it marks a successfully validated job processed. Pass
`--no-mark-processed` for read-only validation.

Historical native jobs used nested worker protocol 1. Their accepted outer
dispatches did not retain the nested worker's source digest. Without an
independently retained digest, retrieval refuses to certify nested evidence and
reports the missing source baseline. The artifacts remain available in Weft,
and the job is not marked processed. The current SDK installation and the
result's own claimed digest cannot supply that baseline.

`agent-offload doctor` emits `agent-offload.doctor/v2`: the raw Execution-owned
`execution_status` envelope (including fact age/provenance) and an explicit
`admission` decision under `offload-task/v1`. It requires current available
capability, authentication, and transport; a current explicit quota refusal
blocks admission, while unknown quota is not a promise of capacity. It does not
claim generation success. Probe failures leave `execution_status` null and
include a visible policy reason. There is no universal `available` flag. For
example, check one exact writer on Studio:

```sh
agent-offload doctor --host studio --selector openai-codex/gpt-6-luna --json
```

Without options, doctor checks only the default task writer.
`--provider NAME` selects every admitted task writer for that provider.
`--selector` checks exactly that writer and overrides `--provider`. `--account`
selects a remote account (default: `agent`). Native `codex` remains retired and
is reported as retired, without a probe, only when requested with `--provider
codex`. The local `task selectors` list is an admitted roster, not evidence of
remote availability.

Task submit and resume request a fresh exact-subject status from Execution on
the provisioned build before creating a new Weft job. Immediately before model
launch, the pinned task executor probes again and applies the same admission
policy. A refusal before an attempt first enters Weft submission leaves the task without a new job,
and the diagnostic names `agent-offload task resume TASK_ID`. To abandon that
task, use `agent-offload task cancel TASK_ID`. A refusal on the executor occurs
after Weft accepted the job: it publishes a terminal `failed` result without
running the model. `task resume` reports that result without resubmitting.
Inspect it with `task retrieve`, fix the cause, and submit a new task identity.
Resume first reconciles any submission that may already have reached Weft (an
unresolved one, or one interrupted after `weft run` started) by its idempotency
key, binding an accepted job without provisioning or a new readiness check. A
lookup that fails keeps the submission unresolved rather than sending again.
A not-found lookup does not release dispatch custody: an earlier in-flight
request may still be accepted. `task resume` can recover that job.
For cancellation, run `agent-offload task cancel TASK_ID`. It derives the
current attempt's key and invokes Weft's fence automatically before closing
a jobless task or binding and disposing of an accepted job.
`agent-offload run` refuses before creating a Weft job.

## Trust boundary

Historical submissions sent the brief through Weft's private payload channel.
The repository traveled through Weft's source snapshot mechanism, including the
submitted working tree rather than only committed files.

Each historical task binds:

- the brief SHA-256 digest;
- a random task and idempotency identifier;
- the exact installed `agent-offload-worker` source digest;
- the Weft source pin;
- the nested `agent-execution-worker` result, session ID, stdout, stderr, and ctx evidence;
- the report SHA-256 digest and separate Markdown artifact.

The conductor continues to validate both returned artifacts. A malformed or
ambiguous transport response is not treated as permission to submit a duplicate
task.

The nested-worker adapter uses SDK protocol 2's protected result path,
`.agent-execution/results/<task-id>.json`, with the same task ID passed as
`--model-call-id`. It supplies no output-path override. Protected worker metadata
is excluded from the source-immutability digest; edits to project source still
fail that check. Native Codex execution remains retired.

## Attached relay (`relay`)

`agent-offload relay` is a second, deliberately different lane: the agent
runtime runs on a remote SSH host (default `agent@studio`) through OMP's
maintained RPC client, while its three tools — `review_read`, `review_glob`,
`review_grep` — execute **on this machine, against the live project root**.
The purpose is moving agent-process memory off the laptop without giving up
grounded access to the working tree. The Bun relay runtime is cached under
`$AGENT_OFFLOAD_STATE/relay-runtime/<relay-source-digest>/` (default state root:
`~/.local/state/agent-offload`), outside the installed package.

```sh
agent-offload relay /path/to/project --prompt "Map the auth flow" --timeout 600
agent-offload relay . --prompt-file brief.txt --json
```

Trust boundary, by contrast with `run`:

- **Attached.** The command stays in the foreground; the remote process tree
  dies with it. Closing the terminal or killing the command cancels the relay
  and stops the remote agent. `--detach` is refused, there is no job id and no resume. An
  interrupted relay writes no result record, and neither does one whose
  bridge fails before the remote agent reports ready; both exit with an
  error instead.
- **Not durable, not snapshot-backed.** No source snapshot leaves the laptop.
  The remote side sees project instructions and the prompt only, both sent
  explicitly with digests recorded. Because the tree is live, two reads can
  observe different revisions; the result records a per-call provenance entry
  (real path, SHA-256, byte count, truncation) for every served read instead
  of claiming snapshot equivalence.
- **Confined.** Tool paths are canonicalized and must resolve inside the
  project root; URL/device schemes, parent traversal, symlink escapes,
  binary and oversized inputs are refused. Reads, matches, files, tool-call
  argument size, and wall time are bounded. Glob and grep do not descend into
  version-control stores or installed dependencies (`.git`, `.jj`, `.hg`,
  `.svn`, `node_modules`, `.venv`, `__pycache__`); `review_read` can still
  read a file there by path.
- **Remote side is a blank room.** Built-in tools, LSP, extensions, skills,
  rules, sessions, pty, workspace-tree scanning, goals, xdev, and secrets
  are disabled twice (omp flags and a config overlay written to a scratch
  directory). The remote files and config cannot contaminate the session.
- **Fail-closed.** The relay refuses to proceed unless the server confirms
  exactly the three host tools are registered, and the conductor refuses any
  result whose tool lifecycle is incomplete or whose served paths fall
  outside the root. A remote failure never falls back to enabling remote
  tools.

Results are written under `outputs/relay-result-*.json` with schema
`agent-offload.relay-result/v1`: relay implementation digest, harness and SDK
identity, host identity, exact remote argv, pinned provider/model, prompt digest,
full RPC event stream, per-call tool evidence, final response, timestamps, and
failure or cancellation status. The record states `durability:
none-attached-live-local-tools` and `snapshot_equivalent: false`.

## Operations

Native readiness probing is disabled with submission.

Cancel or inspect a job with Weft:

```sh
weft job inspect wj1234 --json
weft job kill wj1234
```

Install or refresh the Studio worker through the account updater:

```sh
cd ~/code/agent-tools/agent-host-sync
uv run agent-host-sync --account agent plan --stage tools --tool agent-offload
uv run agent-host-sync --account agent apply --stage tools --tool agent-offload
```

After a worker source change, reinstall the local command and redeploy:

```sh
cd ~/code/agent-tools/agent-offload
uv tool install --force --reinstall ../agent-execution
just install
```

`just install` builds a new release under `~/.local/share/agent-offload/releases/` with agent-execution bundled, checks that it starts, and only then repoints `releases/current`, which the `~/.local/bin` entry points resolve through. agent-execution is not a declared dependency, so a plain `uv tool install .` produces an environment in which every command exits with a message naming the missing package.
