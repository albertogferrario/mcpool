import sys
from pathlib import Path

import pytest

FAKE_SERVER = [sys.executable, str(Path(__file__).parent / "fake_server.py")]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME with empty Claude and Codex configs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "CODEX_HOME"):
        monkeypatch.delenv(variable, raising=False)
    (tmp_path / ".codex").mkdir()
    return tmp_path
