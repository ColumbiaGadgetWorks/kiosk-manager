"""Choose between the Wayland and X11 desktop session for the next login.

Window control (finding, raising and stacking the kiosk page) relies on X11
tools, which GNOME on Wayland only partly honours. GDM starts whichever
session AccountsService has saved for the user, and the user may change their
own saved session without a password, so switching needs no root and takes
effect at the next login (a reboot, on an auto-login kiosk).
"""

import logging
import os
import pwd
import re
import shutil
import subprocess

from . import screen

log = logging.getLogger("kiosk.session")

ACCOUNTS = "org.freedesktop.Accounts"
XSESSIONS = "/usr/share/xsessions"
WAYLAND_SESSIONS = "/usr/share/wayland-sessions"
GDM_CONFIGS = ("/etc/gdm3/daemon.conf", "/etc/gdm3/custom.conf",
               "/etc/gdm/custom.conf")


def _run(args, timeout=10):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return proc.returncode == 0, proc.stdout.decode("utf-8", "replace").strip()


def current():
    """"wayland" or "x11": what this session is running now."""
    return "wayland" if screen.is_wayland() else "x11"


def _user_path():
    if not shutil.which("gdbus"):
        return None
    name = pwd.getpwuid(os.getuid()).pw_name
    ok, out = _run(["gdbus", "call", "--system", "--dest", ACCOUNTS,
                    "--object-path", "/org/freedesktop/Accounts",
                    "--method", "org.freedesktop.Accounts.FindUserByName", name])
    found = re.search(r"'(/org/freedesktop/Accounts/[^']+)'", out) if ok else None
    return found.group(1) if found else None


def _get(path, prop):
    ok, out = _run(["gdbus", "call", "--system", "--dest", ACCOUNTS,
                    "--object-path", path, "--method",
                    "org.freedesktop.DBus.Properties.Get",
                    "org.freedesktop.Accounts.User", prop])
    found = re.search(r"<'([^']*)'>", out) if ok else None
    return found.group(1) if found else ""


def _call(path, method, value):
    return _run(["gdbus", "call", "--system", "--dest", ACCOUNTS,
                 "--object-path", path, "--method",
                 "org.freedesktop.Accounts.User." + method, value])


def wayland_disabled():
    """True when GDM is configured never to start a Wayland session."""
    for path in GDM_CONFIGS:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if re.search(r"^\s*WaylandEnable\s*=\s*false\s*$", text,
                     re.IGNORECASE | re.MULTILINE):
            return True
    return False


def _base():
    """Desktop session family, e.g. "gnome" or "ubuntu"."""
    desktop = (os.environ.get("XDG_SESSION_DESKTOP")
               or os.environ.get("DESKTOP_SESSION") or "gnome").lower()
    for suffix in ("-xorg", "-wayland"):
        if desktop.endswith(suffix):
            desktop = desktop[:-len(suffix)]
    return desktop


def session_names():
    """(x11 session name or None, wayland session name or None)."""
    base = _base()
    x11 = None
    for name in (base + "-xorg", base):
        if os.path.exists(os.path.join(XSESSIONS, name + ".desktop")):
            x11 = name
            break
    wayland = None
    for name in (base, base + "-wayland"):
        if os.path.exists(os.path.join(WAYLAND_SESSIONS, name + ".desktop")):
            wayland = name
            break
    return x11, wayland


def xorg_installed():
    return bool(shutil.which("Xorg")) or os.path.exists("/usr/lib/xorg/Xorg")


def info():
    """What is running now, what starts next, and whether switching can work."""
    x11_name, wayland_name = session_names()
    path = _user_path()
    saved = (_get(path, "Session") or _get(path, "XSession")) if path else ""
    if wayland_disabled():
        nxt = "x11"
    elif saved and saved == x11_name:
        nxt = "x11"
    elif saved and saved == wayland_name:
        nxt = "wayland"
    elif saved:
        nxt = "x11" if os.path.exists(
            os.path.join(XSESSIONS, saved + ".desktop")) else "wayland"
    else:
        nxt = "wayland"  # GDM's default for GNOME when Wayland is allowed
    problems = []
    if not path:
        problems.append("AccountsService is not reachable over D-Bus")
    if not x11_name:
        problems.append("no X11 session is installed (%s)" % XSESSIONS)
    elif not xorg_installed():
        problems.append("Xorg is not installed (sudo apt-get install xserver-xorg)")
    return {
        "ok": True,
        "current": current(),
        "next": nxt,
        "saved_session": saved,
        "x11_session": x11_name,
        "wayland_session": wayland_name,
        "wayland_disabled": wayland_disabled(),
        "problems": problems,
    }


def set_next(target):
    """Make `target` ("x11" or "wayland") the session for the next login.

    Returns (ok, message).
    """
    if target not in ("x11", "wayland"):
        return False, "unknown session type %r" % target
    x11_name, wayland_name = session_names()
    name = x11_name if target == "x11" else wayland_name
    if not name:
        return False, "no %s session is installed" % target
    if target == "x11" and not xorg_installed():
        return False, "Xorg is not installed (sudo apt-get install xserver-xorg)"
    if target == "wayland" and wayland_disabled():
        return False, ("GDM has WaylandEnable=false in its config; remove it "
                       "(needs sudo) before Wayland can start")
    path = _user_path()
    if not path:
        return False, "AccountsService is not reachable over D-Bus"
    ok, out = _call(path, "SetSession", name)
    # Older GDM reads XSession; newer AccountsService also keeps a type.
    xok, xout = _call(path, "SetXSession", name)
    _call(path, "SetSessionType", target)
    if not ok and not xok:
        log.warning("set session %s failed: %s / %s", name, out, xout)
        return False, "could not save the session: %s" % (out or xout)
    log.info("next login session set to %s (%s)", name, target)
    return True, ("next login uses %s (%s); restart the computer to switch"
                  % ("X11" if target == "x11" else "Wayland", name))


def reboot():
    ok, out = _run(["systemctl", "reboot"], timeout=15)
    return ok, "restarting" if ok else "could not restart: %s" % out
