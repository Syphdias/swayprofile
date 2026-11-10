"""sway outputs: the nwg-displays outputs file and mode matching."""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def parse_outputs_file(path: Path | None) -> dict[str, dict[str, str]]:
    """Parse `output NAME { key value ... }` blocks (nwg-displays format).

    NAME is a connector name (`DP-1`) or a quoted output identifier
    (`"Make Model Serial"`, nwg-displays' "use description" mode).
    """
    if not path or not path.exists():
        return {}
    result: dict[str, dict[str, str]] = {}
    current = None
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r'output\s+(?:"([^"]+)"|([^"\s{]+))\s*\{?', line)
        if m:
            current = result.setdefault(m.group(1) or m.group(2), {})
            continue
        if current is None and line.startswith("output"):
            log.warning("%s:%d: cannot parse output line, ignored: %s", path, lineno, line)
            continue
        if line == "}":
            current = None
            continue
        if current is not None:
            key, _, value = line.partition(" ")
            current[key] = value.strip()
    return result


def resolve_mode(output: dict, resolution: str | None, refresh: float) -> str:
    """Pick the available mode closest to `refresh` Hz at `resolution`."""
    if resolution:
        w, h = (int(x) for x in resolution.lower().split("x"))
    else:
        w, h = output["current_mode"]["width"], output["current_mode"]["height"]
    modes = [m for m in output.get("modes", []) if m["width"] == w and m["height"] == h]
    if not modes:
        raise RuntimeError(f"{output['name']}: no {w}x{h} mode available")
    best = min(modes, key=lambda m: abs(m["refresh"] / 1000 - refresh))
    return f"{w}x{h}@{best['refresh'] / 1000:.3f}Hz"


def mode_resolution(mode: str) -> str:
    """'--custom 2560x1440@120Hz' -> '2560x1440'."""
    return mode.removeprefix("--custom").strip().split("@")[0]


def mode_matches(output: dict, mode: str) -> bool:
    m = re.fullmatch(r"(\d+)x(\d+)(?:@([\d.]+)Hz)?", mode.removeprefix("--custom").strip())
    cur = output.get("current_mode") or {}
    if not m or not cur:
        return False
    if (int(m.group(1)), int(m.group(2))) != (cur["width"], cur["height"]):
        return False
    return m.group(3) is None or abs(float(m.group(3)) * 1000 - cur["refresh"]) < 50
