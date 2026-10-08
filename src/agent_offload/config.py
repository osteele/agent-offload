"""User configuration, read from ``$XDG_CONFIG_HOME/agent-offload/config.toml``.

Every key is optional. A command-line flag overrides the file, and a command
that needs a host refuses to guess one when neither supplies it.
"""

from __future__ import annotations

import getpass
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .task_protocol import TaskError

WORKER_PROVIDERS = ("ssh", "agent-host-sync")

# section -> key -> expected type
KEYS: dict[str, dict[str, type]] = {
    "tasks": {"host": str, "account": str},
    "worker": {"provider": str},
    "relay": {"host": str},
}


@dataclass(frozen=True)
class Config:
    task_host: str | None = None
    account: str | None = None
    worker_provider: str = "ssh"
    relay_host: str | None = None


def config_path() -> Path:
    configured = os.environ.get("AGENT_OFFLOAD_CONFIG")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "agent-offload" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = config_path() if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise TaskError(f"{path}: {error}") from error
    for section, values in data.items():
        if section not in KEYS or not isinstance(values, dict):
            raise TaskError(f"{path}: unknown section [{section}]")
        for key, value in values.items():
            expected = KEYS[section].get(key)
            if expected is None:
                raise TaskError(f"{path}: unknown key {section}.{key}")
            if not isinstance(value, expected) or not value:
                raise TaskError(f"{path}: {section}.{key} must be a nonempty string")
    provider = data.get("worker", {}).get("provider", "ssh")
    if provider not in WORKER_PROVIDERS:
        raise TaskError(
            f"{path}: worker.provider must be one of {', '.join(WORKER_PROVIDERS)}, "
            f"not {provider!r}"
        )
    return Config(
        task_host=data.get("tasks", {}).get("host"),
        account=data.get("tasks", {}).get("account"),
        worker_provider=provider,
        relay_host=data.get("relay", {}).get("host"),
    )


def require_task_host(host: str | None, config: Config) -> str:
    resolved = host or config.task_host
    if not resolved:
        raise TaskError(f"no host: pass --host, or set [tasks] host in {config_path()}")
    return resolved


def require_relay_host(host: str | None, config: Config) -> str:
    resolved = host or config.relay_host
    if not resolved:
        raise TaskError(
            f"no relay host: pass --host USER@HOST, or set [relay] host in {config_path()}"
        )
    return resolved


def task_account(account: str | None, config: Config) -> str:
    """The account Weft runs jobs as on the task host; by default, the local user."""
    return account or config.account or getpass.getuser()
