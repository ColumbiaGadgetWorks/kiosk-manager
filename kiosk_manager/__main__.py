"""Command line entry point for kiosk-manager."""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys

from . import config, ipc

COMMANDS = {
    "status": "status",
    "open": "open",
    "stop": "stop_browser",
    "restart": "restart_browser",
    "wake": "wake",
    "blank": "blank",
    "apply-screen": "apply_screen",
    "reload": "reload",
}


def setup_logging(verbose=False, to_file=False):
    handlers = [logging.StreamHandler(sys.stderr)]
    if to_file:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        handlers.append(logging.FileHandler(config.LOG_PATH))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def daemon_call(command, **kwargs):
    payload = {"command": command}
    payload.update(kwargs)
    return ipc.send(config.daemon_socket(), payload)


def cmd_show():
    """Raise the config window, starting it if it is not already up."""
    reply = ipc.send(config.gui_socket(), {"command": "show"}, timeout=2.0)
    if reply.get("ok"):
        return 0
    exe = os.environ.get("KIOSK_MANAGER_BIN") or shutil.which("kiosk-manager")
    if exe:
        subprocess.Popen([exe, "gui", "--no-minimize"], start_new_session=True)
    else:
        subprocess.Popen([sys.executable, "-m", "kiosk_manager", "gui",
                          "--no-minimize"], start_new_session=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kiosk-manager",
        description="Kiosk browser manager for Debian touchscreen terminals.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("daemon", help="run the background service")
    gui_parser = sub.add_parser("gui", help="run the configuration window")
    gui_parser.add_argument("--no-minimize", action="store_true",
                            help="open the window instead of starting minimised")
    sub.add_parser("show", help="raise the configuration window")
    for name in COMMANDS:
        sub.add_parser(name, help="send the %s command to the daemon" % name)
    url_parser = sub.add_parser("set-url", help="change the kiosk page")
    url_parser.add_argument("url")
    sub.add_parser("config-path", help="print the config file location")

    args = parser.parse_args(argv)
    cmd = args.cmd or "gui"

    if cmd == "daemon":
        setup_logging(args.verbose, to_file=True)
        from . import daemon
        return daemon.main()

    if cmd == "gui":
        setup_logging(args.verbose)
        from . import gui
        cfg = config.load()
        minimized = cfg.get("gui", {}).get("start_minimized", True)
        if getattr(args, "no_minimize", False):
            minimized = False
        return gui.main(start_minimized=minimized)

    if cmd == "show":
        return cmd_show()

    if cmd == "config-path":
        print(config.CONFIG_PATH)
        return 0

    if cmd == "set-url":
        url = args.url.strip()
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        cfg = config.load()
        cfg["url"] = url
        config.save(cfg)
        reply = daemon_call("reload")
        print("url set to %s (daemon: %s)"
              % (url, "reloaded" if reply.get("ok") else reply.get("error")))
        return 0

    reply = daemon_call(COMMANDS[cmd])
    print(json.dumps(reply, indent=2))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
