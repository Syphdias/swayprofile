import asyncio
import logging
import textwrap
from pathlib import Path

import pytest

from swayprofile import cli, config, daemon, outputs, plan

MINIMAL = """
default = "normal"
[profiles.normal]
[profiles.eco]
[profiles.gaming]
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "profiles.toml"
    p.write_text(textwrap.dedent(text))
    return p


def cfg_from(tmp_path: Path, text: str) -> config.Config:
    return config.load_config(write(tmp_path, text))


# ---- config


def test_example_config_loads(tmp_path):
    from swayprofile.install import render_config

    cfg = cfg_from(tmp_path, render_config(None, None))
    assert cfg.default == "normal"
    assert set(cfg.cycle) == {"eco", "normal", "gaming"}
    assert cfg.rules[0].match.fullmatch("steam_app_12345")


def test_minimal_config_defaults(tmp_path):
    cfg = cfg_from(tmp_path, MINIMAL)
    assert cfg.cycle == ["normal", "eco", "gaming"]
    assert cfg.idle_profile == "eco"
    assert cfg.lact_enabled is False


@pytest.mark.parametrize(
    "extra, msg",
    [
        ("exit_delay = true", "exit_delay: expected int or float, got bool"),
        ("[idle]\ntimeout = true", "idle.timeout: expected int, got bool"),
        ('cycle = ["eco", "nope"]', "unknown profile 'nope'"),
        ("cycle = [1]", "list of strings"),
        ("cycle = []", "must not be empty"),
        ('inhibit_idle_revert = "sometimes"', "inhibit_idle_revert: expected one of"),
        ('[[rule]]\nmatch = "("\nprofile = "gaming"', "rule[0].match"),
        ('[[rule]]\nprofile = "gaming"', "match is required"),
    ],
)
def test_config_errors(tmp_path, extra, msg):
    # top-level keys must come before the first table
    text = extra + "\n" + MINIMAL if not extra.startswith("[") else MINIMAL + "\n" + extra
    with pytest.raises(config.ConfigError, match=msg.replace("(", r"\(").replace("[", r"\[")):
        cfg_from(tmp_path, text)


def test_profile_field_errors(tmp_path):
    with pytest.raises(config.ConfigError, match="resolution: expected 'WIDTHxHEIGHT'"):
        cfg_from(tmp_path, MINIMAL + '\n[profiles.x]\noutputs.DP-1 = { resolution = "big" }')
    with pytest.raises(config.ConfigError, match="on_enter: expected a list of strings"):
        cfg_from(tmp_path, MINIMAL + "\n[profiles.x]\non_enter = [1]")


def test_unknown_keys_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="swayprofile"):
        cfg_from(tmp_path, MINIMAL + '\n[profiles.x]\npower_profle = "performance"')
    assert "profiles.x.power_profle" in caplog.text


# ---- outputs file / planning


def sway_output(name="DP-1", w=2560, h=1440, refresh=120000, modes=(60000, 120000, 144000), **kw):
    out = {
        "name": name,
        "make": "Dell Inc.",
        "model": "DELL S2721DGF",
        "serial": "ABC123",
        "active": True,
        "current_mode": {"width": w, "height": h, "refresh": refresh},
        "modes": [{"width": w, "height": h, "refresh": r} for r in modes],
        "adaptive_sync_status": "disabled",
    }
    out.update(kw)
    return out


def test_parse_outputs_file(tmp_path, caplog):
    f = tmp_path / "outputs"
    f.write_text(
        textwrap.dedent(
            """\
            # comment
            output DP-1 {
                mode 2560x1440@120.000Hz  # trailing comment
                adaptive_sync on
            }
            output "Dell Inc. DELL S2721DGF ABC123" {
                mode --custom 2560x1440@100Hz
            }
            output broken "x {
            """
        )
    )
    with caplog.at_level(logging.WARNING, logger="swayprofile"):
        parsed = outputs.parse_outputs_file(f)
    assert parsed["DP-1"] == {"mode": "2560x1440@120.000Hz", "adaptive_sync": "on"}
    assert parsed["Dell Inc. DELL S2721DGF ABC123"] == {"mode": "--custom 2560x1440@100Hz"}
    assert "cannot parse output line" in caplog.text


def test_parse_outputs_file_missing():
    assert outputs.parse_outputs_file(None) == {}
    assert outputs.parse_outputs_file(Path("/nonexistent/outputs")) == {}


def test_resolve_mode_picks_closest():
    assert outputs.resolve_mode(sway_output(), None, 143.9) == "2560x1440@144.000Hz"
    assert outputs.resolve_mode(sway_output(), "2560x1440", 59) == "2560x1440@60.000Hz"
    with pytest.raises(RuntimeError, match="no 1920x1080 mode"):
        outputs.resolve_mode(sway_output(), "1920x1080", 60)


def test_mode_matches():
    out = sway_output(refresh=119998)
    assert outputs.mode_matches(out, "2560x1440@120.000Hz")
    assert outputs.mode_matches(out, "2560x1440")
    assert outputs.mode_matches(out, "--custom 2560x1440@120Hz")
    assert not outputs.mode_matches(out, "2560x1440@144Hz")
    assert not outputs.mode_matches(out, "garbage")


def plan_cmds(tmp_path, profile_toml: str, outputs_file: str, sway_outputs):
    of = tmp_path / "outputs"
    of.write_text(outputs_file)
    cfg = cfg_from(tmp_path, f'outputs_file = "{of}"\n' + MINIMAL + profile_toml)
    return [a.arg for a in plan.plan_outputs(cfg, cfg.profiles["x"], sway_outputs)]


def test_plan_outputs_override_and_base(tmp_path):
    cmds = plan_cmds(
        tmp_path,
        "\n[profiles.x]\noutputs.DP-1 = { refresh = 144 }",
        "output DP-1 {\n mode 2560x1440@120.000Hz\n adaptive_sync on\n}\noutput DP-9 {\n mode 1x1\n}\n",
        [sway_output()],
    )
    # DP-9 is not connected, so it is skipped
    assert cmds == ['output "DP-1" mode 2560x1440@144.000Hz adaptive_sync on']


def test_plan_outputs_nothing_to_do(tmp_path):
    out = sway_output(adaptive_sync_status="enabled")
    assert (
        plan_cmds(tmp_path, "\n[profiles.x]", "output DP-1 {\n mode 2560x1440@120.000Hz\n adaptive_sync on\n}\n", [out])
        == []
    )


def test_plan_outputs_identifier_names_merge_with_connector(tmp_path):
    cmds = plan_cmds(
        tmp_path,
        "\n[profiles.x]\noutputs.DP-1 = { refresh = 60 }",
        'output "Dell Inc. DELL S2721DGF ABC123" {\n mode --custom 2560x1440@120Hz\n}\n',
        [sway_output()],
    )
    assert cmds == ['output "DP-1" mode 2560x1440@60.000Hz']


def test_plan_outputs_disable(tmp_path):
    cmds = plan_cmds(tmp_path, "\n[profiles.x]\noutputs.DP-1 = { enabled = false }", "", [sway_output()])
    assert cmds == ['output "DP-1" disable']


# ---- daemon logic (no sway needed)


RULES = """
[[rule]]
name = "game"
match = 'steam_app_\\d+'
profile = "gaming"
[profiles.gaming]
inhibit_idle = true
"""


def make_daemon(tmp_path, extra="") -> daemon.Daemon:
    return daemon.Daemon(cfg_from(tmp_path, "[profiles.normal]\n[profiles.eco]\n" + RULES + extra), dry_run=True)


class Con:
    def __init__(self, id, app_id=None, window_class=None):
        self.id, self.app_id, self.window_class = id, app_id, window_class


def test_desired_precedence(tmp_path):
    async def run():
        d = make_daemon(tmp_path)
        assert d.desired()[:2] == ("normal", "default")
        d.idle = True
        assert d.desired()[:2] == ("eco", "idle")
        assert d.add_window(Con(1, app_id="steam_app_42"))
        assert d.desired()[:2] == ("gaming", "rule: game")
        d.pinned = "eco"
        assert d.desired()[:2] == ("eco", "pinned")
        d.pinned = None
        assert d.remove_window(1)
        assert d.desired()[:2] == ("gaming", "rule: game (exit pending)")
        d.exit_timers.pop(0).cancel()
        assert d.desired()[:2] == ("eco", "idle")

    asyncio.run(run())


class FakeReply:
    def __init__(self, success, error=""):
        self.success, self.error = success, error


class FakeConn:
    """Sway IPC stand-in: commands for con ids in `gone` fail like for a closed window."""

    def __init__(self, gone=()):
        self.gone = set(gone)
        self.commands = []

    async def command(self, cmd):
        self.commands.append(cmd)
        ok = not any(f"con_id={g}]" in cmd for g in self.gone)
        return [FakeReply(ok, "No matching node.")]


def test_apply_inhibit_tolerates_closed_window(tmp_path):
    async def run():
        d = make_daemon(tmp_path)
        d.dry_run = False
        d.conn = FakeConn(gone={2})
        d.add_window(Con(1, app_id="steam_app_1"))
        d.add_window(Con(2, app_id="steam_app_2"))
        await d.apply_inhibit(d.cfg.profiles["gaming"])  # must not raise
        assert d.inhibited == {1}
        await d.apply_inhibit(d.cfg.profiles["normal"])
        assert d.inhibited == set()
        assert "[con_id=1] inhibit_idle fullscreen" in d.conn.commands

    asyncio.run(run())


def test_log_errors_swallows_and_logs(caplog):
    @daemon._log_errors
    async def handler():
        raise RuntimeError("boom")

    assert asyncio.iscoroutinefunction(handler)  # i3ipc awaits it only if so
    with caplog.at_level(logging.ERROR, logger="swayprofile"):
        asyncio.run(handler())
    assert "handler failed" in caplog.text
    assert "boom" in caplog.text


def test_spawn_keeps_reference_and_logs_failure(tmp_path, caplog):
    async def run():
        d = make_daemon(tmp_path)

        async def fail():
            raise RuntimeError("bg boom")

        t = d.spawn(fail())
        assert t in d.tasks
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert t not in d.tasks

    with caplog.at_level(logging.ERROR, logger="swayprofile"):
        asyncio.run(run())
    assert "bg boom" in caplog.text


def test_waybar_line():
    assert '"offline"' in cli.waybar_line(None)
    assert '"offline"' in cli.waybar_line({"profile": None})
    st = {
        "profile": "eco",
        "pinned": True,
        "icon": "a",
        "icon_pinned": "b",
        "reason": "pinned",
        "idle": False,
        "active_rules": [],
        "errors": [],
    }
    line = cli.waybar_line(st)
    assert '"text": "b"' in line and '"pinned"' in line


# ---- config schema 0.1


def test_gpu_key_renamed_to_lact(tmp_path):
    with pytest.raises(config.ConfigError, match=r"profiles\.x\.gpu was renamed to profiles\.x\.lact"):
        cfg_from(tmp_path, MINIMAL + "\n[profiles.x]\ngpu = { power_cap = 200.0 }")
    cfg = cfg_from(tmp_path, MINIMAL + "\n[profiles.x]\nlact = { power_cap = 200.0 }")
    assert cfg.profiles["x"].lact == {"power_cap": 200.0}


def test_dnd_backend(tmp_path):
    assert cfg_from(tmp_path, MINIMAL).dnd_backend == "auto"
    assert cfg_from(tmp_path, 'dnd_backend = "mako"\n' + MINIMAL).dnd_backend == "mako"
    with pytest.raises(config.ConfigError, match="dnd_backend: expected one of"):
        cfg_from(tmp_path, 'dnd_backend = "xfce"\n' + MINIMAL)


def test_lact_defaults(tmp_path):
    cfg = cfg_from(tmp_path, MINIMAL)
    assert cfg.lact_enabled is False
    assert cfg.lact_gpu is None


def test_missing_config_points_to_init_config(tmp_path):
    with pytest.raises(config.ConfigError, match="swayprofile init-config"):
        config.load_config(tmp_path / "nope.toml")


# ---- LACT client / GPU detection


def lact_server(tmp_path, reply: dict):
    """A one-shot fake lactd; returns (socket path, list that receives the request)."""
    import json
    import socket
    import threading

    path = str(tmp_path / "lact.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    got = []

    def serve():
        conn, _ = srv.accept()
        with conn, srv:
            got.append(json.loads(conn.makefile().readline()))
            conn.sendall(json.dumps(reply).encode() + b"\n")

    threading.Thread(target=serve, daemon=True).start()
    return path, got


def test_lact_request_without_args_omits_field(tmp_path):
    from swayprofile.backends.lact import Lact

    path, got = lact_server(tmp_path, {"status": "ok", "data": []})
    assert Lact(path).request("list_devices") == []
    assert got == [{"command": "list_devices"}]


def test_lact_request_error(tmp_path):
    from swayprofile.backends.lact import Lact

    path, _ = lact_server(tmp_path, {"status": "error", "data": {"description": "bad", "source": {"description": "x"}}})
    with pytest.raises(RuntimeError, match="LACT set_profile: bad: x"):
        Lact(path).request("set_profile", {"name": "p"})


class FakeLact:
    def __init__(self, devices):
        self.devices = devices

    def request(self, command, args=None):
        assert command == "list_devices"
        return self.devices


def test_detect_gpu():
    from swayprofile.backends.lact import detect_gpu, resolve_gpu

    one = [{"id": "1002:744C-0000:0b:00.0", "name": "RX"}]
    assert detect_gpu(FakeLact(one)) == "1002:744C-0000:0b:00.0"
    with pytest.raises(RuntimeError, match="no GPUs"):
        detect_gpu(FakeLact([]))
    with pytest.raises(RuntimeError, match=r"several GPUs; set lact.gpu to one of: a \(A\), b \(B\)"):
        detect_gpu(FakeLact([{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]))

    class Cfg:
        lact_gpu = "explicit"

    assert resolve_gpu(Cfg(), FakeLact([])) == "explicit"  # no lookup when configured


# ---- DND backends (stub commands on PATH)


STUBS = {
    # state file holds "true"/"false" (swaync, dunst) or the active modes (mako)
    "swaync-client": """
case "$1" in -D) cat "$STATE";; -dn) echo true > "$STATE";; -df) echo false > "$STATE";; esac""",
    "dunstctl": """
case "$1" in is-paused) cat "$STATE";; set-paused) echo "$2" > "$STATE";; esac""",
    "makoctl": """
if [ "$#" -eq 1 ]; then cat "$STATE"
elif [ "$2" = -a ]; then echo "$3" >> "$STATE"
elif [ "$2" = -r ]; then grep -vx "$3" "$STATE" > "$STATE.new"; mv "$STATE.new" "$STATE"; fi""",
}


@pytest.fixture
def stub_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        f = bindir / name
        f.write_text('#!/bin/sh\necho "$*" >> "$STATE.calls"' + body + "\n")
        f.chmod(0o755)
    state = tmp_path / "state"
    monkeypatch.setenv("STATE", str(state))
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return state


@pytest.mark.parametrize("name, initial", [("swaync", "false"), ("dunst", "false"), ("mako", "default")])
def test_dnd_backends_roundtrip(stub_path, name, initial):
    from swayprofile.backends.dnd import get_backend

    stub_path.write_text(initial + "\n")
    b = get_backend(name)
    assert b.name == name
    assert b.get() is False
    b.set(True)
    assert b.get() is True
    b.set(False)
    assert b.get() is False
    if name == "mako":
        assert stub_path.read_text().split() == ["default"]  # other modes untouched


def test_dnd_backend_auto_and_none(monkeypatch):
    from swayprofile.backends import dnd

    monkeypatch.setattr(dnd, "running_processes", lambda: {"zsh", "mako", "waybar"})
    assert dnd.get_backend("auto").name == "mako"
    monkeypatch.setattr(dnd, "running_processes", lambda: {"zsh"})
    assert dnd.get_backend("auto") is None
    assert dnd.get_backend("none") is None


def test_running_processes_sees_self():
    from swayprofile.backends.dnd import running_processes

    import os

    own = Path(f"/proc/{os.getpid()}/comm").read_text().strip()
    assert own in running_processes()


def test_daemon_dnd_save_and_restore(stub_path, tmp_path, monkeypatch):
    stub_path.write_text("true\n")  # DND already on before gaming
    monkeypatch.setattr(daemon, "DND_STATE_PATH", tmp_path / "dnd.saved")
    d = make_daemon(tmp_path)
    d.cfg.dnd_backend = "dunst"
    d.dry_run = False
    d.cfg.profiles["gaming"].dnd = True
    d.apply_dnd(d.cfg.profiles["gaming"])
    assert d.saved_dnd is True
    assert (tmp_path / "dnd.saved").read_text() == "true"
    stub_path.write_text("true\n")
    d.apply_dnd(d.cfg.profiles["normal"])  # leaving gaming restores the saved state
    assert stub_path.read_text().strip() == "true"
    assert d.saved_dnd is None
    assert not (tmp_path / "dnd.saved").exists()


def test_daemon_dnd_without_backend_is_noop(tmp_path, monkeypatch, caplog):
    from swayprofile.backends import dnd

    monkeypatch.setattr(dnd, "running_processes", lambda: set())
    d = make_daemon(tmp_path)
    d.dry_run = False
    d.cfg.profiles["gaming"].dnd = True
    with caplog.at_level(logging.WARNING, logger="swayprofile"):
        d.apply_dnd(d.cfg.profiles["gaming"])
    assert d.saved_dnd is None
    assert "no supported notification daemon" in caplog.text


# ---- power-profiles-daemon


def test_power_without_dbus_warns_once(monkeypatch, caplog):
    import sys

    from swayprofile.backends import power

    monkeypatch.setitem(sys.modules, "dbus", None)  # makes `import dbus` raise ImportError
    monkeypatch.setattr(power, "_warned_missing", False)
    with caplog.at_level(logging.WARNING, logger="swayprofile"):
        power.set_power_profile("performance")
        power.set_power_profile("balanced")
    assert caplog.text.count("dbus-python") == 1


# ---- init-config / install-service


def test_example_config_is_valid_and_safe(tmp_path):
    from swayprofile.install import render_config

    text = render_config(None, None)
    assert "@init-config" not in text
    cfg = cfg_from(tmp_path, text)
    assert set(cfg.profiles) == {"eco", "normal", "gaming"}
    assert cfg.lact_enabled is False
    assert cfg.dnd_backend == "auto"
    # nothing beyond power-profiles-daemon is active by default
    for p in cfg.profiles.values():
        assert (p.outputs, p.lact, p.dnd, p.inhibit_idle, p.on_enter) == ({}, None, None, False, [])


def test_render_config_fills_in_detection(tmp_path):
    from swayprofile.install import render_config

    outs = [sway_output("DP-2", modes=(24000, 60000, 144000)), sway_output("DP-1", modes=(30000, 75000))]
    text = render_config(outs, {"id": "1002:744C-x", "name": "RX"})
    assert '# gpu = "1002:744C-x"   # RX' in text
    eco = text.split("[profiles.eco]")[1].split("[profiles.normal]")[0]
    gaming = text.split("[profiles.gaming]")[1]
    assert eco.index("outputs.DP-1 = { refresh = 75 }") < eco.index("outputs.DP-2 = { refresh = 60 }")
    assert "outputs.DP-2 = { refresh = 144 }" in gaming
    assert "available 24, 60, 144" in gaming
    cfg_from(tmp_path, text)  # still valid TOML/config


def test_init_config_refuses_overwrite(tmp_path, monkeypatch, capsys):
    from swayprofile import install

    monkeypatch.setattr(install, "detect_outputs", lambda: None)
    monkeypatch.setattr(install, "detect_gpu", lambda _s: None)
    path = tmp_path / "sub" / "profiles.toml"
    assert install.init_config(path) == 0
    assert path.exists()
    path.write_text("mine")
    assert install.init_config(path) == 1
    assert path.read_text() == "mine"
    assert "--force" in capsys.readouterr().err
    assert install.init_config(path, force=True) == 0
    assert path.read_text() != "mine"


def test_render_unit_uses_percent_h(tmp_path):
    from swayprofile.install import render_unit

    home = Path("/home/u")
    assert "ExecStart=%h/.local/bin/swayprofile daemon" in render_unit("/home/u/.local/bin/swayprofile", home)
    assert "ExecStart=/usr/bin/swayprofile daemon" in render_unit("/usr/bin/swayprofile", Path("/home/u"))
    assert "@EXEC@" not in render_unit("/x/swayprofile", Path("/home/u"))
    assert "\nConditionEnvironment=SWAYSOCK\n" in render_unit("/x/swayprofile", Path("/home/u"))


def test_executable_path_not_resolved(tmp_path, monkeypatch):
    from swayprofile import install

    real = tmp_path / "venv" / "swayprofile"
    real.parent.mkdir()
    real.write_text("")
    link = tmp_path / "bin" / "swayprofile"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(install.sys, "argv", [str(link), "install-service"])
    assert install.executable_path() == str(link)


@pytest.fixture
def unit_env(tmp_path):
    calls = []

    def run(**kw):
        from swayprofile.install import install_service

        kw.setdefault("unit_dir", tmp_path / "user")
        kw.setdefault("system_dirs", (tmp_path / "system",))
        kw.setdefault("exec_path", "/opt/sp/bin/swayprofile")
        kw.setdefault("daemon_reload", lambda: calls.append("reload"))
        return install_service(**kw)

    run.unit = tmp_path / "user" / "swayprofile.service"
    run.calls = calls
    return run


def test_install_service_writes_and_reloads(unit_env, capsys):
    assert unit_env() == 0
    assert "ExecStart=/opt/sp/bin/swayprofile daemon" in unit_env.unit.read_text()
    assert unit_env.calls == ["reload"]
    assert "enable --now" in capsys.readouterr().out
    assert unit_env() == 0  # identical: up to date, no second reload
    assert unit_env.calls == ["reload"]


def test_install_service_refuses_changed_unit(unit_env, capsys):
    unit_env()
    unit_env.unit.write_text(unit_env.unit.read_text().replace("RestartSec=3", "RestartSec=9"))
    assert unit_env() == 1
    err = capsys.readouterr().err
    assert "-RestartSec=9" in err and "+RestartSec=3" in err
    assert "RestartSec=9" in unit_env.unit.read_text()
    assert unit_env(force=True) == 0
    assert "RestartSec=3" in unit_env.unit.read_text()


def test_install_service_replaces_symlink_not_target(unit_env, tmp_path):
    target = tmp_path / "repo.service"
    target.write_text("old unit")
    unit_env.unit.parent.mkdir()
    unit_env.unit.symlink_to(target)
    assert unit_env() == 1  # a symlink counts as an existing, different unit
    assert unit_env(force=True) == 0
    assert not unit_env.unit.is_symlink()
    assert target.read_text() == "old unit"


def test_install_service_refuses_to_shadow_packaged_unit(unit_env, tmp_path, capsys):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "swayprofile.service").write_text("pkg")
    assert unit_env() == 1
    assert not unit_env.unit.exists()
    assert "take\nprecedence" in capsys.readouterr().err
    assert unit_env(force=True) == 0


def test_install_service_reload_failure_is_a_warning(unit_env, capsys):
    assert unit_env(daemon_reload=lambda: "Failed to connect to bus") == 0
    assert "warning: `systemctl --user daemon-reload` failed: Failed to connect to bus" in capsys.readouterr().err


def test_lact_verification_skipped_when_superseded(tmp_path, monkeypatch, caplog):
    calls = []

    class OkLact:
        def __init__(self, _path):
            pass

        def request(self, command, args=None):
            calls.append(command)

    async def run():
        d = make_daemon(tmp_path)
        d.cfg.profiles["gaming"].lact = {"power_cap": 1.0}
        monkeypatch.setattr(daemon, "Lact", OkLact)
        monkeypatch.setattr(daemon, "verify_gpu", lambda *a: ["power cap 0 W, want 1.0 W"])
        real_sleep = asyncio.sleep

        async def fast_sleep(_s):
            d.target = "normal"  # another switch happens during the settle delay
            await real_sleep(0)

        monkeypatch.setattr(daemon.asyncio, "sleep", fast_sleep)
        d.target = "gaming"
        await d.apply_lact("swayprofile-gaming", d.cfg.profiles["gaming"])
        return d

    with caplog.at_level(logging.INFO, logger="swayprofile"):
        d = asyncio.run(run())
    assert calls == ["set_profile"]
    assert d.last_errors == []
    assert "superseded by normal" in caplog.text


def test_idle_watcher_ignores_user_swayidle_config(tmp_path, monkeypatch):
    # swayidle merges ~/.config/swayidle/config with CLI events; without -C the
    # user's lock/DPMS timeouts would run a second time from our instance.
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        raise FileNotFoundError

    monkeypatch.setattr(daemon.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(make_daemon(tmp_path).idle_watcher())
    assert calls[0][:3] == ("swayidle", "-C", "/dev/null")
