# kiosk-manager

A small background service plus GTK settings window for a Debian touchscreen
kiosk. It replaces the hand written `scripts/startup.sh` +
`kiosk-browser.service` + `kiosk-browser.timer` arrangement with one program
that owns the browser, the schedule and the screen policy.

What it does:

* Launches Firefox in kiosk mode at a configurable URL when the machine boots.
* At configurable times and days of the week it wakes the screen and opens the
  kiosk page **only if it is not already open** (if it is open, the window is
  raised and optionally sent back to the home URL).
* Sets the session screen blanking and panel power off timeouts, and can
  disable the lock screen and screensaver.
* Runs headless as a systemd user service. The settings window lives in the
  taskbar, starts minimised, and closing it just minimises it again.

## Install

Copy this folder to the kiosk machine (as the `kiosk` user), then:

```bash
cd ~/kiosk-manager && ./install.sh --install-deps
```

Options:

* `--install-deps` runs `apt-get install` for the required packages
  (`python3-gi`, `gir1.2-gtk-3.0`, `x11-xserver-utils`, `xdotool`, `wmctrl`).
  Leave it off if they are already installed.
* `--url https://status.example.com/` sets the kiosk page explicitly. Without
  it, the installer reads the URL out of the old `scripts/startup.sh`.
* `--no-migrate` leaves the existing kiosk-browser units and `startup.sh` alone.

By default the installer:

1. Disables and stops `kiosk-browser.timer` and `kiosk-browser.service`, and
   moves those unit files plus `~/scripts/startup.sh` into
   `~/kiosk-manager-backup-<timestamp>/`. Nothing is deleted.
2. Installs the code to `~/.local/lib/kiosk-manager` and a launcher at
   `~/.local/bin/kiosk-manager`.
3. Installs `~/.config/systemd/user/kiosk-manager.service` and enables it.
4. Installs an autostart entry so the settings window is in the taskbar after
   login, and a menu entry under Settings.

If the kiosk boots straight into a session with no interactive login, run once:

```bash
sudo loginctl enable-linger kiosk
```

## Daily use

The settings window has four tabs.

* **Website**: kiosk URL, Firefox executable and flags, kiosk / private window
  toggles, launch on boot, boot delay, and whether to reopen the browser
  automatically if it gets closed or crashes.
* **Schedule**: the list of wake / open events. Each event has a time, a set of
  weekdays, and switches for waking the screen, opening the page, and returning
  an already open page to the home URL.
* **Screen**: blank after N minutes, power the panel off after N minutes
  (0 means never for both), disable the lock screen, plus buttons to wake,
  blank or re-apply the settings right now.
* **Status**: what the daemon and browser are doing, the next scheduled event,
  and buttons to open, restart or close the browser.

Press **Save settings** to write the config and apply it immediately. No
reboot or service restart is needed.

## Getting back to the window

Kiosk mode is fullscreen, so the taskbar is covered while the page is up. Two
ways back:

* Bind a hotkey to `kiosk-manager show`. Under GNOME:
  Settings > Keyboard > Keyboard Shortcuts > Custom Shortcuts, command
  `/home/kiosk/.local/bin/kiosk-manager show`, key `Ctrl+Alt+K`.
  Under XFCE: Settings > Keyboard > Application Shortcuts.
* Close the browser first (`kiosk-manager stop`), then use the taskbar.

`kiosk-manager show` raises the window if it is already running, and starts it
if it is not.

## Command line

```bash
kiosk-manager gui            # settings window (minimised by default)
kiosk-manager gui --no-minimize
kiosk-manager show           # raise the settings window
kiosk-manager status         # JSON status from the daemon
kiosk-manager open           # open the kiosk page if it is not open
kiosk-manager restart        # restart the browser
kiosk-manager stop           # close the browser
kiosk-manager wake           # wake the screen now
kiosk-manager blank          # blank the screen now
kiosk-manager set-url URL    # change the kiosk page
kiosk-manager config-path    # print the config file location
```

Service control and logs:

```bash
systemctl --user status kiosk-manager.service
systemctl --user restart kiosk-manager.service
journalctl --user -u kiosk-manager.service -f
```

## Configuration file

`~/.config/kiosk-manager/config.json`. The GUI writes it, but it is plain JSON
and can be edited by hand; run `kiosk-manager reload` afterwards.

```json
{
  "url": "https://status.example.com/",
  "browser": {
    "command": "/usr/bin/firefox",
    "kiosk": true,
    "private_window": false,
    "extra_args": [],
    "use_managed_profile": true
  },
  "boot": {
    "launch_on_boot": true,
    "delay_seconds": 15,
    "restart_if_closed": true,
    "restart_grace_seconds": 20
  },
  "schedule": {
    "enabled": true,
    "entries": [
      {
        "id": "a1b2c3d4",
        "enabled": true,
        "time": "07:30",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "wake_screen": true,
        "open_browser": true,
        "return_to_home": true
      }
    ]
  },
  "screen": {
    "manage_timeout": true,
    "blank_after_minutes": 0,
    "dpms_off_after_minutes": 0,
    "disable_lock": true
  },
  "gui": { "start_minimized": true }
}
```

## How the pieces work

* **Boot launch**: the daemon waits for the X display to answer, applies the
  screen settings, waits `boot.delay_seconds`, wakes the screen and opens the
  page. The old timer with `OnBootSec=60` is no longer needed.
* **"Only if not already open"**: the browser is started with a unique marker
  on its command line and its own Firefox profile, so the daemon can always
  tell whether the kiosk instance is running, even after the daemon restarts.
  A scheduled event with `return_to_home` sends Alt+Home to the kiosk window,
  which goes back to the configured URL because that URL is also written into
  the profile as the home page.
* **Screen control**: on X11 it drives `xset s` and `xset dpms`. It also writes
  the GNOME/Cinnamon/MATE gsettings keys and the XFCE power manager settings,
  so the same config works if the desktop is one of those. Waking uses
  `xset dpms force on` plus the GNOME ScreenSaver D-Bus call.
* **Auto reopen**: if the browser exits while the daemon believes it should be
  running, it is relaunched after `restart_grace_seconds`. Closing it from the
  GUI or with `kiosk-manager stop` suppresses that until the next explicit
  open, so the desktop stays reachable for maintenance.

## Uninstall

```bash
./uninstall.sh           # keeps the config and Firefox profile
./uninstall.sh --purge   # removes them too
```

The old `startup.sh` and `kiosk-browser.*` units are still in the backup
folder the installer created if you want to go back.

## Notes and limitations

* Screen control assumes an X11 session (`DISPLAY=:0`), which is what the
  existing unit used. Under a Wayland session the gsettings path still applies
  timeouts and `gdbus` still wakes the screen, but `xdotool` window raising and
  Alt+Home will not work.
* The dedicated Firefox profile lives in
  `~/.local/share/kiosk-manager/firefox-profile`. Its `user.js` is rewritten on
  every launch, so put lasting tweaks in the GUI or the config file rather than
  there. Turn off "Use the dedicated kiosk Firefox profile" to use the default
  profile instead.
* Verified: Python syntax and the CLI wiring. Not verified: the GTK window and
  the X11 calls, which need the kiosk machine itself.
