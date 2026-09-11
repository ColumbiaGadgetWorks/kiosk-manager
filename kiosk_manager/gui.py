"""GTK configuration window for the kiosk daemon.

Lives in the taskbar behind the kiosk page: closing the window only minimises
it, and the daemon restarts it if it dies. `kiosk-manager show` (bind it to a
hotkey) brings it to the front.
"""

import datetime
import logging
import os
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import config, ipc, scheduler, version  # noqa: E402

log = logging.getLogger("kiosk.gui")

CSS = b"""
window, dialog { font-size: 15px; }
.section-title { font-weight: bold; font-size: 17px; }
.hint { color: alpha(currentColor, 0.65); font-size: 13px; }
.status-ok { color: #2e7d32; font-weight: bold; }
.status-bad { color: #c62828; font-weight: bold; }
button { min-height: 40px; padding-left: 14px; padding-right: 14px; }
entry { min-height: 38px; }
.big-button { min-height: 56px; font-size: 16px; }
list row { padding: 6px; }
"""

ON_SCREEN_KEYBOARDS = ["onboard", "matchbox-keyboard", "florence", "squeekboard"]


def call(command, timeout=5.0, **kwargs):
    payload = {"command": command}
    payload.update(kwargs)
    return ipc.send(config.daemon_socket(), payload, timeout=timeout)


def when(timestamp):
    if not timestamp:
        return "never"
    return datetime.datetime.fromtimestamp(timestamp).strftime("%a %d %b %H:%M")


def time_spinners(text):
    """Returns (row, hour_spin, minute_spin) for an HH:MM value."""
    hour, minute = scheduler.parse_hhmm(text)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
    hour_spin.set_value(hour)
    minute_spin = Gtk.SpinButton.new_with_range(0, 59, 5)
    minute_spin.set_value(minute)
    for spin in (hour_spin, minute_spin):
        spin.set_orientation(Gtk.Orientation.VERTICAL)
        spin.set_numeric(True)
        spin.set_wrap(True)
    row.pack_start(hour_spin, False, False, 0)
    row.pack_start(Gtk.Label(label=":"), False, False, 0)
    row.pack_start(minute_spin, False, False, 0)
    return row, hour_spin, minute_spin


def spin_time(hour_spin, minute_spin):
    return "%02d:%02d" % (int(hour_spin.get_value()), int(minute_spin.get_value()))


def day_toggles(selected):
    """Returns (row, {day: ToggleButton}) for a set of weekdays."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    row.set_homogeneous(True)
    buttons = {}
    for day in scheduler.DAYS:
        button = Gtk.ToggleButton(label=scheduler.DAY_LABELS[day])
        button.set_active(day in (selected or []))
        buttons[day] = button
        row.pack_start(button, True, True, 0)
    return row, buttons


def selected_days(buttons):
    return [d for d in scheduler.DAYS if buttons[d].get_active()]


class ScheduleDialog(Gtk.Dialog):
    def __init__(self, parent, entry):
        super().__init__(title="Scheduled event", transient_for=parent, modal=True)
        self.set_default_size(460, -1)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        self.entry = dict(entry)

        box = self.get_content_area()
        box.set_spacing(12)
        box.set_border_width(16)

        time_row, self.hour, self.minute = time_spinners(entry.get("time"))
        time_row.pack_start(self._label("Time"), False, False, 0)
        time_row.reorder_child(time_row.get_children()[-1], 0)
        box.add(time_row)

        box.add(self._label("Days"))
        days_row, self.day_buttons = day_toggles(entry.get("days"))
        box.add(days_row)

        self.wake = self._switch_row(box, "Wake the screen",
                                     entry.get("wake_screen", True))
        self.open_browser = self._switch_row(box, "Open the kiosk page",
                                             entry.get("open_browser", True))
        self.return_home = self._switch_row(
            box, "If already open, return to the home URL",
            entry.get("return_to_home", True))
        self.enabled = self._switch_row(box, "Enabled", entry.get("enabled", True))
        self.show_all()

    @staticmethod
    def _label(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("section-title")
        return label

    def _switch_row(self, box, text, active):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        switch = Gtk.Switch()
        switch.set_active(bool(active))
        switch.set_valign(Gtk.Align.CENTER)
        row.pack_start(switch, False, False, 0)
        box.add(row)
        return switch

    def result(self):
        self.entry.update({
            "time": spin_time(self.hour, self.minute),
            "days": selected_days(self.day_buttons),
            "wake_screen": self.wake.get_active(),
            "open_browser": self.open_browser.get_active(),
            "return_to_home": self.return_home.get_active(),
            "enabled": self.enabled.get_active(),
        })
        return self.entry


class KioskWindow(Gtk.Window):
    def __init__(self, start_minimized=True):
        super().__init__(title="Kiosk Manager")
        self.set_default_size(720, 680)
        self.set_icon_name("preferences-system")
        self.set_skip_taskbar_hint(False)

        self.cfg = self._fetch_config()
        self._build()
        self.connect("delete-event", self._on_delete)

        if start_minimized:
            # Iconify before mapping so the window never flashes over the
            # kiosk page, and do not steal focus from it.
            self.set_focus_on_map(False)
            self.iconify()
        self.show_all()

        GLib.timeout_add_seconds(3, self._refresh_status)
        self._refresh_status()

    # ---- config plumbing ----------------------------------------------
    def _fetch_config(self):
        reply = call("get_config")
        if reply.get("ok") and isinstance(reply.get("config"), dict):
            return reply["config"]
        return config.load()

    def _push_config(self, cfg):
        # Saving a new URL restarts the browser, which can take several seconds.
        reply = call("set_config", timeout=30, config=cfg)
        if not reply.get("ok"):
            # Daemon down: still persist, it picks the file up when it starts.
            config.save(cfg)
            return False, reply.get("error", "daemon not reachable")
        return True, "saved"

    def _call_async(self, command, done, timeout=300, **kwargs):
        """Run a slow daemon call off the GTK thread; `done(reply)` runs on it."""
        def worker():
            reply = call(command, timeout=timeout, **kwargs)
            GLib.idle_add(lambda: done(reply) and False)
        threading.Thread(target=worker, daemon=True).start()

    # ---- layout --------------------------------------------------------
    def _build(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        self.banner = Gtk.Label(xalign=0)
        self.banner.set_margin_top(6)
        self.banner.set_margin_start(12)
        self.banner.set_margin_end(12)
        outer.pack_start(self.banner, False, False, 0)

        notebook = Gtk.Notebook()
        notebook.set_margin_top(8)
        notebook.set_margin_start(8)
        notebook.set_margin_end(8)
        outer.pack_start(notebook, True, True, 0)
        for title, page in (("Website", self._page_website()),
                            ("Schedule", self._page_schedule()),
                            ("Screen", self._page_screen()),
                            ("System", self._page_system()),
                            ("Status", self._page_status())):
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.add(page)
            notebook.append_page(scroller, Gtk.Label(label=title))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_border_width(10)
        save = Gtk.Button(label="Save settings")
        save.get_style_context().add_class("suggested-action")
        save.get_style_context().add_class("big-button")
        save.connect("clicked", self._on_save)
        revert = Gtk.Button(label="Revert")
        revert.connect("clicked", self._on_revert)
        kiosk = Gtk.Button(label="Show kiosk page")
        kiosk.get_style_context().add_class("big-button")
        kiosk.connect("clicked", self._on_show_kiosk)
        actions.pack_start(save, True, True, 0)
        actions.pack_start(revert, False, False, 0)
        actions.pack_start(kiosk, False, False, 0)
        outer.pack_start(actions, False, False, 0)

    def _page_website(self):
        box = self._page_box()

        box.add(self._title("Kiosk page"))
        self.url_entry = Gtk.Entry()
        self.url_entry.set_text(self.cfg.get("url", ""))
        self.url_entry.set_placeholder_text("https://example.com/")
        url_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        url_row.pack_start(self.url_entry, True, True, 0)
        if self._keyboard_command():
            kbd = Gtk.Button(label="Keyboard")
            kbd.connect("clicked", self._on_keyboard)
            url_row.pack_start(kbd, False, False, 0)
        box.add(url_row)
        box.add(self._hint("Firefox opens here at boot and at every scheduled "
                           "event. It is also set as the home page, so a "
                           "scheduled event can return an idle screen to it."))

        box.add(self._title("Browser"))
        self.cmd_entry = Gtk.Entry()
        self.cmd_entry.set_text(self.cfg.get("browser", {}).get(
            "command", "/usr/bin/firefox"))
        box.add(self._field_row("Executable", self.cmd_entry))

        self.extra_entry = Gtk.Entry()
        self.extra_entry.set_text(" ".join(
            self.cfg.get("browser", {}).get("extra_args") or []))
        self.extra_entry.set_placeholder_text("optional extra command line flags")
        box.add(self._field_row("Extra flags", self.extra_entry))

        self.kiosk_switch = self._switch_row(
            box, "Kiosk mode (fullscreen, no browser UI)",
            self.cfg.get("browser", {}).get("kiosk", True))
        self.private_switch = self._switch_row(
            box, "Private window (no history or cookies kept)",
            self.cfg.get("browser", {}).get("private_window", False))
        self.profile_switch = self._switch_row(
            box, "Use the dedicated kiosk Firefox profile",
            self.cfg.get("browser", {}).get("use_managed_profile", True))

        box.add(self._title("Startup"))
        self.boot_switch = self._switch_row(
            box, "Open the kiosk page when the machine boots",
            self.cfg.get("boot", {}).get("launch_on_boot", True))
        self.delay_spin = Gtk.SpinButton.new_with_range(0, 600, 5)
        self.delay_spin.set_value(self.cfg.get("boot", {}).get("delay_seconds", 15))
        box.add(self._field_row("Delay after boot (seconds)", self.delay_spin))
        self.restart_switch = self._switch_row(
            box, "Reopen automatically if the browser is closed or crashes",
            self.cfg.get("boot", {}).get("restart_if_closed", True))
        return box

    def _page_schedule(self):
        box = self._page_box()
        box.add(self._title("Scheduled wake / open events"))
        box.add(self._hint("At each event the screen wakes and the kiosk page is "
                           "opened only if it is not already open."))

        self.schedule_switch = self._switch_row(
            box, "Scheduling enabled",
            self.cfg.get("schedule", {}).get("enabled", True))

        self.schedule_list = Gtk.ListBox()
        self.schedule_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(240)
        scroller.add(self.schedule_list)
        frame = Gtk.Frame()
        frame.add(scroller)
        box.add(frame)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add = Gtk.Button(label="Add event")
        add.connect("clicked", self._on_add_entry)
        edit = Gtk.Button(label="Edit")
        edit.connect("clicked", self._on_edit_entry)
        remove = Gtk.Button(label="Remove")
        remove.connect("clicked", self._on_remove_entry)
        for button in (add, edit, remove):
            row.pack_start(button, True, True, 0)
        box.add(row)

        self._reload_schedule_rows()
        return box

    def _page_screen(self):
        box = self._page_box()
        scfg = self.cfg.get("screen", {})

        box.add(self._title("Screen timeout"))
        self.manage_switch = self._switch_row(
            box, "Let Kiosk Manager control the screen timeout",
            scfg.get("manage_timeout", True))

        self.blank_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.blank_spin.set_value(scfg.get("blank_after_minutes", 0))
        box.add(self._field_row("Blank the screen after (minutes, 0 = never)",
                                self.blank_spin))

        self.dpms_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.dpms_spin.set_value(scfg.get("dpms_off_after_minutes", 0))
        box.add(self._field_row("Power the panel off after (minutes, 0 = never)",
                                self.dpms_spin))

        self.lock_switch = self._switch_row(
            box, "Disable the lock screen and screensaver",
            scfg.get("disable_lock", True))
        box.add(self._hint("Settings are applied to the running session as soon "
                           "as you save, and again at every boot."))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, command in (("Wake screen now", "wake"),
                               ("Blank screen now", "blank"),
                               ("Re-apply now", "apply_screen")):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, c=command: self._command(c))
            row.pack_start(button, True, True, 0)
        box.add(row)
        return box

    def _page_system(self):
        box = self._page_box()
        gcfg = self.cfg.get("gui", {})
        ucfg = self.cfg.get("update", {})

        box.add(self._title("Settings window"))
        self.keep_gui_switch = self._switch_row(
            box, "Keep this window running: start it at boot and reopen it "
                 "if it is closed",
            gcfg.get("keep_running", True))
        self.minimized_switch = self._switch_row(
            box, "Start minimised in the taskbar",
            gcfg.get("start_minimized", True))
        box.add(self._hint("The window always opens behind the kiosk page. Bring "
                           "it forward from the taskbar or with the "
                           "kiosk-manager show hotkey."))

        box.add(self._title("Automatic updates"))
        self.update_switch = self._switch_row(
            box, "Install updates automatically", ucfg.get("enabled", False))

        time_row, self.update_hour, self.update_minute = time_spinners(
            ucfg.get("time", "03:00"))
        box.add(self._field_row("Check and install at", time_row))
        days_row, self.update_days = day_toggles(ucfg.get("days", scheduler.DAYS))
        box.add(days_row)

        self.repo_entry = Gtk.Entry()
        self.repo_entry.set_text(ucfg.get("repo", ""))
        box.add(self._field_row("Repository", self.repo_entry))
        self.branch_entry = Gtk.Entry()
        self.branch_entry.set_text(ucfg.get("branch", "main"))
        box.add(self._field_row("Branch", self.branch_entry))

        self.update_label = Gtk.Label(xalign=0)
        self.update_label.set_selectable(True)
        self.update_label.set_line_wrap(True)
        box.add(self.update_label)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.check_button = Gtk.Button(label="Check for updates")
        self.check_button.connect("clicked", self._on_check_update)
        self.update_button = Gtk.Button(label="Update now")
        self.update_button.connect("clicked", self._on_update_now)
        row.pack_start(self.check_button, True, True, 0)
        row.pack_start(self.update_button, True, True, 0)
        box.add(row)
        box.add(self._hint("An update restarts only the background service. The "
                           "kiosk page stays on screen; this window reopens on "
                           "the new version by itself."))
        return box

    def _page_status(self):
        box = self._page_box()
        box.add(self._title("Status"))
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_selectable(True)
        self.status_label.set_line_wrap(True)
        box.add(self.status_label)

        box.add(self._title("Browser controls"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, command in (("Open page now", "open"),
                               ("Restart browser", "restart_browser"),
                               ("Close browser", "stop_browser")):
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("big-button")
            button.connect("clicked", lambda _b, c=command: self._command(c))
            row.pack_start(button, True, True, 0)
        box.add(row)
        box.add(self._hint("Closing the browser leaves the desktop visible; the "
                           "next scheduled event or a reboot brings it back."))
        return box

    # ---- small widget helpers -----------------------------------------
    @staticmethod
    def _page_box():
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(16)
        return box

    @staticmethod
    def _title(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("section-title")
        label.set_margin_top(6)
        return label

    @staticmethod
    def _hint(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("hint")
        label.set_line_wrap(True)
        return label

    @staticmethod
    def _field_row(text, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        label = Gtk.Label(label=text, xalign=0)
        label.set_size_request(300, -1)
        row.pack_start(label, False, False, 0)
        row.pack_start(widget, True, True, 0)
        return row

    @staticmethod
    def _switch_row(box, text, active):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        row.pack_start(label, True, True, 0)
        switch = Gtk.Switch()
        switch.set_active(bool(active))
        switch.set_valign(Gtk.Align.CENTER)
        row.pack_start(switch, False, False, 0)
        box.add(row)
        return switch

    @staticmethod
    def _keyboard_command():
        for name in ON_SCREEN_KEYBOARDS:
            if shutil.which(name):
                return name
        return None

    def _on_keyboard(self, *_args):
        name = self._keyboard_command()
        if not name:
            return
        try:
            subprocess.Popen([name], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._flash("Could not start %s: %s" % (name, exc), ok=False)

    # ---- schedule list -------------------------------------------------
    def _entries(self):
        return self.cfg.setdefault("schedule", {}).setdefault("entries", [])

    def _reload_schedule_rows(self):
        for child in self.schedule_list.get_children():
            self.schedule_list.remove(child)
        for entry in self._entries():
            row = Gtk.ListBoxRow()
            row.entry_id = entry["id"]
            text = scheduler.describe(entry)
            if not entry.get("enabled", True):
                text += "   [disabled]"
            label = Gtk.Label(label=text, xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            row.add(label)
            self.schedule_list.add(row)
        self.schedule_list.show_all()

    def _selected_entry(self):
        row = self.schedule_list.get_selected_row()
        if row is None:
            return None
        for entry in self._entries():
            if entry["id"] == row.entry_id:
                return entry
        return None

    def _on_add_entry(self, *_args):
        entry = config.new_schedule_entry()
        dialog = ScheduleDialog(self, entry)
        if dialog.run() == Gtk.ResponseType.OK:
            self._entries().append(dialog.result())
            self._reload_schedule_rows()
        dialog.destroy()

    def _on_edit_entry(self, *_args):
        entry = self._selected_entry()
        if not entry:
            self._flash("Select an event first", ok=False)
            return
        dialog = ScheduleDialog(self, entry)
        if dialog.run() == Gtk.ResponseType.OK:
            entry.update(dialog.result())
            self._reload_schedule_rows()
        dialog.destroy()

    def _on_remove_entry(self, *_args):
        entry = self._selected_entry()
        if not entry:
            self._flash("Select an event first", ok=False)
            return
        self._entries().remove(entry)
        self._reload_schedule_rows()

    # ---- actions -------------------------------------------------------
    def _collect_update(self):
        return {
            "enabled": self.update_switch.get_active(),
            "time": spin_time(self.update_hour, self.update_minute),
            "days": selected_days(self.update_days),
            "repo": self.repo_entry.get_text().strip(),
            "branch": self.branch_entry.get_text().strip() or "main",
        }

    def _collect(self):
        cfg = self.cfg
        cfg["url"] = self.url_entry.get_text().strip()
        cfg.setdefault("browser", {}).update({
            "command": self.cmd_entry.get_text().strip() or "/usr/bin/firefox",
            "kiosk": self.kiosk_switch.get_active(),
            "private_window": self.private_switch.get_active(),
            "use_managed_profile": self.profile_switch.get_active(),
            "extra_args": self.extra_entry.get_text().split(),
        })
        cfg.setdefault("boot", {}).update({
            "launch_on_boot": self.boot_switch.get_active(),
            "delay_seconds": int(self.delay_spin.get_value()),
            "restart_if_closed": self.restart_switch.get_active(),
        })
        cfg.setdefault("schedule", {})["enabled"] = self.schedule_switch.get_active()
        cfg.setdefault("screen", {}).update({
            "manage_timeout": self.manage_switch.get_active(),
            "blank_after_minutes": int(self.blank_spin.get_value()),
            "dpms_off_after_minutes": int(self.dpms_spin.get_value()),
            "disable_lock": self.lock_switch.get_active(),
        })
        cfg.setdefault("gui", {}).update({
            "keep_running": self.keep_gui_switch.get_active(),
            "start_minimized": self.minimized_switch.get_active(),
        })
        cfg.setdefault("update", {}).update(self._collect_update())
        return cfg

    def _on_save(self, *_args):
        cfg = self._collect()
        url = cfg["url"]
        if url and not url.startswith(("http://", "https://", "file://")):
            cfg["url"] = "https://" + url
            self.url_entry.set_text(cfg["url"])
        ok, message = self._push_config(cfg)
        if ok:
            self._flash("Settings saved and applied", ok=True)
        else:
            self._flash("Saved to disk, but the daemon is not running (%s)"
                        % message, ok=False)

    def _on_revert(self, *_args):
        self.cfg = self._fetch_config()
        cfg = self.cfg
        bcfg = cfg.get("browser", {})
        boot = cfg.get("boot", {})
        scfg = cfg.get("screen", {})
        gcfg = cfg.get("gui", {})
        ucfg = cfg.get("update", {})
        self.url_entry.set_text(cfg.get("url", ""))
        self.cmd_entry.set_text(bcfg.get("command", "/usr/bin/firefox"))
        self.extra_entry.set_text(" ".join(bcfg.get("extra_args") or []))
        self.kiosk_switch.set_active(bcfg.get("kiosk", True))
        self.private_switch.set_active(bcfg.get("private_window", False))
        self.profile_switch.set_active(bcfg.get("use_managed_profile", True))
        self.boot_switch.set_active(boot.get("launch_on_boot", True))
        self.delay_spin.set_value(boot.get("delay_seconds", 15))
        self.restart_switch.set_active(boot.get("restart_if_closed", True))
        self.schedule_switch.set_active(cfg.get("schedule", {}).get("enabled", True))
        self.manage_switch.set_active(scfg.get("manage_timeout", True))
        self.blank_spin.set_value(scfg.get("blank_after_minutes", 0))
        self.dpms_spin.set_value(scfg.get("dpms_off_after_minutes", 0))
        self.lock_switch.set_active(scfg.get("disable_lock", True))
        self.keep_gui_switch.set_active(gcfg.get("keep_running", True))
        self.minimized_switch.set_active(gcfg.get("start_minimized", True))
        self.update_switch.set_active(ucfg.get("enabled", False))
        hour, minute = scheduler.parse_hhmm(ucfg.get("time", "03:00"))
        self.update_hour.set_value(hour)
        self.update_minute.set_value(minute)
        for day, button in self.update_days.items():
            button.set_active(day in ucfg.get("days", scheduler.DAYS))
        self.repo_entry.set_text(ucfg.get("repo", ""))
        self.branch_entry.set_text(ucfg.get("branch", "main"))
        self._reload_schedule_rows()
        self._flash("Reloaded the saved settings", ok=True)

    def _on_show_kiosk(self, *_args):
        self.iconify()
        reply = call("open", timeout=30, return_to_home=False)
        if not reply.get("ok"):
            self.present_window()
            self._flash("Could not show the kiosk page: %s"
                        % reply.get("error", "?"), ok=False)

    def _set_update_buttons(self, sensitive):
        self.check_button.set_sensitive(sensitive)
        self.update_button.set_sensitive(sensitive)

    def _on_check_update(self, *_args):
        self._set_update_buttons(False)
        self._flash("Checking for updates...", ok=True)

        def done(reply):
            self._set_update_buttons(True)
            if reply.get("ok"):
                self._flash("Update check: %s" % reply.get("result"), ok=True)
            else:
                self._flash("Update check failed: %s" % reply.get("error"), ok=False)
            self._refresh_status()

        self._call_async("update_check", done, timeout=120,
                         update=self._collect_update())

    def _on_update_now(self, *_args):
        self._set_update_buttons(False)
        self._flash("Downloading the update...", ok=True)

        def done(reply):
            self._set_update_buttons(True)
            if reply.get("ok"):
                self._flash("Update: %s" % reply.get("result"), ok=True)
            else:
                self._flash("Update failed: %s" % reply.get("error"), ok=False)
            self._refresh_status()

        self._call_async("update_now", done, timeout=600,
                         update=self._collect_update())

    def _command(self, command):
        reply = call(command, timeout=30)
        if reply.get("ok"):
            self._flash("%s: done" % command.replace("_", " "), ok=True)
        else:
            self._flash("%s failed: %s" % (command, reply.get("error", "?")),
                        ok=False)
        self._refresh_status()

    def _flash(self, text, ok=True):
        ctx = self.banner.get_style_context()
        ctx.remove_class("status-ok")
        ctx.remove_class("status-bad")
        ctx.add_class("status-ok" if ok else "status-bad")
        self.banner.set_text(text)

    def _refresh_status(self):
        reply = call("status")
        if not reply.get("ok"):
            self.status_label.set_markup(
                "<b>Daemon is not running.</b>\n"
                "Start it with:  systemctl --user start kiosk-manager.service")
            self.update_label.set_text("Installed: %s" % version.describe(
                version.installed_commit()))
            return True
        browser_info = reply.get("browser", {})
        daemon_info = reply.get("daemon", {})
        screen_info = reply.get("screen", {})
        lines = [
            "Daemon: running %s (pid %s, up %s)" % (
                daemon_info.get("version", ""), daemon_info.get("pid"),
                self._duration(daemon_info.get("uptime_seconds", 0))),
            "Kiosk page: %s" % (browser_info.get("url") or "(not set)"),
            "Browser: %s" % ("running (pid %s)" % ", ".join(
                str(p) for p in browser_info.get("pids") or [])
                if browser_info.get("running") else "not running"),
            "Window found: %s" % ("yes" if browser_info.get("window") else "no"),
            "Next scheduled event: %s" % (reply.get("next_run") or "none"),
            "   %s" % (reply.get("next_entry") or ""),
            "Session: %s (DISPLAY=%s)" % (screen_info.get("session_type"),
                                          screen_info.get("display") or "unset"),
            "Screensaver: %s" % (screen_info.get("x_screensaver") or "n/a"),
            "DPMS: %s" % (screen_info.get("dpms") or "n/a"),
            "Last action: %s" % daemon_info.get("last_event", ""),
        ]
        self.status_label.set_text("\n".join(lines))
        self._render_update_status(reply.get("update") or {})
        return True

    def _render_update_status(self, info):
        def outcome(record):
            if not record:
                return "never"
            return "%s (%s)" % (record.get("message", ""), when(record.get("at")))

        latest = info.get("latest")
        lines = [
            "Installed: %s" % version.describe(info.get("installed") or ""),
            "Latest on %s: %s" % (self.branch_entry.get_text().strip() or "main",
                                  latest[:7] if latest else "not checked yet"),
            "Last check: %s" % outcome(info.get("last_check")),
            "Last update: %s" % outcome(info.get("last_update")),
            "Next automatic update: %s" % (info.get("next_run") or "off"),
        ]
        if info.get("pending"):
            lines.append("Installing %s now..." % info["pending"][:7])
        if not info.get("git_available", True):
            lines.append("git is not installed: sudo apt-get install git")
        self.update_label.set_text("\n".join(lines))

    @staticmethod
    def _duration(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return "%ds" % seconds
        if seconds < 3600:
            return "%dm" % (seconds // 60)
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)

    def _on_delete(self, *_args):
        # Never actually quit: the operator gets it back from the taskbar.
        self.iconify()
        return True

    def present_window(self):
        self.set_focus_on_map(True)
        self.deiconify()
        self.present()
        return False


def main(start_minimized=True):
    existing = ipc.send(config.gui_socket(), {"command": "ping"}, timeout=2.0)
    if existing.get("ok"):
        # Single instance: hand the request to the window that is already up.
        if not start_minimized:
            ipc.send(config.gui_socket(), {"command": "show"}, timeout=2.0)
        log.info("settings window already running")
        return 0

    window = KioskWindow(start_minimized=start_minimized)

    def handler(request):
        command = (request or {}).get("command")
        if command == "show":
            GLib.idle_add(window.present_window)
            return {"ok": True}
        if command == "ping":
            return {"ok": True, "pong": True, "pid": os.getpid(),
                    "version": version.RUNNING_COMMIT}
        if command == "quit":
            GLib.idle_add(Gtk.main_quit)
            return {"ok": True}
        return {"ok": False, "error": "unknown command"}

    server = ipc.Server(config.gui_socket(), handler)
    try:
        server.start()
    except OSError as exc:
        log.warning("could not start gui ipc socket: %s", exc)
    Gtk.main()
    server.stop()
    return 0
