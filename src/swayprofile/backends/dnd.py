"""Notification daemon do-not-disturb (swaync, dunst, mako)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=True).stdout.strip()


class DndBackend:
    name = ""
    process = ""  # process name (/proc/<pid>/comm) used by dnd_backend = "auto"

    def get(self) -> bool:
        raise NotImplementedError

    def set(self, on: bool):
        raise NotImplementedError


class Swaync(DndBackend):
    name = process = "swaync"

    def get(self) -> bool:
        return _run("swaync-client", "-D", "-sw") == "true"

    def set(self, on: bool):
        _run("swaync-client", "-dn" if on else "-df", "-sw")


class Dunst(DndBackend):
    name = process = "dunst"

    def get(self) -> bool:
        return _run("dunstctl", "is-paused") == "true"

    def set(self, on: bool):
        _run("dunstctl", "set-paused", "true" if on else "false")


class Mako(DndBackend):
    """Needs a `[mode=do-not-disturb]` section (e.g. `invisible=1`) in mako's config."""

    name = process = "mako"
    MODE = "do-not-disturb"

    def get(self) -> bool:
        return self.MODE in _run("makoctl", "mode").split()

    def set(self, on: bool):
        _run("makoctl", "mode", "-a" if on else "-r", self.MODE)


BACKENDS: dict[str, type[DndBackend]] = {b.name: b for b in (Swaync, Dunst, Mako)}


def running_processes() -> set[str]:
    """Names of the current user's processes."""
    uid = os.getuid()
    names = set()
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.parent.stat().st_uid == uid:
                names.add(comm.read_text().strip())
        except OSError:
            continue  # process exited
    return names


def get_backend(setting: str) -> DndBackend | None:
    """The backend for a `dnd_backend` setting; None for "none" or when "auto" finds none."""
    if setting == "none":
        return None
    if setting != "auto":
        return BACKENDS[setting]()
    procs = running_processes()
    for cls in BACKENDS.values():
        if cls.process in procs:
            return cls()
    return None
