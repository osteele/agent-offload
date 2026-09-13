set shell := ["zsh", "-cu"]

default:
    @just --list

check:
    uv run ruff check src tests
    uv run --with-editable ../agent-execution python -m pytest
    bun test src/agent_offload/relay_runtime
    bun run --cwd src/agent_offload/relay_runtime typecheck

format:
    uv run ruff format src tests
