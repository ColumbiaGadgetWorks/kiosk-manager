"""Background service: boot launch, schedule, screen policy, settings window
keeper, self-update and IPC."""

import datetime
import logging
import os
import signal
import subprocess
import threading
import time

from . import browser, config, ipc, procs, scheduler, screen, updater, version

log = logging.getLogger("kiosk.daemon")

TICK_SECONDS = 5

# Backoff between attempts to restart a settings window that keeps dying.
GUI_RETRY_MIN = 30
GUI_RETRY_MAX = 600

# If the machine has been up longer than this when the daemon starts, this is
# a service restart (an update or a crash), not a boot: leave the screen alone.
BOOT_WINDOW_SECONDS = 600


def system_uptime():
    try:
        with open("/proc/uptime", "r", encoding="ascii") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.browser = browser.BrowserManager(self.cfg)
        self.scheduler = scheduler.Scheduler()
        self.update_scheduler = scheduler.Scheduler()
        self.updater = updater.Updater()
        self.lock = threading.RLock()
        self.server = None
        self.started_at = time.time()
        self.last_event = "started"
        self.last_event_at = time.time()
        self.suppress_restart = False
        self.browser_was_running = False
        self.display_ready = False
        self.gui_launched_at = 0.0
        self.gui_next_attempt = 0.0
        self.gui_backoff = GUI_RETRY_MIN
        self.gui_restarted_for = None
        self._stop = threading.Event()

    # ---- helpers -------------------------------------------------------
    def note(self, message):
        self.last_event = message
        self.last_event_at = time.time()
        log.info(message)

    def apply_screen(self):
        backend = screen.apply_settings(self.cfg)
        log.info("screen settings applied via %s", backend)
        return backend

    def wait_for_display(self, timeout=120):
        """Block until an X display answers, so Firefox does not race the WM."""
        if not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = ":0"
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            if screen.probe_display():
                return True
            time.sleep(2)
        return False

    # ---- lifecycle -----------------------------------------------------
    def start(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        log.info("kiosk-manager %s starting", version.describe())
        self.server = ipc.Server(config.daemon_socket(), self.handle)
        self.server.start()
        log.info("ipc listening on %s", config.daemon_socket())

        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGHUP, lambda *_: self.reload_config())

        threading.Thread(target=self._boot_sequence, daemon=True).start()
        self.loop()

    def _signal(self, *_args):
        log.info("shutting down")
        self._stop.set()

    def _boot_sequence(self):
        is_boot = system_uptime() < BOOT_WINDOW_SECONDS
        self.wait_for_display()
        self.display_ready = True
        self.apply_screen()
        with self.lock:
            # The settings window goes up first so the kiosk page lands on top.
            self.check_gui("boot" if is_boot else "service start")

        boot = self.cfg.get("boot", {})
        if not boot.get("launch_on_boot", True):
            self.note("boot launch disabled in config")
            return
        delay = max(0, int(boot.get("delay_seconds", 15))) if is_boot else 0
        if delay:
            log.info("waiting %ds before the boot launch", delay)
            self._stop.wait(delay)
        if self._stop.is_set():
            return
        with self.lock:
            if is_boot:
                screen.wake()
                result = self.browser.ensure_open(return_to_home=True)
            elif self.browser.is_running():
                # Restarted underneath a live kiosk (after an update): leave it
                # alone unless the configured URL changed since it launched.
                result = ("restarted on the new URL" if self.sync_browser_url()
                          else "already running, left untouched")
            else:
                result = self.browser.ensure_open(return_to_home=False)
        self.note("%s launch: %s" % ("boot" if is_boot else "restart", result))

    def loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self._stop.wait(TICK_SECONDS)
        with self.lock:
            if self.server:
                self.server.stop()

    def tick(self):
        now = datetime.datetime.now()
        with self.lock:
            sched = self.cfg.get("schedule", {})
            if sched.get("enabled", True):
                for entry in self.scheduler.due(sched.get("entries"), now):
                    self.fire(entry)

            running = self.browser.is_running()
            boot = self.cfg.get("boot", {})
            if running:
                self.browser_was_running = True
                self.suppress_restart = False
            elif (self.browser_was_running and boot.get("restart_if_closed", True)
                  and not self.suppress_restart):
                grace = max(0, int(boot.get("restart_grace_seconds", 20)))
                if time.time() - self.browser.launched_at > grace:
                    ok, msg = self.browser.launch()
                    self.note("browser closed, relaunched: %s" % msg)
                    if not ok:
                        self.browser_was_running = False

            if self.display_ready:
                self.check_gui()

            if self.update_scheduler.due([self.update_entry()], now):
                ucfg = dict(self.cfg.get("update", {}))
                threading.Thread(target=self._auto_update, args=(ucfg,),
                                 daemon=True).start()

    def fire(self, entry):
        log.info("schedule fired: %s", scheduler.describe(entry))
        actions = []
        if entry.get("wake_screen", True):
            screen.wake()
            actions.append("woke screen")
        self.check_gui("scheduled event")
        if entry.get("open_browser", True):
            result = self.browser.ensure_open(
                return_to_home=entry.get("return_to_home", True))
            actions.append(result)
        self.note("scheduled %s -> %s" % (entry.get("time"), ", ".join(actions)))

    # ---- settings window -----------------------------------------------
    def check_gui(self, reason="not running"):
        """Keep the settings window running, and on the installed build."""
        reply = ipc.send(config.gui_socket(), {"command": "ping"}, timeout=2.0)
        if reply.get("ok"):
            if reply.get("version") != version.installed_commit():
                self._restart_outdated_gui(reply)
            return
        if not self.cfg.get("gui", {}).get("keep_running", True):
            return
        now = time.time()
        if now < self.gui_next_attempt:
            return
        if self.gui_launched_at and now - self.gui_launched_at < 60:
            self.gui_backoff = min(GUI_RETRY_MAX, self.gui_backoff * 2)
        else:
            self.gui_backoff = GUI_RETRY_MIN
        self.gui_next_attempt = now + self.gui_backoff
        self.launch_gui(reason)

    def _restart_outdated_gui(self, reply):
        installed = version.installed_commit()
        if self.gui_restarted_for == installed:
            return  # already tried once for this build; do not loop
        self.gui_restarted_for = installed
        log.info("settings window is running an older build, restarting it")
        quit_reply = ipc.send(config.gui_socket(), {"command": "quit"}, timeout=2.0)
        if not quit_reply.get("ok"):
            # Builds before 1.1 have no quit command.
            pid = reply.get("pid")
            try:
                if pid:
                    os.kill(int(pid), signal.SIGTERM)
                else:
                    subprocess.run(["pkill", "-f", "kiosk_manager gui"],
                                   check=False, timeout=5)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                log.warning("could not stop the old settings window: %s", exc)
        time.sleep(1.5)
        self.launch_gui("updated")

    def launch_gui(self, reason):
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        try:
            procs.spawn(procs.self_command("gui"), env=env, scope="kiosk-manager-gui")
        except OSError as exc:
            log.error("could not start the settings window: %s", exc)
            return
        self.gui_launched_at = time.time()
        self.note("settings window started (%s)" % reason)
        threading.Thread(target=self._kiosk_back_on_top, daemon=True).start()

    def _kiosk_back_on_top(self):
        """Once the new window has mapped, put the kiosk page back in front."""
        time.sleep(4)
        with self.lock:
            if self.browser.is_running():
                self.browser.raise_window()

    # ---- updates -------------------------------------------------------
    def update_entry(self):
        ucfg = self.cfg.get("update", {})
        return {
            "id": "auto-update",
            "enabled": bool(ucfg.get("enabled", False)),
            "time": ucfg.get("time", "03:00"),
            "days": ucfg.get("days", scheduler.DAYS),
        }

    def _auto_update(self, ucfg):
        ok, message = self.updater.apply(ucfg)
        self.note("auto-update: %s" % message)

    def _update_cfg(self, request):
        """Settings from the request (unsaved GUI values) or the saved config."""
        ucfg = dict(self.cfg.get("update", {}))
        override = (request or {}).get("update")
        if isinstance(override, dict):
            ucfg.update(override)
        return ucfg

    # ---- config --------------------------------------------------------
    def sync_browser_url(self):
        """Restart the kiosk browser if it is showing a superseded URL.

        Firefox only reads the URL (and the home page in user.js) at launch,
        so without this a new URL would not appear until the next reboot.
        """
        running = self.browser.running_url()
        wanted = self.cfg.get("url", "")
        if not running or not wanted or running == wanted:
            return False
        ok, msg = self.browser.restart()
        self.note("kiosk URL changed, browser restarted: %s" % msg)
        return ok

    def reload_config(self):
        with self.lock:
            self.cfg = config.load()
            self.browser.update_config(self.cfg)
            self.apply_screen()
            self.sync_browser_url()
        self.note("config reloaded")
        return self.cfg

    def set_config(self, new_cfg):
        with self.lock:
            config.save(new_cfg)
            self.cfg = config.load()
            self.browser.update_config(self.cfg)
            self.apply_screen()
            self.sync_browser_url()
        self.note("config updated from GUI")
        return self.cfg

    # ---- IPC -----------------------------------------------------------
    def status(self):
        with self.lock:
            sched = self.cfg.get("schedule", {})
            when, entry = scheduler.next_run_all(
                sched.get("entries", []) if sched.get("enabled", True) else [])
            next_update = scheduler.next_run(self.update_entry())
            return {
                "ok": True,
                "daemon": {
                    "pid": os.getpid(),
                    "version": version.describe(),
                    "uptime_seconds": int(time.time() - self.started_at),
                    "last_event": self.last_event,
                    "last_event_at": self.last_event_at,
                },
                "browser": self.browser.status(),
                "screen": screen.current_state(),
                "next_run": when.isoformat(sep=" ", timespec="minutes") if when else None,
                "next_entry": scheduler.describe(entry) if entry else None,
                "update": dict(
                    self.updater.status(),
                    next_run=next_update.isoformat(sep=" ", timespec="minutes")
                    if next_update else None),
            }

    def handle(self, request):
        command = (request or {}).get("command", "")
        if command == "ping":
            return {"ok": True, "pong": True, "version": version.RUNNING_COMMIT}
        if command == "status":
            return self.status()
        if command == "get_config":
            with self.lock:
                return {"ok": True, "config": self.cfg}
        if command == "set_config":
            cfg = request.get("config")
            if not isinstance(cfg, dict):
                return {"ok": False, "error": "missing config"}
            return {"ok": True, "config": self.set_config(cfg)}
        if command == "reload":
            return {"ok": True, "config": self.reload_config()}
        if command == "open":
            with self.lock:
                self.suppress_restart = False
                result = self.browser.ensure_open(
                    return_to_home=request.get("return_to_home", True))
            self.note("manual open: %s" % result)
            return {"ok": True, "result": result}
        if command == "restart_browser":
            with self.lock:
                self.suppress_restart = False
                ok, msg = self.browser.restart()
            self.note("manual restart: %s" % msg)
            return {"ok": ok, "result": msg}
        if command == "stop_browser":
            with self.lock:
                self.suppress_restart = True
                self.browser_was_running = False
                stopped = self.browser.stop()
            self.note("manual stop: %s" % (stopped or "nothing running"))
            return {"ok": True, "stopped": stopped}
        if command == "wake":
            return {"ok": True, "backends": screen.wake()}
        if command == "blank":
            return {"ok": screen.blank_now()}
        if command == "apply_screen":
            return {"ok": True, "backend": self.apply_screen()}
        # Update commands run in the IPC thread, outside the daemon lock, so a
        # slow network never stalls the schedule.
        if command == "update_check":
            try:
                available, latest = self.updater.check(self._update_cfg(request))
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "available": available, "latest": latest,
                    "result": "update available (%s)" % latest[:7]
                    if available else "up to date"}
        if command == "update_now":
            ok, message = self.updater.apply(self._update_cfg(request))
            self.note("manual update: %s" % message)
            return {"ok": ok, "result": message, "error": None if ok else message}
        if command == "quit":
            self._stop.set()
            return {"ok": True}
        return {"ok": False, "error": "unknown command: %s" % command}


def main():
    daemon = Daemon()
    daemon.start()
    return 0
