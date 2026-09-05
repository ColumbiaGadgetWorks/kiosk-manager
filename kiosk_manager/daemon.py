"""Background service: boot launch, schedule, screen policy, IPC."""

import datetime
import logging
import os
import signal
import threading
import time

from . import browser, config, ipc, scheduler, screen

log = logging.getLogger("kiosk.daemon")

TICK_SECONDS = 5


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.browser = browser.BrowserManager(self.cfg)
        self.scheduler = scheduler.Scheduler()
        self.lock = threading.RLock()
        self.server = None
        self.started_at = time.time()
        self.last_event = "started"
        self.last_event_at = time.time()
        self.suppress_restart = False
        self.pending_boot_launch = None
        self.browser_was_running = False
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
        self.wait_for_display()
        self.apply_screen()
        boot = self.cfg.get("boot", {})
        if not boot.get("launch_on_boot", True):
            self.note("boot launch disabled in config")
            return
        delay = max(0, int(boot.get("delay_seconds", 15)))
        if delay:
            log.info("waiting %ds before the boot launch", delay)
            self._stop.wait(delay)
        if self._stop.is_set():
            return
        with self.lock:
            screen.wake()
            result = self.browser.ensure_open(return_to_home=True)
        self.note("boot launch: %s" % result)

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

    def fire(self, entry):
        log.info("schedule fired: %s", scheduler.describe(entry))
        actions = []
        if entry.get("wake_screen", True):
            screen.wake()
            actions.append("woke screen")
        if entry.get("open_browser", True):
            result = self.browser.ensure_open(
                return_to_home=entry.get("return_to_home", True))
            actions.append(result)
        self.note("scheduled %s -> %s" % (entry.get("time"), ", ".join(actions)))

    # ---- config --------------------------------------------------------
    def reload_config(self):
        with self.lock:
            self.cfg = config.load()
            self.browser.update_config(self.cfg)
            self.apply_screen()
        self.note("config reloaded")
        return self.cfg

    def set_config(self, new_cfg):
        with self.lock:
            config.save(new_cfg)
            self.cfg = config.load()
            self.browser.update_config(self.cfg)
            self.apply_screen()
        self.note("config updated from GUI")
        return self.cfg

    # ---- IPC -----------------------------------------------------------
    def status(self):
        with self.lock:
            entries = self.cfg.get("schedule", {}).get("entries", [])
            when, entry = scheduler.next_run_all(
                entries if self.cfg.get("schedule", {}).get("enabled", True) else [])
            return {
                "ok": True,
                "daemon": {
                    "pid": os.getpid(),
                    "uptime_seconds": int(time.time() - self.started_at),
                    "last_event": self.last_event,
                    "last_event_at": self.last_event_at,
                },
                "browser": self.browser.status(),
                "screen": screen.current_state(),
                "next_run": when.isoformat(sep=" ", timespec="minutes") if when else None,
                "next_entry": scheduler.describe(entry) if entry else None,
            }

    def handle(self, request):
        command = (request or {}).get("command", "")
        if command == "ping":
            return {"ok": True, "pong": True}
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
        if command == "quit":
            self._stop.set()
            return {"ok": True}
        return {"ok": False, "error": "unknown command: %s" % command}


def main():
    daemon = Daemon()
    daemon.start()
    return 0
