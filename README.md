# agent-offload

Runs coding agents on another machine, so their memory and CPU stay off the
machine you work on. It has two modes:

- A **task** sends a brief and a snapshot of a project to an agent on a remote
  host, as a [Weft](https://github.com/osteele/weft) job. The agent edits the
  snapshot without a shell, an optional check command runs on the result, and
  the edits come back as a diff to inspect and apply locally.
- A **relay** runs the agent process on a remote host while its read-only tools
  work against the live project on this machine.

Each agent run uses [agent-execution](https://github.com/osteele/agent-execution),
which confines the agent's tools and records what it was given and what it did.

## Status

Used daily from a macOS laptop with a macOS remote host. CI also runs the tests
on Linux. Windows is not supported; use WSL. There are no releases; install
from a checkout.

## Requirements

- Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and
  [just](https://just.systems/).
- [agent-execution](https://github.com/osteele/agent-execution), checked out
  beside this repository. It is bundled into the install rather than declared
  as a dependency.
- For tasks: [Weft](https://github.com/osteele/weft), with the remote host
  set up for it ([docs/weft.md](docs/weft.md)). The account Weft runs jobs as
  must be reachable as `ssh ACCOUNT@HOST` and have Python 3.11 or later (or
  uv) and [Bun](https://bun.sh/).
- For the relay: [Bun](https://bun.sh/) locally, and SSH access to a host with
  [OMP](https://github.com/can1357/oh-my-pi) installed.
- On the remote host, credentials for the model that does the writing.

## Install

```sh
git clone https://github.com/osteele/agent-execution.git
git clone https://github.com/osteele/agent-offload.git
cd agent-offload
just install
```

`just install` builds a release under `~/.local/share/agent-offload/releases/`
with agent-execution bundled, checks that it starts, and then points
`releases/current` at it. The commands in `~/.local/bin` run through that link.
A plain `uv tool install .` leaves agent-execution out, and every command then
exits with a message naming the missing package.

## Configuration

Commands that run on another machine need a host: pass `--host`, or set
defaults in `~/.config/agent-offload/config.toml`. `$XDG_CONFIG_HOME` is
honored, and `AGENT_OFFLOAD_CONFIG` names a different file.

```toml
[tasks]
host = "build-box"         # Weft host for `task submit` and `doctor`
account = "agent"          # account Weft runs jobs as there; default: your user name

[relay]
host = "agent@build-box"   # SSH destination for `relay`
```

Unknown keys are errors, and a command-line flag overrides the file.

## Tasks

```sh
agent-offload task submit PROJECT --brief brief.txt --host HOST --check 'just check'
agent-offload task status TASK_ID
agent-offload task diff TASK_ID
agent-offload task apply TASK_ID
```

Other subcommands are `list`, `retrieve`, `resume`, `continue`, `cancel`, and
`selectors`.

- `task submit` records the request under `~/.local/state/agent-offload/tasks`
  (or `AGENT_OFFLOAD_STATE`), copies the installed agent-offload and
  agent-execution packages to a versioned directory on the host if they are
  not there yet, and queues a Weft job that runs that copy. It returns without
  waiting for the agent.
- The agent reads and edits the snapshot through confined tools and cannot run
  shell commands. Files over 2 MiB can be read but not edited.
- `--check` runs a command on the result after the agent finishes. If the check
  fails, or changes source files, the result is marked `validation_failed` and
  cannot be applied.
- `task apply` checks the result against the files the task started from and
  refuses to overwrite local changes. It keeps a backup and does not commit.
- `task continue` starts a new task from a failed task's retained edits, carries failed-check output as quoted untrusted diagnostic data, and accepts `--selector` to choose another admitted writer.
- `task retrieve` includes an advisory destructive-change summary. `task diff` keeps stdout as a patch and prints review warnings to stderr.
- `task resume` recovers an interrupted submission without creating a second
  job.

The default writer is `openai-codex/gpt-6-luna`; `--selector` chooses another
from the list `task selectors` prints. The agent's time limit defaults to 1800
seconds, and `--timeout` accepts up to 7200.

To check that a writer is reachable and admitted before submitting:

```sh
agent-offload doctor --host HOST --selector openai-codex/gpt-6-luna --json
```

A first task, once the host runs Weft jobs ([docs/weft.md](docs/weft.md)):

```sh
agent-offload doctor --host HOST     # prints "admit: ..." and exits 0
agent-offload task submit . --host HOST --prompt "Add a docstring to main()"
agent-offload task status TASK_ID    # repeat until "status" is "completed"
agent-offload task diff TASK_ID
```

`submit` prints the task ID. While the job runs, `status` is Weft's job status,
starting at `queued`; Weft starts the job about a minute after submission. The
task ends `completed` when its edits are ready to apply, `validation_failed`
when the check failed or changed files, or `failed`; `task retrieve` shows the
agent's report either way.

## Relay

```sh
agent-offload relay /path/to/project --host USER@HOST --prompt "Map the auth flow" --timeout 600
agent-offload relay . --host USER@HOST --prompt-file brief.txt --json
```

The agent runs on the remote host through OMP, but its three tools
(`review_read`, `review_glob`, `review_grep`) run on this machine against the
live project. The relay differs from a task in these ways:

- **It stays attached.** The command runs in the foreground, and the remote
  agent stops when it does. There is no job ID and no resume.
- **Nothing is copied.** No snapshot leaves this machine. Because the project is
  live, two reads can see different versions, so the result records the path,
  SHA-256, and size of every file served.
- **Tools are confined to the project.** Paths must resolve inside the project
  root. Parent traversal, symlink escapes, and binary or oversized files are
  refused, and reads, matches, and wall time are bounded.
- **The remote side starts empty.** OMP's built-in tools, extensions, skills,
  rules, and sessions are disabled, so nothing in the remote account reaches
  the agent.
- **It fails closed.** The relay stops unless exactly its three tools are
  registered, and it never falls back to remote tools.

The relay prints the agent's final response under `=== ATTACHED RELAY RESULT
===` and exits 0; a failed run prints `failure: REASON` and exits 4. Either
way the full record, including every file the tools served, is written to
`outputs/relay-result-*.json`, and `--json` prints it.

## Retired report jobs

An earlier lane ran report-only Codex jobs. New submissions are refused, but
`agent-offload status JOB` and `agent-offload retrieve JOB` still inspect and
validate existing ones.

## Reference

[docs/reference.md](docs/reference.md) covers the detailed contract:
- task states, retries, and limits
- worker builds and their providers
- cancellation and acknowledgment
- writer admission
- result records
- operations

[docs/weft.md](docs/weft.md) covers what tasks need from Weft and how to check
a host.

## License

MIT. See [LICENSE](LICENSE).
