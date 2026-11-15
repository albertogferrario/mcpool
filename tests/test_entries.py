import json
import tomllib

import pytest

from mcpool import entries

CODEX = '''# my codex config
model = "x"

[mcp_servers.alpha]
command = "npx"
args = [
    "-y",
    "@scope/alpha@latest",  # trailing comment
    "--stores",
    "[{\\"id\\":\\"a]\\"}]",
]

[mcp_servers.alpha.env]
API_KEY = "secret"

[mcp_servers.alpha.tools.hover]
approval_mode = "approve"

[mcp_servers.beta]
command = "node"

[mcp_servers.remote]
url = "https://example.com/mcp"
'''


@pytest.fixture
def configs(home):
    (home / ".claude.json").write_text(json.dumps({
        "numStartups": 3,
        "mcpServers": {"g": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]},
                       "web": {"type": "http", "url": "https://x"}},
        "projects": {str(home / "proj"): {"mcpServers": {"p": {"command": "uvx", "args": ["tool"]}}}},
    }))
    (home / ".codex" / "config.toml").write_text(CODEX)
    return home


def labels(found):
    return sorted(e.label for e in found)


def test_reads_stdio_entries_from_both_clients(configs):
    found = entries.read()
    assert labels(found) == ["claude:g", f"claude:p@{configs / 'proj'}", "codex:alpha", "codex:beta"]
    alpha = next(e for e in found if e.name == "alpha")
    assert alpha.env == {"API_KEY": "secret"}
    assert alpha.args[-1] == '[{"id":"a]"}]'


def test_rewrites_a_claude_project_entry_and_keeps_the_rest(configs):
    project = next(e for e in entries.read() if e.name == "p")
    entries.write(project, "/bin/tool", ["--x"])
    data = json.loads((configs / ".claude.json").read_text())
    assert data["projects"][str(configs / "proj")]["mcpServers"]["p"] == {"command": "/bin/tool", "args": ["--x"]}
    assert data["numStartups"] == 3 and data["mcpServers"]["g"]["args"] == ["-y", "pkg"]


def test_rewrites_a_codex_entry_and_keeps_comments_and_sub_tables(configs):
    alpha = next(e for e in entries.read() if e.name == "alpha")
    entries.write(alpha, "/bin/node", ["/x/index.js", "--stores", '[{"id":"a]"}]'])
    text = (configs / ".codex" / "config.toml").read_text()
    parsed = tomllib.loads(text)["mcp_servers"]
    assert parsed["alpha"]["command"] == "/bin/node"
    assert parsed["alpha"]["args"] == ["/x/index.js", "--stores", '[{"id":"a]"}]']
    assert parsed["alpha"]["env"] == {"API_KEY": "secret"}
    assert parsed["alpha"]["tools"]["hover"] == {"approval_mode": "approve"}
    assert parsed["beta"] == {"command": "node"} and parsed["remote"]["url"] == "https://example.com/mcp"
    assert text.startswith("# my codex config")


def test_adds_args_to_a_codex_entry_without_them(configs):
    beta = next(e for e in entries.read() if e.name == "beta")
    entries.write(beta, "/bin/node", ["/y.js"])
    assert tomllib.loads((configs / ".codex" / "config.toml").read_text())["mcp_servers"]["beta"] == {
        "command": "/bin/node", "args": ["/y.js"]}


def test_fingerprint_changes_only_with_mcp_entries(configs):
    before = entries.fingerprint(entries.read())
    data = json.loads((configs / ".claude.json").read_text())
    data["numStartups"] = 99
    (configs / ".claude.json").write_text(json.dumps(data))
    assert entries.fingerprint(entries.read()) == before
    entries.write(next(e for e in entries.read() if e.name == "g"), "/bin/x", [])
    assert entries.fingerprint(entries.read()) != before


def test_missing_configs_read_as_empty(home):
    assert entries.read() == []
