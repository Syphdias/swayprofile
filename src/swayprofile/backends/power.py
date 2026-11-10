"""power-profiles-daemon via D-Bus (needs dbus-python; optional)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_warned_missing = False


def set_power_profile(name: str):
    """Switch the power-profiles-daemon profile. Without dbus-python this logs
    once and does nothing, so the rest of a profile still applies."""
    global _warned_missing
    try:
        import dbus
    except ImportError:
        if not _warned_missing:
            log.warning("python dbus module (dbus-python) not installed; power profile switching disabled")
            _warned_missing = True
        return

    bus = dbus.SystemBus()
    for busname, path in (
        ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
        ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),
    ):
        try:
            obj = bus.get_object(busname, path)
        except dbus.DBusException:
            continue
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        if str(props.Get(busname, "ActiveProfile")) != name:
            props.Set(busname, "ActiveProfile", dbus.String(name))
        return
    raise RuntimeError("power-profiles-daemon not reachable on D-Bus")
