"""Where adopted packages come from: npm, GitHub (built from source) and PyPI (uv tools).

Each source installs, reports and removes one package, and resolves the command
that runs it directly. Version choice applies a release cooldown: the newest
stable release published at least `cooldown_days` ago, never a pre-release or a
yanked release, and never older than the installed one. A hijacked release is
usually pulled within days, and a startup check cannot detect malicious code.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from mcpool import paths
from mcpool.settings import Settings

STABLE = re.compile(r"\d+(\.\d+)*")


def run(cmd: list[str], cwd: str | None = None, timeout: int = 900) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{output[-300:]} (from {Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''})")
    return result.stdout


def version_key(version: str) -> tuple:
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.+]", version))


def eligible(published: dict[str, datetime], installed: str | None, cooldown_days: int,
             now: datetime | None = None) -> str | None:
    """Newest stable version public for cooldown_days; never older than the installed one."""
    cutoff = (now or datetime.now().astimezone()) - timedelta(days=cooldown_days)
    stable = [v for v, when in published.items() if STABLE.fullmatch(v) and when <= cutoff]
    best = max(stable, key=version_key, default=None)
    if installed and (best is None or version_key(best) <= version_key(installed)):
        return installed
    return best


def newest_stable(published: dict[str, datetime]) -> str | None:
    return max((v for v in published if STABLE.fullmatch(v)), key=version_key, default=None)


class Npm:
    """A package installed globally with npm and run with node."""

    def __init__(self, key: str, info: dict, settings: Settings):
        self.key, self.info, self.settings = key, info, settings

    @property
    def marker(self) -> str:
        return f"{self.settings.npm_root}/{self.key}/"

    def installed(self) -> str | None:
        manifest = Path(self.settings.npm_root) / self.key / "package.json"
        return json.loads(manifest.read_text()).get("version") if manifest.exists() else None

    def published(self) -> dict[str, datetime]:
        times = json.loads(run([self.settings.npm, "view", self.key, "time", "--json"]))
        return {v: datetime.fromisoformat(t.replace("Z", "+00:00"))
                for v, t in times.items() if v not in ("created", "modified")}

    def candidate(self) -> str | None:
        return eligible(self.published(), self.installed(), self.settings.cooldown_days)

    def newest(self) -> str | None:
        return newest_stable(self.published())

    def install(self, version: str) -> None:
        run([self.settings.npm, "i", "-g", f"{self.key}@{version}", "--no-audit", "--no-fund"])

    def uninstall(self) -> None:
        run([self.settings.npm, "rm", "-g", self.key])

    def command(self, binary: str | None = None) -> list[str]:
        """node and the script npx would run: the named bin, the only bin, or the one named after the package."""
        root = Path(self.settings.npm_root) / self.key
        manifest = json.loads((root / "package.json").read_text())
        bins = manifest.get("bin")
        if isinstance(bins, str):
            script = bins
        else:
            bins = bins or {}
            if not bins:
                raise RuntimeError(f"{self.key} declares no bin")
            script = bins.get(binary or "") or bins.get(self.key.split("/")[-1]) or next(iter(bins.values()))
        return [self.settings.node, str((root / script).resolve())]


class GitHub:
    """A Node package built from a GitHub repository into builds/<key>-<sha>, run through <key>-current."""

    def __init__(self, key: str, info: dict, settings: Settings):
        self.key, self.info, self.settings = key, info, settings
        self.current = paths.builds_dir() / f"{key}-current"

    @property
    def marker(self) -> str:
        return str(self.current)

    def installed(self) -> str | None:
        sha = self.current / ".mcpool-sha"
        return sha.read_text().strip()[:12] if sha.exists() else None

    def candidate(self) -> str:
        ref = self.info.get("ref", "HEAD")
        remote = run(["git", "ls-remote", f"https://github.com/{self.info['repo']}", ref]).split()[0]
        installed = self.installed()
        return installed if installed and remote.startswith(installed) else remote

    def newest(self) -> str:
        return self.candidate()

    def install(self, sha: str) -> None:
        target = paths.builds_dir() / f"{self.key}-{sha[:12]}"
        if not (target / ".mcpool-sha").exists():
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "-q", f"https://github.com/{self.info['repo']}", str(target)])
            run(["git", "checkout", "-q", sha], cwd=str(target))
            run([self.settings.npm, "ci", "--no-audit", "--no-fund"], cwd=str(target))
            if "build" in json.loads((target / "package.json").read_text()).get("scripts", {}):
                run([self.settings.npm, "run", "build"], cwd=str(target))
            (target / ".mcpool-sha").write_text(run(["git", "rev-parse", "HEAD"], cwd=str(target)).strip())
        tmp = self.current.with_name(self.current.name + ".tmp")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, self.current)
        self._prune()

    def _prune(self) -> None:
        """Keep the current build and the newest previous one."""
        keep = self.current.resolve()
        builds = sorted((b for b in paths.builds_dir().glob(f"{self.key}-*") if not b.is_symlink()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        for old in [b for b in builds if b != keep][1:]:
            shutil.rmtree(old, ignore_errors=True)

    def uninstall(self) -> None:
        self.current.unlink(missing_ok=True)
        for build in paths.builds_dir().glob(f"{self.key}-*"):
            shutil.rmtree(build, ignore_errors=True)

    def command(self, binary: str | None = None) -> list[str]:
        manifest = json.loads((self.current / "package.json").read_text())
        bins = manifest.get("bin")
        if isinstance(bins, str):
            script = bins
        else:
            bins = bins or {}
            script = bins.get(binary or "") or next(iter(bins.values()), None) or manifest.get("main", "index.js")
        return [self.settings.node, str(self.current / script)]


class PyPI:
    """A Python package installed as a uv tool and run through its entry point."""

    def __init__(self, key: str, info: dict, settings: Settings):
        self.key, self.info, self.settings = key, info, settings

    @property
    def bin_dir(self) -> Path:
        return paths.home() / ".local" / "bin"

    @property
    def marker(self) -> str:
        return str(self.bin_dir / self.info.get("command", self.key))

    def installed(self) -> str | None:
        match = re.search(rf"^{re.escape(self.key)} v(\S+)", run([self.settings.uv, "tool", "list"]), re.M)
        return match.group(1) if match else None

    def published(self) -> dict[str, datetime]:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{self.key}/json", timeout=30) as response:
            releases = json.load(response)["releases"]
        return {v: datetime.fromisoformat(files[0]["upload_time_iso_8601"].replace("Z", "+00:00"))
                for v, files in releases.items() if files and not any(f.get("yanked") for f in files)}

    def candidate(self) -> str | None:
        return eligible(self.published(), self.installed(), self.settings.cooldown_days)

    def newest(self) -> str | None:
        return newest_stable(self.published())

    @property
    def receipt(self) -> Path:
        return paths.home() / ".local/share/uv/tools" / self.key / "uv-receipt.toml"

    def install(self, version: str) -> None:
        """Install or replace the tool, keeping the Python version of an existing installation."""
        python = re.search(r'^python = "([^"]+)"', self.receipt.read_text(), re.M) if self.receipt.exists() else None
        run([self.settings.uv, "tool", "install", f"{self.key}=={version}",
             *(["--python", python.group(1)] if python else []), "--force"])
        if not Path(self.marker).exists():
            self.info["command"] = self._entry_point()

    def _entry_point(self) -> str:
        names = re.findall(r'name = "([^"]+)", install-path', self.receipt.read_text())
        return self.key if self.key in names else names[0]

    def uninstall(self) -> None:
        run([self.settings.uv, "tool", "uninstall", self.key])

    def command(self, binary: str | None = None) -> list[str]:
        return [str(self.bin_dir / (binary or self.info.get("command", self.key)))]


SOURCES = {"npm": Npm, "github": GitHub, "pypi": PyPI}


def package(key: str, info: dict, settings: Settings):
    return SOURCES[info["source"]](key, info, settings)
