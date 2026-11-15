"""Start an MCP server over stdio and check that it answers tools/list."""

import json
import os
import select
import subprocess
import time

TIMEOUT_SECONDS = 120


def check(cmdline: list[str], env: dict[str, str] | None = None, cwd: str | None = None,
          timeout: float = TIMEOUT_SECONDS) -> str | None:
    """None when the server lists at least one tool, else the reason it did not."""
    try:
        proc = subprocess.Popen(cmdline, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=cwd, env={**os.environ, **(env or {})}, start_new_session=True)
    except OSError as error:
        return f"cannot start: {error}"

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcpool", "version": "1"}}})
        deadline = time.monotonic() + timeout
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                return "timeout"
            line = proc.stdout.readline()
            if not line:
                return "exited: " + proc.stderr.read()[-200:].strip()
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1:
                if "error" in message:
                    return f"initialize error: {message['error'].get('message', '')[:150]}"
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            elif message.get("id") == 2:
                return None if message.get("result", {}).get("tools") else "no tools"
    except OSError as error:
        return f"exited: {error}"
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            os.killpg(proc.pid, 9)
