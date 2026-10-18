# Cavalry Bridge - shared HTTP server, loaded by cavalry_bridge.pyp.
#
# One server per Cinema 4D session, on 127.0.0.1 only:
#   GET  /state   - object transforms published by the "Cavalry Bridge" tag
#                   (C4D -> Cavalry, polled by the C4D Bridge script)
#   POST /shapes  - shape outlines sent by "Send Selected to C4D" in Cavalry
#                   (Cavalry -> C4D; each send becomes a new Cavalry Spline)
# The server runs on background threads; it never touches the document, it
# only swaps data and calls c4d.EventAdd() so objects re-evaluate.
import http.server
import json
import threading

import c4d

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1234

# Core message sent when shapes arrive; the plugin's MessageData (registered
# under this ID at developers.maxon.net as "CavalryBridgeReceiver") turns each
# send into a new Cavalry Spline object.
SHAPES_RECEIVED = 1071604


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/state":
            self.send_error(404)
            return
        self._reply(self.server.state)

    def do_POST(self):
        if self.path.split("?")[0] != "/shapes":
            self.send_error(404)
            return
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            shapes = data["shapes"]
        except Exception as e:
            self.send_error(400, str(e))
            return
        # Swapped as a whole, so readers never see a half-written list.
        self.server.shapes = shapes
        self.server.shapes_version += 1
        with self.server.lock:
            self.server.pending.append(shapes)
        # Both thread-safe: the plugin creates a new Cavalry Spline on the main
        # thread, and "Follow Latest Send" objects pick it up in CheckDirty.
        c4d.SpecialEventAdd(SHAPES_RECEIVED)
        c4d.EventAdd()
        self._reply(json.dumps({"received": len(shapes)}).encode("utf-8"))

    def _reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the C4D console quiet


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, port):
        super().__init__((host, port), _Handler)
        self.state = b'{"objects":[]}'
        self.shapes = []
        self.shapes_version = 0
        self.lock = threading.Lock()
        self.pending = []  # sends not yet turned into objects
        threading.Thread(target=self.serve_forever, name="CavalryBridgeServer", daemon=True).start()


_server = None


def get():
    """The running server, or None."""
    return _server


def ensure(host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Returns a server on host:port, (re)starting it if needed."""
    global _server
    if _server is not None and _server.server_address[:2] == (host, port):
        return _server
    old = _server
    stop()
    try:
        _server = _Server(host, port)
    except OSError as e:
        print("[Cavalry Bridge] cannot serve on %s:%d (%s)" % (host, port, e))
        return None
    if old is not None:  # keep shapes already received
        _server.shapes = old.shapes
        _server.shapes_version = old.shapes_version + 1
    print("[Cavalry Bridge] listening on http://%s:%d" % (host, port))
    return _server


def take_pending():
    """Sends received since the last call, oldest first."""
    if _server is None:
        return []
    with _server.lock:
        pending, _server.pending = _server.pending, []
    return pending


def stop():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
