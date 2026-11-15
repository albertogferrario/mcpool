import pytest

from mcpool.entries import Entry
from mcpool.manage import is_serena_from_cwd, launcher_parts, parse_npm_spec, parse_pypi_spec, shared_wrap, unwrap


def entry(command, *args):
    return Entry("claude", None, "x", command, list(args))


@pytest.mark.parametrize("e, expected", [
    (entry("npx", "-y", "@scope/pkg@latest"), ("npm", "@scope/pkg@latest", [])),
    (entry("/usr/local/bin/npx", "pkg", "--flag", "v"), ("npm", "pkg", ["--flag", "v"])),
    (entry("npx", "-y", "github:owner/repo-mcp"), ("npm", "github:owner/repo-mcp", [])),
    (entry("npm", "exec", "--yes", "--package", "@a/b@1.2.3", "b-bin", "--x"), ("npm", "@a/b@1.2.3\0b-bin", ["--x"])),
    (entry("pnpm", "dlx", "pkg"), ("npm", "pkg", [])),
    (entry("bunx", "pkg"), ("npm", "pkg", [])),
    (entry("uvx", "mcp-server-fetch"), ("pypi", "mcp-server-fetch", [])),
    (entry("uv", "tool", "run", "--from", "a==0.3", "a-cmd", "--r", "."), ("pypi", "a==0.3\0a-cmd", ["--r", "."])),
])
def test_launcher_entries_are_recognised(e, expected):
    assert launcher_parts(e) == expected


@pytest.mark.parametrize("e", [
    entry("npx", "--unknown-flag", "pkg"),
    entry("npx", "-y"),
    entry("php", "artisan", "boost:mcp"),
    entry("./local-binary", "mcp"),
    entry("node", "/abs/index.js"),
])
def test_other_entries_are_left_alone(e):
    assert launcher_parts(e) is None


def test_npm_specs():
    assert parse_npm_spec("@scope/pkg@latest") == ("@scope/pkg", {"source": "npm", "policy": "auto"}, None, None)
    assert parse_npm_spec("pkg") == ("pkg", {"source": "npm", "policy": "auto"}, None, None)
    key, info, version, binary = parse_npm_spec("@a/b@1.2.3\0b-bin")
    assert (key, info["policy"], info["version"], version, binary) == ("@a/b", "pinned", "1.2.3", "1.2.3", "b-bin")


def test_github_specs():
    assert parse_npm_spec("github:owner/repo-mcp#v2") == (
        "repo-mcp", {"source": "github", "repo": "owner/repo-mcp", "ref": "v2"}, None, None)
    assert parse_npm_spec("https://github.com/owner/repo.git")[1] == {"source": "github", "repo": "owner/repo"}


def test_pypi_specs():
    assert parse_pypi_spec("mcp-server-fetch") == (
        "mcp-server-fetch", {"source": "pypi", "command": "mcp-server-fetch", "policy": "auto"}, None, None)
    key, info, version, _ = parse_pypi_spec("a[extra]==0.3\0a-cmd")
    assert (key, info["command"], info["policy"], version) == ("a", "a-cmd", "pinned", "0.3")


def test_shared_wrapper_round_trip():
    backend = ["/bin/node", "/x/index.js", "--flag"]
    wrapped = shared_wrap("name", backend)
    assert wrapped[1:5] == ["serve", "--name", "name", "--"]
    assert unwrap(wrapped) == backend
    assert unwrap(backend) == backend


def test_serena_from_cwd():
    assert is_serena_from_cwd(entry("/x/serena", "start-mcp-server", "--context", "claude-code", "--project-from-cwd"))
    assert not is_serena_from_cwd(entry("/x/serena", "start-mcp-server", "--project", "/repo"))
