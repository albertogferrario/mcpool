"""Read and rewrite the stdio MCP entries of Claude Code and Codex.

Claude Code: ~/.claude.json, global `mcpServers` and per-project `projects.<path>.mcpServers`.
Codex: ~/.codex/config.toml, `[mcp_servers.<name>]` tables. Codex is edited in place,
line by line, so that comments, ordering and sub-tables such as per-tool approvals survive.
Repository .mcp.json files are never read or written.
"""

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from mcpool import paths


@dataclass
class Entry:
    source: str            # "claude" or "codex"
    scope: str | None      # project path for Claude per-project entries, else None
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.source}:{self.name}" + (f"@{self.scope}" if self.scope else "")

    @property
    def cmdline(self) -> list[str]:
        return [self.command, *self.args]

    @property
    def cwd(self) -> str:
        return self.scope if self.scope and Path(self.scope).is_dir() else str(paths.home())


def _entry(source: str, scope: str | None, name: str, spec: dict) -> Entry:
    return Entry(source, scope, name, spec["command"], [str(a) for a in spec.get("args", [])],
                 {k: str(v) for k, v in (spec.get("env") or {}).items()})


def read() -> list[Entry]:
    """All stdio entries. Raises when a config cannot be parsed, so no caller acts on a partial read."""
    entries = []
    claude_file = paths.claude_config()
    if claude_file.exists():
        claude = json.loads(claude_file.read_text())
        scopes = [(None, claude.get("mcpServers") or {})]
        scopes += [(path, p.get("mcpServers") or {}) for path, p in (claude.get("projects") or {}).items()]
        for scope, servers in scopes:
            for name, spec in servers.items():
                if spec.get("type", "stdio") == "stdio" and "command" in spec:
                    entries.append(_entry("claude", scope, name, spec))
    codex_file = paths.codex_config()
    if codex_file.exists():
        for name, spec in (tomllib.loads(codex_file.read_text()).get("mcp_servers") or {}).items():
            if "command" in spec:
                entries.append(_entry("codex", None, name, spec))
    return entries


def fingerprint(entries: list[Entry]) -> str:
    data = sorted((e.label, e.cmdline, sorted(e.env)) for e in entries)
    return hashlib.sha1(json.dumps(data).encode()).hexdigest()


def write(entry: Entry, command: str, args: list[str]) -> None:
    """Replace the command and args of one entry, re-reading its file just before writing."""
    if entry.source == "claude":
        _write_claude(entry, command, args)
    else:
        _write_codex(entry, command, args)


def _write_claude(entry: Entry, command: str, args: list[str]) -> None:
    file = paths.claude_config()
    data = json.loads(file.read_text())
    servers = data["mcpServers"] if entry.scope is None else data["projects"][entry.scope]["mcpServers"]
    servers[entry.name]["command"], servers[entry.name]["args"] = command, args
    tmp = file.with_name(file.name + ".mcpool.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, file)


def _write_codex(entry: Entry, command: str, args: list[str]) -> None:
    file = paths.codex_config()
    text = file.read_text()
    head = re.search(r"^\[mcp_servers\." + re.escape(_toml_key(entry.name)) + r"\]\s*\n", text, re.M)
    if head is None:
        raise KeyError(f"[mcp_servers.{entry.name}] not found in {file}")
    following = re.search(r"^\[", text[head.end():], re.M)
    start = head.end()
    stop = start + following.start() if following else len(text)
    body = re.sub(r"^command\s*=.*$", "command = " + json.dumps(command), text[start:stop], count=1, flags=re.M)
    new_args = "args = " + json.dumps(args)
    match = re.search(r"^args\s*=\s*", body, re.M)
    if match:
        body = body[:match.start()] + new_args + body[_array_end(body, match.end()):]
    else:
        body = re.sub(r"^(command = .*)$", lambda m: m.group(1) + "\n" + new_args, body, count=1, flags=re.M)
    updated = text[:start] + body + text[stop:]
    parsed = tomllib.loads(updated)["mcp_servers"][entry.name]
    if parsed.get("command") != command or [str(a) for a in parsed.get("args", [])] != args:
        raise RuntimeError(f"rewriting [mcp_servers.{entry.name}] did not produce the expected entry")
    file.write_text(updated)


def _toml_key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def _array_end(text: str, i: int) -> int:
    """Index just past the TOML array that starts at or after i, honouring quoted strings."""
    depth, quote = 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            i = text.find("\n", i)
            if i < 0:
                break
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated TOML array")
