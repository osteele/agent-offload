# agent-offload

`agent-offload` validates and retrieves results from historical report-only
Codex jobs submitted through Weft. New native Codex submissions are retired:
`run`, direct submission, and remote worker execution refuse the provider before
launching it.

Use the grounded OMP Anthropic route for repository-grounded execution.
OpenAI through OMP is packet-only.

## Historical jobs

Inspect or retrieve an existing job:

```sh
agent-offload status wj1234
agent-offload retrieve wj1234
```

Retrieval validates the result envelope and report artifact before displaying
either. By default it marks a successfully validated job processed. Pass
`--no-mark-processed` for read-only validation.

`agent-offload doctor` reports native Codex execution as retired without probing
Studio. `agent-offload run` refuses before creating a Weft job.

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
uv tool install --force --reinstall ../agent-review/packages/agent-execution
uv tool install --force --reinstall .
```
