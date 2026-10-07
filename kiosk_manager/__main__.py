"""Command line entry point for kiosk-manager."""

import argparse
import json
import logging
import os
import subprocess
import sys

from . import config, ipc, procs, version

COMMANDS = {
    "status": "status",
    "open": "open",
    "stop": "stop_browser",
    "restart": "restart_browser",
    "wake": "wake",
    "blank": "blank",
    "apply-screen": "apply_screen",
    "reload": "reload",
    "update-check": "update_check",
    "update": "update_now",
    "watchdog": "watchdog",
    "minimize": "minimize_browser",
}

# Commands that touch the network or restart the browser need longer.
SLOW_COMMANDS = {"update_check": 120, "update_now": 600, "restart_browser": 30,
                 "stop_browser": 30, "open": 60, "watchdog": 60,
                 "minimize_browser": 10}


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
    return ipc.send(config.daemon_socket(), payload,
                    timeout=SLOW_COMMANDS.get(command, 5.0))


def cmd_show():
    """Raise the config window, starting it if it is not already up."""
    reply = ipc.send(config.gui_socket(), {"command": "show"}, timeout=2.0)
    if reply.get("ok"):
        return 0
    subprocess.Popen(procs.self_command("gui", "--show"),
                     start_new_session=True)
    return 0


# kioskmgr://<action> links from the kiosk page.
URL_ACTIONS = {"show", "minimize", "kiosk"}


def cmd_handle_url(url):
    rest = (url or "").split(":", 1)[-1] if ":" in (url or "") else (url or "")
    action = rest.strip("/").split("/")[0].split("?")[0]
    action = action.lower() or "show"
    if action not in URL_ACTIONS:
        print("unknown kioskmgr action: %s" % action, file=sys.stderr)
        return 1
    if action == "show":
        return cmd_show()
    if action == "minimize":
        reply = daemon_call("minimize_browser")
        if not reply.get("ok"):
            # Daemon down or no window: at least bring the settings up.
            cmd_show()
        return 0
    reply = daemon_call("open", ensure_kiosk=True)
    return 0 if reply.get("ok") else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kiosk-manager",
        description="Kiosk browser manager for Debian touchscreen terminals.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("daemon", help="run the background service")
    gui_parser = sub.add_parser("gui", help="run the configuration window")
    gui_parser.add_argument("--show", action="store_true",
                            help="bring the window to the front when it opens")
    # Older builds started the window with this; same meaning as --show.
    gui_parser.add_argument("--no-minimize", action="store_true",
                            help=argparse.SUPPRESS)
    show_parser = sub.add_parser("show", help="raise the configuration window")
    # Tolerate a kioskmgr:// URL if a desktop handler passes one through.
    show_parser.add_argument("url", nargs="?", help=argparse.SUPPRESS)
    url_parser = sub.add_parser(
        "handle-url", help="run a kioskmgr:// link (show, minimize, kiosk)")
    url_parser.add_argument("url")
    for name in COMMANDS:
        sub.add_parser(name, help="send the %s command to the daemon" % name)
    set_url_parser = sub.add_parser("set-url", help="change the kiosk page")
    set_url_parser.add_argument("url")
    sub.add_parser("config-path", help="print the config file location")
    sub.add_parser("version", help="print the installed version")

    args = parser.parse_args(argv)
    cmd = args.cmd or "gui"

    if cmd == "daemon":
        setup_logging(args.verbose, to_file=True)
        from . import daemon
        return daemon.main()

    if cmd == "gui":
        setup_logging(args.verbose)
        from . import gui
        return gui.main(show=args.show or args.no_minimize)

    if cmd == "show":
        if args.url:
            return cmd_handle_url(args.url)
        return cmd_show()

    if cmd == "handle-url":
        return cmd_handle_url(args.url)

    if cmd == "config-path":
        print(config.CONFIG_PATH)
        return 0

    if cmd == "version":
        print(version.describe(version.installed_commit()))
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
