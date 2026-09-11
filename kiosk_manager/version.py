"""Which build is installed, and which build this process is running."""

import os

from . import __version__

# install.sh writes the git commit it installed next to the package.
VERSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")


def installed_commit():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# Captured at import, so a long-running process can tell it has been superseded.
RUNNING_COMMIT = installed_commit()


def describe(commit=None):
    commit = RUNNING_COMMIT if commit is None else commit
    if commit:
        return "%s (%s)" % (__version__, commit[:7])
    return "%s (unknown build)" % __version__
