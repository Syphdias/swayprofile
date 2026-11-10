"""LACT (GPU control daemon) client, sysfs verification and `lact-sync`."""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config, Profile


class Lact:
    """Minimal client for the LACT daemon socket (newline-delimited JSON)."""

    def __init__(self, path: str):
        self.path = path

    def request(self, command: str, args: dict | None = None):
        # Commands with arguments require "args"; argument-less ones (list_devices)
        # are rejected when it is present.
        msg: dict = {"command": command}
        if args is not None:
            msg["args"] = args
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.path)
            s.sendall(json.dumps(msg).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf.strip():
            raise RuntimeError(f"LACT {command}: lactd closed the connection without replying")
        resp = json.loads(buf)
        if resp.get("status") != "ok":
            raise RuntimeError(f"LACT {command}: {_lact_error(resp.get('data'))}")
        return resp.get("data")


def _lact_error(data) -> str:
    parts = []
    while isinstance(data, dict):
        parts.append(str(data.get("description")))
        data = data.get("source")
    return ": ".join(parts) or "unknown error"


LACT_RETRIES = 10
LACT_RETRY_DELAY = 3


def verify_gpu(gpu_id: str | None, want: dict) -> list[str]:
    """Compare the GPU's sysfs state with the LACT settings we asked for."""
    if not gpu_id or not want:
        return []
    dev = Path("/sys/bus/pci/devices") / gpu_id.rsplit("-", 1)[-1]
    problems = []
    try:
        if "power_cap" in want:
            caps = list(dev.glob("hwmon/hwmon*/power1_cap"))
            if caps:
                have = int(caps[0].read_text()) / 1_000_000
                if abs(have - float(want["power_cap"])) > 1:
                    problems.append(f"power cap {have:.0f} W, want {want['power_cap']} W")
        if "performance_level" in want:
            have = (dev / "power_dpm_force_performance_level").read_text().strip()
            if have != want["performance_level"]:
                problems.append(f"performance level {have}, want {want['performance_level']}")
        if "power_profile_mode_index" in want:
            for line in (dev / "pp_power_profile_mode").read_text().splitlines():
                m = re.match(r"\s*(\d+)\s+\S+?\s*\*", line)
                if m:
                    if int(m.group(1)) != int(want["power_profile_mode_index"]):
                        problems.append(f"power profile mode {m.group(1)}, want {want['power_profile_mode_index']}")
                    break
    except (OSError, ValueError) as e:
        problems.append(f"cannot read GPU state: {e}")
    return problems


def detect_gpu(lact: Lact) -> str:
    """The GPU id, if LACT knows exactly one GPU."""
    devices = lact.request("list_devices") or []
    if len(devices) == 1:
        return devices[0]["id"]
    if not devices:
        raise RuntimeError("LACT reports no GPUs")
    ids = ", ".join(f"{d['id']} ({d.get('name', '?')})" for d in devices)
    raise RuntimeError(f"LACT reports several GPUs; set lact.gpu to one of: {ids}")


def resolve_gpu(cfg: Config, lact: Lact) -> str:
    return cfg.lact_gpu or detect_gpu(lact)


def lact_profile_name(cfg: Config, profile: Profile) -> str:
    return cfg.lact_prefix + profile.name


def build_lact_profile(gpu: str, lact: Lact, profile: Profile) -> dict:
    """GPU config = LACT's default (no-profile) config for the GPU, overlaid with profile.lact."""
    base = lact.request("get_profile", {"name": None}) or {}
    gpu_cfg = dict((base.get("gpus") or {}).get(gpu, {}))
    gpu_cfg.update(profile.lact or {})
    return {"gpus": {gpu: gpu_cfg}, "rule": None}


def cmd_lact_sync(cfg: Config, dry_run: bool, force: bool):
    lact = Lact(cfg.lact_socket)
    try:
        gpu = resolve_gpu(cfg, lact)
    except RuntimeError as e:
        sys.exit(str(e))
    existing = lact.request("list_profiles", {"include_state": False}) or {}
    names = set((existing.get("profiles") or {}).keys())
    current = existing.get("current_profile")
    if existing.get("auto_switch"):
        print("WARNING: LACT auto profile switching is enabled; it will fight swayprofile.")
    base_gpu = ((lact.request("get_profile", {"name": None}) or {}).get("gpus") or {}).get(gpu, {})
    print(f"LACT default config for {gpu} (copied into every profile):")
    print("  " + json.dumps(base_gpu))
    for p in cfg.profiles.values():
        if p.lact is None:
            continue
        name = lact_profile_name(cfg, p)
        want = build_lact_profile(gpu, lact, p)
        create = {"command": "create_profile", "args": {"name": name, "base": {"provided": want}}}
        changes = ", ".join(
            f"{k}: {base_gpu.get(k, '(unset)')} -> {v}" for k, v in p.lact.items() if base_gpu.get(k) != v
        )
        print(f"\n== {name} ==")
        print(f"  changes vs default: {changes or 'none'}")
        if name in names:
            have = lact.request("get_profile", {"name": name}) or {}
            have_gpu = (have.get("gpus") or {}).get(gpu)
            if have_gpu == want["gpus"][gpu]:
                print("  up to date")
                continue
            print("  differs from existing LACT profile")
            print(f"  have: {json.dumps(have_gpu)}")
            print(f"  want: {json.dumps(want['gpus'][gpu])}")
            if not force:
                print("  (use --force to replace it)")
                continue
            if name == current:
                print(f"  {name} is LACT's active profile; switch away first (e.g. `swayprofile set` another profile)")
                continue
            delete = {"command": "delete_profile", "args": {"name": name}}
            print(f"  {'would send' if dry_run else 'sending'}: {json.dumps(delete)}")
            if not dry_run:
                lact.request("delete_profile", {"name": name})
        print(f"  {'would send' if dry_run else 'sending'}:")
        print("    " + json.dumps(create, indent=2).replace("\n", "\n    "))
        if not dry_run:
            lact.request("create_profile", create["args"])
