import json
import sys

import pytest
from conftest import FAKE_SERVER

from mcpool import entries
from mcpool.manage import Manager, Report, shared_wrap
from mcpool.settings import Settings


class FakePackage:
    """Stands in for a source. Installing records the version; installing "broken" sets the
    flag that makes the fake server fail, as a broken release installed in place would."""

    def __init__(self, key, info, log):
        self.key, self.info, self.log = key, info, log
        self.marker = "fake_server.py"
        self.flag = log["broken_flag"]

    def installed(self):
        return self.log.get(self.key)

    def candidate(self):
        return self.log.get("candidate", "2.0.0")

    def newest(self):
        return "2.0.0"

    def install(self, version):
        self.log[self.key] = version
        if version == "broken":
            self.flag.touch()
        else:
            self.flag.unlink(missing_ok=True)

    def uninstall(self):
        self.log.setdefault("uninstalled", []).append(self.key)

    def command(self, binary=None):
        return list(FAKE_SERVER)


@pytest.fixture
def manager(home, monkeypatch):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "pkg": {"type": "stdio", "command": "npx", "args": ["-y", "@scope/pkg@latest", "--flag"]},
        "other": {"type": "stdio", "command": "php", "args": ["artisan", "x"]}}}))
    flag = home / "broken-release"
    monkeypatch.setenv("FAKE_BROKEN_FLAG", str(flag))
    log = {"broken_flag": flag}
    m = Manager(Settings(cooldown_days=7, unused_grace_days=0), Report())
    monkeypatch.setattr(m, "package", lambda key, info: FakePackage(key, info, log))
    m.fake_log = log
    return m


def config(home):
    return json.loads((home / ".claude.json").read_text())["mcpServers"]


def test_adopts_a_launcher_entry_and_leaves_others(manager, home):
    manager.reconcile(force=True)
    servers = config(home)
    assert servers["pkg"]["command"] == sys.executable
    assert servers["pkg"]["args"] == [FAKE_SERVER[1], "--flag"]
    assert servers["other"] == {"type": "stdio", "command": "php", "args": ["artisan", "x"]}
    assert "@scope/pkg" in manager.registry["packages"]
    original = {"command": "npx", "args": ["-y", "@scope/pkg@latest", "--flag"]}
    assert manager.registry["originals"]["claude:pkg"] == original


def test_failing_direct_command_leaves_the_entry_unchanged(manager, home):
    manager.fake_log["candidate"] = "broken"
    manager.reconcile(force=True)
    assert config(home)["pkg"]["command"] == "npx"
    assert "@scope/pkg" not in manager.registry["packages"]


def test_restore_puts_original_entries_back(manager, home):
    manager.reconcile(force=True)
    assert manager.restore() == ["claude:pkg"]
    assert config(home)["pkg"]["args"] == ["-y", "@scope/pkg@latest", "--flag"]


def test_share_and_unshare_rewrite_entries(manager, home):
    manager.reconcile(force=True)
    direct = [config(home)["pkg"]["command"], *config(home)["pkg"]["args"]]
    manager.set_shared("pkg", True)
    shared_cmd = [config(home)["pkg"]["command"], *config(home)["pkg"]["args"]]
    assert shared_cmd == shared_wrap("pkg", direct)
    manager.set_shared("pkg", False)
    assert [config(home)["pkg"]["command"], *config(home)["pkg"]["args"]] == direct


def test_unused_packages_are_removed_after_the_grace_period(manager, home):
    manager.reconcile(force=True)
    servers = json.loads((home / ".claude.json").read_text())
    del servers["mcpServers"]["pkg"]
    (home / ".claude.json").write_text(json.dumps(servers))
    manager.reconcile()
    assert manager.fake_log.get("uninstalled") == ["@scope/pkg"]
    assert "@scope/pkg" not in manager.registry["packages"]


def test_upgrade_rolls_back_when_entries_break(manager, home):
    manager.reconcile(force=True)
    manager.fake_log["candidate"] = "broken"
    manager.upgrade()
    assert manager.fake_log["@scope/pkg"] == "2.0.0"
    assert any("rolled back" in line for line in manager.report.lines)


def test_upgrade_keeps_a_working_release(manager, home):
    manager.reconcile(force=True)
    manager.fake_log["candidate"] = "2.1.0"
    manager.upgrade()
    assert manager.fake_log["@scope/pkg"] == "2.1.0"


def test_reconcile_skips_work_when_entries_are_unchanged(manager, home, monkeypatch):
    manager.reconcile(force=True)
    monkeypatch.setattr(entries, "write", lambda *a: pytest.fail("no write expected"))
    manager.reconcile()
