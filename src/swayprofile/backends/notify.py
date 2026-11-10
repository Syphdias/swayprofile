"""Desktop notifications via notify-send."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from typing import Any

log = logging.getLogger(__name__)

NOTIFY_TIMEOUT_MS = 4000


def notify(
    summary: str, body: str = "", urgency: str = "normal", *, replace_id: str | None = None, want_id: bool = False
) -> str | None:
    """Send a desktop notification (blocking; use `anotify` from async code).

    want_id=True returns the notification id; passing it back as replace_id replaces
    that notification (notify-send's -p/-r) instead of stacking a new one. The
    x-canonical-private-synchronous hint would be simpler, but swaync silently drops it.
    """
    if not shutil.which("notify-send"):
        return None
    cmd = ["notify-send", "-a", "swayprofile", "-u", urgency, "-t", str(NOTIFY_TIMEOUT_MS)]
    if want_id:
        cmd.append("-p")
    if replace_id:
        cmd += ["-r", replace_id]
    try:
        out = subprocess.run([*cmd, summary, body], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notify-send failed: %s", e)
        return None
    if out.returncode != 0:
        log.warning("notify-send failed: %s", out.stderr.strip())
        return None
    if not want_id:
        return None
    return out.stdout.strip() or None


async def anotify(*args: Any, **kwargs: Any) -> str | None:
    """`notify` without blocking the event loop."""
    return await asyncio.to_thread(notify, *args, **kwargs)
