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
* Every 5 minutes a watchdog checks that Firefox is fullscreen in kiosk mode on
  the right page (not another site, an error page, a login page or a popup),
  and closes and reopens it if not. It also checks that the settings window is
  running behind the page, and reopens or re-stacks it if not.
* Runs headless as a systemd user service. The service also keeps the settings
  window running: it starts it at boot behind the kiosk page, reopens it if it
  dies, and closing it only sends it back behind the page.
* Updates itself from this GitHub repository at a time and on days you pick,
  without taking the kiosk page off the screen.

## Install

Copy this folder to the kiosk machine (as the `kiosk` user), then:

```bash
cd ~/kiosk-manager && ./install.sh --install-deps
```

Options:

* `--install-deps` runs `apt-get install` for the required packages
  (`python3-gi`, `gir1.2-gtk-3.0`, `x11-xserver-utils`, `xdotool`,
  `x11-utils` for the watchdog's `xprop`, and `git` for automatic updates).
  Leave it off if they are already installed.
* `--url https://status.example.com/` sets the kiosk page explicitly. Without
  it, the installer uses the URL from the old `scripts/startup.sh`, or the
  default `https://columbiagadgetworks.org/kiosk/?kiosk`.
* `--no-migrate` leaves the existing kiosk-browser units and `startup.sh` alone.

By default the installer:

1. Disables and stops `kiosk-browser.timer` and `kiosk-browser.service`, and
   moves those unit files plus `~/scripts/startup.sh` into
   `~/kiosk-manager-backup-<timestamp>/`. Nothing is deleted.
2. Installs the code to `~/.local/lib/kiosk-manager` and a launcher at
   `~/.local/bin/kiosk-manager`.
3. Installs `~/.config/systemd/user/kiosk-manager.service` and enables it.
4. Installs a menu entry under Settings. There is no login autostart entry:
   the service starts the settings window itself (and removes the autostart
   entry version 1.0 created).

Install from a git clone so the installed build has a commit id to compare
against when checking for updates. A tarball install works too; its first
automatic update simply reinstalls the current branch head.

If the kiosk boots straight into a session with no interactive login, run once:

```bash
sudo loginctl enable-linger kiosk
```

## Daily use

The settings window has five tabs.

* **Website**: kiosk URL, Firefox executable and flags, kiosk / private window
  toggles, launch on boot, boot delay, whether to reopen the browser
  automatically if it gets closed or crashes, and the watchdog: on/off, how
  often it checks, the page title that marks the right page, and how long it
  leaves the screen alone after someone uses the desktop.
* **Schedule**: the list of wake / open events. Each event has a time, a set of
  weekdays, and switches for waking the screen, opening the page, and returning
  an already open page to the home URL.
* **Screen**: blank after N minutes, power the panel off after N minutes
  (0 means never for both), disable the lock screen, plus buttons to wake,
  blank or re-apply the settings right now.
* **System**: whether the service keeps this window running (starts it at
  boot and reopens it if it closes), and the
  automatic update switch, time, days, repository and branch, with
  **Check for updates** and **Update now** buttons.
* **Status**: what the daemon and browser are doing, the last watchdog result,
  the next scheduled event, and buttons to open, restart or close the browser.

The **Open kiosk page (fullscreen)** button at the bottom of every tab puts the
page back in front. If the browser is not fullscreen, is on another page or
shows a popup, it is restarted on the kiosk page first.

Press **Save settings** to write the config and apply it immediately. No
reboot or service restart is needed. Changing the URL restarts the browser on
the new page.

## The kiosk page

The default page is `https://columbiagadgetworks.org/kiosk/?kiosk`, a page on
the CGW website that nothing else on the site links to (source:
`layouts/kiosk.html` in the website repository). It shows the fundbot
figures, read through the website's `/api/fund/campaigns`. The `?kiosk`
parameter tells the page it is on the shop touchscreen: its links
(which would open pages in a tab the kiosk cannot leave) become plain labels
under larger QR codes, and it shows the Open GUI and Minimize buttons.
Everyone else visiting the sheet without the parameter still gets the links.
The sheet has two panels, the shop fund and a Columbia Gadget Works calendar,
swapped in place by a button without leaving the page, so the watchdog's
title check holds on both.

Configs that still hold a fundbot sheet URL (`https://fundbot.adman.casa/`,
with or without `?kiosk`) are moved to the website page automatically, and a
running browser is restarted onto it. To show the normal layout on the kiosk,
set the URL to `https://columbiagadgetworks.org/kiosk/`.

### The Open GUI and Minimize buttons

In kiosk mode that sheet shows an **Open GUI** button, a link to
`kioskmgr://show`, and a **Minimize** button in the top right corner, a link
to `kioskmgr://minimize`. They are the touchscreen equivalent of the hotkey: no
keyboard needed to reach the settings window. Minimize drops the kiosk page to
show the settings window that is always running behind it. The watchdog then
leaves the screen alone for `watchdog.operator_grace_minutes` after the last
touch in the settings window, and restores the page after that.

Two pieces make the links work, both set up by the installer:

* `~/.local/share/applications/kiosk-manager-url.desktop` registers this app as
  the handler for `kioskmgr://`, running `kiosk-manager handle-url %u`.
* `handlers.json` in the kiosk Firefox profile, rewritten at every launch, tells
  Firefox to hand that scheme to the system handler without asking first.

To test it by hand:

```bash
xdg-open kioskmgr://show
xdg-open kioskmgr://minimize
```

If that raises the window but the button in the page does nothing, Firefox is
not honouring the preloaded handler. Check that
`~/.local/share/kiosk-manager/firefox-profile/handlers.json` exists after a
launch, then restart the browser from the Status tab.

## Getting back to the window

Kiosk mode is fullscreen, so the taskbar is covered while the page is up. The
page's **Minimize** and **Open GUI** buttons are the easy way. Otherwise:

* Bind a hotkey to `kiosk-manager show`. Under GNOME:
  Settings > Keyboard > Keyboard Shortcuts > Custom Shortcuts, command
  `/home/kiosk/.local/bin/kiosk-manager show`, key `Ctrl+Alt+K`.
  Under XFCE: Settings > Keyboard > Application Shortcuts.
* Close the browser first (`kiosk-manager stop`), then use the taskbar.

`kiosk-manager show` raises the window if it is already running, and starts it
if it is not. The **Open kiosk page (fullscreen)** button at the bottom of the
window, or closing the window, brings the kiosk page back to the front.

## Command line

```bash
kiosk-manager gui            # settings window (opens behind the kiosk page)
kiosk-manager gui --show     # ...and bring it to the front
kiosk-manager show           # raise the settings window
kiosk-manager minimize       # minimise the kiosk page
kiosk-manager watchdog       # run the watchdog checks now
kiosk-manager status         # JSON status from the daemon
kiosk-manager open           # open the kiosk page if it is not open
kiosk-manager restart        # restart the browser
kiosk-manager stop           # close the browser
kiosk-manager wake           # wake the screen now
kiosk-manager blank          # blank the screen now
kiosk-manager set-url URL    # change the kiosk page
kiosk-manager config-path    # print the config file location
kiosk-manager version        # print the installed version and commit
kiosk-manager update-check   # ask GitHub whether a newer build exists
kiosk-manager update         # install it now (restarts the service only)
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
  "url": "https://columbiagadgetworks.org/kiosk/?kiosk",
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
  "gui": {
    "keep_running": true
  },
  "watchdog": {
    "enabled": true,
    "interval_minutes": 5,
    "expected_title": "Shop Fund",
    "operator_grace_minutes": 10
  },
  "update": {
    "enabled": false,
    "time": "03:00",
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "repo": "https://github.com/ColumbiaGadgetWorks/kiosk-manager.git",
    "branch": "main"
  }
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
* **Watchdog**: every `watchdog.interval_minutes` the service reads back what
  is on screen with `xdotool` and `xprop`. The browser is closed and reopened
  if it was not started with `--kiosk`, its window is not
  `_NET_WM_STATE_FULLSCREEN`, it has more than one window (a popup or dialog),
  or the window title does not contain `watchdog.expected_title` (Firefox error
  pages, login pages and other sites all have other titles). A minimised page
  is just restored. The settings window is reopened if it is gone,
  un-minimised if it is minimised, and put back under the page if it is on
  top. Checks pause after Minimize on the page or a touch in the settings
  window, and never reopen a browser that was closed on purpose from the GUI
  or with `kiosk-manager stop`.
* **Settings window keeper**: with `gui.keep_running` on, the service starts
  the window once the display is up, before the kiosk page, so the
  page ends up in front. It pings the window every few seconds and restarts it
  if it has gone, backing off from 30 seconds to 10 minutes if it keeps dying.
  After an update it restarts a window still running the old build.
* **Own scopes**: the browser and the settings window are started with
  `systemd-run --user --scope`, so restarting `kiosk-manager.service` (which
  an update does) leaves both on screen. A service restart more than 10
  minutes after boot also skips the screen wake and the boot delay.
* **Updates**: at the configured time the service runs `git ls-remote` against
  the repository. If the branch head differs from the installed commit
  (`~/.local/lib/kiosk-manager/VERSION`) it fetches the branch into
  `~/.local/share/kiosk-manager/src` and runs that copy's
  `install.sh --update` as a separate transient job. That copies the files,
  keeps your config, and restarts the service. The restarted service reports
  whether the new commit landed; output goes to
  `~/.local/share/kiosk-manager/update.log`.
  Anyone who can push to the branch can change what runs on the kiosk, so
  keep push access to the repository limited, or point `update.branch` at a
  branch you only merge tested changes into.

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
* Verified: Python syntax, the scheduler, the settings window keeper logic,
  the watchdog decisions against stubbed window state, the updater against a
  local git repository, and the CLI wiring. Not verified: the GTK window,
  systemd scopes, and the X11 calls (fullscreen and stacking read-back,
  minimise), which need the kiosk machine itself.
