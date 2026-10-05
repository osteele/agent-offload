set shell := ["zsh", "-cu"]

default:
    @just --list

check:
    uv run ruff check src tests
    uv run ruff format --check src tests
    uv run --with-editable ../agent-execution python -m pytest
    # Install the locked toolchain first so a fresh or stale checkout (a
    # remote workspace, say) typechecks with the pinned tsc, not one on PATH.
    bun install --frozen-lockfile --cwd src/agent_offload/relay_runtime
    bun test src/agent_offload/relay_runtime
    bun run --cwd src/agent_offload/relay_runtime typecheck

format:
    uv run ruff format src tests

releases := env("HOME") / ".local/share/agent-offload/releases"
bin_dir := env("HOME") / ".local/bin"
entry_points := "agent-offload agent-offload-worker agent-offload-task-worker"

# Build a release with agent-execution bundled, verify it starts, then flip
# `current` so no reader ever sees a half-built environment. Older releases are
# left in place for rollback. mktemp gives every invocation its own directory
# and pointer name, so two installs started together never build into the same
# release, and neither can rebuild the one `current` already names.
install:
    #!/usr/bin/env zsh
    set -euo pipefail
    mkdir -p "{{releases}}"
    release="$(mktemp -d "{{releases}}/$(date +%Y%m%dT%H%M%S).XXXXXX")"
    UV_TOOL_DIR="$release/tools" UV_TOOL_BIN_DIR="$release/bin" \
        uv tool install --reinstall --with ../agent-execution .
    "$release/bin/agent-offload-worker" identity >/dev/null
    pointer="$release.current"
    ln -s "$release" "$pointer"
    mv -fh "$pointer" "{{releases}}/current"
    mkdir -p "{{bin_dir}}"
    for name in {{entry_points}}; do
        ln -sfn "{{releases}}/current/bin/$name" "{{bin_dir}}/$name"
    done
    echo "installed $release"
    "{{bin_dir}}/agent-offload-worker" identity

# Install on Studio through agent-host-sync; account is agent or osteele.
deploy account="agent":
    agent-host-sync --account {{account}} apply --stage tools --tool agent-offload

# Show what deploying to Studio would change, without changing anything.
deploy-status account="agent":
    agent-host-sync --account {{account}} plan --stage tools --tool agent-offload
