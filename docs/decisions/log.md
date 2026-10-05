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
