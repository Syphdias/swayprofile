"""Turn a profile into the actions needed from the current state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .backends.lact import lact_profile_name
from .config import Config, OutputOverride, Profile
from .outputs import mode_matches, mode_resolution, parse_outputs_file, resolve_mode


@dataclass
class Action:
    desc: str
    kind: str  # sway | ppd | lact | noop
    arg: Any = None


def plan_outputs(cfg: Config, profile: Profile, sway_outputs: list[dict]) -> list[Action]:
    """Effective output config = outputs file values overlaid with profile overrides."""
    by_name = {o["name"]: o for o in sway_outputs}
    # Outputs may be named by connector ("DP-1") or identifier ("Make Model Serial");
    # normalize both to the connector name. Unknown names = not connected.
    connector = {o["name"]: o["name"] for o in sway_outputs}
    for o in sway_outputs:
        connector[f"{o.get('make')} {o.get('model')} {o.get('serial')}"] = o["name"]
    base = {connector[k]: v for k, v in parse_outputs_file(cfg.outputs_file).items() if k in connector}
    overrides = {connector[k]: v for k, v in profile.outputs.items() if k in connector}
    actions = []
    for name in sorted(set(base) | set(overrides)):
        out = by_name[name]
        b = base.get(name, {})
        ov = overrides.get(name, OutputOverride())

        enabled = ov.enabled if ov.enabled is not None else b.get("disable") is None
        if not enabled:
            if out.get("active", True):
                actions.append(Action(f"output {name} disable", "sway", f'output "{name}" disable'))
            continue
        parts = []
        if not out.get("active", True):
            parts.append("enable")

        if ov.refresh is not None:
            res = ov.resolution or (mode_resolution(b["mode"]) if "mode" in b else None)
            mode = resolve_mode(out, res, ov.refresh)
        else:
            mode = b.get("mode")
        if mode and not mode_matches(out, mode):
            parts.append(f"mode {mode}")

        vrr = (
            ov.adaptive_sync
            if ov.adaptive_sync is not None
            else (b["adaptive_sync"] == "on" if "adaptive_sync" in b else None)
        )
        if vrr is not None and (out.get("adaptive_sync_status") == "enabled") != vrr:
            parts.append(f"adaptive_sync {'on' if vrr else 'off'}")

        if parts:
            cmd = f'output "{name}" ' + " ".join(parts)
            actions.append(Action(cmd, "sway", cmd))
    return actions


def plan_profile(cfg: Config, profile: Profile, sway_outputs: list[dict]) -> list[Action]:
    actions = plan_outputs(cfg, profile, sway_outputs)
    if profile.power_profile:
        actions.append(Action(f"power-profiles-daemon -> {profile.power_profile}", "ppd", profile.power_profile))
    if profile.lact is not None:
        name = lact_profile_name(cfg, profile)
        req = {"command": "set_profile", "args": {"name": name, "auto_switch": False}}
        if cfg.lact_enabled:
            actions.append(Action(f"LACT {json.dumps(req)}", "lact", name))
        else:
            actions.append(Action(f"(skipped, lact.enabled=false) LACT {json.dumps(req)}", "noop"))
    return actions
