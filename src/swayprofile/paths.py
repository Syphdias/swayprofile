"""Filesystem locations (overridable via environment variables)."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get(
        "SWAYPROFILE_CONFIG",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "swayprofile" / "profiles.toml",
    )
)
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCKET_PATH = Path(os.environ.get("SWAYPROFILE_SOCKET", RUNTIME_DIR / "swayprofile.sock"))
# DND state saved before a profile turned DND on. Lives in the runtime dir so it
# survives daemon restarts/crashes (but not logout).
DND_STATE_PATH = RUNTIME_DIR / "swayprofile.dnd"
