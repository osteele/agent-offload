from __future__ import annotations

import argparse
import json
import sys

from agent_execution.execution_status import add_status_parser

from .protocol import SUPPORTED_MODE, SUPPORTED_PROVIDER, worker_identity
from .worker import execute


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-offload-worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    identity = subparsers.add_parser("identity", help="print the installed worker identity")
    identity.set_defaults(handler=cmd_identity)
    add_status_parser(subparsers)

    run = subparsers.add_parser("execute", help="execute one report-only agent task")
    run.add_argument("--task-id", required=True)
    run.add_argument("--provider", default=SUPPORTED_PROVIDER)
    run.add_argument("--mode", default=SUPPORTED_MODE)
    run.add_argument("--harness-selector", required=True)
    run.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    run.add_argument("--prompt-payload", required=True)
    run.add_argument("--expect-prompt-sha256", required=True)
    run.add_argument("--expect-protocol", type=int, required=True)
    run.add_argument("--expect-source-sha256", required=True)
    run.add_argument("--harness-timeout", type=float, default=900.0)
    run.add_argument("--ctx-timeout", type=float, default=90.0)
    run.set_defaults(handler=cmd_execute)
    return parser


def cmd_identity(_args: argparse.Namespace) -> int:
    print(json.dumps(worker_identity(), sort_keys=True))
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    result, result_path, report_path = execute(
        task_id=args.task_id,
        provider=args.provider,
        mode=args.mode,
        selector=args.harness_selector,
        effort=args.effort,
        prompt_payload=args.prompt_payload,
        prompt_sha256=args.expect_prompt_sha256,
        expect_protocol=args.expect_protocol,
        expect_source_sha256=args.expect_source_sha256,
        harness_timeout=args.harness_timeout,
        ctx_timeout=args.ctx_timeout,
    )
    summary = {
        "status": result["status"],
        "result_path": str(result_path),
        "report_path": str(report_path) if report_path.is_file() else None,
        "failure": result["failure"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "execution-status":
        # The bundled Offload worker need not contain an agent-execution-worker,
        # and the global worker belongs to Review, so status observations
        # forward to this build's own Execution implementation. Its argument
        # parser and JSON envelope are the authority; the argv is forwarded
        # untouched and a nonzero status must not be swallowed into exit zero.
        from agent_execution.worker_cli import main as execution_main

        raise SystemExit(execution_main(arguments))
    args = build_parser().parse_args(arguments)
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main(sys.argv[1:])
