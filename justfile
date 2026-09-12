set shell := ["zsh", "-cu"]

default:
    @just --list

check:
    uv run ruff check src tests
    uv run --with ../agent-execution pytest
    bun test src/agent_offload/relay_runtime

format:
    uv run ruff format src tests
