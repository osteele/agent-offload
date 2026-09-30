"""Console entry points that name a missing agent-execution before importing it.

agent-execution is bundled with `--with ../agent-execution` rather than declared,
so an install that omits it builds cleanly and then fails in every command. The
check lives here, not in the package `__init__`, because agent-host-sync imports
agent-execution-free submodules such as `agent_offload.relay` from source.
"""

import sys

MISSING_EXECUTION_MESSAGE = (
    "agent-offload: the agent_execution package is not installed in this environment.\n"
    "agent-offload needs agent-execution bundled alongside it; it is not a declared dependency.\n"
    "Reinstall from ~/code/agent-tools/agent-offload with `just install`, or\n"
    "`uv tool install --force --reinstall --with ../agent-execution .`."
)


def require_agent_execution() -> None:
    try:
        import agent_execution  # noqa: F401
    except ModuleNotFoundError as error:
        if error.name != "agent_execution":
            raise
        sys.exit(MISSING_EXECUTION_MESSAGE)


def cli() -> None:
    require_agent_execution()
    from .cli import main

    main()


def worker() -> None:
    require_agent_execution()
    from .worker_cli import main

    main()


def task_worker() -> None:
    require_agent_execution()
    from .task_worker import main

    main()
