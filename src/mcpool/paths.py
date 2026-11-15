"""File locations. Computed on each call so that tests and callers can redirect HOME."""

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def _xdg(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or home() / default)


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "mcpool"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "mcpool"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "mcpool"


def settings_file() -> Path:
    return config_dir() / "config.toml"


def registry_file() -> Path:
    return data_dir() / "registry.json"


def builds_dir() -> Path:
    return data_dir() / "builds"


def shared_dir() -> Path:
    """State of the shared servers: one directory per server."""
    return state_dir() / "shared"


def manage_state_file() -> Path:
    return state_dir() / "manage-state.json"


def log_file() -> Path:
    return state_dir() / "mcpool.log"


def claude_config() -> Path:
    return home() / ".claude.json"


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or home() / ".codex") / "config.toml"
