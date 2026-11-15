"""Adoption, sharing, upgrades and clean-up of MCP entries.

reconcile  Adopt entries started through a package launcher (npx, npm exec, pnpm dlx,
           bunx, uvx, uv tool run): install the package once, record it, and rewrite the
           entry to run it directly, after the direct command answers tools/list. A version
           written in the entry (pkg@1.2.3, pkg==1.2.3) is installed as is and pinned.
           Serena entries using --project-from-cwd move to the shared per-project launcher.
           A registered package that no entry has used for `unused_grace_days` is removed.
upgrade    Install newer releases outside the cooldown (GitHub builds follow HEAD), keep
           them only if every entry that passed the check before still passes, else
           reinstall the previous version. uv tools are reinstalled in place, so their
           upgrade waits for a run with none of their processes alive.

Every entry is rewritten in place in its own config file; its original command is kept
in the registry so that `mcpool uninstall` can restore it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from mcpool import entries as config
from mcpool import paths, smoke
from mcpool.entries import Entry
from mcpool.settings import Settings
from mcpool.sources import PyPI, package, run

NPM_LAUNCHERS = [("npx",), ("npm", "exec"), ("pnpm", "dlx"), ("bunx",)]
UV_LAUNCHERS = [("uvx",), ("uv", "tool", "run")]
LAUNCHER_FLAGS = {"-y", "--yes", "-q", "--quiet"}
SPEC_FLAGS = {"-p", "--package", "--from"}


def mcpool_command() -> str:
    """The mcpool executable of the running installation."""
    return str(Path(sys.executable).parent / "mcpool")


class Report:
    """Log lines and the short notification summary of one run."""

    def __init__(self):
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        print(line, flush=True)
        paths.log_file().parent.mkdir(parents=True, exist_ok=True)
        with paths.log_file().open("a") as f:
            f.write(line + "\n")

    def add(self, summary: str) -> None:
        self.lines.append(summary)

    def notify(self) -> None:
        if not self.lines or sys.platform != "darwin":
            return
        text = "; ".join(self.lines).replace('"', "'")[:240]
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "mcpool"'],
                       capture_output=True)


# ---- registry and state ---------------------------------------------------------

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    """Write atomically, readable by the owner only: original entries may carry credentials in their args."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def load_registry() -> dict:
    registry = load_json(paths.registry_file(), {})
    for key, default in (("packages", {}), ("shared", []), ("originals", {})):
        registry.setdefault(key, default)
    return registry


def save_registry(registry: dict) -> None:
    save_json(paths.registry_file(), registry)


# ---- launcher parsing -----------------------------------------------------------

def launcher_parts(entry: Entry) -> tuple[str, str, list[str]] | None:
    """(kind, package spec, server args) for an entry started through a package launcher.

    With -p/--package/--from the spec carries the named binary or command after a NUL.
    Unknown launcher flags leave the entry alone.
    """
    words = [Path(entry.command).name, *entry.args]
    for kind, launchers in (("npm", NPM_LAUNCHERS), ("pypi", UV_LAUNCHERS)):
        for launcher in launchers:
            if tuple(words[:len(launcher)]) != launcher:
                continue
            rest, explicit = words[len(launcher):], None
            while rest and rest[0].startswith("-"):
                flag = rest.pop(0)
                if flag in SPEC_FLAGS and rest:
                    explicit = rest.pop(0)
                elif flag not in LAUNCHER_FLAGS:
                    return None
            if not rest:
                return None
            first, server_args = rest[0], rest[1:]
            return kind, (first if explicit is None else f"{explicit}\0{first}"), server_args
    return None


def parse_npm_spec(spec: str) -> tuple[str, dict, str | None, str | None]:
    """(registry key, registry info, explicit version or None, binary named in the entry or None)."""
    spec, _, binary = spec.partition("\0")
    binary = binary or None
    github = re.match(r"^(?:github:|git\+https://github\.com/|https://github\.com/)(.+)$", spec)
    if github:
        repo, _, ref = github.group(1).partition("#")
        repo = repo.removesuffix(".git")
        info = {"source": "github", "repo": repo, **({"ref": ref} if ref else {})}
        return repo.split("/")[-1], info, None, binary
    at = spec.rfind("@")
    name, version = (spec[:at], spec[at + 1:]) if at > 0 else (spec, None)
    if version in (None, "", "latest"):
        return name, {"source": "npm", "policy": "auto"}, None, binary
    return name, {"source": "npm", "policy": "pinned", "version": version}, version, binary


def parse_pypi_spec(spec: str) -> tuple[str, dict, str | None, str | None]:
    spec, _, command = spec.partition("\0")
    name, _, version = spec.partition("==")
    name = re.split(r"[\[<>=~!]", name)[0]
    info = {"source": "pypi", "command": command or name}
    if version:
        return name, {**info, "policy": "pinned", "version": version}, version, None
    return name, {**info, "policy": "auto"}, None, None


def is_serena_from_cwd(entry: Entry) -> bool:
    return (Path(entry.command).name == "serena" and entry.args[:1] == ["start-mcp-server"]
            and "--project-from-cwd" in entry.args)


def shared_wrap(name: str, cmdline: list[str]) -> list[str]:
    return [mcpool_command(), "serve", "--name", name, "--", *cmdline]


def unwrap(cmdline: list[str]) -> list[str]:
    """The backend command of an `mcpool serve --name` entry; other entries unchanged."""
    is_named = Path(cmdline[0]).name == "mcpool" and cmdline[1:3] == ["serve", "--name"]
    if is_named and "--" in cmdline:
        return cmdline[cmdline.index("--") + 1:]
    return cmdline


# ---- adoption -------------------------------------------------------------------

class Manager:
    def __init__(self, settings: Settings, report: Report | None = None):
        self.settings = settings
        self.report = report or Report()
        self.registry = load_registry()
        self.state = load_json(paths.manage_state_file(), {})

    def save(self) -> None:
        save_registry(self.registry)
        save_json(paths.manage_state_file(), self.state)

    def package(self, key: str, info: dict):
        return package(key, info, self.settings)

    def _remember(self, entry: Entry) -> None:
        self.registry["originals"].setdefault(entry.label, {"command": entry.command, "args": entry.args})

    def adopt(self, entry: Entry) -> None:
        kind, spec, server_args = launcher_parts(entry)
        key, info, version, binary = parse_npm_spec(spec) if kind == "npm" else parse_pypi_spec(spec)
        if info["source"] == "github":
            key = next((k for k, v in self.registry["packages"].items() if v.get("repo") == info["repo"]), key)
        known = self.registry["packages"].get(key)
        pkg = self.package(key, known or info)

        installed = pkg.installed()
        if installed is None or (version and installed != version):
            target = version or pkg.candidate() or pkg.newest()
            if target is None:
                raise RuntimeError("no installable version found")
            pkg.install(target)

        direct = [*pkg.command(binary), *server_args]
        reason = smoke.check(direct, entry.env, entry.cwd)
        if reason:
            before = smoke.check(entry.cmdline, entry.env, entry.cwd)
            raise RuntimeError(f"direct command failed ({reason}); the launcher entry "
                               f"{'works' if before is None else 'fails too'}; entry left unchanged")
        shared = entry.name in self.registry["shared"]
        new = shared_wrap(entry.name, direct) if shared else direct
        if not known:
            self.registry["packages"][key] = pkg.info
        self._remember(entry)
        self.save()
        config.write(entry, new[0], new[1:])
        mode = "shared" if shared else "one per session"
        self.report.log(f"adopted {entry.label}: {key} {pkg.installed()} runs directly, {mode}")
        hint = "" if shared else "; `mcpool share` it if it keeps no session state"
        self.report.add(f"{entry.name} adopted ({mode}){hint}")

    def adopt_serena(self, entry: Entry) -> None:
        new = [mcpool_command(), "serve", "--serena"]
        self._remember(entry)
        self.save()
        config.write(entry, new[0], new[1:])
        self.report.log(f"adopted {entry.label}: Serena shared per project root")
        self.report.add(f"{entry.name} now shares one Serena per project")

    def remove_unused(self, current: list[Entry]) -> None:
        unused_since = self.state.setdefault("unused_since", {})
        for key, info in list(self.registry["packages"].items()):
            pkg = self.package(key, info)
            if any(pkg.marker in arg for e in current for arg in e.cmdline):
                unused_since.pop(key, None)
                continue
            since = unused_since.setdefault(key, time.time())
            if time.time() - since < self.settings.unused_grace_days * 86400:
                continue
            pkg.uninstall()
            del self.registry["packages"][key]
            unused_since.pop(key, None)
            self.save()
            self.report.log(f"removed {key}: unused by every MCP entry for {self.settings.unused_grace_days}+ days")
            self.report.add(f"{key} uninstalled (unused)")

    def reconcile(self, force: bool = False) -> None:
        current = config.read()
        unchanged = self.state.get("fingerprint") == config.fingerprint(current)
        if unchanged and not force and not self.state.get("unused_since"):
            return
        failed = self.state.setdefault("adopt_failed", {})
        for entry in current:
            attempt = f"{entry.label} {entry.cmdline}"
            if failed.get(entry.label) == attempt and not force:
                continue
            try:
                if is_serena_from_cwd(entry) and self.settings.serena:
                    self.adopt_serena(entry)
                elif launcher_parts(entry):
                    self.adopt(entry)
                else:
                    continue
                failed.pop(entry.label, None)
            except Exception as error:
                self.report.log(f"adopt {entry.label}: {error}")
                if failed.get(entry.label) != attempt:
                    self.report.add(f"{entry.name} not adopted: see {paths.log_file()}")
                failed[entry.label] = attempt
        current = config.read()
        self.remove_unused(current)
        self.state["fingerprint"] = config.fingerprint(config.read())
        self.save()

    # ---- sharing ------------------------------------------------------------------

    def set_shared(self, name: str, shared: bool) -> list[str]:
        """Share or stop sharing every entry called `name`; returns the labels changed."""
        names = set(self.registry["shared"])
        names.add(name) if shared else names.discard(name)
        self.registry["shared"] = sorted(names)
        self.save()
        changed = []
        for entry in config.read():
            if entry.name != name:
                continue
            backend = unwrap(entry.cmdline)
            new = shared_wrap(name, backend) if shared else backend
            if new != entry.cmdline:
                config.write(entry, new[0], new[1:])
                changed.append(entry.label)
        return changed

    # ---- restore --------------------------------------------------------------------

    def restore(self) -> list[str]:
        """Write every adopted entry back to its original command."""
        restored = []
        for entry in config.read():
            original = self.registry["originals"].get(entry.label)
            if original and [original["command"], *original["args"]] != entry.cmdline:
                config.write(entry, original["command"], original["args"])
                restored.append(entry.label)
        return restored

    # ---- upgrade ----------------------------------------------------------------------

    def users_of(self, marker: str) -> list[Entry]:
        """Entries running a package, with a shared wrapper removed so the check runs the installed backend."""
        result = []
        for e in config.read():
            if any(marker in a for a in e.cmdline):
                backend = unwrap(e.cmdline)
                result.append(Entry(e.source, e.scope, e.name, backend[0], backend[1:], e.env))
        return result

    def check(self, checks: list[Entry], only: set[str] | None = None) -> dict[str, str | None]:
        return {e.label: smoke.check(e.cmdline, e.env, e.cwd) for e in checks if only is None or e.label in only}

    def verified_upgrade(self, name: str, checks: list[Entry], old, new, install, rollback) -> None:
        before = self.check(checks)
        passing = {label for label, reason in before.items() if reason is None}
        for label, reason in before.items():
            if reason:
                self.report.log(f"{name}: {label} already failing before the upgrade ({reason}); not counted")
        install(new)
        broken = {label: reason for label, reason in self.check(checks, only=passing).items() if reason}
        if broken:
            self.report.log(f"{name}: {old} -> {new} broke {broken}; rolling back")
            rollback(old)
            self.report.add(f"{name} {str(new)[:12]} rolled back")
            return
        self.report.log(f"{name}: upgraded {old} -> {new}, verified on {sorted(passing) or 'no entries'}")
        self.report.add(f"{name} {old}->{str(new)[:12]}")

    def postponed(self, name: str, old, new, markers: list[str]) -> bool:
        waiting = self.state.setdefault("postponed_since", {})
        lines = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout.splitlines()
        users = [line for line in lines if any(m in line for m in markers) and "mcpool upgrade" not in line
                 and "mcpool daily" not in line]
        if not users:
            waiting.pop(name, None)
            return False
        days = (time.time() - waiting.setdefault(name, time.time())) / 86400
        self.report.log(f"{name}: {old} -> {new} postponed for {days:.0f} days, {len(users)} running processes use it")
        notified = self.state.setdefault("postpone_notified", {})
        if days >= self.settings.postpone_notice_days and notified.get(name) != new:
            notified[name] = new
            self.report.add(f"{name} {new} waiting {days:.0f} days: close its sessions once to let it upgrade")
        return True

    def upgrade_package(self, key: str, info: dict) -> None:
        pkg = self.package(key, info)
        old = pkg.installed()
        if info.get("policy") == "pinned":
            latest = pkg.candidate()
            if latest and latest != info["version"]:
                self.report.log(f"{key}: pinned {info['version']}, {latest} available")
                if self.state.setdefault("notified_pinned", {}).get(key) != latest:
                    self.state["notified_pinned"][key] = latest
                    self.report.add(f"{key} {latest} available (pinned {info['version']})")
            else:
                self.report.log(f"{key}: pinned {info['version']}, nothing newer")
            return
        new = pkg.candidate()
        if new is None or new == old:
            self.report.log(f"{key}: up to date ({old})")
            return
        if isinstance(pkg, PyPI) and self.postponed(key, old, new, [pkg.marker]):
            return
        self.verified_upgrade(key, self.users_of(pkg.marker), old, new, pkg.install, pkg.install)

    def upgrade_serena(self) -> None:
        """Serena installed as a uv tool: checked plain and through the shared launcher."""
        receipt = paths.home() / ".local/share/uv/tools/serena-agent/uv-receipt.toml"
        if not (self.settings.serena and receipt.exists()):
            return
        pkg = PyPI("serena-agent", {"command": "serena"}, self.settings)
        old, new = pkg.installed(), pkg.candidate()
        if new is None or new == old:
            self.report.log(f"serena-agent: up to date ({old})")
            return
        if self.postponed("serena-agent", old, new, ["start-mcp-server", "serve --serena"]):
            return
        empty = tempfile.mkdtemp(prefix="mcpool-empty-")
        repo = tempfile.mkdtemp(prefix="mcpool-serena-check-")
        run(["git", "init", "-q", repo])
        checks = [Entry("check", empty, "serena stdio", self.settings.serena,
                        ["start-mcp-server", "--context", "claude-code"]),
                  Entry("check", repo, "serena shared", mcpool_command(), ["serve", "--serena"])]
        try:
            self.verified_upgrade("serena-agent", checks, old, new, pkg.install, pkg.install)
        finally:
            stop_check_servers(str(Path(repo).resolve()))


    def upgrade(self) -> None:
        steps = [*[(k, lambda k=k, v=v: self.upgrade_package(k, v)) for k, v in self.registry["packages"].items()],
                 ("serena-agent", self.upgrade_serena)]
        for name, step in steps:
            try:
                step()
            except Exception as error:
                self.report.log(f"{name}: failed: {error}")
                self.report.add(f"{name} failed")
        self.save()


def stop_check_servers(identity: str) -> None:
    """Stop the shared server started for a throwaway check repository, and its watcher."""
    from mcpool import shared
    root = paths.shared_dir()
    for state in root.iterdir() if root.exists() else []:
        if (state / "identity").exists() and (state / "identity").read_text() == identity:
            shared.stop_server(state)
            watcher = shared.read_pid(state / "watcher.pid")
            if watcher and shared.alive(watcher):
                os.kill(watcher, 15)
            shutil.rmtree(state, ignore_errors=True)
