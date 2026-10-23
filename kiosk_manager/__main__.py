"""Command line entry point for kiosk-manager."""

import argparse
import json
import subprocess
import sys

from . import config, ipc, logs, procs, version

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


def setup_logging(component, verbose=False, to_file=True):
    return logs.setup(component, verbose=verbose, to_file=to_file)


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
    diag_parser = sub.add_parser(
        "diagnose", help="write a full report of the session, windows and logs")
    diag_parser.add_argument("--print", dest="to_stdout", action="store_true",
                             help="print the report instead of saving it")
    diag_parser.add_argument("--log-lines", type=int, default=120,
                             help="log lines to include (default 120)")
    logs_parser = sub.add_parser("logs", help="print the tail of a log file")
    logs_parser.add_argument("component", nargs="?", default="daemon",
                             choices=logs.COMPONENTS)
    logs_parser.add_argument("-n", "--lines", type=int, default=200)
    logs_parser.add_argument("-p", "--path", action="store_true",
                             help="print the log file path and exit")
    debug_parser = sub.add_parser(
        "debug", help="turn debug logging on or off and reload the daemon")
    debug_parser.add_argument("state", choices=("on", "off"))

    args = parser.parse_args(argv)
    cmd = args.cmd or "gui"

    if cmd == "daemon":
        setup_logging("daemon", args.verbose)
        from . import daemon
        return daemon.main()

    if cmd == "gui":
        setup_logging("gui", args.verbose)
        from . import gui
        return gui.main(show=args.show or args.no_minimize)

    if cmd == "diagnose":
        setup_logging("cli", args.verbose, to_file=False)
        from . import diagnose
        if args.to_stdout:
            print(diagnose.report(args.log_lines))
            return 0
        path = diagnose.write_report(log_lines=args.log_lines)
        print("wrote %s" % path)
        print("send that file, or read it with:  less %s" % path)
        return 0

    if cmd == "logs":
        if args.path:
            print(logs.log_path(args.component))
            return 0
        sys.stdout.write(logs.tail(args.component, args.lines))
        return 0

    if cmd == "debug":
        want = args.state == "on"
        cfg = config.load()
        cfg.setdefault("logging", {})["debug"] = want
        config.save(cfg)
        reply = daemon_call("reload")
        print("debug logging %s (daemon: %s)"
              % (args.state, "reloaded" if reply.get("ok")
                 else reply.get("error", "not running")))
        print("the settings window picks this up when it next starts")
        return 0

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
