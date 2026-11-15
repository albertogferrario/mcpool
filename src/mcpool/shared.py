"""Shared MCP servers: one streamable-HTTP server, many stdio sessions.

A client (Claude Code, Codex) starts `mcpool serve ...` as an ordinary stdio MCP
server. The launcher makes sure the shared server is running, registers the
session's pid, and relays stdio messages to the server over streamable HTTP.
A detached watcher per server polls the registered pids and stops the server
once no live session has been seen for `idle_shutdown_seconds`. Polling also
covers sessions killed before their own cleanup ran.

Two kinds of shared servers:
  serena   one Serena server per project root, resolved from the working
           directory (nearest ancestor with .serena/project.yml or .git; a git
           worktree is its own root). Outside any project, plain stdio Serena.
  named    one server per name and command line for all sessions, hosted by
           mcp-proxy around a stdio command. For servers without per-session state.
"""

import fcntl
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from mcpool import paths, settings

HOST = "127.0.0.1"
PORT_BASE, PORT_SPAN = 21000, 8000
START_TIMEOUT_SECONDS = 120
WATCH_INTERVAL_SECONDS = 60
SERENA_ARGS = ["--context", "claude-code"]


class Lock:
    def __init__(self, state: Path):
        self.fd = os.open(state / "lock", os.O_CREAT | os.O_RDWR)

    def __enter__(self):
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def find_project_root(start: Path | None = None) -> str | None:
    """Nearest ancestor holding .serena/project.yml or .git (a directory, or a file for worktrees)."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / ".serena" / "project.yml").is_file() or (directory / ".git").exists():
            return str(directory)
    return None


def state_for(label: str, identity: str) -> tuple[Path, int]:
    digest = hashlib.sha1(identity.encode()).hexdigest()
    port = PORT_BASE + int(digest, 16) % PORT_SPAN
    state = paths.shared_dir() / f"{label}-{digest[:10]}"
    (state / "clients").mkdir(parents=True, exist_ok=True)
    (state / "identity").write_text(identity)
    return state, port


def live_clients(state: Path) -> list[int]:
    pids = []
    for f in (state / "clients").iterdir():
        pid = int(f.name) if f.name.isdigit() else None
        if pid and alive(pid):
            pids.append(pid)
        else:
            f.unlink(missing_ok=True)
    return pids


def ensure_server(state: Path, port: int, command: list[str], cwd: str) -> None:
    pid = read_pid(state / "server.pid")
    if pid and alive(pid) and port_open(port):
        return
    if port_open(port):
        raise RuntimeError(f"port {port} is taken by a process that is not this server; see {state}")
    with open(state / "server.log", "ab") as log:
        proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
    (state / "server.pid").write_text(str(proc.pid))
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not port_open(port):
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with code {proc.returncode}; see {state / 'server.log'}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"server did not open port {port}; see {state / 'server.log'}")
        time.sleep(0.25)


def stop_server(state: Path) -> None:
    pid = read_pid(state / "server.pid")
    if pid and alive(pid):
        os.killpg(pid, signal.SIGTERM)
    (state / "server.pid").unlink(missing_ok=True)


def ensure_watcher(state: Path) -> None:
    pid = read_pid(state / "watcher.pid")
    if pid and alive(pid):
        return
    proc = subprocess.Popen([sys.executable, "-m", "mcpool", "watch", str(state)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    (state / "watcher.pid").write_text(str(proc.pid))


def watch(state: Path, interval: float = WATCH_INTERVAL_SECONDS, idle: float | None = None) -> None:
    idle = settings.load().idle_shutdown_seconds if idle is None else idle
    idle_since = None
    while True:
        time.sleep(interval)
        with Lock(state):
            server = read_pid(state / "server.pid")
            if not (server and alive(server)):
                break
            if live_clients(state):
                idle_since = None
                continue
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= idle:
                stop_server(state)
                break
    (state / "watcher.pid").unlink(missing_ok=True)


def _http_client(url: str):
    """Streamable HTTP client for MCP SDK 2.x (two streams) or 1.x (three)."""
    try:
        from mcp.client.streamable_http import streamable_http_client
        return streamable_http_client(url)
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client
        return streamablehttp_client(url, timeout=30, sse_read_timeout=3600)


async def relay(url: str) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    async def pump(source, sink):
        async for message in source:
            if isinstance(message, Exception):
                print(f"mcpool: {message!r}", file=sys.stderr)
                continue
            await sink.send(message)

    async with stdio_server() as (stdin_read, stdout_write):
        async with _http_client(url) as streams:
            http_read, http_write = streams[0], streams[1]
            async with anyio.create_task_group() as tg:
                async def upstream():
                    await pump(stdin_read, http_write)
                    tg.cancel_scope.cancel()

                tg.start_soon(upstream)
                tg.start_soon(pump, http_read, stdout_write)
            await http_write.aclose()
        await stdout_write.aclose()


def serve(state: Path, port: int, command: list[str], cwd: str) -> None:
    me = state / "clients" / str(os.getpid())
    with Lock(state):
        ensure_server(state, port, command, cwd)
        ensure_watcher(state)
        me.touch()

    import anyio

    try:
        anyio.run(relay, f"http://{HOST}:{port}/mcp")
    finally:
        me.unlink(missing_ok=True)


def serve_serena() -> None:
    serena = settings.load().serena
    if not serena:
        sys.exit("mcpool: Serena not found; install it or set `serena` in " + str(paths.settings_file()))
    root = find_project_root()
    if root is None:
        os.execv(serena, [serena, "start-mcp-server", *SERENA_ARGS, "--project-from-cwd"])
    state, port = state_for(f"serena-{Path(root).name}", root)
    command = [serena, "start-mcp-server", "--transport", "streamable-http", "--host", HOST,
               "--port", str(port), "--project", root, *SERENA_ARGS]
    serve(state, port, command, root)


def serve_named(name: str, backend: list[str]) -> None:
    state, port = state_for(name, "\0".join([name, *backend]))
    command = [sys.executable, "-m", "mcp_proxy", "--host", HOST, "--port", str(port),
               "--pass-environment", "--", *backend]
    serve(state, port, command, str(paths.home()))


def servers() -> list[dict]:
    """Shared servers with their state, for `mcpool status`."""
    result = []
    root = paths.shared_dir()
    for state in sorted(root.iterdir()) if root.exists() else []:
        if not (state / "identity").exists():
            continue
        pid = read_pid(state / "server.pid")
        result.append({"name": state.name, "identity": (state / "identity").read_text().split("\0")[0],
                       "pid": pid if pid and alive(pid) else None,
                       "clients": live_clients(state) if (state / "clients").exists() else []})
    return result
