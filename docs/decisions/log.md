# Decision log

Smaller decisions that still foreclosed something, newest last. The full
convention (tiers, the credible-alternative gate, full records) is the
decision-records skill; a full record needs Oliver's explicit confirmation.

- **2026-09-26** — In the context of dispatching tasks to a shared worker,
  facing tooling skew between the submitting CLI, its bundled
  agent-execution, and the installed worker, we decided to refuse on any
  identity mismatch at submit, worker start, and retrieval, and neglected
  warn-and-continue and self-healing the skew from inside submit, to keep
  `task retrieve`/`apply` results attributable to exact tooling bytes,
  accepting a wasted queue slot per skew and keeping host updates in the
  manual `agent-host-sync` lane (agent-host-sync decision 0001: never mutate
  an installed path in place). Ratified by Oliver on 2026-09-26
  (relayed by Noble Harmonica; which live session hosted the ratification is
  not established, and at least two agent-offload sessions exist). Enforcement
  sites are `tasks.py` submit preflight and retrieval,
  and `task_worker.py` worker start.
- **2026-10-05** — In the context of a repository whose snapshot holds a file
  over the 2 MiB task file limit, facing a submit that refused the whole
  project over a file no task would edit (ao11), we decided to carry such a
  file read-only — outside the editable baseline, censused by size and mode,
  refused if a worker alters it or a result returns it as an edit — and
  neglected a task-scoped exclusion flag or project-level task config, to keep
  one plain submit command working on a project holding a corpus or fixture
  without a second exclusion surface beside `.weft.toml`, accepting that an
  oversized file is silently unavailable for editing and that its integrity is
  judged by stat rather than by content.
- **2026-10-09** — In the context of running task workers on hosts that do not
  have the author's account updater, facing a submit that could not create a
  job without agent-host-sync, we decided that agent-offload provisions the
  worker itself by copying the installed agent-offload and agent-execution
  packages over SSH into a content-addressed build, and neglected discovering a
  worker the user preinstalled on the host, to keep a fresh host usable with
  nothing installed but Python and Bun and to make the remote digests equal the
  local ones by construction, accepting a per-version copy over SSH and a build
  directory per installed version that nothing yet collects. agent-host-sync
  remains a configurable provider. Chosen by Oliver on 2026-10-09 (relayed by
  Glowing Glow, which presented the choice).
- **2026-10-09** — In the context of removing the author's host from the
  command defaults, facing a choice of where a user's default host lives, we
  decided on a TOML file (`~/.config/agent-offload/config.toml`, read with
  `tomllib`) and neglected an `AGENT_OFFLOAD_HOST` environment variable, to hold
  the task host, its account, the relay destination, and the worker provider
  in one validated place, accepting a Python 3.11 floor. Chosen by Oliver on
  2026-10-09 (relayed by Glowing Glow).
