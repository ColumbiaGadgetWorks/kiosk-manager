"""Unit tests that need no display, GTK or network.

Run with:  python3 -m unittest discover -s tests -t .
The auto-updater runs these before installing a new build.
"""

import datetime
import os
import tempfile
import time
import unittest
from unittest import mock

from kiosk_manager import procs, scheduler, screen, session


class SchedulerTest(unittest.TestCase):
    ENTRY = {"id": "a", "enabled": True, "time": "08:00",
             "days": ["mon", "tue", "wed", "thu", "fri"]}

    def test_fires_once_within_catchup(self):
        sched = scheduler.Scheduler()
        monday = datetime.datetime(2026, 11, 9, 8, 0, 30)
        self.assertEqual(sched.due([self.ENTRY], monday), [self.ENTRY])
        self.assertEqual(sched.due([self.ENTRY], monday), [])

    def test_skips_other_days_and_late(self):
        sched = scheduler.Scheduler()
        saturday = datetime.datetime(2026, 11, 14, 8, 0, 30)
        self.assertEqual(sched.due([self.ENTRY], saturday), [])
        late = datetime.datetime(2026, 11, 9, 8, 10)
        self.assertEqual(sched.due([self.ENTRY], late), [])

    def test_next_run_rolls_over_weekend(self):
        friday_evening = datetime.datetime(2026, 11, 13, 18, 0)
        self.assertEqual(scheduler.next_run(self.ENTRY, friday_evening),
                         datetime.datetime(2026, 11, 16, 8, 0))

    def test_describe_blank(self):
        entry = dict(self.ENTRY, wake_screen=False, blank_screen=True,
                     open_browser=False)
        self.assertIn("blank screen", scheduler.describe(entry))


class FakeRun:
    """Stands in for a module's _run, answering gdbus idle-time queries."""

    def __init__(self, idle_ms=60000):
        self.idle_ms = idle_ms
        self.calls = []

    def __call__(self, args, timeout=10):
        self.calls.append(list(args))
        if any("GetIdletime" in a for a in args):
            return True, "(uint64 %d,)" % self.idle_ms
        return True, ""

    def power_modes(self):
        return [c[-1] for c in self.calls if "PowerSaveMode" in c]


class ScreenWaylandTest(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()
        patches = [mock.patch.object(screen, "_run", self.run),
                   mock.patch.object(screen, "_has", lambda c: True),
                   mock.patch.object(screen, "is_wayland", lambda: True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(screen._stop_input_watch)

    def test_blank_then_input_wakes(self):
        self.assertTrue(screen.blank_now())
        self.assertEqual(self.run.power_modes(), ["<int32 1>"])
        time.sleep(2.6)
        self.run.idle_ms = 100
        time.sleep(1.2)
        self.assertEqual(self.run.power_modes(), ["<int32 1>", "<int32 0>"])

    def test_wake_cancels_watch(self):
        screen.blank_now()
        screen.wake()
        polls = len([c for c in self.run.calls if "GetIdletime" in c[-1]])
        time.sleep(2.6)
        self.assertEqual(
            len([c for c in self.run.calls if "GetIdletime" in c[-1]]), polls)

    def test_wayland_never_uses_xset_timeouts(self):
        with mock.patch.object(screen, "is_x11", lambda: True):
            screen.apply_settings({"screen": {"blank_after_minutes": 10}})
        xset = [c for c in self.run.calls if c[0] == "xset"]
        self.assertEqual(xset, [["xset", "s", "default"]])


class SessionEnvTest(unittest.TestCase):
    def test_refresh_picks_up_new_xauthority(self):
        out = (b"HOME=/home/kiosk\nDISPLAY=:0\n"
               b"XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.NEW\n")
        result = mock.Mock(returncode=0, stdout=out)
        with mock.patch.object(procs.shutil, "which", lambda c: "/bin/systemctl"), \
                mock.patch.object(procs.subprocess, "run", lambda *a, **k: result), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0", "XAUTHORITY": "/old"}):
            self.assertEqual(procs.refresh_session_env(), ["XAUTHORITY"])
            self.assertEqual(os.environ["XAUTHORITY"],
                             "/run/user/1000/.mutter-Xwaylandauth.NEW")
            self.assertEqual(procs.refresh_session_env(), [])


class DesktopSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        xs = os.path.join(self.tmp.name, "xsessions")
        ws = os.path.join(self.tmp.name, "wayland-sessions")
        os.makedirs(xs)
        os.makedirs(ws)
        open(os.path.join(xs, "gnome-xorg.desktop"), "w").close()
        open(os.path.join(ws, "gnome.desktop"), "w").close()
        self.saved = {"Session": ""}
        self.calls = []
        for name, value in (("XSESSIONS", xs), ("WAYLAND_SESSIONS", ws),
                            ("GDM_CONFIGS", ())):
            p = mock.patch.object(session, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.object(session, "_run", self.fake_run),
                  mock.patch.object(session, "xorg_installed", lambda: True),
                  mock.patch.object(session.shutil, "which", lambda c: "/usr/bin/" + c),
                  mock.patch.dict(os.environ, {"XDG_SESSION_DESKTOP": "gnome"})):
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, args, timeout=10):
        self.calls.append(args)
        method = args[args.index("--method") + 1]
        if method.endswith("FindUserByName"):
            return True, "(objectpath '/org/freedesktop/Accounts/User1000',)"
        if method.endswith("Properties.Get"):
            return True, "(<'%s'>,)" % self.saved.get(args[-1], "")
        if method.endswith("User.SetSession"):
            self.saved["Session"] = args[-1]
        return True, "()"

    def test_names(self):
        self.assertEqual(session.session_names(), ("gnome-xorg", "gnome"))

    def test_switch_to_x11_and_back(self):
        with mock.patch.object(session.screen, "is_wayland", lambda: True):
            self.assertEqual(session.info()["next"], "wayland")
            ok, _ = session.set_next("x11")
            self.assertTrue(ok)
            self.assertEqual(self.saved["Session"], "gnome-xorg")
            info = session.info()
            self.assertEqual((info["current"], info["next"]), ("wayland", "x11"))
            ok, _ = session.set_next("wayland")
            self.assertTrue(ok)
            self.assertEqual(session.info()["next"], "wayland")

    def test_wayland_blocked_by_gdm(self):
        conf = os.path.join(self.tmp.name, "daemon.conf")
        with open(conf, "w") as fh:
            fh.write("[daemon]\nWaylandEnable=false\n")
        with mock.patch.object(session, "GDM_CONFIGS", (conf,)):
            ok, message = session.set_next("wayland")
            self.assertFalse(ok)
            self.assertIn("WaylandEnable", message)
            self.assertEqual(session.info()["next"], "x11")


if __name__ == "__main__":
    unittest.main()
