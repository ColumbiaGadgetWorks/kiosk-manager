"""Tiny newline-delimited JSON protocol over a unix socket.

Used both for GUI -> daemon commands and for `kiosk-manager show` -> GUI.
"""

import json
import logging
import os
import socket
import threading

log = logging.getLogger("kiosk.ipc")


def send(path, payload, timeout=5.0):
    """Send one request and return the decoded reply (or an error dict)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
    except OSError as exc:
        return {"ok": False, "error": "not running (%s)" % exc}
    try:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        if not raw:
            return {"ok": True}
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            sock.close()
        except OSError:
            pass


class Server(threading.Thread):
    """Accept loop that hands each request dict to `handler`."""

    def __init__(self, path, handler):
        super().__init__(name="ipc-server", daemon=True)
        self.path = path
        self.handler = handler
        self._sock = None
        self._stop = threading.Event()

    def start(self):
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self._sock.listen(8)
        self._sock.settimeout(1.0)
        super().start()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(10.0)
            buf = b""
            while b"\n" not in buf:
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
            if not buf.strip():
                return
            request = json.loads(buf.decode("utf-8", "replace").strip())
            reply = self.handler(request) or {"ok": True}
        except Exception as exc:  # never let a bad request kill the server
            log.exception("ipc request failed")
            reply = {"ok": False, "error": str(exc)}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
