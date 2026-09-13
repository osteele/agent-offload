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

## Attached relay (`relay`)

`agent-offload relay` is a second, deliberately different lane: the agent
runtime runs on a remote SSH host (default `agent@studio`) through OMP's
maintained RPC client, while its three tools — `review_read`, `review_glob`,
`review_grep` — execute **on this machine, against the live project root**.
The purpose is moving agent-process memory off the laptop without giving up
grounded access to the working tree.

```sh
agent-offload relay /path/to/project --prompt "Map the auth flow" --timeout 600
agent-offload relay . --prompt-file brief.txt --model provider/model --json
```

Trust boundary, by contrast with `run`:

- **Attached.** The command stays in the foreground; the remote process tree
  dies with it. `--detach` is refused, there is no job id and no resume.
- **Not durable, not snapshot-backed.** No source snapshot leaves the laptop.
  The remote side sees project instructions and the prompt only, both sent
  explicitly with digests recorded. Because the tree is live, two reads can
  observe different revisions; the result records a per-call provenance entry
  (real path, SHA-256, byte count, truncation) for every served read instead
  of claiming snapshot equivalence.
- **Confined.** Tool paths are canonicalized and must resolve inside the
  project root; URL/device schemes, parent traversal, symlink escapes,
  binary and oversized inputs are refused. Reads, matches, files, tool-call
  argument size, and wall time are bounded.
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
uv tool install --force --reinstall --with ../agent-execution .
```
