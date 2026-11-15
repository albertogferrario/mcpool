"""Background jobs on macOS (launchd).

mcpool.reconcile  runs `mcpool reconcile` whenever ~/.claude.json or ~/.codex/config.toml
                  changes; it returns at once when the MCP entries are unchanged.
mcpool.daily      runs `mcpool daily` once a day at the configured time, or at wake.

Both run at background priority.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from mcpool import paths
from mcpool.manage import mcpool_command
from mcpool.settings import Settings

LABELS = ("mcpool.reconcile", "mcpool.daily")
SYSTEM_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def agents_dir() -> Path:
    return paths.home() / "Library" / "LaunchAgents"


def plist_path(label: str) -> Path:
    return agents_dir() / f"{label}.plist"


def _search_path(settings: Settings) -> str:
    dirs = [str(Path(p).parent) for p in (settings.node, settings.uv, settings.serena) if p]
    dirs += [str(paths.home() / ".local/bin"), *SYSTEM_PATH]
    return ":".join(dict.fromkeys(dirs))


def _agent(label: str, arguments: list[str], settings: Settings, **trigger) -> dict:
    log = str(paths.state_dir() / "launchd.log")
    return {"Label": label, "ProgramArguments": [mcpool_command(), *arguments],
            "EnvironmentVariables": {"PATH": _search_path(settings), "HOME": str(paths.home())},
            "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
            "StandardOutPath": log, "StandardErrorPath": log, **trigger}


def definitions(settings: Settings) -> dict[str, dict]:
    return {
        "mcpool.reconcile": _agent("mcpool.reconcile", ["reconcile"], settings,
                                   WatchPaths=[str(paths.claude_config()), str(paths.codex_config())],
                                   ThrottleInterval=15),
        "mcpool.daily": _agent("mcpool.daily", ["daily"], settings,
                               StartCalendarInterval={"Hour": settings.daily_hour, "Minute": settings.daily_minute}),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(settings: Settings) -> None:
    if sys.platform != "darwin":
        raise SystemExit("mcpool: background jobs are only implemented for macOS (launchd) so far")
    agents_dir().mkdir(parents=True, exist_ok=True)
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for label, definition in definitions(settings).items():
        _launchctl("bootout", f"{domain}/{label}")
        plist_path(label).write_bytes(plistlib.dumps(definition))
        result = _launchctl("bootstrap", domain, str(plist_path(label)))
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {label} failed: {result.stderr.strip()}")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    for label in LABELS:
        _launchctl("bootout", f"{domain}/{label}")
        plist_path(label).unlink(missing_ok=True)


def loaded() -> dict[str, bool]:
    domain = f"gui/{os.getuid()}"
    return {label: _launchctl("print", f"{domain}/{label}").returncode == 0 for label in LABELS}
