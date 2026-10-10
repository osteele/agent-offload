# agent-offload reference

The detailed contract behind the commands in the [README](../README.md).

## Tasks

Submit returns a task identity and Weft job identity without waiting for the agent. Requests and accepted job bindings live under `~/.local/state/agent-offload/tasks` (override with `AGENT_OFFLOAD_STATE`). JSON responses use `agent-offload.task/v1`, `agent-offload.tasks/v1`, and `agent-offload.task-result/v1`. Consumers use this CLI rather than reading the registry. Agent Dispatch's **Remote coding** control uses the same API.

Before queueing, submission provisions the task's worker build (see [Worker builds](#worker-builds)); provisioning is allowed 15 minutes, and the task's execution timeout applies separately.

`task status` and `task list` expose Weft's observed lifecycle as `job_status`. For jobs that completed or ended `failed`, `dead`, or `killed`, `status` comes from the validated task result, including `failed` and `validation_failed`. If that result is unavailable or invalid, `status` is `unknown` with a diagnostic; job completion alone does not establish task success. A job that ended `failed`, `dead`, or `killed` without publishing a result, such as a worker that refused to start because its installed version does not match, has `status` `failed_without_result`; its diagnostic carries the exit code and the tail of the job log. A canceled job keeps Weft's `canceled` status, and resume does not resubmit it.

Every task row — from `submit`, `resume`, `status`, and `list` — also carries an additive `submission` object describing how far submission has provably got. It is derived only from the retained task record, never from live Weft or host state. `stage` is one of: `provisioning_pending` (the record is durable; nothing remote has started), `provisioning_in_progress` (a worker build was being provisioned when the record was last written), `provisioning_failed` (the last provisioning attempt failed; `provisioning_error` names why, and the diagnostic carries the task identity and its same-ID `task resume` — unless the pinned build no longer exists in the installed packages, which no resume can repair), `worker_ready` (the pinned build is recorded; writer admission and the Weft send are next), `submission_possibly_accepted` (a `weft run` was started under the attempt's idempotency key and its outcome is not yet bound; a null `job_id` is never evidence that Weft accepted nothing), `accepted` (Weft accepted a job and its identity is retained), `cancelled_no_job` (cancelled with no job; `cancelled_because` is `never_submitted` when the durable marker proves no send was attempted, or `weft_reported_no_job` when Weft's fence answered no job), or `unknown` (the record predates a needed marker, so no stage is claimed; `task resume` reconciles it). `provisioning` reports the recorded build-provisioning state: `pending`, `in_progress`, `ready`, `failed`, or `unknown` for a record written before the field existed. A recorded phase is authoritative even when a retry retains an older build identity; only legacy records without the field infer `ready` from that identity. `submission_started` is the durable marker persisted immediately before `weft run`: `false` proves no send was attempted in the current attempt, `true` means a send may have been accepted, and `null` means the record predates the marker. `build_id` and `job_id` mirror the retained build and job identities.

Each task runs against a Weft source snapshot with `workspace-write-no-shell`. The retained baseline holds the files that snapshot carries: files matching the source exclusions Weft reports (`weft sync inspect --json`), such as `testdata/`, `bin/`, and `CLAUDE.md`, are left out of both. The snapshot carries only the project directory, so a `--check` run is refused when the project's `Package.swift` or `[tool.uv.sources]` path dependencies resolve outside it; without `--check` nothing builds remotely and such projects are accepted. Run the checks of such projects in a remote workspace that also holds their dependencies. Confined relative symlinks are carried as read-only link objects: the request records them under `symlinks` as `{relative_path: literal_relative_target}` (a file link or a directory link; the literal target is immutable evidence, and editing the target's content does not retarget the link), the census never recurses through a linked directory, and the agent may read through a link but may not create, replace, retarget, or delete one. Returned changes may not address a link or any descendant reached through a linked directory; the canonical target file stays ordinarily editable. A kept link must resolve, through any chain of further links, to an existing path inside the project: absolute targets, escapes (including indirect ones through intermediate links, while confined `..` steps such as `launchers/claude -> ../agent-launcher` are fine), cycles, dangling links, and targets that cross protected VCS/harness metadata or a source exclusion are refused at submit with the offending link named, never silently dropped or materialized. New requests always include `symlinks`, even when it is `{}`: an explicit empty inventory is checked strictly, and `null` is invalid. Older retained requests may have no `symlinks` field; absence does not assert an empty local inventory. Before inventories, Weft materialized links whose literal targets were absolute or lexically escaped the project snapshot; relative links lexically staying within it were carried and refused at legacy submission. Thus unchanged legacy requests can resume, continue, and apply unrelated edits without a new local link-confinement census. Legacy continuations preserve that absence in their children; they still check ordinary baseline/carried files and path sets, and cannot write through a live link or linked directory. Legacy resubmission and continuation still refuse unrecorded links that Weft would carry, based on the literal target rather than the final referent. The agent can read and edit project files but cannot run shell commands. `--check` supplies an optional, caller-chosen validation command executed after the agent finishes; its arguments use shell quoting without shell expansion. A failing check prevents application. Without a check, the result's `validation` field is null. The default coding writer is `openai-codex/gpt-6-luna`. Interactive user-facing Codex sessions use `openai-codex/gpt-6-astra`. `--selector` overrides the writer with any identity registered by agent-execution; `agent-offload task selectors --json` lists them with the default, under schema `agent-offload.task-selectors/v1`. A task's time is one fixed `--timeout` of 1–7200 seconds or a renewable budget ([Time budgets](#time-budgets)); a fresh submission with no time flag uses the approved renewable policy.

An empty project is valid: the task starts with an empty baseline and can create files. A census error still stops submission before queueing.

### Time budgets

Every request carries exactly one of two time choices, and the choice is
explicit for `task continue` and defaulted for a fresh `task submit`.

`--timeout SECONDS` is a fixed hard writer limit of 1–7200 seconds. The
request then has no `budget` object, `request.timeout` is that hard limit,
and the writer never renews: when the limit is reached the attempt ends. A
budgeted attempt's Weft job gets an outer wall time of `ceil(cap) + 1020`
seconds; a fixed attempt keeps its documented `int(timeout) + 1020` seconds.
Explicit `--timeout` requests, and every request stored by older releases,
keep this fixed meaning across upgrades and remain resumable.

A fresh `task submit` with no time flags runs under the approved renewable
policy: 3600 s initially, 1800 s extensions, a 900 s progress window, and a
finite cap of 14400 s (four hours). `--budget-initial SECONDS` (1–7200)
chooses a different initial amount and is mutually exclusive with
`--timeout`; `--budget-cap SECONDS` sets the absolute cap (default 14400,
finite, positive, and at most 14400) and requires `--budget-initial`. The cap
must be at least the initial amount, extension, and progress window, but need
not fit a full additional extension after the initial amount. A final extension
is clipped to the cap. An explicit cap together with `--timeout` is refused:
a fixed limit cannot grow. The extension and the progress window are fixed;
there are no other knobs.

A budgeted request persists one optional `budget` object in its immutable
request, holding exactly the four numeric policy fields `initial_seconds`,
`extension_seconds`, `progress_window_seconds`, and `max_seconds`, and keeps
`request.timeout` as the renewable initial amount (it still bounds ordinary
validation such as the check timeout). The complete shape is validated
before persistence and again at worker entry: a non-object policy, booleans,
NaN, infinity, missing or unexpected fields, values outside the bounds, caps
below any policy duration, and initial amounts that disagree with
`request.timeout` all refuse before a job is queued or a writer is
launched, and the worker publishes the refusal as an ordinary retrievable
failed result. Execution's typed `RenewableBudget` policy remains the
authority on the policy's own invariants. The worker then launches with
`timeout=None` and that typed policy, so Execution owns the writer process
lifecycle: it grants or refuses each extension from the progress window and
terminates at the cap.

Successful confined write/edit operations qualify when they change files;
other restricted tools qualify only for novel inputs that complete successfully.
Repeated identical reads, failed operations, and unqualified activity do not
renew. The latest qualifying evidence must fall inside the 900 s window, and
reaching the cap ends the attempt. There is no
automatic continuation and no automatic retry at exhaustion. `task continue`
requires an unsuccessful terminal attempt with validated partial edits and a
fresh `--timeout` or `--budget-initial` choice, optionally with `--budget-cap`.
It never inherits the parent's budget or exhaustion. Completed tasks cannot be
continued. You can verify and apply completed work, then submit a fresh task.
If the edits are unaccepted, instead seed an isolated source copy with the
validated diff and submit that copy. `task apply` is bound to the retained
original project; it cannot target an arbitrary copy.

Each attempt's budget telemetry (policy, elapsed time, every extension
granted or refused with its evidence age, termination reason, progress counts,
and distinct changed-file count) is recorded by Execution inside the worker record of the published task result
and is parsed and validated with it at retrieval. It is never a separate
artifact and never locally reinvented, and it describes observed activity,
not usefulness or billed cost.

Snapshot exclusions apply to baseline files and to the carried link inventory, exactly alike: a link under an excluded path is dropped with that path, and a kept link whose resolution crosses an excluded path refuses the submission, because its target would be missing remotely. For example, `.weft.toml` with `[sync] exclude_dirs = ["/harness/src"]` excludes that root-relative path and its descendants while preserving `nested/harness/src`. Patterns without a leading `/` match path components at any depth.

A project file over the 2 MiB task file limit is carried, not refused. It stays out of the editable baseline and travels in the snapshot, so the writer and the `--check` command can read it; the request records its size and permissions. A result that returns it as an edit is refused, and so is a worker that changed it, since the protocol moves whole file contents and cannot carry one that large. Resume compares those sizes and permissions alongside the baseline. For inventory-bearing requests, carried symlinks are compared the same way: the worker censuses the exact inventory before generation, after the writer finishes, and after validation, and a missing, retargeted, replaced, added, or unsafe link fails the task even when its resolved contents happen to match. For legacy requests without that field, the worker still checks its remote snapshot; local resume/apply do not infer an empty inventory from the absent field.

Checks must preserve ordinary source files, including their permissions. A check that edits, creates, or deletes source files publishes `validation_failed` even if it exits successfully, with the writer's evidence and a diagnostic in the validation record. Excluded cache directories such as `__pycache__` and `.pytest_cache` may change.

A timed-out check publishes `validation_failed` with captured output and a timeout diagnostic. The worker exits unsuccessfully after publishing the result, so it remains available for retrieval and cannot be applied.

Worker setup and snapshot census failures publish a failed result with a diagnostic report and no changes. `task cancel` cancels an active Weft job; for an already-terminal job, it acknowledges the disposition without requesting another cancellation. A retained task proven never to have entered Weft submission can be canceled without contacting Weft, preserving its immutable request and preventing resume. For an attempted or missing-marker submission, `task cancel` derives the current attempt's key and calls public `weft job fence --idempotency-key KEY --json` in the retained project context. Only a valid `weft.job.fence.v1` fenced=true answer permits jobless closure. A returned receipt is durably bound before cancellation or acknowledgment. Lookup absence and failed, malformed, or interrupted fence calls never release custody; retry `task cancel` or use `task resume` to recover an accepted job. This path requires Weft's fence API on the submitting machine.

Retrieval validates task identity, retained request and worker digests, the writer's tool policy and transcript, and every changed file's baseline. It also returns a deterministic `review` object with `removed_files`, `removed_python_test_definitions` (path and qualified name), and `unassessed_test_files` (path and reason). This advisory inventory is derived from validated before/after changes, not writer claims; each list is capped at 1000 sorted entries and `truncated` reports omitted entries. Python scope is AST `def`/`async def test_*` at module scope or as direct class members; nested helpers are excluded. It does not claim pytest/unittest collection or parameterized-case totals. Candidate paths include Python `test_*.py` and `*_test.py`, JavaScript/TypeScript `*.test`/`*.spec` files with `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, or `.cjs` extensions, and every file beneath a `test/` or `tests/` directory component at any depth. Python AST review inventories only module-level and direct class-member `test_*` functions; syntax/decoding failures and non-Python candidates are explicitly unassessed. A whole-file deletion is listed as a removed file even when parsing fails, and supported definitions are also listed. Each category is capped at 1000 and `truncated` marks omitted records; `task diff` explicitly labels its stderr counts as capped when truncated. Deletions and warnings never block application; completion, check, baseline, and conflict gates determine eligibility. `task diff` writes only the patch to stdout and the review summary and warnings to stderr. Apply verifies the retained symlink inventory, then checks all destination files before writing any of them, refusing local conflicts, link paths, and protected agent or VCS metadata. Returned changes must be UTF-8 text; individual files are limited to 2 MiB and the source manifest to 10000 files and 64 MiB. Existing binary source files can be read but cannot be returned as edits. Application keeps a backup in the task journal and does not commit or deploy project changes.

If Weft cannot download a result, the diagnostic names the task and job and includes an exact `task retrieve` retry command. Retry retrieval with that same task ID. For a completed job, or a failed job whose artifact is reported ready, task status stays `unknown` while the result is unreadable. `task resume` does not submit another attempt in this case. Check output is stored in the validated result artifact, so job logs cannot substitute for it.

Successful application, confirmed cancellation or abandonment through `task cancel`, and resuming a failed attempt mark the disposed Weft job processed. Status, list, retrieval, and diff do not. The task response exposes `processed_at` for its disposition: acknowledgment of an accepted job, local closure of a proven never-submitted attempt, or closure after Weft's final-no-job fence. If acknowledgment fails after application, the applied marker and edits remain durable; repeat `task apply` to retry only the acknowledgment, without touching project files. A failed attempt's acknowledgment must succeed before resume queues a replacement. A job still running is not marked processed; repeat the disposition command after it finishes.

Tasks in the same registry serialize applications to a canonical project path. A competing application is refused until the current application finishes; retrying then checks its baseline against the updated files.

### Worker builds

Before creating a Weft job, `task submit` provisions an immutable worker build on the task host for the task's account, the account Weft runs jobs as there. The task record retains the build's absolute `bin_dir` and build ID, and Weft launches that build's worker by absolute path, so a queued job runs the build it was sent with. Provisioning does not change PATH or anything else on the host. Older task records without a pinned path are provisioned when resumed.

Provisioning always streams the installed `agent_offload` and `agent_execution` packages over `ssh ACCOUNT@HOST` into `~/.local/share/agent-offload/builds/BUILD_ID`, where the build ID is a digest of the two packages' source digests. It writes small launchers that run the packages with the first Python 3.11 or later that `uv python find` or `python3` supplies, installs agent-execution's pinned OMP runtime with Bun, and publishes the build by renaming a complete staging directory. A build that already exists is reused, so only the first task after an install pays for the copy. Because the copied bytes are the installed ones, the build's digests equal the dispatch's by construction.

`[worker] provider` accepts `ssh` or `agent-host-sync`; both configured values provision the same installed-package pair over SSH. The selected build is content-addressed by the pinned package digests and independent of source-checkout state.

The build's own worker reports its agent-offload and bundled agent-execution digests. The host checks staged packages before publishing, and the caller checks both returned digests against the request even when reusing an already-published build. A task whose recorded digests no longer match the installed pair refuses before anything is copied or built — the pinned bytes are never reconstructed from a checkout — and the refusal names the pinned and installed digests; that task identity cannot resume, so submit a new task for the same brief. Any other provisioning failure keeps the identity and names `agent-offload task resume TASK_ID`, which provisions again under the same idempotency identity.

Submit passes `--no-sync`, so `weft run` returns once the job is recorded and Weft's daemon pushes it to the host on its next pass; a task can therefore wait about a minute before it starts. If `weft run` still exceeds its 180-second budget, submit asks `weft job lookup --idempotency-key` whether the job was recorded, binds it if so, and submits once more if not. A lookup that fails or answers for another key leaves the submission unresolved. If submission is interrupted or unresolved, use `agent-offload task resume TASK_ID`; it always resumes under the same idempotency identity, preventing a second job. A changed baseline or carried-symlink inventory still refuses resume. For any unresolved task, including initial tasks and continuations, receipt recovery runs before the drift checks, so an already accepted job binds despite local drift; absence or lookup failure never permits sending changed source. Resuming a `failed_without_result` task submits the retained request again as a new attempt with its own idempotency key; the task keeps its identity and lists the earlier jobs in `prior_job_ids`. Resume provisions the retained matching worker build before resending, repairing a build that changed or was collected while a job was queued. If the submitting machine's agent-offload or agent-execution has changed since submission, resume refuses a failed job's resubmission; submit a new task for the same brief. `--id` lets a caller choose the identity before starting the command. Submission failures retain that identity and never fall back to local execution. Cancellation operates on an accepted Weft job, closes a proven never-submitted task, or obtains an atomic key-fence result for an unresolved submission.

Tasks execute agent-execution inside their pinned offload build. The separately installed `agent-execution-worker` may belong to a different agent-review release; changing that release does not change a task's execution code. Provisioning, worker execution, and result retrieval each enforce the task's exact package digests. Automatic provisioning leaves the host's live tool and PATH unchanged.

## Continuing partial work

The command prints the child identity. The time choice is required and fresh:
`--timeout` (fixed, 1–7200 seconds) or `--budget-initial SECONDS` (renewable,
with an optional `--budget-cap`). The child never inherits the parent's budget
or its exhaustion.
Use `--brief FILE` instead of `--prompt TEXT`, or omit both to retain the parent's
requirements. `--id CHILD` selects a new identity without overwriting an existing task.
The child inherits the parent's writer by default; `--selector SELECTOR` may choose another exact selector in the admitted OMP registry, including when the parent's writer has since been retired. An invalid selector refuses before child creation. A valid but unavailable selection creates durable child custody and gives a same-ID resume instruction after admission refusal; it does not submit to Weft until ordinary executor admission succeeds. The child inherits the parent's check, host, account, baseline, carried-file census, and immutable lineage.

Continuation validates the retained parent result and applies its partial patch only
inside the disposable worker snapshot. The local project's full editable manifest,
carried-file census, and carried-symlink inventory must still match the original
baseline, including the path set. Local changes refuse continuation; there is no automatic merge. The child request retains the parent's link inventory, and the child's worker verifies it with the same three censuses.

The parent remains unchanged, including its acknowledgment state. A failed parent
already acknowledged with `task cancel` is eligible. For `validation_failed`, the child
brief also carries the validated check argv, exit code (including null on timeout),
and bounded stdout/stderr/error as JSON-quoted `UNTRUSTED DIAGNOSTIC DATA`, never
instructions. Output tails are bounded per stream and in total, with truncation marked;
original requirements and manual continuation instructions remain separate. No check
result means no diagnostic is added. The inherited check still runs on the child result.
The child records the parent task, job, request digest, and canonical digest of the
immutable result artifact without copying the transcript. Derived local review annotations
and optional diff rendering do not enter that digest.
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

### Inspecting historical jobs

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

## Writer readiness (`doctor`) and admission

`agent-offload doctor` emits `agent-offload.doctor/v2`: the raw Execution-owned
`execution_status` envelope (including fact age/provenance) and an explicit
`admission` decision under `offload-task/v1`. It requires current available
capability, authentication, and transport; a current explicit quota refusal
blocks admission, while unknown quota is not a promise of capacity. It does not
claim generation success. Probe failures leave `execution_status` null and
include a visible policy reason. There is no universal `available` flag. For
example, check one exact writer:

```sh
agent-offload doctor --host HOST --selector openai-codex/gpt-6-luna --json
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

## Trust boundary of historical jobs

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

## Relay result records

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

After a source change to agent-offload or agent-execution, reinstall the local command:

```sh
cd agent-offload
just install
```

The next task copies the new build to the host. Tasks never build from source checkouts, so no checkout commit, revert, or host refresh step is involved in task provisioning.

`just install` builds a new release under `~/.local/share/agent-offload/releases/` with agent-execution bundled, checks that it starts, and only then repoints `releases/current`, which the `~/.local/bin` entry points resolve through. agent-execution is not a declared dependency, so a plain `uv tool install .` produces an environment in which every command exits with a message naming the missing package.
