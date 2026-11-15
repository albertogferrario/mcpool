# mcpool

mcpool keeps the local MCP servers of Claude Code and Codex lean and current without manual steps.

Each Claude Code or Codex session normally starts its own copy of every configured stdio MCP server, usually through `npx` or `uvx`. With several sessions open, the copies add up: the language server behind Serena can take a few gigabytes per session, and every `npx` server adds a Node process plus an npm wrapper. mcpool changes that in four ways:

- **Shared servers.** Serena runs once per project root, shared by every Claude Code and Codex session working in that project; a git worktree is its own project. Servers without per-session state can run once for all sessions.
- **Adoption.** When you add a server with `claude mcp add` or `codex mcp add`, mcpool installs its package once and rewrites the entry to run it directly, within seconds, and only after the new command answers `tools/list`.
- **Upgrades.** Once a day mcpool upgrades adopted packages to the newest release that has been public for seven days, checks every entry that uses the package, and reinstalls the previous version if one of them stops working.
- **Clean-up.** A package that no entry has used for a day is uninstalled.

Status: alpha, macOS only (the background jobs use launchd).

## Install

```sh
uv tool install mcpool
mcpool install
```

`mcpool install` records the paths of `node`, `npm`, `uv` and `serena` in `~/.config/mcpool/config.toml`, starts two background jobs, and adopts the entries already configured. Running sessions keep their servers; new sessions use the adopted entries.

## What changes in your configs

mcpool reads `~/.claude.json` (global and per-project servers) and `~/.codex/config.toml`. Repository `.mcp.json` files are never touched: they are shared with other people, and local paths would break them.

| Entry before | Entry after |
|---|---|
| `npx -y some-mcp@latest --flag` | `/path/to/node /path/to/node_modules/some-mcp/dist/index.js --flag` |
| `npx -y github:owner/repo` | `/path/to/node ~/.local/share/mcpool/builds/repo-current/dist/index.js` |
| `uvx some-mcp` | `~/.local/bin/some-mcp` |
| `serena start-mcp-server ... --project-from-cwd` | `mcpool serve --serena` |
| a shared server (see below) | `mcpool serve --name NAME -- <direct command>` |

Recognised launchers: `npx`, `npm exec`, `pnpm dlx`, `bunx`, `uvx`, `uv tool run`. Entries started any other way (a local binary, `php artisan ...`, a remote URL) stay as they are. A version written in the entry (`pkg@1.2.3`, `pkg==1.2.3`) is installed as written and pinned; mcpool then only notifies when a newer release exists.

The original command of every adopted entry is kept, and `mcpool uninstall` writes it back.

## Sharing

A shared server runs once, behind a local streamable-HTTP endpoint (`mcp-proxy` for ordinary servers, Serena's own HTTP transport for Serena). Each session starts `mcpool serve`, a small stdio relay to that endpoint. A watcher stops the server ten minutes after its last session ends, including sessions that were killed.

Whether a server can be shared depends on whether it keeps state per session, and mcpool cannot detect that. New servers therefore run once per session, and the adoption notification suggests sharing. Share a server when its tools are stateless, such as documentation lookups or API wrappers. Keep browser automation, device control, and anything holding an open page or selection per session.

```sh
mcpool share context7      # every entry named context7 now uses one shared server
mcpool unshare context7
```

A shared server starts with the environment of the first session that needs it. Entries that need different environment values (two API keys, for example) should use different names.

## Upgrades and supply-chain safety

For npm and PyPI packages, the daily job installs the newest stable release published at least seven days earlier. Pre-releases and yanked releases are skipped, and nothing is downgraded. Compromised releases of popular packages are usually detected and pulled within days; the post-upgrade check only proves that a server starts and lists its tools, and cannot detect malicious code. Packages built from GitHub follow the repository HEAD without a delay, so adopt only repositories you trust.

Tools installed with `uv` are replaced in place, so their upgrade waits for a moment when none of their processes is running. After seven days of waiting, mcpool asks you to close those sessions once.

`cooldown_days`, `unused_grace_days`, the daily time and the other settings live in `~/.config/mcpool/config.toml`.

## Commands

| Command | Purpose |
|---|---|
| `mcpool install` | detect tools, start the background jobs, adopt existing entries |
| `mcpool status` | background jobs, shared servers with sessions and memory, adopted packages |
| `mcpool share NAME` / `unshare NAME` | run every entry with that name through one shared server, or once per session |
| `mcpool reconcile` | adopt new entries now (runs automatically when a config changes) |
| `mcpool upgrade` | upgrade adopted packages now (runs automatically every day) |
| `mcpool uninstall [--purge]` | restore the original entries and stop the background jobs; `--purge` also removes adopted packages and mcpool data |
| `mcpool serve` | the stdio entry point written into MCP configs |

## Files

| Path | Content |
|---|---|
| `~/.config/mcpool/config.toml` | settings |
| `~/.local/share/mcpool/registry.json` | adopted packages, shared names, original entries |
| `~/.local/share/mcpool/builds/` | packages built from GitHub |
| `~/.local/state/mcpool/mcpool.log` | what was adopted, upgraded, rolled back or removed |
| `~/.local/state/mcpool/shared/` | one directory per shared server: log, pids, sessions |
| `~/Library/LaunchAgents/mcpool.*.plist` | the two background jobs |

## Limits

- macOS only for now. Adoption and sharing are portable; Linux needs a systemd version of the two background jobs.
- npm packages are installed globally with the `npm` found at install time. After switching Node versions with a version manager, run `mcpool install` again.
- Codex entries are edited in place, line by line, so comments and per-tool settings survive. An unusual layout may make mcpool skip an entry, which the log reports.

## Development

```sh
uv sync
uv run pytest
uv run ruff check
```

## License

MIT
