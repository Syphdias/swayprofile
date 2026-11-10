"""Config file loading and validation."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass
class OutputOverride:
    refresh: float | None = None  # Hz, resolved to the closest available mode
    resolution: str | None = None  # "WxH"; default: from outputs file / current
    adaptive_sync: bool | None = None
    enabled: bool | None = None


@dataclass
class Profile:
    name: str
    icon: str = ""  # shown while selected automatically
    icon_pinned: str = ""  # shown while pinned (default: icon)
    power_profile: str | None = None  # power-profiles-daemon profile
    outputs: dict[str, OutputOverride] = field(default_factory=dict)
    lact: dict | None = None  # LACT GpuConfig overrides
    dnd: bool | None = None
    inhibit_idle: bool = False
    on_enter: list[str] = field(default_factory=list)
    on_exit: list[str] = field(default_factory=list)


@dataclass
class Rule:
    name: str
    match: re.Pattern
    profile: str
    exit_delay: float
    notify: bool


@dataclass
class Config:
    default: str
    cycle: list[str]
    outputs_file: Path | None
    inhibit_idle_revert: str
    hook_timeout: float
    notify_manual: bool
    idle_enabled: bool
    idle_timeout: int
    idle_profile: str
    idle_notify: bool
    dnd_backend: str
    lact_enabled: bool
    lact_socket: str
    lact_gpu: str | None
    lact_prefix: str
    rules: list[Rule]
    profiles: dict[str, Profile]


def _expect(d: dict, key: str, typ: type | tuple[type, ...], default: Any = None, where: str = "") -> Any:
    v = d.get(key, default)
    types = typ if isinstance(typ, tuple) else (typ,)
    # bool is a subclass of int, so `timeout = true` would pass an int check.
    bad_bool = isinstance(v, bool) and bool not in types
    if v is not None and (bad_bool or not isinstance(v, types)):
        want = " or ".join(t.__name__ for t in types)
        raise ConfigError(f"{where}{key}: expected {want}, got {type(v).__name__}")
    return v


def _expect_str_list(d: dict, key: str, default: list[str], where: str = "") -> list[str]:
    v = _expect(d, key, list, default, where)
    if not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{where}{key}: expected a list of strings")
    return v


def _check_keys(d: dict, allowed: set[str], where: str):
    """Warn about unknown keys (typos would otherwise be silently ignored)."""
    for k in d.keys() - allowed:
        log.warning("config: unknown key %s%s (ignored)", where, k)


_TOP_KEYS = {
    "default",
    "cycle",
    "exit_delay",
    "outputs_file",
    "inhibit_idle_revert",
    "hook_timeout",
    "notify_manual",
    "dnd_backend",
    "idle",
    "lact",
    "rule",
    "profiles",
}
_PROFILE_KEYS = {
    "icon",
    "icon_pinned",
    "power_profile",
    "outputs",
    "lact",
    "dnd",
    "inhibit_idle",
    "on_enter",
    "on_exit",
}
_OUTPUT_KEYS = {"refresh", "resolution", "adaptive_sync", "enabled"}
_RULE_KEYS = {"name", "match", "profile", "exit_delay", "notify"}
_IDLE_KEYS = {"enabled", "timeout", "profile", "notify"}
_LACT_KEYS = {"enabled", "socket", "gpu", "prefix"}
INHIBIT_IDLE_VALUES = {"focus", "fullscreen", "open", "none", "visible"}
DND_BACKENDS = {"auto", "swaync", "dunst", "mako", "none"}
# Keys renamed in 0.1; an error is clearer than silently ignoring the old name.
_RENAMED_PROFILE_KEYS = {"gpu": "lact"}


def load_config(path: Path = CONFIG_PATH) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"config file not found: {path}\nCreate one with `swayprofile init-config`, then edit it."
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    _check_keys(raw, _TOP_KEYS, "")

    profiles: dict[str, Profile] = {}
    for name, p in _expect(raw, "profiles", dict, {}).items():
        w = f"profiles.{name}."
        if not isinstance(p, dict):
            raise ConfigError(f"profiles.{name}: expected a table")
        for old, new in _RENAMED_PROFILE_KEYS.items():
            if old in p:
                raise ConfigError(f"{w}{old} was renamed to {w}{new}")
        _check_keys(p, _PROFILE_KEYS, w)
        outputs = {}
        for oname, o in _expect(p, "outputs", dict, {}, w).items():
            ow = f"{w}outputs.{oname}."
            if not isinstance(o, dict):
                raise ConfigError(f"{w}outputs.{oname}: expected a table")
            _check_keys(o, _OUTPUT_KEYS, ow)
            resolution = _expect(o, "resolution", str, None, ow)
            if resolution is not None and not re.fullmatch(r"\d+x\d+", resolution.lower()):
                raise ConfigError(f"{ow}resolution: expected 'WIDTHxHEIGHT', got {resolution!r}")
            outputs[oname] = OutputOverride(
                refresh=_expect(o, "refresh", (int, float), None, ow),
                resolution=resolution,
                adaptive_sync=_expect(o, "adaptive_sync", bool, None, ow),
                enabled=_expect(o, "enabled", bool, None, ow),
            )
        profiles[name] = Profile(
            name=name,
            icon=_expect(p, "icon", str, "", w),
            icon_pinned=_expect(p, "icon_pinned", str, "", w),
            power_profile=_expect(p, "power_profile", str, None, w),
            outputs=outputs,
            lact=_expect(p, "lact", dict, None, w),
            dnd=_expect(p, "dnd", bool, None, w),
            inhibit_idle=_expect(p, "inhibit_idle", bool, False, w),
            on_enter=_expect_str_list(p, "on_enter", [], w),
            on_exit=_expect_str_list(p, "on_exit", [], w),
        )
    if not profiles:
        raise ConfigError("no profiles defined")

    def check_profile(name, where):
        if name not in profiles:
            raise ConfigError(f"{where}: unknown profile {name!r}")
        return name

    default = check_profile(_expect(raw, "default", str, "normal"), "default")
    cycle = [check_profile(n, "cycle") for n in _expect_str_list(raw, "cycle", list(profiles))]
    if not cycle:
        raise ConfigError("cycle: must not be empty")
    exit_delay = float(_expect(raw, "exit_delay", (int, float), 60))
    inhibit_idle_revert = _expect(raw, "inhibit_idle_revert", str, "fullscreen")
    if inhibit_idle_revert not in INHIBIT_IDLE_VALUES:
        raise ConfigError(
            f"inhibit_idle_revert: expected one of {', '.join(sorted(INHIBIT_IDLE_VALUES))}, "
            f"got {inhibit_idle_revert!r}"
        )

    rules = []
    for i, r in enumerate(_expect(raw, "rule", list, [])):
        w = f"rule[{i}]."
        if not isinstance(r, dict):
            raise ConfigError(f"rule[{i}]: expected a table")
        _check_keys(r, _RULE_KEYS, w)
        pattern = _expect(r, "match", str, None, w)
        if not pattern:
            raise ConfigError(f"{w}match is required")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ConfigError(f"{w}match: {e}") from e
        rules.append(
            Rule(
                name=_expect(r, "name", str, pattern, w),
                match=rx,
                profile=check_profile(_expect(r, "profile", str, None, w), f"{w}profile"),
                exit_delay=float(_expect(r, "exit_delay", (int, float), exit_delay, w)),
                notify=_expect(r, "notify", bool, True, w),
            )
        )

    dnd_backend = _expect(raw, "dnd_backend", str, "auto")
    if dnd_backend not in DND_BACKENDS:
        raise ConfigError(f"dnd_backend: expected one of {', '.join(sorted(DND_BACKENDS))}, got {dnd_backend!r}")

    idle = _expect(raw, "idle", dict, {})
    _check_keys(idle, _IDLE_KEYS, "idle.")
    lact = _expect(raw, "lact", dict, {})
    _check_keys(lact, _LACT_KEYS, "lact.")
    of = _expect(raw, "outputs_file", str, "~/.config/sway/outputs")
    return Config(
        default=default,
        cycle=cycle,
        outputs_file=Path(of).expanduser() if of else None,
        inhibit_idle_revert=inhibit_idle_revert,
        hook_timeout=float(_expect(raw, "hook_timeout", (int, float), 60)),
        notify_manual=_expect(raw, "notify_manual", bool, True),
        dnd_backend=dnd_backend,
        idle_enabled=_expect(idle, "enabled", bool, True, "idle."),
        idle_timeout=_expect(idle, "timeout", int, 120, "idle."),
        idle_profile=check_profile(_expect(idle, "profile", str, "eco", "idle."), "idle.profile"),
        idle_notify=_expect(idle, "notify", bool, True, "idle."),
        lact_enabled=_expect(lact, "enabled", bool, False, "lact."),
        lact_socket=_expect(lact, "socket", str, "/run/lactd.sock", "lact."),
        lact_gpu=_expect(lact, "gpu", str, None, "lact."),
        lact_prefix=_expect(lact, "prefix", str, "swayprofile-", "lact."),
        rules=rules,
        profiles=profiles,
    )
