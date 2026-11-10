"""`init-config` and `install-service`: first-time setup."""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from .paths import CONFIG_PATH

SYSTEM_UNIT_DIRS = (Path("/etc/systemd/user"), Path("/usr/lib/systemd/user"))
UNIT_NAME = "swayprofile.service"


def _data(name: str) -> str:
    return (files("swayprofile") / "data" / name).read_text()


# --------------------------------------------------------------------------- init-config


def detect_outputs() -> list[dict] | None:
    """Connected outputs from sway (None if sway is not reachable)."""
    try:
        import i3ipc

        return [o.ipc_data for o in i3ipc.Connection().get_outputs()]
    except Exception:
        return None


def detect_gpu(socket_path: str) -> dict | None:
    """The only GPU LACT knows about, or None (lactd not reachable, several GPUs, ...)."""
    from .backends.lact import Lact

    try:
        devices = Lact(socket_path).request("list_devices") or []
    except Exception:
        return None
    return devices[0] if len(devices) == 1 else None


def _output_lines(outputs: list[dict], pick: str) -> list[str]:
    """Commented `outputs.<name>` suggestions: pick "min" = lowest rate of at least
    60 Hz (24/30 Hz modes exist but are not usable for a desktop), "max" = highest."""
    lines = []
    for o in sorted(outputs, key=lambda o: o["name"]):
        cur = o.get("current_mode") or {}
        if not cur:
            continue
        size = (cur["width"], cur["height"])
        rates = sorted({round(m["refresh"] / 1000) for m in o.get("modes", []) if (m["width"], m["height"]) == size})
        if not rates:
            continue
        usable = [r for r in rates if r >= 60] or rates
        want = usable[0] if pick == "min" else rates[-1]
        desc = " ".join(str(o.get(k) or "") for k in ("make", "model")).strip()
        now = f"{cur['width']}x{cur['height']}@{round(cur['refresh'] / 1000)} Hz"
        available = ", ".join(map(str, rates))
        lines.append(f"# outputs.{o['name']} = {{ refresh = {want} }}   # {desc}: now {now}, available {available}")
    return lines


def render_config(outputs: list[dict] | None, gpu: dict | None) -> str:
    """The example config, with `@init-config:...` lines filled in from detection."""
    out = []
    for line in _data("profiles.example.toml").splitlines():
        m = re.search(r"\s*@init-config:(\w+)(?::(\w+))?$", line)
        if not m:
            out.append(line)
            continue
        plain = line[: m.start()].rstrip()
        if m.group(1) == "outputs" and outputs:
            out += _output_lines(outputs, m.group(2)) or [plain]
        elif m.group(1) == "gpu" and gpu:
            out.append(f'# gpu = "{gpu["id"]}"   # {gpu.get("name", "")}; auto-detected while it is the only GPU')
        else:
            out.append(plain)
    return "\n".join(out) + "\n"


def init_config(path: Path = CONFIG_PATH, force: bool = False) -> int:
    if path.exists() and not force:
        print(f"{path} already exists; use --force to overwrite it", file=sys.stderr)
        return 1
    outputs = detect_outputs()
    gpu = detect_gpu("/run/lactd.sock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(outputs, gpu))
    print(f"wrote {path}")
    if outputs is None:
        print("  sway not reachable: output suggestions are generic placeholders")
    else:
        print(f"  outputs found: {', '.join(sorted(o['name'] for o in outputs)) or 'none'} (as commented suggestions)")
    if gpu:
        print(f"  GPU via LACT: {gpu.get('name', gpu['id'])} (LACT stays disabled until you enable it)")
    print("Next: edit it, check with `swayprofile plan <profile>`, then `swayprofile install-service`.")
    return 0


# --------------------------------------------------------------------------- install-service


def executable_path() -> str:
    """Absolute path of this swayprofile executable, as invoked (symlinks not
    resolved: a pipx/uv venv behind ~/.local/bin may move on reinstall)."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "swayprofile" and argv0.is_absolute():
        return str(argv0)
    found = shutil.which("swayprofile")
    if found:
        return str(Path(found).absolute())
    raise RuntimeError("cannot find the swayprofile executable in PATH")


def render_unit(exec_path: str, home: Path | None = None) -> str:
    home = home or Path.home()
    if exec_path.startswith(f"{home}/"):
        exec_path = "%h/" + exec_path[len(str(home)) + 1 :]
    return _data(UNIT_NAME).replace("@EXEC@", exec_path)


def _daemon_reload() -> str | None:
    """Run `systemctl --user daemon-reload`; return an error message on failure."""
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return getattr(e, "stderr", None) or str(e)
    return None


def install_service(
    force: bool = False,
    unit_dir: Path | None = None,
    system_dirs: tuple[Path, ...] = SYSTEM_UNIT_DIRS,
    exec_path: str | None = None,
    daemon_reload: Callable[[], str | None] = _daemon_reload,
) -> int:
    if unit_dir is None:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        unit_dir = config_home / "systemd" / "user"
    unit = unit_dir / UNIT_NAME

    system_units = [d / UNIT_NAME for d in system_dirs if (d / UNIT_NAME).exists()]
    if system_units and not force:
        print(
            f"{system_units[0]} already exists (installed by a package). A user unit would silently take\n"
            "precedence over it, so nothing was written. Use it with:\n"
            "  systemctl --user enable --now swayprofile\n"
            "(or pass --force to install a user unit anyway)",
            file=sys.stderr,
        )
        return 1

    try:
        content = render_unit(exec_path or executable_path())
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if unit.exists() or unit.is_symlink():
        old = unit.read_text() if unit.exists() else ""
        if old == content and not unit.is_symlink():
            print(f"{unit} is up to date")
            return 0
        if not force:
            what = f"{unit} is a symlink to {os.readlink(unit)}" if unit.is_symlink() else f"{unit} differs"
            diff = difflib.unified_diff(old.splitlines(), content.splitlines(), str(unit), "new", lineterm="")
            print(f"{what}; use --force to replace it:", file=sys.stderr)
            print("\n".join(diff), file=sys.stderr)
            return 1
        unit.unlink()  # also replaces a symlink instead of writing through it

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    print(f"wrote {unit}")
    err = daemon_reload()
    if err:
        print(f"warning: `systemctl --user daemon-reload` failed: {err.strip()}", file=sys.stderr)
    print("Not enabled. To start it now and with every sway session:\n  systemctl --user enable --now swayprofile")
    return 0
