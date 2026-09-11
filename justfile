set shell := ["zsh", "-cu"]

default:
    @just --list

check:
    uv run ruff check src tests
    uv run pytest

format:
    uv run ruff format src tests
