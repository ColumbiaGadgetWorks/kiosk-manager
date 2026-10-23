"""One place that decides where the logs go and how verbose they are.

Files land in the XDG state directory (~/.local/state/kiosk-manager/logs),
which is where the spec puts data a program keeps between runs that is neither
configuration nor user data. Each component writes its own file so the daemon
and the settings window cannot interleave or fight over a rotation.

stderr keeps getting everything too, so `journalctl --user -u
kiosk-manager.service` still shows the daemon exactly as before.
"""

import collections
import logging
import logging.handlers
import os
import sys

from . import config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s[%(process)d]: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Components that own a log file.
COMPONENTS = ("daemon", "gui", "cli")


def log_path(component):
    return os.path.join(config.LOG_DIR, "%s.log" % component)


def _options():
    try:
        return config.load().get("logging", {}) or {}
    except Exception:  # a broken config must never stop logging from starting
        return {}


def _int(value, default, minimum):
    """A junk value in the config costs a sane default, never the log file."""
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def setup(component, verbose=False, to_file=True):
    """Install handlers for this process. Returns the log file path, if any."""
    opts = _options()
    debug = bool(verbose or opts.get("debug"))
    handlers = [logging.StreamHandler(sys.stderr)]
    path = None
    if to_file:
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            path = log_path(component)
            handlers.append(logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_int(opts.get("max_bytes"), 1_000_000, 50_000),
                backupCount=_int(opts.get("keep_files"), 3, 1),
                encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("kiosk-manager: cannot write %s (%s), logging to stderr only"
                  % (path, exc), file=sys.stderr)
            path = None
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format=FORMAT, datefmt=DATE_FORMAT,
                        handlers=handlers, force=True)
    return path


def apply_level(debug):
    """Flip debug logging on or off in a running process, without a restart."""
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    if root.level != level:
        root.setLevel(level)
        logging.getLogger("kiosk").info(
            "debug logging %s", "on" if debug else "off")
    return level


def tail(component, lines=200):
    """The last `lines` lines of one component log."""
    path = log_path(component)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(collections.deque(fh, maxlen=max(1, lines)))
    except OSError as exc:
        return "(no log at %s: %s)\n" % (path, exc)
