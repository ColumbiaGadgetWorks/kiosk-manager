"""Starting child programs so they outlive the daemon.

Under systemd every process the service starts lives in the service cgroup, so
`systemctl --user restart kiosk-manager` (which is what an update does) would
kill the kiosk browser and the settings window too. Running them in their own
transient scope keeps the screen untouched while the daemon restarts.
"""

import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("kiosk.procs")


def under_systemd():
    return bool(os.environ.get("INVOCATION_ID")) and shutil.which("systemd-run") is not None


def self_command(*args):
    """Command line that runs this program again with `args`."""
    exe = os.environ.get("KIOSK_MANAGER_BIN") or shutil.which("kiosk-manager")
    if exe:
        return [exe] + list(args)
    return [sys.executable, "-m", "kiosk_manager"] + list(args)


def _unit_name(prefix):
    return "%s-%d" % (prefix, int(time.time() * 1000))


def spawn(cmd, env=None, scope=None):
    """Start a long-lived program detached from the daemon.

    With `scope`, the program runs in its own systemd scope when possible and
    falls back to a plain child process otherwise.
    """
    env = env if env is not None else dict(os.environ)
    if scope and under_systemd():
        wrapped = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                   "--unit=" + _unit_name(scope)] + list(cmd)
        try:
            proc = subprocess.Popen(wrapped, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            log.warning("systemd-run unavailable (%s), starting %s directly", exc, cmd[0])
        else:
            # systemd-run execs the program once the scope exists, so a quick
            # non-zero exit means the scope itself could not be created.
            time.sleep(1.0)
            if proc.poll() in (None, 0):
                return proc
            log.warning("systemd-run --scope failed (rc=%s), starting %s directly",
                        proc.returncode, cmd[0])
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def run_job(cmd, name):
    """Run a one-shot job as a transient user service that survives our restart."""
    if under_systemd():
        unit = _unit_name(name)
        try:
            proc = subprocess.run(
                ["systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit]
                + list(cmd),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
                check=False)
            if proc.returncode == 0:
                return unit
            log.warning("systemd-run job failed: %s",
                        proc.stdout.decode("utf-8", "replace").strip())
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("systemd-run job failed: %s", exc)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return "direct"
