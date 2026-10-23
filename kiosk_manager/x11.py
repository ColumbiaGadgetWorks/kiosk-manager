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
                              stderr=subprocess.PIPE, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed to run: %s", args[0], exc)
        return False, ""
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        # Non-zero is routine for xdotool searches that match nothing, so this
        # stays at debug; the callers log the decisions that matter.
        log.debug("%s rc=%d: %s", " ".join(args), proc.returncode,
                  proc.stderr.decode("utf-8", "replace").strip()[:200])
    return proc.returncode == 0, out


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


def wm_class(wid):
    ok, out = _run(["xprop", "-id", str(wid), "WM_CLASS"])
    if not ok or "=" not in out:
        return ""
    return out.split("=", 1)[1].strip().replace('"', "")


def window_pid(wid):
    ok, out = _run(["xprop", "-id", str(wid), "_NET_WM_PID"])
    if not ok or "=" not in out:
        return ""
    return out.split("=", 1)[1].strip()


def inventory():
    """Every managed window with the details the watchdog reasons about."""
    found = []
    for wid in stacking():
        found.append({
            "id": wid,
            "hex": hex(wid),
            "title": title(wid),
            "class": wm_class(wid),
            "pid": window_pid(wid),
            "state": sorted(state(wid)),
            "type": sorted(window_types(wid)),
        })
    return found


def activate(wid):
    """Un-minimise, raise and focus a window. True when it reached the top.

    Both commands are attempted even when the first reports success: under a
    compositor that denies activation, xdotool still exits 0 while nothing
    moves, so the caller checks the stack afterwards rather than the exit code.
    """
    act_ok, _ = _run(["xdotool", "windowactivate", "--sync", str(wid)], timeout=5)
    raise_ok, _ = _run(["xdotool", "windowraise", str(wid)])
    log.debug("activate %s: windowactivate=%s windowraise=%s", wid, act_ok, raise_ok)
    return act_ok or raise_ok


def minimize(wid):
    ok, _ = _run(["xdotool", "windowminimize", str(wid)])
    log.debug("minimize %s: %s", wid, ok)
    return ok


def is_below(lower, upper):
    """True when window `lower` is stacked under window `upper`."""
    order = stacking()
    try:
        return order.index(lower) < order.index(upper)
    except ValueError:
        return False
