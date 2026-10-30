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


def is_wayland():
    """True on a Wayland session. The service may not inherit
    XDG_SESSION_TYPE, so fall back to the compositor's socket."""
    if session_type() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.exists(os.path.join(runtime, "wayland-0"))


def _mutter_power(mode):
    """GNOME/Mutter panel power: 0 = on, 1 = off. Works on Wayland and X11."""
    if not _has("gdbus"):
        return False
    ok, _ = _run(["gdbus", "call", "--session",
                  "--dest", "org.gnome.Mutter.DisplayConfig",
                  "--object-path", "/org/gnome/Mutter/DisplayConfig",
                  "--method", "org.freedesktop.DBus.Properties.Set",
                  "org.gnome.Mutter.DisplayConfig", "PowerSaveMode",
                  "<int32 %d>" % mode])
    return ok


def _screensaver(active):
    if not _has("gdbus"):
        return False
    ok, _ = _run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
                  "--object-path", "/org/gnome/ScreenSaver",
                  "--method", "org.gnome.ScreenSaver.SetActive",
                  "true" if active else "false"])
    return ok


def is_x11():
    # An X display is usable whenever DISPLAY is set and xset can talk to it.
    # Under Wayland that display is XWayland, not the real screen: use
    # x11_session() to decide whether xset timeouts mean anything.
    return bool(os.environ.get("DISPLAY")) and _has("xset")


def x11_session():
    """True when the X server is the real display, so xset controls the panel."""
    return is_x11() and not is_wayland()


def _gsettings_get(schema, key):
    """A gsettings value as a string, or None when the schema is not there."""
    if not _has("gsettings"):
        return None
    ok, out = _run(["gsettings", "get", schema, key], timeout=5)
    return out.strip() if ok else None


# A GVariant string as gdbus prints it: 'single' quotes, or "double" when the
# text itself contains a single quote.
_GV_STR = r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""


def _gv(text):
    return text[1:-1] if text else ""


def idle_inhibitors():
    """Programs currently asking the desktop not to blank, as "app (reason)".

    GNOME's session manager and logind both keep a list. A video, a web page
    holding a wake lock, or a stray `xset s off` under XWayland all show up
    here, and any one of them keeps the screen on indefinitely.
    """
    found = []
    if _has("gdbus"):
        ok, out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.SessionManager",
                        "--object-path", "/org/gnome/SessionManager",
                        "--method", "org.gnome.SessionManager.GetInhibitors"], timeout=5)
        for path in re.findall(r"'(/org/gnome/SessionManager/Inhibitor\d+)'", out if ok else ""):
            def ask(method):
                _, reply = _run(["gdbus", "call", "--session",
                                 "--dest", "org.gnome.SessionManager",
                                 "--object-path", path,
                                 "--method", "org.gnome.SessionManager.Inhibitor." + method],
                                timeout=5)
                return reply
            flags = re.search(r"uint32 (\d+)", ask("GetFlags"))
            if not flags or not int(flags.group(1)) & 8:  # 8 = inhibit idle
                continue
            app = re.search(_GV_STR, ask("GetAppId"))
            reason = re.search(_GV_STR, ask("GetReason"))
            found.append("%s (%s)" % (_gv(app.group(1)) if app else "?",
                                      (_gv(reason.group(1)) if reason else "") or "no reason given"))
    if _has("gdbus"):
        # logind's own list; one tuple per lock, fields kept separate.
        ok, out = _run(["gdbus", "call", "--system", "--dest", "org.freedesktop.login1",
                        "--object-path", "/org/freedesktop/login1",
                        "--method", "org.freedesktop.login1.Manager.ListInhibitors"], timeout=5)
        tuple_re = r"\(\s*" + r",\s*".join([_GV_STR] * 4)
        for what, who, why, mode in re.findall(tuple_re, out if ok else ""):
            what, who, why, mode = map(_gv, (what, who, why, mode))
            if "idle" in what.split(":") and mode == "block":
                found.append("%s (%s)" % (who, why or "no reason given"))
    return found


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

    if x11_session():
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
    elif is_x11():
        # Wayland: DISPLAY is XWayland, so xset cannot reach the panel, and
        # `xset s off` there can turn into an idle inhibitor that stops GNOME
        # from ever blanking. Put XWayland's screensaver back to its default
        # (which also clears one an older build left behind) and leave the
        # timeouts to the compositor's settings below.
        _run(["xset", "s", "default"])
        notes.append("xwayland-reset")

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
    if not screen.get("manage_timeout", True):
        return ""
    blank_s, dpms_s = _timeouts(cfg)
    problems = []

    # GNOME (X11 or Wayland) blanks on its own timer; read it back directly.
    idle_s = min([t for t in (blank_s, dpms_s) if t > 0] or [0])
    current = _gsettings_get("org.gnome.desktop.session", "idle-delay")
    if current is not None:
        found = re.search(r"(\d+)\s*$", current)  # "uint32 600" -> 600
        if found and int(found.group(1)) != idle_s:
            problems.append("GNOME blank delay %ss, want %ss" % (found.group(1), idle_s))

    if not x11_session():
        return "; ".join(problems)
    ok, out = _run(["xset", "q"], timeout=5)
    if not ok:
        return "; ".join(problems)
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
    if is_wayland():
        # xset only reaches XWayland there, not the real panel.
        if _mutter_power(0):
            done.append("mutter")
        if _screensaver(False):
            done.append("gnome-screensaver")
        log.info("wake via %s", ",".join(done) or "nothing available")
        return done
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
    if is_wayland():
        # Power the panel off through Mutter; if that is refused, fall back to
        # the shell's black screen.
        return _mutter_power(1) or _screensaver(True)
    if is_x11():
        # "force off" is ignored while DPMS is disabled, which is the default
        # (never power off), so switch it on first. The timeouts stay as set.
        _run(["xset", "+dpms"])
        ok, _ = _run(["xset", "dpms", "force", "off"])
        return ok
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
    info = {"session_type": "wayland" if is_wayland() else session_type(),
            "display": os.environ.get("DISPLAY", "")}
    delay = _gsettings_get("org.gnome.desktop.session", "idle-delay")
    if delay is not None:
        info["gnome_idle_delay"] = delay
    inhibitors = idle_inhibitors()
    if inhibitors:
        info["kept_awake_by"] = inhibitors
    if x11_session():
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
