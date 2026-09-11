# agent-offload

`agent-offload` runs a report-only coding-agent task as a durable Weft job on Studio and returns a validated Markdown report. It is for repository analysis, investigation, and review that can run independently of the requesting session.

The current lane is deliberately report-only. Codex runs against a disposable Weft source snapshot. The worker hashes the submitted source before and after execution and rejects the report if any source byte, path, symlink target, or file mode changed. It never returns a patch or mutates the requesting working tree.

Studio tasks pin Codex `gpt-5.5`. On Studio, Codex CLI 0.153.4 routes
`gpt-6-astra` repository access through a code-mode host that times out before
the first read. The conductor rejects other selectors instead of returning an
authoritative-looking report that inspected no source.

## When to offload

Use Studio when all of these are true:

- The deliverable is a report, not a working-tree change.
- The brief is self-contained and names its evidence and acceptance criteria.
- The task benefits from another model context or can outlive the local session.
- Studio's Codex quota is available.

Keep work local when it needs interactive clarification, local GUI state, credentials that are absent on Studio, edits to the live working copy, or tight iteration with the requesting agent.

## Run a task

Write the brief to a file, then run:

```sh
agent-offload run /path/to/project --brief /path/to/brief.txt
```

The command checks Studio inventory and Codex authentication, submits the current project working tree through Weft, waits for the durable result, validates the result and report artifacts, prints the report, and marks the job processed.

Use a specific reasoning effort or detach after submission:

```sh
agent-offload run . --brief brief.txt --effort high
agent-offload run . --brief brief.txt --detach
```

A detached run prints its Weft job ID. Retrieve it later:

```sh
agent-offload status wj1234
agent-offload retrieve wj1234
```

If a synchronous wait expires, the job continues on Studio. The error prints the same retrieval command.

## Pick an executor

`coding-delegate` can select the Studio lane without applying the local memory-pressure gate:

```sh
python ~/.claude/skills/coding-delegate/pick_delegate.py pick \
  --mode report --executor auto --brief brief.txt
```

`--executor auto` prefers Studio for a report and falls back to a local provider when the remote lane is unavailable. `--executor studio` refuses instead of falling back. Studio write tasks are refused.

Record the durable job with the dispatch history:

```sh
python ~/.claude/skills/coding-delegate/pick_delegate.py record codex ok \
  --executor studio --job-id wj1234 --task analysis-key --session "$CODEX_THREAD_ID"
```

## Trust boundary

The conductor sends the brief through Weft's private payload channel. The repository travels through Weft's source snapshot mechanism, which includes the submitted working tree rather than only committed files.

Each task binds:

- the brief SHA-256 digest;
- a random task and idempotency identifier;
- the exact installed `agent-offload-worker` source digest;
- the Weft source pin;
- the nested `agent-review-worker` result, Codex session ID, stdout, stderr, and ctx evidence;
- the report SHA-256 digest and separate Markdown artifact.

The remote worker refuses source or protocol drift before launching Codex. The conductor validates both returned artifacts before displaying the report. A malformed or ambiguous transport response is not treated as permission to submit a duplicate task.

## Operations

Check readiness without a billed model call:

```sh
agent-offload doctor
```

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

After a worker source change, reinstall the local command and redeploy before submitting:

```sh
cd ~/code/agent-tools/agent-offload
uv tool install --force --reinstall .
```
