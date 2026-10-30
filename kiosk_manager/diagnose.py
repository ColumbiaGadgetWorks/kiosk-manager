"""One report that answers "why did nothing happen on screen?".

Window trouble on this kiosk is almost always environmental rather than a bug
in the logic: the session is Wayland instead of X11, a tool is missing, the
settings window is a native Wayland client that xdotool cannot see, or Firefox
is on XWayland while the compositor refuses the activation. None of that is
visible from a stack trace, so collect it all in one place instead.
"""

import datetime
import json
import os
import platform
import shutil
import subprocess

from . import config, ipc, logs, screen, version, x11

# Tools the window and screen layers reach for.
TOOLS = ("xdotool", "wmctrl", "xprop", "xset", "gdbus", "busctl", "gsettings",
         "xdg-mime", "xdg-open", "systemd-run", "systemctl", "firefox",
         "firefox-esr", "git")

# Environment that decides which display stack a process talks to.
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE",
                "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "GDK_BACKEND",
                "MOZ_ENABLE_WAYLAND", "QT_QPA_PLATFORM", "XAUTHORITY",
                "XDG_RUNTIME_DIR")


def _run(args, timeout=10):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout,
                              check=False)
        return proc.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return "(%s)" % exc


def _proc_env(pid, keys=DISPLAY_VARS):
    """Read the display-related environment of another process of ours."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return {"error": str(exc)}
    found = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        key = key.decode("utf-8", "replace")
        if key in keys:
            found[key] = value.decode("utf-8", "replace")
    return found


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return " ".join(a.decode("utf-8", "replace")
                            for a in fh.read().split(b"\0") if a)
    except OSError as exc:
        return "(%s)" % exc


def environment_summary():
    """One line for the daemon log at startup."""
    return ("session=%s wayland=%s x11=%s display=%r waylanddisplay=%r "
            "desktop=%r xdotool=%s xprop=%s wmctrl=%s gdbus=%s x11_layer=%s"
            % (screen.session_type(), screen.is_wayland(), screen.is_x11(),
               os.environ.get("DISPLAY", ""),
               os.environ.get("WAYLAND_DISPLAY", ""),
               os.environ.get("XDG_CURRENT_DESKTOP", ""),
               bool(shutil.which("xdotool")), bool(shutil.which("xprop")),
               bool(shutil.which("wmctrl")), bool(shutil.which("gdbus")),
               x11.available()))


def _section(title):
    return "\n== %s %s\n" % (title, "=" * max(0, 68 - len(title)))


def report(log_lines=120):
    """The full plain-text report."""
    out = []

    out.append("kiosk-manager diagnostics")
    out.append("generated %s" % datetime.datetime.now().isoformat(timespec="seconds"))

    out.append(_section("Versions"))
    out.append("kiosk-manager: %s" % version.describe(version.installed_commit()))
    out.append("python:        %s" % platform.python_version())
    out.append("kernel:        %s" % platform.release())
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    out.append("os:            %s"
                               % line.split("=", 1)[1].strip().strip('"'))
    except OSError:
        pass
    out.append("gnome-shell:   %s" % (_run(["gnome-shell", "--version"])
                                      if shutil.which("gnome-shell") else "(absent)"))

    out.append(_section("Session"))
    for var in DISPLAY_VARS:
        out.append("%-20s %s" % (var + ":", os.environ.get(var, "(unset)")))
    out.append("%-20s %s" % ("screen.is_wayland:", screen.is_wayland()))
    out.append("%-20s %s" % ("screen.is_x11:", screen.is_x11()))
    out.append("%-20s %s" % ("x11 layer usable:", x11.available()))
    out.append("%-20s %s" % ("X server answers:", screen.probe_display()))
    if screen.is_wayland():
        out.append("")
        out.append("NOTE: this is a Wayland session. The compositor, not this")
        out.append("program, decides which window is on top. xdotool can only")
        out.append("see and raise XWayland clients, and GNOME refuses most")
        out.append("activation requests from other processes. A GTK settings")
        out.append("window started under Wayland is invisible to xdotool.")

    out.append(_section("Tools"))
    for tool in TOOLS:
        out.append("%-16s %s" % (tool + ":", shutil.which(tool) or "(absent)"))

    out.append(_section("Windows the X layer can see"))
    if x11.available():
        inventory = x11.inventory()
        if not inventory:
            out.append("(no managed windows found; under Wayland this is normal")
            out.append(" for native clients and means stacking cannot be read)")
        for win in inventory:
            out.append("%s  pid=%-7s %-22s %s"
                       % (win["hex"], win["pid"], win["class"], win["title"][:48]))
            out.append("    state=%s type=%s"
                       % (",".join(win["state"]) or "-",
                          ",".join(win["type"]) or "-"))
        top = x11.top_app_window()
        out.append("")
        out.append("top application window: %s %r"
                   % (hex(top) if top else "(none)",
                      x11.title(top) if top else ""))
    else:
        out.append("(skipped: DISPLAY, xdotool or xprop missing)")

    out.append(_section("Processes"))
    try:
        from . import browser as browser_mod
        bm = browser_mod.BrowserManager(config.load())
        pids = bm._find_pids()
        out.append("kiosk browser pids: %s" % (pids or "(not running)"))
        for pid in pids[:4]:
            out.append("  pid %d: %s" % (pid, _proc_cmdline(pid)[:160]))
            out.append("    env: %s" % json.dumps(_proc_env(pid)))
        out.append("kiosk browser windows: %s" % bm.windows())
    except Exception as exc:
        out.append("(browser inspection failed: %s)" % exc)
    gui_pid = _run(["pgrep", "-f", "kiosk_manager gui"]).split()
    out.append("settings window pids: %s" % (gui_pid or "(none found by pgrep)"))
    for pid in gui_pid[:2]:
        if pid.isdigit():
            out.append("    env: %s" % json.dumps(_proc_env(int(pid))))

    out.append(_section("Services and sockets"))
    out.append("service active: %s"
               % _run(["systemctl", "--user", "is-active", "kiosk-manager.service"]))
    out.append("scopes: %s" % (_run(["systemctl", "--user", "list-units",
                                     "--type=scope", "--no-legend",
                                     "kiosk-manager-*"]) or "(none)"))
    for name, path in (("daemon", config.daemon_socket()),
                       ("gui", config.gui_socket())):
        reply = ipc.send(path, {"command": "ping"}, timeout=2.0)
        out.append("%s socket %s -> %s" % (name, path, json.dumps(reply)))

    out.append(_section("Daemon status"))
    status = ipc.send(config.daemon_socket(), {"command": "status"}, timeout=5.0)
    out.append(json.dumps(status, indent=2, default=str))

    out.append(_section("Screen state"))
    out.append(json.dumps(screen.current_state(), indent=2, default=str))
    try:
        out.append("drift from config: %s" % (screen.drift(config.load()) or "none"))
    except Exception as exc:
        out.append("drift check failed: %s" % exc)
    try:
        out.append("keeping the screen awake: %s"
                   % (", ".join(screen.idle_inhibitors()) or "nothing"))
        for schema, key in (("org.gnome.desktop.session", "idle-delay"),
                            ("org.gnome.desktop.screensaver", "idle-activation-enabled"),
                            ("org.gnome.desktop.screensaver", "lock-enabled"),
                            ("org.gnome.settings-daemon.plugins.power", "idle-dim"),
                            ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type")):
            value = screen._gsettings_get(schema, key)
            if value is not None:
                out.append("%s %s: %s" % (schema, key, value))
    except Exception as exc:
        out.append("screen settings check failed: %s" % exc)

    out.append(_section("Config"))
    out.append(json.dumps(config.load(), indent=2, default=str))

    for component in logs.COMPONENTS:
        out.append(_section("Log tail: %s" % component))
        out.append(logs.tail(component, log_lines).rstrip())

    return "\n".join(out) + "\n"


def write_report(directory=None, log_lines=120):
    """Save a report and return its path."""
    directory = directory or config.STATE_DIR
    os.makedirs(directory, exist_ok=True)
    name = "diagnostics-%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report(log_lines))
    return path
