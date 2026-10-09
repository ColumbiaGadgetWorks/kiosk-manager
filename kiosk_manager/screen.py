"""Screen blanking / DPMS / wake control.

X11 is the primary target (the kiosk service exports DISPLAY=:0), but the
gsettings and D-Bus paths are applied too so the same code behaves under a
GNOME/Wayland session.
"""

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("kiosk.screen")


def _has(cmd):
    return shutil.which(cmd) is not None


def _run(args, timeout=10):
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            log.debug("%s -> rc=%s %s", args[0], proc.returncode,
                      proc.stdout.decode("utf-8", "replace").strip())
        return proc.returncode == 0, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", args[0], exc)
        return False, ""


def session_type():
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() or "unknown"


def is_x11():
    # An X display is usable whenever DISPLAY is set and xset can talk to it.
    return bool(os.environ.get("DISPLAY")) and _has("xset")


def _gsettings(schema, key, value):
    if not _has("gsettings"):
        return False
    ok, _ = _run(["gsettings", "set", schema, key, value])
    return ok


def _timeouts(cfg):
    """(blank seconds, panel-off seconds), 0 meaning never."""
    screen = cfg.get("screen", {})
    blank_s = max(0, int(screen.get("blank_after_minutes", 0))) * 60
    dpms_s = max(0, int(screen.get("dpms_off_after_minutes", 0))) * 60
    return blank_s, dpms_s


def apply_settings(cfg):
    """Push the configured timeouts into the running session."""
    screen = cfg.get("screen", {})
    if not screen.get("manage_timeout", True):
        return "not managed"

    blank_s, dpms_s = _timeouts(cfg)
    disable_lock = bool(screen.get("disable_lock", True))
    # Desktops with their own idle timer (GNOME, Cinnamon) blank and power off
    # the panel at one delay and overrule xset, so give them the first of the
    # two that is set; 0 there also means never.
    idle_s = min([t for t in (blank_s, dpms_s) if t > 0] or [0])
    notes = []

    if is_x11():
        if blank_s <= 0:
            _run(["xset", "s", "off"])
            _run(["xset", "s", "noblank"])
        else:
            _run(["xset", "s", str(blank_s), str(blank_s)])
        if dpms_s <= 0:
            _run(["xset", "-dpms"])
        else:
            _run(["xset", "+dpms"])
            _run(["xset", "dpms", "0", "0", str(dpms_s)])
        notes.append("xset")

    # GNOME / Cinnamon / MATE style session settings. Missing schemas just fail.
    _gsettings("org.gnome.desktop.session", "idle-delay", "uint32 %d" % idle_s)
    _gsettings("org.gnome.desktop.screensaver", "idle-activation-enabled",
               "false" if idle_s <= 0 else "true")
    if disable_lock:
        _gsettings("org.gnome.desktop.screensaver", "lock-enabled", "false")
        _gsettings("org.gnome.desktop.lockdown", "disable-lock-screen", "true")
    _gsettings("org.gnome.settings-daemon.plugins.power",
               "sleep-inactive-ac-type", "'nothing'")
    _gsettings("org.gnome.settings-daemon.plugins.power",
               "sleep-inactive-ac-timeout", "0")
    _gsettings("org.cinnamon.desktop.session", "idle-delay", "uint32 %d" % idle_s)
    _gsettings("org.mate.screensaver", "idle-activation-enabled",
               "false" if blank_s <= 0 else "true")
    notes.append("gsettings")

    if _has("xfconf-query"):
        # The XFCE power manager re-applies its own DPMS times over xset, so
        # set them too (in minutes; 0 is never).
        def xfpm(prop, kind, value):
            _run(["xfconf-query", "-c", "xfce4-power-manager", "-p",
                  "/xfce4-power-manager/" + prop, "-n", "-t", kind, "-s", value])
        xfpm("dpms-enabled", "bool", "false" if dpms_s <= 0 else "true")
        xfpm("blank-on-ac", "int", str(blank_s // 60))
        xfpm("dpms-on-ac-sleep", "uint", str(dpms_s // 60))
        xfpm("dpms-on-ac-off", "uint", str(dpms_s // 60))
        notes.append("xfconf")

    return "+".join(notes) or "no backend"


def drift(cfg):
    """What the X server is doing that the config did not ask for.

    Desktop power managers and screensavers can quietly put their own
    timeouts back, which leaves the screen on all night (or blanking early).
    Returns a short description, or "" when everything matches.
    """
    screen = cfg.get("screen", {})
    if not screen.get("manage_timeout", True) or not is_x11():
        return ""
    ok, out = _run(["xset", "q"], timeout=5)
    if not ok:
        return ""
    blank_s, dpms_s = _timeouts(cfg)
    problems = []
    timeout = re.search(r"timeout:\s*(\d+)", out)
    if timeout and int(timeout.group(1)) != blank_s:
        problems.append("blank after %ss, want %ss" % (timeout.group(1), blank_s))
    enabled = "DPMS is Enabled" in out
    off = re.search(r"Off:\s*(\d+)", out)
    if "DPMS is " not in out:
        pass  # this display has no power management to check
    elif dpms_s <= 0 and enabled and off and int(off.group(1)) > 0:
        problems.append("panel powers off after %ss, want never" % off.group(1))
    elif dpms_s > 0 and not enabled:
        problems.append("DPMS disabled, want off after %ss" % dpms_s)
    elif dpms_s > 0 and off and int(off.group(1)) != dpms_s:
        problems.append("panel off after %ss, want %ss" % (off.group(1), dpms_s))
    for saver in ("xscreensaver", "light-locker"):
        if _has("pgrep") and _run(["pgrep", "-x", saver])[0]:
            problems.append("%s is running and may override these times" % saver)
    return "; ".join(problems)


def wake():
    """Bring the panel back on and reset the idle timer."""
    done = []
    if is_x11():
        _run(["xset", "dpms", "force", "on"])
        _run(["xset", "s", "reset"])
        done.append("xset")
        if _has("xdotool"):
            # A no-op modifier tap counts as user activity for most idle timers.
            _run(["xdotool", "key", "--clearmodifiers", "shift"])
            done.append("xdotool")
    if _has("gdbus"):
        _run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
              "--object-path", "/org/gnome/ScreenSaver",
              "--method", "org.gnome.ScreenSaver.SetActive", "false"])
        done.append("gdbus")
    if _has("xdg-screensaver"):
        _run(["xdg-screensaver", "reset"])
        done.append("xdg-screensaver")
    log.info("wake via %s", ",".join(done) or "nothing available")
    return done


def blank_now():
    """Force the panel off immediately (useful for testing from the GUI)."""
    if is_x11():
        _run(["xset", "dpms", "force", "off"])
        return True
    if _has("gdbus"):
        ok, _ = _run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
                      "--object-path", "/org/gnome/ScreenSaver",
                      "--method", "org.gnome.ScreenSaver.SetActive", "true"])
        return ok
    return False


def probe_display():
    """True when an X server is answering on the current DISPLAY."""
    if not _has("xset"):
        return False
    ok, _ = _run(["xset", "q"], timeout=5)
    return ok


def current_state():
    """Best-effort read-back of what the session is actually doing."""
    info = {"session_type": session_type(), "display": os.environ.get("DISPLAY", "")}
    if is_x11():
        ok, out = _run(["xset", "q"])
        if ok:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("timeout:"):
                    info["x_screensaver"] = line
                elif line.startswith("DPMS is"):
                    info["dpms"] = line
                elif "Monitor is" in line:
                    info["monitor"] = line.strip()
    return info
