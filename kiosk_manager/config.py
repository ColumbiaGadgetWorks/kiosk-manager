"""Configuration + state paths for kiosk-manager."""

import copy
import json
import os
import tempfile
import uuid

APP = "kiosk-manager"

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP
)
DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PROFILE_DIR = os.path.join(DATA_DIR, "firefox-profile")
LOG_PATH = os.path.join(DATA_DIR, "kiosk-manager.log")


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        base = os.path.join(tempfile.gettempdir(), "kiosk-manager-%d" % os.getuid())
    d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return d


def daemon_socket():
    return os.path.join(runtime_dir(), "daemon.sock")


def gui_socket():
    return os.path.join(runtime_dir(), "gui.sock")


DEFAULT_URL = "https://fundbot.adman.casa/?kiosk"

# URLs older installs wrote (the old startup.sh page, or the installer
# placeholder). They are upgraded to DEFAULT_URL so the fundbot sheet shows its
# kiosk layout; any other URL is left exactly as configured.
LEGACY_URLS = {
    "https://fundbot.adman.casa/",
    "https://fundbot.adman.casa",
    "https://example.com/",
}

DEFAULTS = {
    "url": DEFAULT_URL,
    "browser": {
        "command": "/usr/bin/firefox",
        "kiosk": True,
        "private_window": False,
        "extra_args": [],
        "use_managed_profile": True,
    },
    "boot": {
        "launch_on_boot": True,
        "delay_seconds": 15,
        "restart_if_closed": True,
        "restart_grace_seconds": 20,
    },
    "schedule": {
        "enabled": True,
        "entries": [],
    },
    "screen": {
        "manage_timeout": True,
        # minutes; 0 == never blank / never power off
        "blank_after_minutes": 0,
        "dpms_off_after_minutes": 0,
        "disable_lock": True,
    },
    "gui": {
        "start_minimized": True,
        # Daemon starts the settings window at boot and restarts it if it dies.
        "keep_running": True,
    },
    "update": {
        "enabled": False,
        "time": "03:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "repo": "https://github.com/ColumbiaGadgetWorks/kiosk-manager.git",
        "branch": "main",
    },
}


def new_schedule_entry():
    return {
        "id": uuid.uuid4().hex[:8],
        "enabled": True,
        "time": "08:00",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "wake_screen": True,
        "open_browser": True,
        "return_to_home": True,
    }


def _merge(defaults, loaded):
    out = copy.deepcopy(defaults)
    if not isinstance(loaded, dict):
        return out
    for key, val in loaded.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    cfg = _merge(DEFAULTS, raw)
    if str(cfg.get("url", "")).strip() in LEGACY_URLS:
        cfg["url"] = DEFAULT_URL
    # Repair schedule entries so a hand-edited file can't crash the daemon.
    fixed = []
    for entry in cfg.get("schedule", {}).get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        merged = _merge(new_schedule_entry(), entry)
        merged["id"] = str(merged.get("id") or uuid.uuid4().hex[:8])
        fixed.append(merged)
    cfg["schedule"]["entries"] = fixed
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return cfg
