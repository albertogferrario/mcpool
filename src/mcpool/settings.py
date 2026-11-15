"""User settings in ~/.config/mcpool/config.toml, written by `mcpool install`."""

import shutil
import subprocess
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from mcpool import paths


@dataclass
class Settings:
    node: str = ""
    npm: str = ""
    npm_root: str = ""
    uv: str = ""
    serena: str = ""
    cooldown_days: int = 7
    unused_grace_days: int = 1
    postpone_notice_days: int = 7
    idle_shutdown_seconds: int = 600
    daily_hour: int = 4
    daily_minute: int = 30


def load() -> Settings:
    file = paths.settings_file()
    if not file.exists():
        return detect()
    data = tomllib.loads(file.read_text())
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in data.items() if k in known})


def save(settings: Settings) -> None:
    file = paths.settings_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k} = {v!r}" if isinstance(v, int) else f'{k} = "{v}"' for k, v in asdict(settings).items()]
    file.write_text("# Written by `mcpool install`; edit freely.\n" + "\n".join(lines) + "\n")


def detect() -> Settings:
    """Absolute paths of the tools mcpool runs, from the current PATH."""
    npm = shutil.which("npm") or ""
    sibling = Path(npm).parent / "node" if npm else None
    node = str(sibling) if sibling and sibling.exists() else shutil.which("node") or ""
    npm_root = ""
    if npm:
        result = subprocess.run([npm, "root", "-g"], capture_output=True, text=True)
        npm_root = result.stdout.strip() if result.returncode == 0 else ""
    return Settings(node=node, npm=npm, npm_root=npm_root,
                    uv=shutil.which("uv") or "", serena=shutil.which("serena") or "")
