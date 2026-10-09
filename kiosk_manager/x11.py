"""Read-back of X11 window state: fullscreen / minimised flags, titles and the
window manager's stacking order. Used by the watchdog to see what is actually
on screen rather than trusting what we last asked for.
"""

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("kiosk.x11")

_HEX = re.compile(r"0x[0-9a-fA-F]+")


def _has(cmd):
    return shutil.which(cmd) is not None


def _run(args, timeout=10):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout,
                              check=False)
        return proc.returncode == 0, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", args[0], exc)
        return False, ""


def available():
    return bool(os.environ.get("DISPLAY")) and _has("xdotool") and _has("xprop")


def _root_windows(prop):
    ok, out = _run(["xprop", "-root", prop])
    if not ok:
        return []
    return [int(h, 16) for h in _HEX.findall(out.split("#", 1)[-1])]


def stacking():
    """Managed top-level windows, bottom of the stack first."""
    return _root_windows("_NET_CLIENT_LIST_STACKING")


def search(*criteria):
    """xdotool search, limited to windows the window manager manages.

    Firefox and GTK both create unmapped helper windows that share the class
    and pid of the real one; only managed windows are of interest here.
    """
    ok, out = _run(["xdotool", "search"] + list(criteria))
    if not ok:
        return []
    managed = set(stacking())
    found = []
    for token in out.split():
        try:
            wid = int(token)
        except ValueError:
            continue
        if not managed or wid in managed:
            found.append(wid)
    return found


def state(wid):
    """Set of _NET_WM_STATE atoms, e.g. {"FULLSCREEN", "HIDDEN"}."""
    ok, out = _run(["xprop", "-id", str(wid), "_NET_WM_STATE"])
    if not ok or "=" not in out:
        return set()
    atoms = out.split("=", 1)[1]
    return {a.strip().replace("_NET_WM_STATE_", "")
            for a in atoms.split(",") if a.strip()}


def window_types(wid):
    """Set of _NET_WM_WINDOW_TYPE names, e.g. {"NORMAL"} or {"DOCK"}."""
    ok, out = _run(["xprop", "-id", str(wid), "_NET_WM_WINDOW_TYPE"])
    if not ok or "=" not in out:
        return set()
    return {a.strip().replace("_NET_WM_WINDOW_TYPE_", "")
            for a in out.split("=", 1)[1].split(",") if a.strip()}


def top_app_window():
    """The highest visible application window, skipping panels, docks and the
    desktop, which window managers keep above or below everything."""
    for wid in reversed(stacking()):
        if window_types(wid) & {"DOCK", "DESKTOP", "NOTIFICATION", "TOOLTIP"}:
            continue
        if "HIDDEN" in state(wid):
            continue
        return wid
    return None


def title(wid):
    ok, out = _run(["xdotool", "getwindowname", str(wid)])
    return out.strip() if ok else ""


def activate(wid):
    """Un-minimise, raise and focus a window."""
    _run(["xdotool", "windowactivate", "--sync", str(wid)], timeout=5)
    _run(["xdotool", "windowraise", str(wid)])


def minimize(wid):
    ok, _ = _run(["xdotool", "windowminimize", str(wid)])
    return ok


def is_below(lower, upper):
    """True when window `lower` is stacked under window `upper`."""
    order = stacking()
    try:
        return order.index(lower) < order.index(upper)
    except ValueError:
        return False
