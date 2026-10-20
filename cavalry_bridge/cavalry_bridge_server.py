# Cavalry Bridge - shared HTTP server, loaded by cavalry_bridge.pyp.
#
# One server per Cinema 4D session, on 127.0.0.1 only:
#   GET  /state   - objects published by the "Cavalry Bridge" tag
#                   (C4D -> Cavalry, polled by the C4D Bridge script)
#   POST /shapes  - shapes sent by "Send Selected to C4D" in Cavalry
#                   (Cavalry -> C4D; each send becomes new Cavalry Spline(s))
# The server runs on background threads; it never touches the document, it
# only swaps data and calls c4d.EventAdd() so objects re-evaluate.
#
# While it runs it also keeps a small presence file in the temp folder
# (cavalry_bridge_c4d.json: port, process id, start time). The Cavalry script
# reads it to connect automatically, and to avoid trying to connect while
# Cinema 4D isn't running (a refused connection blocks Cavalry ~2 s on Windows).
import datetime
import http.server
import json
import math
import os
import tempfile
import threading

import c4d

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1234
PRESENCE_FILE = os.path.join(tempfile.gettempdir(), "cavalry_bridge_c4d.json")

# Core message sent when shapes arrive; the plugin's MessageData (registered
# under this ID at developers.maxon.net as "CavalryBridgeReceiver") turns each
# send into new Cavalry Spline objects.
SHAPES_RECEIVED = 1071604


# ------------------------------------------------- sent hierarchy -> outlines

def _compose(m, t):
    """2D affine (a, b, c, d, tx, ty) of parent m times node transform t
    (position, counter-clockwise rotation in degrees, scale)."""
    r = math.radians(t.get("rot", 0.0))
    sx, sy = t.get("scale", [1.0, 1.0])
    px, py = t.get("pos", [0.0, 0.0])
    la, lb, lc, ld = math.cos(r) * sx, math.sin(r) * sx, -math.sin(r) * sy, math.cos(r) * sy
    a, b, c, d, tx, ty = m
    return (a * la + c * lb, b * la + d * lb, a * lc + c * ld, b * lc + d * ld,
            a * px + c * py + tx, b * px + d * py + ty)


def flatten(nodes, m=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)):
    """World-space shapes ({name, contours}) from a sent hierarchy, for
    Cavalry Splines that follow the latest send."""
    shapes = []
    for node in nodes:
        nm = _compose(m, node.get("transform", {}))
        if node.get("contours"):
            a, b, c, d, tx, ty = nm
            contours = []
            for contour in node["contours"]:
                pts = []
                for p in contour["points"]:
                    q = []
                    for i in range(0, 6, 2):
                        x, y = p[i], p[i + 1]
                        q += [a * x + c * y + tx, b * x + d * y + ty]
                    pts.append(q)
                contours.append({"closed": contour.get("closed", False), "points": pts})
            shapes.append({"id": node.get("id"), "name": node.get("name"), "contours": contours})
        shapes += flatten(node.get("children", []), nm)
    return shapes


# ---------------------------------------------------------------- server

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
            send = {"tree": data["tree"]} if "tree" in data else {"shapes": data["shapes"]}
            flat = flatten(send["tree"]) if "tree" in send else send["shapes"]
        except Exception as e:
            self.send_error(400, str(e))
            return
        # Swapped as a whole, so readers never see a half-written list.
        self.server.shapes = flat
        self.server.shapes_version += 1
        with self.server.lock:
            self.server.pending.append(send)
        # Both thread-safe: the plugin creates the new objects on the main
        # thread, and "Follow Latest Send" objects pick it up in CheckDirty.
        c4d.SpecialEventAdd(SHAPES_RECEIVED)
        c4d.EventAdd()
        self._reply(json.dumps({"received": len(flat)}).encode("utf-8"))

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
    _write_presence(port)
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
        _remove_presence()


def _write_presence(port):
    try:
        with open(PRESENCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid(),
                       "started": datetime.datetime.now().isoformat(timespec="seconds")}, f)
    except OSError as e:
        print("[Cavalry Bridge] cannot write %s (%s)" % (PRESENCE_FILE, e))


def _remove_presence():
    try:
        with open(PRESENCE_FILE, "r", encoding="utf-8") as f:
            mine = json.load(f).get("pid") == os.getpid()
        if mine:  # another Cinema 4D may own it now
            os.remove(PRESENCE_FILE)
    except (OSError, ValueError):
        pass
