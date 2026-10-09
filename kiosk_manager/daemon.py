"""Background service: boot launch, schedule, screen policy, settings window
keeper, kiosk watchdog, self-update and IPC."""

import datetime
import logging
import os
import signal
import subprocess
import threading
import time

from . import (browser, config, instance, ipc, procs, scheduler, screen, updater,
               version, x11)

log = logging.getLogger("kiosk.daemon")

TICK_SECONDS = 5

# Backoff between attempts to restart a settings window that keeps dying.
GUI_RETRY_MIN = 30
GUI_RETRY_MAX = 600

# If the machine has been up longer than this when the daemon starts, this is
# a service restart (an update or a crash), not a boot: leave the screen alone.
BOOT_WINDOW_SECONDS = 600

# A page that is still loading has a placeholder title; give it this long
# before the watchdog judges the title.
PAGE_LOAD_SECONDS = 90

# A settings window that holds its lock but has not answered for this long is
# hung: it is stopped so a working one can start.
GUI_HUNG_SECONDS = 120


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
        self.gui_silent_since = 0.0
        self.watchdog_next = time.time() + 60
        self.watchdog_result = "not run yet"
        self.watchdog_at = 0.0
        # Until this time someone is using the desktop (pressed Minimize or
        # opened the settings window), so the watchdog leaves the screen alone.
        self.operator_until = 0.0
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
        if not instance.acquire("daemon"):
            # A second daemon would fight the first over the browser and the
            # settings window. systemd retries, so this clears once the other
            # one is stopped.
            log.error("another kiosk-manager daemon is running (pid %s), exiting",
                      instance.holder("daemon"))
            raise SystemExit(1)
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
                if time.time() >= self.watchdog_next:
                    self.watchdog_next = time.time() + self.watchdog_interval()
                    self.watchdog()

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
            self.gui_silent_since = 0.0
            if reply.get("version") != version.installed_commit():
                self._restart_outdated_gui(reply)
            return
        now = time.time()
        holder = instance.holder("gui")
        if holder:
            # A window is running (it holds the lock) but not answering: it is
            # still starting, or hung. Never start a second one beside it.
            if not self.gui_silent_since:
                self.gui_silent_since = now
            elif now - self.gui_silent_since > GUI_HUNG_SECONDS:
                log.warning("settings window (pid %s) stopped answering, restarting it", holder)
                self._kill(holder)
                self.gui_silent_since = 0.0
            return
        self.gui_silent_since = 0.0
        if not self.cfg.get("gui", {}).get("keep_running", True):
            return
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
        # The new window cannot start until the old one has let go of its lock.
        for _ in range(20):
            if not instance.holder("gui"):
                break
            time.sleep(0.5)
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
            if self.browser.is_running() and not self.operator_active():
                self.browser.raise_window()

    @staticmethod
    def _kill(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError) as exc:
            log.warning("could not stop pid %s: %s", pid, exc)

    def stray_guis(self):
        """Settings window processes other than the one holding the lock.

        Builds from before the lock existed, or a copy started where it could
        not see the running one, would otherwise sit on screen as a second
        settings window.
        """
        keep = instance.holder("gui")
        ok, out = x11._run(["pgrep", "-u", str(os.getuid()), "-f", "kiosk_manager gui"])
        if not ok:
            return []
        strays = []
        for token in out.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid in (os.getpid(), keep):
                continue
            try:
                # One the daemon has only just started may not hold the lock yet.
                if time.time() - os.stat("/proc/%d" % pid).st_ctime < 30:
                    continue
            except OSError:
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as fh:
                    args = fh.read().split(b"\0")
            except OSError:
                continue
            # Only the python process itself, not a shell that mentions it.
            if b"kiosk_manager" in args and b"gui" in args:
                strays.append(pid)
        return strays

    def gui_window(self):
        """(reply, window id) for the settings window; the id is None if unknown."""
        reply = ipc.send(config.gui_socket(), {"command": "ping"}, timeout=2.0)
        if not reply.get("ok") or not reply.get("pid") or not x11.available():
            return reply, None
        wins = x11.search("--all", "--pid", str(reply["pid"]),
                          "--name", "^Kiosk Manager$")
        return reply, (wins[-1] if wins else None)

    # ---- watchdog ------------------------------------------------------
    def watchdog_cfg(self):
        return self.cfg.get("watchdog", {})

    def watchdog_interval(self):
        minutes = self.watchdog_cfg().get("interval_minutes", 5)
        try:
            return max(1, int(minutes)) * 60
        except (TypeError, ValueError):
            return 300

    def operator_active(self):
        """True while someone is using the desktop instead of the kiosk page."""
        if time.time() < self.operator_until:
            return True
        reply = ipc.send(config.gui_socket(), {"command": "ping"}, timeout=2.0)
        grace = self.grace_seconds()
        last = reply.get("last_active") or 0
        return bool(reply.get("ok") and last and time.time() - last < grace)

    def grace_seconds(self):
        try:
            return max(0, int(self.watchdog_cfg().get("operator_grace_minutes", 10))) * 60
        except (TypeError, ValueError):
            return 600

    def watchdog(self, force=False, touch_gui=True):
        """Put the screen back the way it should be: one settings window
        running behind a fullscreen kiosk page showing the right site, and the
        screen timeouts as configured.

        `force` skips the operator grace period (the GUI's Open kiosk page
        button). `touch_gui=False` leaves the settings window alone, for when
        it is the window asking. Runs under self.lock.
        """
        wcfg = self.watchdog_cfg()
        if not force and not wcfg.get("enabled", True):
            return "disabled"
        if not force and self.operator_active():
            self.watchdog_record("skipped: the desktop is in use")
            return self.watchdog_result
        if not x11.available():
            self.watchdog_record("skipped: xdotool/xprop or DISPLAY missing")
            return self.watchdog_result

        actions = []
        strays = self.stray_guis()
        for pid in strays:
            self._kill(pid)
        if strays:
            actions.append("closed %d extra settings window%s"
                           % (len(strays), "" if len(strays) == 1 else "s"))
            time.sleep(1.0)

        drifted = screen.drift(self.cfg)
        if drifted:
            self.apply_screen()
            actions.append("screen settings had changed (%s), re-applied" % drifted)

        gui_reply, gui_win = self.gui_window()
        if not touch_gui:
            pass
        elif not gui_reply.get("ok"):
            # check_gui owns the restart backoff; force one attempt now.
            if self.cfg.get("gui", {}).get("keep_running", True):
                self.gui_next_attempt = 0
                self.check_gui("watchdog: settings window not running")
                actions.append("started the settings window")
        elif gui_win and "HIDDEN" in x11.state(gui_win):
            ipc.send(config.gui_socket(), {"command": "restore"}, timeout=2.0)
            time.sleep(1.0)  # let it map before the stacking check below
            actions.append("restored the minimised settings window")

        if self.suppress_restart and not force:
            # Closed on purpose from the GUI or the CLI: leave the desktop up.
            actions.append("browser closed on purpose, left closed")
        else:
            actions.extend(self._check_browser())

        browser_win = self.browser.window_id()
        _, gui_win = self.gui_window()
        if browser_win and gui_win and not x11.is_below(gui_win, int(browser_win)):
            self.browser.raise_window()
            actions.append("put the settings window behind the page")
        elif browser_win and not self.suppress_restart:
            top = x11.top_app_window()
            if top and top != int(browser_win):
                # Some other window (a dialog, another app) is covering the page.
                self.browser.raise_window()
                actions.append("brought the page back in front of %r" % x11.title(top)[:40])

        self.watchdog_record(", ".join(actions) or "all good")
        return self.watchdog_result

    def _check_browser(self):
        info = self.browser.inspect()
        want_kiosk = self.cfg.get("browser", {}).get("kiosk", True)
        loaded = time.time() - self.browser.launched_at > PAGE_LOAD_SECONDS
        expected = str(self.watchdog_cfg().get("expected_title", "")).strip()

        problem = None
        if not info["running"]:
            ok, msg = self.browser.launch()
            self.browser_was_running = ok
            return ["browser was not running, opened it: %s" % msg]
        if info["windows"] == 0:
            problem = "no browser window"
        elif info["windows"] > 1:
            problem = "%d browser windows (popup or dialog)" % info["windows"]
        elif want_kiosk and not info["kiosk_flag"]:
            problem = "browser not started in kiosk mode"
        elif info["minimized"]:
            self.browser.raise_window()
            return ["restored the minimised kiosk page"]
        elif want_kiosk and not info["fullscreen"]:
            problem = "browser is not fullscreen"
        elif expected and loaded and expected.lower() not in info["title"].lower():
            problem = "wrong page (title %r)" % info["title"]

        if not problem:
            return []
        log.warning("watchdog: %s, restarting the browser", problem)
        ok, msg = self.browser.restart()
        return ["%s, restarted: %s" % (problem, msg)]

    def watchdog_record(self, result):
        self.watchdog_result = result
        self.watchdog_at = time.time()
        if result not in ("all good", "disabled") and not result.startswith("skipped"):
            self.note("watchdog: %s" % result)
        else:
            log.debug("watchdog: %s", result)

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
                "watchdog": {
                    "result": self.watchdog_result,
                    "at": self.watchdog_at,
                    "next": self.watchdog_next,
                    "operator_until": self.operator_until,
                },
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
                self.operator_until = 0.0
                # Back to the kiosk page: the desktop is no longer in use.
                ipc.send(config.gui_socket(), {"command": "idle"}, timeout=2.0)
                if request.get("ensure_kiosk"):
                    # The GUI's Open kiosk page button: fix whatever is wrong
                    # (not fullscreen, wrong page, popups) and bring it forward.
                    self.browser_was_running = True
                    result = self.watchdog(
                        force=True, touch_gui=not request.get("from_gui"))
                    self.browser.raise_window()
                else:
                    result = self.browser.ensure_open(
                        return_to_home=request.get("return_to_home", True))
            self.note("manual open: %s" % result)
            return {"ok": True, "result": result}
        if command == "minimize_browser":
            # The page's Minimize button: show the desktop and settings window
            # and keep the watchdog off them for the grace period.
            with self.lock:
                self.operator_until = time.time() + self.grace_seconds()
                ok = self.browser.minimize()
            ipc.send(config.gui_socket(), {"command": "show"}, timeout=2.0)
            self.note("kiosk page minimised from the page")
            return {"ok": ok, "result": "minimised" if ok else "no kiosk window"}
        if command == "watchdog":
            with self.lock:
                result = self.watchdog(force=bool(request.get("force")))
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
