# swayprofile

[![AI:X EXECUTE](https://img.shields.io/badge/AI-EXECUTE-grey?style=flat&logoSize=auto&labelColor=13171f)](https://0rwx.com)

Switch between power/performance profiles in [sway](https://swaywm.org/), manually,
automatically while a game window is open, or when you are idle.

- Documentation (and FAQ – if any) can be found in this README.
- Feel free to file issues to report bugs, ask questions, or request features.
- Feel free to open a pull request.

A profile can set:

- **monitor refresh rate**, resolution, adaptive sync, or turn outputs off (via sway IPC)
- the **power-profiles-daemon** profile (`power-saver` / `balanced` / `performance`)
- the **GPU** settings (power limit, performance level, power profile mode) via [LACT](https://github.com/ilya-zlobintsev/LACT)
- **do-not-disturb** in swaync, dunst or mako (the previous state is restored when leaving)
- `inhibit_idle` on game windows, and arbitrary `on_enter` / `on_exit` commands

Which profile is active is decided by: **manual pin > window rule > idle > default**.

- **Window rules** match a window's app_id or X11 class with a regex; the default rule sends
  Steam games and gamescope to `gaming`. After the last matching window closes, the profile
  is kept for `exit_delay` seconds; a matching window reappearing in that time cancels the switch.
- **Idle** runs its own `swayidle` instance and switches to `eco` after `timeout` seconds. Your
  own swayidle/lock setup is untouched. Anything that inhibits idle (fullscreen windows,
  `inhibit_idle` rules) also prevents idle-eco.
- **Manual pin** (`swayprofile set`, or clicking the waybar module) overrides both until
  `swayprofile auto`.

Every sway session starts in the default profile; after `swaymsg reload` the current profile is
re-applied.

## Requirements

- sway, Python ≥ 3.12, [i3ipc](https://pypi.org/project/i3ipc/)
- optional, per feature:
  - power profiles: power-profiles-daemon and dbus-python
  - idle switching: swayidle
  - do-not-disturb: swaync, dunst or mako
  - GPU control: LACT (≥ 0.6), with access to its socket (usually the `wheel` group)
  - notifications: `notify-send` (libnotify)

A missing optional piece only disables that feature (with a message in the log).

## Install

### Arch Linux (AUR)

```
git clone https://aur.archlinux.org/swayprofile.git && cd swayprofile && makepkg -si
```

(or with your AUR helper). The package installs the systemd user unit, so skip
`install-service` below.

### From source

Install the dependencies from your distribution, then install swayprofile with pipx so it
can use them:

| | Arch | Debian/Ubuntu | Fedora |
|---|---|---|---|
| i3ipc | `python-i3ipc` | `python3-i3ipc` | `python3-i3ipc` |
| dbus-python | `python-dbus` | `python3-dbus` | `python3-dbus` |

```
pipx install --system-site-packages git+https://github.com/Syphdias/swayprofile
swayprofile install-service
```

`--system-site-packages` lets swayprofile use the distribution's dbus-python, which otherwise
has to be compiled. pip still installs its own copy of i3ipc into the venv; that works, but to
use the distribution's copy instead: `pipx runpip swayprofile uninstall -y i3ipc python-xlib`.

`install-service` writes `~/.config/systemd/user/swayprofile.service` pointing at the installed
executable. It does not enable it, and does not overwrite or shadow an existing unit without
`--force`.

## Setup

```
swayprofile init-config          # writes ~/.config/swayprofile/profiles.toml
$EDITOR ~/.config/swayprofile/profiles.toml
swayprofile plan gaming          # what switching to `gaming` would change right now
systemctl --user enable --now swayprofile
```

`init-config` writes a commented example config in which only the power-profiles-daemon switch
is active. Your connected outputs (with suggested refresh rates) and your GPU are filled in as
commented-out suggestions; uncomment what you want. The file documents every key.

The service only starts in a sway session: it needs `SWAYSOCK` in the systemd user
environment, which sway's stock `/etc/sway/config.d/50-systemd-user.conf` imports. If your
sway config does not include that file, add
`exec systemctl --user import-environment SWAYSOCK WAYLAND_DISPLAY` to it.

### Outputs

Output settings a profile does not override come from `outputs_file` (default
`~/.config/sway/outputs`, the file [nwg-displays](https://github.com/nwg-piotr/nwg-displays)
writes). So `normal` with no `outputs` means "whatever your outputs file says". Without that
file, outputs a profile does not mention are left alone. Outputs are named by connector
(`DP-1`) or by `"Make Model Serial"`; `swaymsg -t get_outputs` lists both. A `refresh` is
matched to the closest mode the output supports.

### GPU (LACT)

GPU control is off until you enable it, since it changes power limits. Each profile's `lact`
table holds LACT `GpuConfig` fields, overlaid on LACT's current default config for the GPU
(fan curves etc. are kept). swayprofile switches between LACT profiles named `swayprofile-<profile>`:

1. add `lact = { ... }` to the profiles in your config
2. `swayprofile lact-sync --dry-run`: shows the LACT profiles it would create, and what differs
   from the default config
3. `swayprofile lact-sync`: creates them (`--force` replaces outdated ones)
4. set `[lact] enabled = true`, then `swayprofile reload`

After every switch swayprofile reads the GPU state back from sysfs and reports a mismatch.
LACT's own automatic profile switching should stay off. With several GPUs, set `[lact] gpu`;
`init-config` lists the ID.

### Do-not-disturb

`dnd_backend = "auto"` uses whichever of swaync, dunst and mako is running; set it to one of
those names or to `"none"`. For mako, define the mode in its config:

```
[mode=do-not-disturb]
invisible=1
```

swaync is what the author uses; dunst and mako support is community-tested.

## Usage

```
swayprofile set eco|normal|gaming   # pin a profile (automatic switching ignored)
swayprofile auto                    # release the pin
swayprofile cycle                   # pin the next profile in `cycle`
swayprofile status [--json]
swayprofile reload                  # re-read the config and re-apply
swayprofile plan <profile>          # dry run: what applying <profile> would change now
swayprofile lact-sync [--dry-run] [--force]
swayprofile daemon [--dry-run]      # normally run by systemd
```

## Waybar

```jsonc
"custom/swayprofile": {
    "return-type": "json",
    "exec": "~/.local/bin/swayprofile waybar",
    "format": "{}",
    // *-release: switching on press can change the refresh rate mid-click and leave waybar with a stuck click
    "on-click-release": "~/.local/bin/swayprofile cycle",
    "on-click-middle-release": "~/.local/bin/swayprofile auto",
    "escape": false
},
```

Use the full path: waybar's `PATH` usually does not include `~/.local/bin` (with the AUR package
it is `/usr/bin/swayprofile`). The module shows the profile's `icon` while selected automatically
and `icon_pinned` while pinned (falling back to `icon`, then to the profile name), has the
details in its tooltip, and
gets the CSS classes `<profile>` plus `pinned` or `auto` (`offline` while the daemon is not
running):

```css
#custom-swayprofile.gaming { color: #f38ba8; }
#custom-swayprofile.pinned { font-weight: bold; }
```

## Troubleshooting

- `journalctl --user -u swayprofile`: every switch, its reason, and which process sent each
  command
- `swayprofile status`: current profile, reason, matched windows and the last errors
- `swayprofile plan <profile>`: what a switch would do
- `SWAYPROFILE_SOCKET=$XDG_RUNTIME_DIR/swayprofile-test.sock swayprofile daemon --dry-run`:
  a daemon that only logs what it would do, next to the real one
- service does not start: check `systemctl --user show-environment | grep SWAYSOCK`

## Development

```
git clone https://github.com/Syphdias/swayprofile && cd swayprofile
pipx install --editable --system-site-packages .
uvx ruff check . && uvx ruff format --check .
uvx pytest                          # no sway, LACT or dependencies needed
```

The version comes from git tags. Pushing a `v*` tag runs the tests and publishes a GitHub
Release with the wheel and sdist (the AUR package installs that wheel).

## License

MIT
