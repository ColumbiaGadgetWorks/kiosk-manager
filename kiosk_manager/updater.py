"""Self-update from the project git repository.

An update fetches the configured branch into a private checkout, then hands off
to that checkout's `install.sh --update`, which copies the files into place and
restarts the service. The daemon records the commit it expects, so the
restarted daemon can report whether the update landed.
"""

import datetime
import json
import logging
import os
import shutil
import subprocess
import threading
import time

from . import config, procs, version

log = logging.getLogger("kiosk.updater")

SRC_DIR = os.path.join(config.DATA_DIR, "src")
STATE_PATH = os.path.join(config.DATA_DIR, "update-state.json")
LOG_PATH = os.path.join(config.DATA_DIR, "update.log")

# An install that has not restarted the daemon within this long has failed.
PENDING_TIMEOUT = 15 * 60


def _git(args, cwd=None, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, check=False)
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        lines = out.splitlines()
        # git puts the useful part on the fatal:/error: line, not the last one.
        detail = next((line for line in lines
                       if line.startswith(("fatal:", "error:"))),
                      lines[-1] if lines else "exit %d" % proc.returncode)
        raise RuntimeError("git %s failed: %s" % (args[0], detail))
    return out


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


class Updater:
    def __init__(self):
        self._lock = threading.Lock()
        self.busy = False
        self.state = _load_state()
        self.reconcile()

    # ---- state ---------------------------------------------------------
    def _save(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, STATE_PATH)

    def _record(self, key, message, ok):
        self.state[key] = {"at": time.time(), "message": message, "ok": bool(ok)}
        log.info("%s: %s", key, message)

    def reconcile(self):
        """Settle an update that was handed to install.sh earlier."""
        pending = self.state.get("pending_commit")
        if not pending:
            return
        if version.installed_commit() == pending:
            self._record("last_update", "updated to %s" % pending[:7], ok=True)
        elif time.time() - self.state.get("pending_since", 0) > PENDING_TIMEOUT:
            self._record("last_update", "update to %s did not finish, see %s"
                         % (pending[:7], LOG_PATH), ok=False)
        else:
            return
        self.state.pop("pending_commit", None)
        self.state.pop("pending_since", None)
        self._save()

    def status(self):
        self.reconcile()
        return {
            "version": version.describe(),
            "installed": version.installed_commit(),
            "latest": self.state.get("latest_commit"),
            "last_check": self.state.get("last_check"),
            "last_update": self.state.get("last_update"),
            "pending": self.state.get("pending_commit"),
            "busy": self.busy,
            "git_available": shutil.which("git") is not None,
            "log": LOG_PATH,
        }

    # ---- operations ----------------------------------------------------
    def check(self, ucfg):
        """Ask the remote for the branch head. Returns (available, commit)."""
        if not shutil.which("git"):
            raise RuntimeError("git is not installed (sudo apt-get install git)")
        repo = ucfg.get("repo", "").strip()
        branch = ucfg.get("branch", "main").strip() or "main"
        if not repo:
            raise RuntimeError("no update repository configured")
        try:
            out = _git(["ls-remote", repo, "refs/heads/" + branch], timeout=60)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self._record("last_check", "check failed: %s" % exc, ok=False)
            self._save()
            raise RuntimeError(str(exc))
        if not out:
            self._record("last_check", "branch %s not found" % branch, ok=False)
            self._save()
            raise RuntimeError("branch %s not found in %s" % (branch, repo))
        remote = out.split()[0]
        available = remote != version.installed_commit()
        self.state["latest_commit"] = remote
        self._record("last_check", "update available (%s)" % remote[:7]
                     if available else "up to date", ok=True)
        self._save()
        return available, remote

    def apply(self, ucfg):
        """Install the newest commit if there is one. Returns (ok, message)."""
        with self._lock:
            if self.busy:
                return False, "an update is already running"
            self.reconcile()
            if self.state.get("pending_commit"):
                return False, "an update is already being installed"
            self.busy = True
        try:
            available, remote = self.check(ucfg)
            if not available:
                return True, "already up to date"
            head = self._fetch(ucfg)
            script = os.path.join(SRC_DIR, "install.sh")
            if not os.path.isfile(script):
                raise RuntimeError("install.sh is missing from %s" % SRC_DIR)
            self.state["pending_commit"] = head
            self.state["pending_since"] = time.time()
            self._save()
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write("\n=== %s installing %s\n"
                         % (datetime.datetime.now().isoformat(timespec="seconds"),
                            head))
            # install.sh restarts this service, so it must not run inside it.
            how = procs.run_job(
                ["/bin/bash", "-c", 'exec "$0" --update >>"$1" 2>&1',
                 script, LOG_PATH],
                "kiosk-manager-update")
            log.info("update %s handed to install.sh (%s)", head[:7], how)
            return True, "installing %s, the service will restart" % head[:7]
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.state.pop("pending_commit", None)
            self.state.pop("pending_since", None)
            self._record("last_update", "update failed: %s" % exc, ok=False)
            self._save()
            return False, str(exc)
        finally:
            self.busy = False

    def _fetch(self, ucfg):
        repo = ucfg.get("repo", "").strip()
        branch = ucfg.get("branch", "main").strip() or "main"
        if os.path.isdir(os.path.join(SRC_DIR, ".git")):
            _git(["remote", "set-url", "origin", repo], cwd=SRC_DIR)
            _git(["fetch", "--quiet", "--depth", "1", "origin", branch],
                 cwd=SRC_DIR, timeout=300)
            _git(["reset", "--quiet", "--hard", "FETCH_HEAD"], cwd=SRC_DIR)
        else:
            shutil.rmtree(SRC_DIR, ignore_errors=True)
            os.makedirs(os.path.dirname(SRC_DIR), exist_ok=True)
            _git(["clone", "--quiet", "--depth", "1", "--branch", branch, repo,
                  SRC_DIR], timeout=300)
        return _git(["rev-parse", "HEAD"], cwd=SRC_DIR)
