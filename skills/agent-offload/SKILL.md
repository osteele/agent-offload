---
name: agent-offload
description: Run durable report-only coding-agent work on Studio through Weft. Use when analysis, investigation, or review should be offloaded to Studio; when a task should survive the requesting session; when choosing between local and remote agent execution; or when retrieving and validating an agent-offload job.
---

# Agent Offload

Use `agent-offload` for self-contained repository analysis whose deliverable is a report. The current Studio lane is Codex-only and report-only. Never send a write task, expect remote edits, or treat the returned report as verified fact. Codex runs in a disposable Weft snapshot; the worker rejects the report if the source-tree digest changes.

The worker pins `gpt-5.5`. Do not override it with `gpt-6-astra`: Studio's Codex
0.153.4 code-mode host times out before repository reads for that model.

## Route the task

Offload when the brief can stand alone, the result may arrive later, and another context is useful. Keep work local when it needs interaction, GUI state, local-only credentials, or edits to the requesting working copy.

For automatic selection:

```sh
python ~/.claude/skills/coding-delegate/pick_delegate.py pick \
  --mode report --executor auto --brief BRIEF
```

`auto` prefers Studio and falls back locally. Use `--executor studio` when fallback would violate the task's resource or isolation requirement. Studio refuses `--mode write`.

## Submit

Make the brief name the repository scope, exact questions, exclusions, evidence to inspect, and the report format. Then run:

```sh
agent-offload run PROJECT --brief BRIEF
```

Use `--detach` for an asynchronous job. The printed `wj…` identifier is the durable handle. A local wait timeout does not cancel the job.

Do not bypass `agent-offload doctor`. It checks versioned Weft inventory plus Studio Codex authentication. The worker performs the authoritative protocol and source-digest check before model launch.

## Retrieve and verify

```sh
agent-offload status JOB
agent-offload retrieve JOB
```

Retrieval discovers artifact names from the versioned Weft job record, validates the result schema, task binding, report digest, worker identity, nested Agent Review evidence, and separate report bytes, then marks the job processed.

The result is delegated work. Apply the `delegated-work-verification` skill before relying on findings or changing code from them. Inspect the nested `harness_result` in JSON when provenance, the Codex transcript, stderr, or ctx evidence matters:

```sh
agent-offload retrieve JOB --json
```

## Operate

Use Weft for job lifecycle operations:

```sh
weft job inspect JOB --json
weft job kill JOB
```

Refresh the installed worker only through the Studio account updater:

```sh
cd ~/code/agent-tools/agent-host-sync
uv run agent-host-sync --account agent plan --stage tools --tool agent-offload
uv run agent-host-sync --account agent apply --stage tools --tool agent-offload
```

The updater's exit code 4 means installation succeeded but existing remote sessions must restart. New Weft jobs start fresh processes and use the updated executable.
