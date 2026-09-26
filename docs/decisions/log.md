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
  an installed path in place). Ratified by Oliver in the agent-offload
  session; enforcement sites are `tasks.py` submit preflight and retrieval,
  and `task_worker.py` worker start.
