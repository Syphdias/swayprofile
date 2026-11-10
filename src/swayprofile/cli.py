"""swayprofile - switch sway/system power profiles (eco / normal / gaming).

Commands:
  swayprofile set <profile>       pin a profile (automatic triggers are ignored)
  swayprofile auto                release the pin, back to automatic selection
  swayprofile cycle               pin the next profile in the cycle list
  swayprofile status [--json]     show daemon state
  swayprofile reload              re-read the config file and re-apply
  swayprofile waybar              JSON stream for a waybar custom module
  swayprofile plan <profile>      print what applying <profile> would do (dry run)
  swayprofile lact-sync [--dry-run] [--force]
                                  create/update the swayprofile-* LACT profiles
  swayprofile daemon [--dry-run]  run the daemon (normally via systemd)
  swayprofile init-config [--force]
                                  write an example config with your outputs/GPU filled in
  swayprofile install-service [--force]
                                  write the systemd user unit (does not enable it)

Profile precedence: manual pin > window rule > idle > default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time

from .backends.lact import cmd_lact_sync
from .config import Config, ConfigError, load_config
from .daemon import Daemon, daemon_running
from .install import init_config, install_service
from .paths import SOCKET_PATH
from .plan import plan_profile

log = logging.getLogger(__name__)


def client_request(req: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(30)
        s.connect(str(SOCKET_PATH))
        s.sendall(json.dumps(req).encode() + b"\n")
        return json.loads(s.makefile().readline())


def format_status(st: dict) -> str:
    lines = [f"profile: {st['profile']}{' (pinned)' if st['pinned'] else ''}", f"reason:  {st['reason']}"]
    lines.append(f"idle:    {st['idle']}")
    for r in st["active_rules"]:
        lines.append(f"rule:    {r['name']} windows={r['windows']}{' exit pending' if r['exit_pending'] else ''}")
    if st.get("dry_run"):
        lines.append("DRY RUN: no changes are applied")
    for e in st.get("errors", []):
        lines.append(f"error:   {e}")
    return "\n".join(lines)


def waybar_line(st: dict | None) -> str:
    if st is None or not st.get("profile"):
        tooltip = "swayprofile daemon not running" if st is None else "swayprofile daemon starting"
        return json.dumps({"text": "?", "tooltip": tooltip, "class": "offline", "alt": "offline"})
    text = (st.get("icon_pinned") if st["pinned"] else st["icon"]) or st["profile"]
    tooltip = format_status(st) + "\n\nleft click: cycle · middle click: auto"
    obj = {
        "text": text,
        "tooltip": tooltip,
        "class": [st["profile"], "pinned" if st["pinned"] else "auto"],
        "alt": st["profile"],
    }
    return json.dumps(obj, ensure_ascii=False)


def cmd_waybar():
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(str(SOCKET_PATH))
                s.sendall(b'{"cmd": "subscribe"}\n')
                for line in s.makefile():
                    print(waybar_line(json.loads(line)), flush=True)
        except (OSError, json.JSONDecodeError):
            pass
        print(waybar_line(None), flush=True)
        time.sleep(3)


def cmd_plan(cfg: Config, name: str):
    import i3ipc

    if name not in cfg.profiles:
        sys.exit(f"unknown profile {name!r}")
    p = cfg.profiles[name]
    outputs = [o.ipc_data for o in i3ipc.Connection().get_outputs()]
    print(f"Plan for profile {name!r} (from current state):")
    actions = plan_profile(cfg, p, outputs)
    for a in actions:
        print(f"  - {a.desc}")
    if p.dnd is not None:
        print(f"  - dnd -> {p.dnd} (previous state restored on leaving)")
    if p.inhibit_idle:
        print(f"  - inhibit_idle open on windows matched by rules for {name!r}")
    for c in p.on_enter:
        print(f"  - hook on_enter: {c}")
    if not actions and p.dnd is None and not p.on_enter:
        print("  (nothing to do)")


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("swayprofile")
    except PackageNotFoundError:
        return "unknown (not installed)"


def main():
    ap = argparse.ArgumentParser(
        prog="swayprofile", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("profile")
    sub.add_parser("auto")
    sub.add_parser("cycle")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    sub.add_parser("reload")
    sub.add_parser("waybar")
    s = sub.add_parser("plan")
    s.add_argument("profile")
    s = sub.add_parser("lact-sync")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("daemon")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("init-config")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")
    s = sub.add_parser("install-service")
    s.add_argument("--force", action="store_true", help="replace an existing or shadow a packaged unit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.cmd == "waybar":
        return cmd_waybar()
    if args.cmd == "init-config":
        sys.exit(init_config(force=args.force))
    if args.cmd == "install-service":
        sys.exit(install_service(force=args.force))

    if args.cmd in ("daemon", "plan", "lact-sync"):
        try:
            cfg = load_config()
        except ConfigError as e:
            sys.exit(f"config error: {e}")
        if args.cmd == "plan":
            return cmd_plan(cfg, args.profile)
        if args.cmd == "lact-sync":
            return cmd_lact_sync(cfg, args.dry_run, args.force)
        if daemon_running():
            sys.exit(f"a swayprofile daemon is already running on {SOCKET_PATH}")
        try:
            asyncio.run(Daemon(cfg, dry_run=args.dry_run).run())
        except KeyboardInterrupt:
            pass
        except EOFError:
            log.info("sway IPC connection closed, exiting")
        return

    req = {"cmd": args.cmd}
    if args.cmd == "set":
        req["profile"] = args.profile
    try:
        resp = client_request(req)
    except OSError as e:
        sys.exit(f"cannot reach swayprofile daemon at {SOCKET_PATH}: {e}")
    if not resp.get("ok"):
        sys.exit(f"error: {resp.get('error')}")
    if args.cmd == "status" and args.json:
        print(json.dumps(resp["status"], indent=2))
    else:
        print(format_status(resp["status"]))
