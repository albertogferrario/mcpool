import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from conftest import FAKE_SERVER

from mcpool import shared, smoke


def test_smoke_check_passes_and_fails():
    assert smoke.check(FAKE_SERVER) is None
    assert smoke.check([sys.executable, "-c", "import sys; sys.exit(3)"]).startswith("exited")
    assert smoke.check(["/nonexistent/binary"]).startswith("cannot start")


def test_project_root_rules(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "deep").mkdir(parents=True)
    worktree = repo / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../.git/worktrees/feature\n")
    serena_project = tmp_path / "plain" / "proj"
    (serena_project / ".serena").mkdir(parents=True)
    (serena_project / ".serena" / "project.yml").write_text("x: 1\n")
    assert shared.find_project_root(repo / "src" / "deep") == str(repo.resolve())
    assert shared.find_project_root(worktree) == str(worktree.resolve())
    assert shared.find_project_root(serena_project) == str(serena_project.resolve())
    assert shared.find_project_root(tmp_path / "plain") is None


def session(env, cwd, results, key):
    """One stdio client through `mcpool serve --name`: initialize, then tools/list."""
    proc = subprocess.Popen([sys.executable, "-m", "mcpool", "serve", "--name", "fake", "--", *FAKE_SERVER],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, cwd=cwd)

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(ident):
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            message = json.loads(line)
            if message.get("id") == ident:
                return message

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": key, "version": "0"}}})
    init = receive(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = receive(2)
    proc.stdin.close()
    results[key] = (init["result"]["serverInfo"]["name"], [t["name"] for t in tools["result"]["tools"]],
                    proc.wait(timeout=30))


def test_two_sessions_share_one_backend(home):
    pidfile = home / "backend-pids"
    env = {**os.environ, "HOME": str(home), "FAKE_PIDFILE": str(pidfile)}
    results = {}
    threads = [threading.Thread(target=session, args=(env, str(home), results, k)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    try:
        assert results["a"][1:] == (["echo"], 0) and results["b"][1:] == (["echo"], 0)
        assert len(pidfile.read_text().split()) == 1
        state = next((home / ".local/state/mcpool/shared").iterdir())
        assert shared.live_clients(state) == []
    finally:
        for state in (home / ".local/state/mcpool/shared").iterdir():
            shared.stop_server(state)
            watcher = shared.read_pid(state / "watcher.pid")
            if watcher and shared.alive(watcher):
                os.kill(watcher, 15)


def test_watcher_stops_an_idle_server(home):
    state = home / "state"
    (state / "clients").mkdir(parents=True)
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    (state / "server.pid").write_text(str(server.pid))
    dead_client = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_client.wait()
    (state / "clients" / str(dead_client.pid)).touch()
    started = time.monotonic()
    shared.watch(state, interval=0.2, idle=0.5)
    assert server.wait(timeout=5) is not None
    assert time.monotonic() - started < 5
    assert list((state / "clients").iterdir()) == []
    assert not Path(state / "server.pid").exists()
