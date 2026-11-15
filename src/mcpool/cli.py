"""Command line: `mcpool <command>`."""

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mcpool import __version__, paths, service, settings, shared
from mcpool.manage import Manager, Report


def _locked():
    """Serialize reconcile, upgrade and the commands that rewrite configs."""
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.state_dir() / "manage.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def cmd_serve(args) -> None:
    if args.serena:
        shared.serve_serena()
    elif args.name and args.backend:
        shared.serve_named(args.name, args.backend)
    else:
        sys.exit("usage: mcpool serve --serena | mcpool serve --name NAME -- COMMAND [ARGS...]")


def cmd_watch(args) -> None:
    shared.watch(Path(args.state))


def _run(force_reconcile: bool, do_reconcile: bool, do_upgrade: bool) -> None:
    _locked()
    report = Report()
    manager = Manager(settings.load(), report)
    if do_reconcile:
        try:
            manager.reconcile(force=force_reconcile)
        except Exception as error:
            report.log(f"reconcile failed: {error}")
            report.add("reconcile failed")
    if do_upgrade:
        report.log("upgrade started")
        manager.upgrade()
        report.log(f"upgrade finished: {report.lines or 'nothing changed'}")
    report.notify()


def cmd_reconcile(args) -> None:
    _run(force_reconcile=args.force, do_reconcile=True, do_upgrade=False)


def cmd_upgrade(args) -> None:
    _run(force_reconcile=False, do_reconcile=False, do_upgrade=True)


def cmd_daily(args) -> None:
    _run(force_reconcile=True, do_reconcile=True, do_upgrade=True)


def cmd_install(args) -> None:
    current = settings.load() if paths.settings_file().exists() else settings.detect()
    detected = settings.detect()
    for name in ("node", "npm", "npm_root", "uv", "serena"):
        if not getattr(current, name) and getattr(detected, name):
            setattr(current, name, getattr(detected, name))
    settings.save(current)
    print(f"settings: {paths.settings_file()}")
    for name in ("node", "npm", "uv", "serena"):
        print(f"  {name:7} {getattr(current, name) or '(not found)'}")
    if not current.npm:
        print("  npm not found: npm-based servers will be left as they are")
    service.install(current)
    print("background jobs: reconcile on config change, daily upgrade at "
          f"{current.daily_hour:02d}:{current.daily_minute:02d}")
    if not args.no_adopt:
        print("adopting existing entries...")
        _run(force_reconcile=True, do_reconcile=True, do_upgrade=False)


def cmd_uninstall(args) -> None:
    _locked()
    manager = Manager(settings.load())
    restored = manager.restore()
    service.uninstall()
    print(f"restored {len(restored)} entries; background jobs removed")
    for label in restored:
        print(f"  {label}")
    if args.purge:
        for key, info in list(manager.registry["packages"].items()):
            try:
                manager.package(key, info).uninstall()
            except Exception as error:
                print(f"  could not uninstall {key}: {error}")
        for directory in (paths.data_dir(), paths.state_dir(), paths.config_dir()):
            shutil.rmtree(directory, ignore_errors=True)
        print("packages and mcpool data removed")


def cmd_share(args) -> None:
    _locked()
    changed = Manager(settings.load()).set_shared(args.name, shared=args.command == "share")
    verb = "shared" if args.command == "share" else "one per session"
    print(f"{args.name}: {verb}; entries rewritten: {', '.join(changed) or 'none'}")


def _rss_mb(pid: int) -> int:
    """Resident memory of a process and its descendants, in MB."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    rows = [tuple(map(int, line.split())) for line in out.splitlines() if line.strip()]
    children: dict[int, list[int]] = {}
    rss = {}
    for p, pp, r in rows:
        children.setdefault(pp, []).append(p)
        rss[p] = r
    total, stack = 0, [pid]
    while stack:
        p = stack.pop()
        total += rss.get(p, 0)
        stack.extend(children.get(p, []))
    return total // 1024


def cmd_status(args) -> None:
    current = settings.load()
    print(f"mcpool {__version__}")
    for label, ok in service.loaded().items():
        print(f"  {label:17} {'loaded' if ok else 'not loaded'}")
    print("shared servers:")
    servers = shared.servers()
    for s in servers:
        state = f"pid {s['pid']}, {len(s['clients'])} sessions, {_rss_mb(s['pid'])} MB" if s["pid"] else "stopped"
        print(f"  {s['name']:40} {state}")
    if not servers:
        print("  (none)")
    manager = Manager(current)
    print("packages:")
    for key, info in manager.registry["packages"].items():
        try:
            version = manager.package(key, info).installed() or "not installed"
        except Exception as error:
            version = f"unknown ({error})"
        policy = f"pinned {info['version']}" if info.get("policy") == "pinned" else info["source"]
        print(f"  {key:40} {version} ({policy})")
    print(f"shared names: {', '.join(manager.registry['shared']) or '(none)'}")
    print(f"log: {paths.log_file()}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcpool", description=__doc__)
    p.add_argument("--version", action="version", version=f"mcpool {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="stdio entry point of a shared server (used in MCP configs)")
    serve.add_argument("--serena", action="store_true", help="one Serena per project root")
    serve.add_argument("--name", help="one server per name for all sessions")
    serve.add_argument("backend", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...] of the stdio server")
    serve.set_defaults(func=cmd_serve)

    watch = sub.add_parser("watch")
    watch.add_argument("state")
    watch.set_defaults(func=cmd_watch)

    reconcile = sub.add_parser("reconcile", help="adopt new entries, remove unused packages")
    reconcile.add_argument("--force", action="store_true", help="re-check every entry")
    reconcile.set_defaults(func=cmd_reconcile)
    sub.add_parser("upgrade", help="upgrade adopted packages, verified, with rollback").set_defaults(func=cmd_upgrade)
    sub.add_parser("daily", help="reconcile, then upgrade").set_defaults(func=cmd_daily)

    install = sub.add_parser("install", help="detect tools, start background jobs, adopt existing entries")
    install.add_argument("--no-adopt", action="store_true", help="do not adopt existing entries now")
    install.set_defaults(func=cmd_install)
    uninstall = sub.add_parser("uninstall", help="restore original entries and stop background jobs")
    uninstall.add_argument("--purge", action="store_true", help="also remove adopted packages and mcpool data")
    uninstall.set_defaults(func=cmd_uninstall)

    for name, text in (("share", "run every entry with this name through one shared server"),
                       ("unshare", "run every entry with this name once per session again")):
        s = sub.add_parser(name, help=text)
        s.add_argument("name")
        s.set_defaults(func=cmd_share)

    sub.add_parser("status", help="background jobs, shared servers, packages").set_defaults(func=cmd_status)
    return p


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "backend", None) and args.backend[:1] == ["--"]:
        args.backend = args.backend[1:]
    args.func(args)
