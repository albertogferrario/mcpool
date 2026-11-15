"""Minimal stdio MCP server for tests: one tool.

Appends its pid to $FAKE_PIDFILE when set. Exits at once when the file named by
$FAKE_BROKEN_FLAG exists, which stands for a broken release installed in place.
"""

import json
import os
import sys

if os.path.exists(os.environ.get("FAKE_BROKEN_FLAG", "/nonexistent")):
    sys.exit(1)

if os.environ.get("FAKE_PIDFILE"):
    with open(os.environ["FAKE_PIDFILE"], "a") as f:
        f.write(f"{os.getpid()}\n")

for line in sys.stdin:
    message = json.loads(line)
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": message["params"]["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "ping":
        result = {}
    else:
        error = {"code": -32601, "message": "not found"}
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "error": error}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
