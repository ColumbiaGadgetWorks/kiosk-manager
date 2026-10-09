"""One running copy per program: the daemon and the settings window.

An exclusive flock on a file in the data directory, which (unlike the IPC
sockets in the runtime directory) is the same path whether the program was
started by the systemd service or from the desktop. The lock is released by
the kernel when the process exits, however it exits, so it never goes stale.
"""

import fcntl
import logging
import os

from . import config

log = logging.getLogger("kiosk.instance")

_held = {}


def _path(name):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    return os.path.join(config.DATA_DIR, "%s.lock" % name)


def acquire(name):
    """Take the lock for `name`. False if another process holds it."""
    if name in _held:
        return True
    fh = open(_path(name), "a+", encoding="ascii")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _held[name] = fh  # keep the file open: closing it releases the lock
    return True


def holder(name):
    """Pid of the process holding the lock for `name`, or None if it is free."""
    path = _path(name)
    try:
        fh = open(path, "r", encoding="ascii")
    except OSError:
        return None
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            try:
                return int(fh.read().strip() or 0) or None
            except ValueError:
                return None
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None
