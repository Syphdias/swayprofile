"""The profile daemon: selection, applying, window/idle tracking, control socket."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import signal
import socket
import struct
from pathlib import Path
from typing import Any

from .backends.dnd import DndBackend, get_backend
from .backends.lact import LACT_RETRIES, LACT_RETRY_DELAY, Lact, resolve_gpu, verify_gpu
from .backends.notify import NOTIFY_TIMEOUT_MS, anotify
from .backends.power import set_power_profile
from .config import Config, ConfigError, Profile, load_config
from .paths import DND_STATE_PATH, SOCKET_PATH
from .plan import plan_profile

log = logging.getLogger(__name__)


def _peer_chain(writer: asyncio.StreamWriter, depth: int = 4) -> str:
    """Describe the client process and its parents (for debugging who switched)."""
    try:
        sock = writer.get_extra_info("socket")
        pid, _, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    except OSError:
        return "?"
    chain = []
    for _ in range(depth):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        chain.append(f"{pid}:{cmdline[:80]}")
        if ppid <= 1:
            break
        pid = ppid
    return " <- ".join(chain)


def _log_errors(fn):
    """For sway event handlers: i3ipc.aio stops the whole connection (and so the
    daemon) when a handler raises, so log the error and carry on instead."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            log.exception("%s failed", fn.__name__)

    return wrapper


def daemon_running() -> bool:
    """True if a daemon answers on SOCKET_PATH (a stale socket file doesn't count)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(SOCKET_PATH))
        return True
    except OSError:
        return False


# Subscribers whose unsent output exceeds this are dropped (they stopped reading).
MAX_SUBSCRIBER_BUFFER = 1 << 20


class Daemon:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.conn: Any = None  # i3ipc.aio.Connection, set in run()
        self.pinned: str | None = None
        self.idle = False
        # rule index -> set of matched con ids
        self.rule_windows: dict[int, set[int]] = {}
        # rule index -> pending exit timer
        self.exit_timers: dict[int, asyncio.TimerHandle] = {}
        self.current: str | None = None
        self.target: str | None = None  # profile currently being applied
        self.reason = ""
        self.saved_dnd: bool | None = None if dry_run else self._load_saved_dnd()
        self.inhibited: set[int] = set()
        self.subscribers: set[asyncio.StreamWriter] = set()
        self.idle_proc: asyncio.subprocess.Process | None = None
        self.idle_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.last_errors: list[str] = []
        self.switch_notification_id: str | None = None
        self._dnd: DndBackend | None = None
        self._dnd_setting: str | None = None
        # Strong references to background tasks: the event loop only keeps weak
        # ones, so an unreferenced task can be garbage-collected mid-run.
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    # ---- profile selection

    def active_rule(self) -> int | None:
        for i, _ in enumerate(self.cfg.rules):
            if self.rule_windows.get(i) or i in self.exit_timers:
                return i
        return None

    def desired(self) -> tuple[str, str, bool]:
        """Return (profile, reason, notify)."""
        if self.pinned:
            return self.pinned, "pinned", self.cfg.notify_manual
        i = self.active_rule()
        if i is not None:
            r = self.cfg.rules[i]
            suffix = " (exit pending)" if not self.rule_windows.get(i) else ""
            return r.profile, f"rule: {r.name}{suffix}", r.notify
        if self.idle and self.cfg.idle_enabled:
            return self.cfg.idle_profile, "idle", self.cfg.idle_notify
        return self.cfg.default, "default", self.cfg.notify_manual

    async def update(self, force: bool = False):
        async with self.lock:
            name, reason, should_notify = self.desired()
            self.reason = reason
            if name != self.current or force:
                await self.apply(name, notify_switch=should_notify and name != self.current)
            self.broadcast()

    # ---- applying

    async def sway(self, cmd: str):
        if self.dry_run:
            log.info("[dry-run] swaymsg %s", cmd)
            return
        for reply in await self.conn.command(cmd):
            if not reply.success:
                raise RuntimeError(f"sway: {cmd}: {reply.error}")

    async def apply(self, name: str, notify_switch: bool):
        cfg = self.cfg
        old = cfg.profiles.get(self.current) if self.current else None
        new = cfg.profiles[name]
        log.info("applying profile %s (%s), previous %s", name, self.reason, self.current)
        self.target = name
        # Published right away so errors reported later by background tasks
        # (apply_lact) land in this list instead of a replaced one.
        errors: list[str] = []
        self.last_errors = errors

        try:
            outputs = [o.ipc_data for o in await self.conn.get_outputs()]
            actions = plan_profile(cfg, new, outputs)
        except Exception as e:
            errors.append(str(e))
            actions = []
        for a in actions:
            try:
                if a.kind == "sway":
                    await self.sway(a.arg)
                elif a.kind == "ppd":
                    if self.dry_run:
                        log.info("[dry-run] %s", a.desc)
                    else:
                        await asyncio.to_thread(set_power_profile, a.arg)
                elif a.kind == "lact":
                    if self.dry_run:
                        log.info("[dry-run] %s", a.desc)
                    else:
                        # Background task: retries while lactd is still starting up.
                        self.spawn(self.apply_lact(a.arg, new))
                else:
                    log.info("%s", a.desc)
            except Exception as e:
                log.error("%s failed: %s", a.desc, e)
                errors.append(f"{a.desc}: {e}")

        # swaync hides popups while DND is on (and when it gets switched on), so:
        # turning DND off/restoring happens before the notification, turning it on
        # only after the notification popup has expired.
        dnd_on_later = new.dnd is True and not self.dry_run
        if not dnd_on_later:
            try:
                await asyncio.to_thread(self.apply_dnd, new)
            except Exception as e:
                errors.append(f"dnd: {e}")
        if notify_switch:
            self.switch_notification_id = await anotify(
                f"{new.icon.strip()} Profile: {name}".strip(),
                self.reason,
                replace_id=self.switch_notification_id,
                want_id=True,
            )
        if dnd_on_later:
            self.spawn(self.delayed_dnd(new, NOTIFY_TIMEOUT_MS / 1000 + 0.5 if notify_switch else 0))

        try:
            await self.apply_inhibit(new)
        except Exception as e:
            errors.append(f"inhibit_idle: {e}")

        hooks = []
        if old is not None and old.name != name:
            hooks += [(old.name, "on_exit", cmd) for cmd in old.on_exit]
        if old is None or old.name != name:
            hooks += [(name, "on_enter", cmd) for cmd in new.on_enter]
        if hooks:
            # One task, run in order: the old profile's on_exit before the new on_enter.
            self.spawn(self.run_hooks(hooks))

        self.current = name
        if errors:
            await anotify(f"swayprofile: errors applying {name}", "\n".join(errors), "critical")

    async def delayed_dnd(self, profile: Profile, delay: float):
        await asyncio.sleep(delay)
        async with self.lock:
            if self.target != profile.name:
                return  # switched away meanwhile; that switch handled DND
            try:
                await asyncio.to_thread(self.apply_dnd, profile)
            except Exception as e:
                log.error("dnd: %s", e)
                await anotify("swayprofile: DND error", str(e), "critical")

    @staticmethod
    def _load_saved_dnd() -> bool | None:
        try:
            return {"true": True, "false": False}.get(DND_STATE_PATH.read_text().strip())
        except OSError:
            return None

    def _store_saved_dnd(self):
        if self.dry_run:
            return
        if self.saved_dnd is None:
            DND_STATE_PATH.unlink(missing_ok=True)
        else:
            DND_STATE_PATH.write_text("true" if self.saved_dnd else "false")

    async def apply_lact(self, lact_name: str, profile: Profile):
        """Switch the LACT profile, retrying while lactd isn't ready, then verify via sysfs."""
        lact = Lact(self.cfg.lact_socket)
        for attempt in range(LACT_RETRIES):
            if self.target != profile.name:
                log.info("LACT %s: superseded by %s", lact_name, self.target)
                return
            try:
                await asyncio.to_thread(lact.request, "set_profile", {"name": lact_name, "auto_switch": False})
                break
            except (FileNotFoundError, PermissionError, ConnectionRefusedError) as e:
                # lactd not up yet, or socket not yet chgrp'ed to the admin group
                log.info("LACT not ready (%s), retry %d/%d", e, attempt + 1, LACT_RETRIES)
                await asyncio.sleep(LACT_RETRY_DELAY)
            except Exception as e:
                return await self.lact_error(f"LACT set_profile {lact_name}: {e}")
        else:
            return await self.lact_error(f"LACT set_profile {lact_name}: lactd not reachable")
        await asyncio.sleep(1)
        if self.target != profile.name:
            # Switched again meanwhile: the GPU now reflects (or is moving to) another
            # profile, so comparing it with this one would report a false mismatch.
            log.info("LACT %s: not verified, superseded by %s", lact_name, self.target)
            return
        try:
            gpu = await asyncio.to_thread(resolve_gpu, self.cfg, lact)
        except Exception as e:
            log.warning("LACT profile %s set, but cannot verify it: %s", lact_name, e)
            return
        problems = await asyncio.to_thread(verify_gpu, gpu, profile.lact or {})
        if problems:
            await self.lact_error(
                f"LACT accepted {lact_name} but the GPU does not match it ({'; '.join(problems)}). "
                "See `journalctl -u lactd`."
            )
        else:
            log.info("LACT profile %s active and verified", lact_name)

    async def lact_error(self, msg: str):
        log.error("%s", msg)
        self.last_errors.append(msg)
        self.broadcast()
        await anotify("swayprofile: LACT error", msg, "critical")

    def dnd(self) -> DndBackend | None:
        """The DND backend, looked up lazily: the notification daemon may start
        after swayprofile, and a reload may change dnd_backend."""
        if self._dnd is None or self._dnd_setting != self.cfg.dnd_backend:
            self._dnd_setting = self.cfg.dnd_backend
            self._dnd = get_backend(self.cfg.dnd_backend)
            if self._dnd:
                log.info("DND backend: %s", self._dnd.name)
        return self._dnd

    def apply_dnd(self, profile: Profile):
        if profile.dnd is None and self.saved_dnd is None:
            return  # nothing to set or restore
        backend = self.dnd()
        if backend is None:
            if self.cfg.dnd_backend != "none":
                log.warning("no supported notification daemon (swaync, dunst, mako) running; DND unchanged")
            return
        if profile.dnd is not None:
            if self.saved_dnd is None:
                self.saved_dnd = False if self.dry_run else backend.get()
                self._store_saved_dnd()
            if self.dry_run:
                log.info("[dry-run] dnd (%s) -> %s", backend.name, profile.dnd)
            else:
                backend.set(profile.dnd)
        else:
            if self.dry_run:
                log.info("[dry-run] dnd (%s) restore -> %s", backend.name, self.saved_dnd)
            else:
                backend.set(self.saved_dnd)
            self.saved_dnd = None
            self._store_saved_dnd()

    def inhibit_targets(self, profile: Profile) -> set[int]:
        if not profile.inhibit_idle:
            return set()
        ids: set[int] = set()
        for i, r in enumerate(self.cfg.rules):
            if r.profile == profile.name:
                ids |= self.rule_windows.get(i, set())
        return ids

    async def apply_inhibit(self, profile: Profile):
        targets = self.inhibit_targets(profile)
        for cid in self.inhibited - targets:
            try:
                await self.sway(f"[con_id={cid}] inhibit_idle {self.cfg.inhibit_idle_revert}")
            except RuntimeError:
                pass  # window is gone
        done = set()
        for cid in targets - self.inhibited:
            try:
                await self.sway(f"[con_id={cid}] inhibit_idle open")
                done.add(cid)
            except RuntimeError as e:
                # Usually a window that closed right after opening (launcher, splash).
                log.warning("%s", e)
        self.inhibited = (self.inhibited & targets) | done

    async def run_hooks(self, hooks: list[tuple[str, str, str]]):
        for profile, kind, cmd in hooks:
            await self.run_hook(profile, kind, cmd)

    async def run_hook(self, profile: str, kind: str, cmd: str):
        if self.dry_run:
            log.info("[dry-run] hook %s.%s: %s", profile, kind, cmd)
            return
        log.info("hook %s.%s: %s", profile, kind, cmd)
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await asyncio.wait_for(proc.communicate(), self.cfg.hook_timeout)
            if proc.returncode != 0:
                msg = f"{profile}.{kind}: `{cmd}` exited {proc.returncode}\n{out.decode(errors='replace')[-300:]}"
                log.error("%s", msg)
                await anotify("swayprofile: hook failed", msg, "critical")
        except TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()  # reap it
            log.error("hook %s.%s timed out: %s", profile, kind, cmd)
            await anotify("swayprofile: hook timed out", f"{profile}.{kind}: `{cmd}`", "critical")
        except Exception as e:
            log.error("hook %s.%s failed: %s", profile, kind, e)
            await anotify("swayprofile: hook failed", f"{profile}.{kind}: `{cmd}`: {e}", "critical")

    # ---- window tracking

    def match_rules(self, con) -> list[int]:
        keys = [k for k in (getattr(con, "app_id", None), con.window_class) if k]
        return [i for i, r in enumerate(self.cfg.rules) if any(r.match.fullmatch(k) for k in keys)]

    def add_window(self, con) -> bool:
        changed = False
        for i in self.match_rules(con):
            log.info("window %s (%s) matches rule %s", con.id, con.app_id or con.window_class, self.cfg.rules[i].name)
            self.rule_windows.setdefault(i, set()).add(con.id)
            t = self.exit_timers.pop(i, None)
            if t:
                t.cancel()
            changed = True
        return changed

    def remove_window(self, con_id: int) -> bool:
        changed = False
        self.inhibited.discard(con_id)
        for i, ids in self.rule_windows.items():
            if con_id in ids:
                ids.discard(con_id)
                changed = True
                if not ids:
                    delay = self.cfg.rules[i].exit_delay
                    log.info("rule %s: last window closed, leaving in %ss", self.cfg.rules[i].name, delay)
                    self.exit_timers[i] = asyncio.get_running_loop().call_later(delay, lambda i=i: self._exit_rule(i))
        return changed

    def _exit_rule(self, i: int):
        self.exit_timers.pop(i, None)
        log.info("rule %s: exit delay elapsed", self.cfg.rules[i].name)
        self.spawn(self.update())

    async def scan_windows(self):
        self.rule_windows.clear()
        tree = await self.conn.get_tree()
        for con in tree.leaves():
            self.add_window(con)

    @_log_errors
    async def on_window_new(self, _conn, event):
        if self.add_window(event.container):
            await self.update()
            # A new game window while already in the profile still needs inhibiting.
            if self.current:
                async with self.lock:
                    await self.apply_inhibit(self.cfg.profiles[self.current])

    @_log_errors
    async def on_window_close(self, _conn, event):
        if self.remove_window(event.container.id):
            await self.update()

    @_log_errors
    async def on_reload(self, _conn, _event):
        log.info("sway reloaded, re-applying %s", self.current)
        await asyncio.sleep(0.5)
        self.inhibited.clear()  # for_window rules may have reset inhibit_idle
        await self.update(force=True)

    def on_shutdown(self, _conn, _event):
        log.info("sway is shutting down")
        self.conn.main_quit()

    # ---- idle

    def restart_idle(self):
        if self.idle_task:
            self.idle_task.cancel()
        if self.idle_proc and self.idle_proc.returncode is None:
            self.idle_proc.terminate()
        self.idle = False
        self.idle_task = self.spawn(self.idle_watcher())

    async def idle_watcher(self):
        # No PR_SET_PDEATHSIG: preexec_fn is unsafe once threads exist (to_thread),
        # and systemd's KillMode=control-group stops swayidle with the daemon anyway.
        if not self.cfg.idle_enabled:
            return
        while True:
            try:
                # -C /dev/null: swayidle always loads the user's swayidle config in
                # addition to CLI events, which would run their lock/DPMS commands twice.
                self.idle_proc = await asyncio.create_subprocess_exec(
                    "swayidle",
                    "-C",
                    "/dev/null",
                    "timeout",
                    str(self.cfg.idle_timeout),
                    "echo idle",
                    "resume",
                    "echo active",
                    stdout=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                log.error("swayidle not found; idle detection disabled")
                return
            assert self.idle_proc.stdout
            async for line in self.idle_proc.stdout:
                state = line.decode().strip()
                if state in ("idle", "active"):
                    self.idle = state == "idle"
                    log.info("idle: %s", self.idle)
                    try:
                        await self.update()
                    except Exception:
                        # Keep watching; a failed switch must not end idle detection.
                        log.exception("idle switch failed")
            await self.idle_proc.wait()
            log.warning("swayidle exited (%s), restarting in 5s", self.idle_proc.returncode)
            self.idle = False
            await asyncio.sleep(5)

    # ---- control socket

    def status(self) -> dict:
        p = self.cfg.profiles.get(self.current or "")
        return {
            "profile": self.current,
            "icon": p.icon if p else "",
            "icon_pinned": (p.icon_pinned or p.icon) if p else "",
            "pinned": self.pinned is not None,
            "reason": self.reason,
            "idle": self.idle,
            "active_rules": [
                {"name": r.name, "windows": sorted(self.rule_windows.get(i, ())), "exit_pending": i in self.exit_timers}
                for i, r in enumerate(self.cfg.rules)
                if self.rule_windows.get(i) or i in self.exit_timers
            ],
            "dnd_saved": self.saved_dnd,
            "errors": self.last_errors,
            "profiles": self.cfg.cycle,
            "dry_run": self.dry_run,
        }

    def broadcast(self):
        line = (json.dumps(self.status(), ensure_ascii=False) + "\n").encode()
        for w in list(self.subscribers):
            if w.is_closing():
                self.subscribers.discard(w)
                continue
            if w.transport.get_write_buffer_size() > MAX_SUBSCRIBER_BUFFER:
                log.warning("dropping subscriber that stopped reading")
                self.subscribers.discard(w)
                w.close()
                continue
            w.write(line)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await reader.readline()
            if not line.strip():
                return  # connect-only probe (daemon_running) or empty request
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd not in ("status", "subscribe"):
                log.info("client command %s from %s", req, _peer_chain(writer))
            resp: dict = {"ok": True}
            if cmd == "set":
                name = req.get("profile")
                if name not in self.cfg.profiles:
                    resp = {"ok": False, "error": f"unknown profile {name!r}"}
                else:
                    self.pinned = name
                    await self.update()
            elif cmd == "auto":
                self.pinned = None
                await self.update()
            elif cmd == "cycle":
                cur = self.current if self.current in self.cfg.cycle else self.cfg.cycle[-1]
                self.pinned = self.cfg.cycle[(self.cfg.cycle.index(cur) + 1) % len(self.cfg.cycle)]
                await self.update()
            elif cmd == "reload":
                try:
                    new_cfg = load_config()
                except ConfigError as e:
                    resp = {"ok": False, "error": str(e)}
                else:
                    self.cfg = new_cfg
                    if self.pinned not in self.cfg.profiles:
                        self.pinned = None
                    for t in self.exit_timers.values():
                        t.cancel()
                    self.exit_timers.clear()
                    await self.scan_windows()
                    self.restart_idle()
                    await self.update(force=True)
            elif cmd == "subscribe":
                self.subscribers.add(writer)
                writer.write((json.dumps(self.status(), ensure_ascii=False) + "\n").encode())
                await writer.drain()
                await reader.read()  # wait until the client goes away
                return
            elif cmd != "status":
                resp = {"ok": False, "error": f"unknown command {cmd!r}"}
            resp["status"] = self.status()
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except Exception as e:
            log.exception("client error")
            try:
                writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            self.subscribers.discard(writer)
            writer.close()

    # ---- main

    async def run(self):
        from i3ipc import Event
        from i3ipc.aio import Connection

        self.conn = await Connection().connect()
        self.conn.on(Event.WINDOW_NEW, self.on_window_new)
        self.conn.on(Event.WINDOW_CLOSE, self.on_window_close)
        self.conn.on(Event.WORKSPACE_RELOAD, self.on_reload)
        self.conn.on(Event.SHUTDOWN, self.on_shutdown)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.conn.main_quit)

        server = None
        try:
            await self.scan_windows()
            await self.update(force=True)
            self.restart_idle()

            # Only accept clients once a profile is applied, so status is never empty.
            SOCKET_PATH.unlink(missing_ok=True)  # stale; main() checked no daemon answers
            old_umask = os.umask(0o077)  # socket is created 0600, no chmod race
            try:
                server = await asyncio.start_unix_server(self.handle_client, path=str(SOCKET_PATH))
            finally:
                os.umask(old_umask)

            await self.conn.main()
        finally:
            # Don't leave DND switched on behind the user's back when stopping.
            if self.saved_dnd is not None and not self.dry_run and (backend := self.dnd()):
                try:
                    backend.set(self.saved_dnd)
                    self.saved_dnd = None
                    self._store_saved_dnd()
                except Exception as e:
                    log.error("restoring DND on exit failed: %s", e)
            if self.idle_task:
                self.idle_task.cancel()
            if self.idle_proc and self.idle_proc.returncode is None:
                self.idle_proc.terminate()
            if server is not None:
                server.close()
                SOCKET_PATH.unlink(missing_ok=True)
