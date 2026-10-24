# Cavalry Bridge - Cinema 4D plugin (everything in this one file).
#
#   Extensions > Cavalry Bridge   a panel for both directions:
#       Cinema 4D -> Cavalry      "Send Selected to Cavalry" streams splines,
#                                 folders (objects with children) and other
#                                 objects live into Cavalry's C4D Bridge window
#       Cavalry -> Cinema 4D      what arrived last, and "Add Cavalry Spline"
#   Cavalry Spline                the object shapes from Cavalry arrive as: a
#                                 real spline generator (Extrude, Sweep, Loft...
#                                 work directly), Width / Height like a
#                                 Rectangle's so the Scale tool resizes it.
#
# Each send from Cavalry creates new objects in the active document, each
# holding its own copy of the shapes (saved with the scene): one Cavalry
# Spline ("Single object"), or Nulls and a Cavalry Spline per shape with
# their layers' position / rotation / scale and keyframes ("Group").
#
# Cinema 4D -> Cavalry runs through a "Cavalry Bridge" Null whose Python tag
# lists the objects to send; the panel creates and manages it. The tag's
# draw() publishes what the active camera sees (screen positions follow the
# camera exactly), and the bridge's local HTTP server hands that to Cavalry.
#
# Cinema 4D -> Cavalry goes through a file, so nothing runs while nothing
# changes: the bridge tag rewrites cavalry_bridge_state.json (temp folder)
# only when what it sends changes, and Cavalry reads it only when its
# modification time moves - no polling requests, no console noise.
#
# Cavalry -> Cinema 4D goes through the local HTTP server (127.0.0.1 only),
# one request per send:
#   POST /shapes  shapes sent from Cavalry
#   GET  /state   the same data as the state file (for older Cavalry scripts)
# While it runs, a presence file in the temp folder (port, process, start
# time) tells the Cavalry script Cinema 4D is there, so it never waits on a
# refused connection (which blocks Cavalry ~2 s on Windows). Cavalry in turn
# touches a heartbeat file, which the panel shows as "Connected".
import datetime
import http.server
import json
import math
import os
import sys
import tempfile
import threading
import time
import types

import c4d
from c4d import gui, plugins

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# Plugin IDs registered at developers.maxon.net. The object ID is stored in
# every scene that contains a Cavalry Spline, so it must never change.
PLUGIN_ID = 1071603        # Cavalry Spline object ("Cavalry Bridge")
RECEIVER_ID = 1071604      # MessageData + core message ("CavalryBridgeReceiver")
PANEL_ID = 1072165         # Extensions > Cavalry Bridge

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1234
# Files in the temp folder both apps share:
PRESENCE_FILE = os.path.join(tempfile.gettempdir(), "cavalry_bridge_c4d.json")       # C4D is running (port)
STATE_FILE = os.path.join(tempfile.gettempdir(), "cavalry_bridge_state.json")        # what C4D sends live
HEARTBEAT_FILE = os.path.join(tempfile.gettempdir(), "cavalry_bridge_cavalry.json")  # Cavalry is listening
HEARTBEAT_SECONDS = 6.0   # Cavalry touches its file every 2 s

# Private container ID holding a send's shapes as JSON (not in the
# description, so it is saved and copied with the object but not shown).
SHAPES_DATA = 10100

# C4D Bezier tangents are 3/4 the length of a cubic Bezier control offset
# (C4D's own circle: 41.4 for radius 100, not 55.2); Cavalry handles are
# absolute control points.
TANGENT_SCALE = 0.75


# =========================================================== local server

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


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/state":
            self.send_error(404)
            return
        self.server.last_get = time.time()
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
        self.server.last_send = (time.time(), len(flat))
        with self.server.lock:
            self.server.pending.append(send)
        # Both thread-safe: the receiver creates the new objects on the main
        # thread, and "Follow Latest Send" objects pick it up in CheckDirty.
        c4d.SpecialEventAdd(RECEIVER_ID)
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
        self.last_get = 0.0
        self.last_send = None   # (time, shape count)
        self.lock = threading.Lock()
        self.pending = []       # sends not yet turned into objects
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
        _server.shapes, _server.last_send = old.shapes, old.last_send
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


def _remove_state():
    """A state file left by an earlier session (or a crash) would show stale
    objects in Cavalry; start clean."""
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


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


# ============================================ Cavalry keyframes -> C4D keys
#
# Cavalry sends, per attribute, its keyframes ([frame, value]) and the value
# on every frame from the first to the last key. Cavalry's easing curves
# can't be read directly, so each segment between two keys gets the Bezier
# handles (length and height) that best fit the sampled curve - the way an
# animator shapes an ease in the F-Curve editor. Where no single curve fits
# (Bounce, Spring, Step...), that segment gets a key on every frame instead.

# Cavalry attribute -> (C4D vector parameter, component, value conversion,
# error that is never visible: 0.25 px, 0.05 degrees, 0.1 % scale)
TRACKS = {
    "position.x": (c4d.ID_BASEOBJECT_REL_POSITION, c4d.VECTOR_X, lambda v: v, 0.25),
    "position.y": (c4d.ID_BASEOBJECT_REL_POSITION, c4d.VECTOR_Y, lambda v: v, 0.25),
    # Cavalry rotates counter-clockwise; a positive C4D bank turns clockwise.
    "rotation.z": (c4d.ID_BASEOBJECT_REL_ROTATION, c4d.VECTOR_Z, lambda v: -math.radians(v), math.radians(0.05)),
    "scale.x": (c4d.ID_BASEOBJECT_REL_SCALE, c4d.VECTOR_X, lambda v: v, 0.001),
    "scale.y": (c4d.ID_BASEOBJECT_REL_SCALE, c4d.VECTOR_Y, lambda v: v, 0.001),
}

# A fitted segment may be off by this fraction of the attribute's overall
# range (or the invisible amount above, whichever is larger) before it is
# baked frame by frame instead.
FIT_TOLERANCE = 0.005


def apply_static(op, transform):
    """Sets the layer's transform at the time of the send."""
    op[c4d.ID_BASEOBJECT_REL_POSITION] = c4d.Vector(transform["pos"][0], transform["pos"][1], 0)
    op[c4d.ID_BASEOBJECT_REL_ROTATION] = c4d.Vector(0, 0, -math.radians(transform["rot"]))
    op[c4d.ID_BASEOBJECT_REL_SCALE] = c4d.Vector(transform["scale"][0], transform["scale"][1], 1)


def apply_animation(op, anim):
    """Creates PSR tracks on op from a node's `anim` block."""
    fps = float(anim.get("fps") or 25)
    for attr, data in anim.get("attrs", {}).items():
        if attr not in TRACKS or not data.get("keys"):
            continue
        param, component, convert, invisible = TRACKS[attr]
        keys = [(f, convert(v)) for f, v in data["keys"]]
        samples = [convert(v) for v in data.get("samples", [])]
        _build_curve(op, param, component, keys, data.get("first", keys[0][0]), samples, fps, invisible)


def _track(op, param, component):
    desc = c4d.DescID(c4d.DescLevel(param, c4d.DTYPE_VECTOR, 0),
                      c4d.DescLevel(component, c4d.DTYPE_REAL, 0))
    track = op.FindCTrack(desc)
    if track is None:
        track = c4d.CTrack(op, desc)
        op.InsertTrackSorted(track)
    return track.GetCurve()


def _add_key(curve, frame, value, fps):
    key = curve.AddKey(c4d.BaseTime(frame / fps))["key"]
    key.SetValue(curve, value)
    return key


def _segment_rows(fa, fb, first, samples):
    rows = []
    for f in range(fa + 1, fb):
        i = f - first
        if 0 <= i < len(samples):
            rows.append(((f - fa) / float(fb - fa), samples[i]))
    return rows


def _bezier_u(x, ta, tb):
    """Curve parameter where the time component (0..1, handles at ta and
    1 - tb) reaches x; that component is monotonic for ta, tb in (0, 1)."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        u = (lo + hi) / 2
        m = 1 - u
        if 3 * m * m * u * ta + 3 * m * u * u * (1 - tb) + u ** 3 < x:
            lo = u
        else:
            hi = u
    return (lo + hi) / 2


def _fit_bezier(va, vb, rows, ta, tb):
    """Best value offsets (da, db) of the two handles for handle lengths ta,
    tb (fractions of the segment); returns (da, db, max error)."""
    eqs = []
    for x, y in rows:
        u = _bezier_u(x, ta, tb)
        m = 1 - u
        b0, b1, b2, b3 = m ** 3, 3 * m * m * u, 3 * m * u * u, u ** 3
        eqs.append((b1, b2, y - (b0 + b1) * va - (b2 + b3) * vb))
    a11 = sum(e[0] * e[0] for e in eqs)
    a12 = sum(e[0] * e[1] for e in eqs)
    a22 = sum(e[1] * e[1] for e in eqs)
    c1 = sum(e[0] * e[2] for e in eqs)
    c2 = sum(e[1] * e[2] for e in eqs)
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-12:
        return 0.0, 0.0, float("inf")
    da = (c1 * a22 - c2 * a12) / det
    db = (a11 * c2 - a12 * c1) / det
    return da, db, max(abs(e[0] * da + e[1] * db - e[2]) for e in eqs)


def _fit_handles(fa, va, fb, vb, first, samples, tolerance):
    """Bezier handles (frames, value) for the segment fa..fb that follow the
    samples within tolerance, or None. Tries handles at a third of the
    segment first, then searches handle lengths (how eases are shaped)."""
    span = fb - fa
    rows = _segment_rows(fa, fb, first, samples)
    if len(rows) < 2:  # too few frames to tell: straight line
        d = (vb - va) / 3.0
        return (span / 3.0, d), (-span / 3.0, -d)
    best = None
    grid = [(1 / 3.0, 1 / 3.0)] + [(a / 10.0, b / 10.0) for a in range(1, 10) for b in range(1, 10)]
    for ta, tb in grid:
        da, db, err = _fit_bezier(va, vb, rows, ta, tb)
        if best is None or err < best[0]:
            best = (err, ta, tb, da, db)
        if ta == tb == 1 / 3.0 and err <= tolerance:
            break
    err, ta, tb, da, db = best
    if err > tolerance:  # refine around the best grid point
        for ra in (-0.04, -0.02, 0.02, 0.04):
            for rb in (-0.04, -0.02, 0, 0.02, 0.04):
                a, b = min(max(ta + ra, 0.01), 0.99), min(max(tb + rb, 0.01), 0.99)
                e = _fit_bezier(va, vb, rows, a, b)
                if e[2] < best[0]:
                    best = (e[2], a, b, e[0], e[1])
        err, ta, tb, da, db = best
    if err > tolerance:
        return None
    return (ta * span, da), (-tb * span, db)


def _set_tangents(curve, key, left, right, fps):
    """left/right: (frames, value) Bezier handle offsets."""
    key.ChangeNBit(c4d.NBIT_CKEY_AUTO, c4d.NBITCONTROL_CLEAR)
    key.ChangeNBit(c4d.NBIT_CKEY_BREAK, c4d.NBITCONTROL_SET)
    key.SetTimeLeft(curve, c4d.BaseTime(left[0] / fps))
    key.SetValueLeft(curve, left[1])
    key.SetTimeRight(curve, c4d.BaseTime(right[0] / fps))
    key.SetValueRight(curve, right[1])


def _build_curve(op, param, component, keys, first, samples, fps, invisible):
    curve = _track(op, param, component)
    values = [v for _, v in keys] + samples
    tolerance = max((max(values) - min(values)) * FIT_TOLERANCE, invisible)

    c4d_keys = [_add_key(curve, f, v, fps) for f, v in keys]
    lefts = [(0, 0)] * len(keys)    # flat before the first key...
    rights = [(0, 0)] * len(keys)   # ...and after the last
    baked = [False] * len(keys)     # segment starting at this key is baked
    for s in range(len(keys) - 1):
        (fa, va), (fb, vb) = keys[s], keys[s + 1]
        handles = _fit_handles(fa, va, fb, vb, first, samples, tolerance)
        if handles is not None:
            rights[s], lefts[s + 1] = handles
            continue
        # No single curve follows this stretch: key every frame (linear).
        baked[s] = True
        for f in range(fa + 1, fb):
            i = f - first
            if 0 <= i < len(samples):
                _add_key(curve, f, samples[i], fps).SetInterpolation(curve, c4d.CINTERPOLATION_LINEAR)

    for s, key in enumerate(c4d_keys):
        # A key's interpolation shapes the segment after it; its left handle
        # still shapes the one before, so set handles on every key.
        key.SetInterpolation(curve, c4d.CINTERPOLATION_LINEAR if baked[s] else c4d.CINTERPOLATION_SPLINE)
        _set_tangents(curve, key, lefts[s], rights[s], fps)


# ======================================== Cinema 4D -> Cavalry (bridge tag)

BRIDGE_NAME = "Cavalry Bridge"
UD_ENABLED, UD_HOST, UD_PORT, UD_OBJECTS = 1, 2, 3, 4   # the bridge tag's user data
SAMPLES_PER_POINT = 8   # for spline types without Bezier handles

# The bridge tag's code: the work is done here, in the plugin.
TAG_CODE = '''# Cavalry Bridge - publishes the objects listed in this tag's user data to
# Cavalry. The work is done by the cavalry_bridge plugin; manage the list from
# Extensions > Cavalry Bridge.
import c4d
try:
    import cavalry_bridge_runtime as bridge
except ImportError:
    bridge = None


def main():
    pass


def draw(bd):
    return bridge.tag_draw(op, bd) if bridge else True
'''


def _ud(tag, bc_id, default):
    try:
        v = tag[c4d.ID_USERDATA, bc_id]
        return default if v is None else v
    except Exception:
        return default


def _to_frame(bd, sf, p):
    """World point -> normalised safe-frame coords (0..1, y down)."""
    s = bd.WS(p)
    w = max(sf["cr"] - sf["cl"], 1)
    h = max(sf["cb"] - sf["ct"], 1)
    return (s.x - sf["cl"]) / w, (s.y - sf["ct"]) / h


def _segments(rs):
    """(first point index, point count, closed) for each segment."""
    if rs.GetSegmentCount() == 0:
        return [(0, rs.GetPointCount(), rs.IsClosed())]
    out, start = [], 0
    for s in range(rs.GetSegmentCount()):
        seg = rs.GetSegment(s)
        out.append((start, seg["cnt"], seg["closed"]))
        start += seg["cnt"]
    return out


def _spline_data(obj, bd, sf):
    """World-space Bezier segments of a spline object (or None), each point
    as (position, in handle, out handle), in world XY and on screen."""
    rs = obj.GetRealSpline()
    if rs is None or rs.GetPointCount() == 0:
        return None
    mg = obj.GetMg()  # GetRealSpline points are in the object's space
    stype = rs.GetInterpolationType()
    segments = []
    for s, (start, count, closed) in enumerate(_segments(rs)):
        triples = []
        if stype in (c4d.SPLINETYPE_BEZIER, c4d.SPLINETYPE_LINEAR):
            for i in range(start, start + count):
                p = rs.GetPoint(i)
                if stype == c4d.SPLINETYPE_BEZIER:
                    t = rs.GetTangent(i)
                    triples.append((p, p + t["vl"] / TANGENT_SCALE, p + t["vr"] / TANGENT_SCALE))
                else:
                    triples.append((p, p, p))
        else:
            # Cubic / Akima / B-spline: no handles to carry, so sample it.
            n = max(count * SAMPLES_PER_POINT, 2)
            for k in range(n):
                q = rs.GetSplinePoint(k / float(n if closed else n - 1), s)
                triples.append((q, q, q))
        world, screen = [], []
        for p, h_in, h_out in triples:
            p, h_in, h_out = mg * p, mg * h_in, mg * h_out
            world.append([p.x, p.y, h_in.x, h_in.y, h_out.x, h_out.y])
            screen.append([*_to_frame(bd, sf, p), *_to_frame(bd, sf, h_in), *_to_frame(bd, sf, h_out)])
        segments.append({"closed": bool(closed), "world": world, "screen": screen})
    return segments


# ----------------------------------------------- polygon objects -> outlines
#
# A Plane, Cone, Torus, Extrude... has no spline to send, so it travels as
# its outline: everything its faces cover, as the camera sees it (and, for
# World XY, as seen straight down the Z axis). The faces are filled into a
# fine grid, the edge of the filled area is traced (holes, like a torus's,
# come out as their own loops), straightened, and its corners are snapped
# back onto the mesh's own points, so a Plane arrives as its four corners.

OUTLINE_CELLS = 400            # grid size along the longer side of the outline
MAX_OUTLINE_TRIANGLES = 120000 # above this, a convex outline keeps C4D responsive
_outline_cache = {}            # (object GUID, view) -> (state key, loops)


def _polygon_parts(op, mg):
    """(PolygonObject, global matrix) pairs that make up what `op` shows:
    its own polygons, or its generator / deformer cache."""
    cache = op.GetDeformCache() or op.GetCache()
    if cache is not None:
        return _cache_parts(cache, mg)
    if isinstance(op, c4d.PolygonObject) and op.GetPolygonCount():
        return [(op, mg)]
    return []


def _cache_parts(node, parent_mg):
    parts = []
    while node:
        m = parent_mg * node.GetMl()
        parts += _polygon_parts(node, m)
        if node.GetDown():
            parts += _cache_parts(node.GetDown(), m)
        node = node.GetNext()
    return parts


def _mesh(parts):
    """All points (world space) and triangles (point index triples) of the parts."""
    pts, tris = [], []
    for p, mg in parts:
        base = len(pts)
        pts += [mg * v for v in p.GetAllPoints()]
        for poly in p.GetAllPolygons():
            a, b, c, d = base + poly.a, base + poly.b, base + poly.c, base + poly.d
            tris.append((a, b, c))
            if poly.c != poly.d:
                tris.append((a, c, d))
    return pts, tris


def _convex_hull(pts):
    """The 2D convex hull of pts (monotone chain)."""
    order = sorted(set(pts))
    if len(order) < 3:
        return order

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in order:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(order):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _fill(pts, tris, ox, oy, cell, w, h):
    """Rows of a w x h grid as bit masks: bit i of row j is set when the centre
    of cell (i, j) lies inside any triangle."""
    rows = [0] * h
    g = [((x - ox) / cell - 0.5, (y - oy) / cell - 0.5) for x, y in pts]
    for t in tris:
        a, b, c = g[t[0]], g[t[1]], g[t[2]]
        y0 = max(math.ceil(min(a[1], b[1], c[1])), 0)
        y1 = min(math.floor(max(a[1], b[1], c[1])), h - 1)
        for j in range(y0, y1 + 1):
            lo, hi = float("inf"), float("-inf")
            for (xa, ya), (xb, yb) in ((a, b), (b, c), (c, a)):
                if (ya <= j <= yb) or (yb <= j <= ya):
                    if ya == yb:
                        xs = (xa, xb)
                    else:
                        xs = (xa + (j - ya) * (xb - xa) / (yb - ya),)
                    lo, hi = min(lo, *xs), max(hi, *xs)
            i0, i1 = max(math.ceil(lo), 0), min(math.floor(hi), w - 1)
            if i0 <= i1:
                rows[j] |= ((1 << (i1 - i0 + 1)) - 1) << i0
    return rows


def _bits(m):
    while m:
        low = m & -m
        yield low.bit_length() - 1
        m ^= low


def _trace(rows):
    """Closed loops (grid corner coordinates) around the filled cells, filled
    side always on the same hand, so holes wind the other way."""
    nxt = {}
    above = 0
    for j, row in enumerate(rows + [0]):
        for i in _bits(row & ~above):          # top edges
            nxt.setdefault((i + 1, j), []).append((i, j))
        for i in _bits(above & ~row):          # bottom edges of the row above
            nxt.setdefault((i, j), []).append((i + 1, j))
        above = row
    for j, row in enumerate(rows):
        for i in _bits(row & ~(row << 1)):     # left edges
            nxt.setdefault((i, j), []).append((i, j + 1))
        for i in _bits(row & ~(row >> 1)):     # right edges
            nxt.setdefault((i + 1, j + 1), []).append((i + 1, j))
    loops = []
    for start in list(nxt):
        while nxt.get(start):
            loop, prev, cur = [start], start, nxt[start].pop()
            while cur != start:
                outs = nxt[cur]
                if len(outs) > 1:
                    # Two cells touching only at a corner: turn so they stay apart.
                    dx, dy = cur[0] - prev[0], cur[1] - prev[1]
                    outs.sort(key=lambda q: dx * (q[1] - cur[1]) - dy * (q[0] - cur[0]))
                loop.append(cur)
                prev, cur = cur, outs.pop()
            loops.append(loop)
    return loops


def _corners(loop):
    """Drops points in the middle of straight runs."""
    n = len(loop)
    out = []
    for k in range(n):
        a, b, c = loop[k - 1], loop[k], loop[(k + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]):
            out.append(b)
    return out


def _simplify(pts, tol):
    """Douglas-Peucker on a closed loop."""
    if len(pts) < 4:
        return pts

    def dp(seg):
        (ax, ay), (bx, by) = seg[0], seg[-1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1e-12
        far, far_d = 0, -1.0
        for k in range(1, len(seg) - 1):
            d = abs((seg[k][0] - ax) * dy - (seg[k][1] - ay) * dx) / length
            if d > far_d:
                far, far_d = k, d
        if far_d <= tol:
            return [seg[0]]
        return dp(seg[:far + 1]) + dp(seg[far:])
    first = pts[0]
    far = max(range(len(pts)), key=lambda k: (pts[k][0] - first[0]) ** 2 + (pts[k][1] - first[1]) ** 2)
    return dp(pts[:far + 1]) + dp(pts[far:] + [first])


def _outline_loops(pts, tris):
    """Outline loops ([(x, y), ...]) of the area 2D triangles cover."""
    if not tris:
        return []
    used = {i for t in tris for i in t}
    xs = [pts[i][0] for i in used]
    ys = [pts[i][1] for i in used]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    span = max(maxx - minx, maxy - miny)
    if span <= 1e-9:
        return []
    if len(tris) > MAX_OUTLINE_TRIANGLES:
        return [_convex_hull([pts[i] for i in used])]
    cell = span / OUTLINE_CELLS
    ox, oy = minx - cell, miny - cell
    w = int((maxx - minx) / cell) + 3
    h = int((maxy - miny) / cell) + 3
    rows = _fill(pts, tris, ox, oy, cell, w, h)

    # Mesh points by grid cell, to snap the traced corners onto.
    near = {}
    for i in used:
        x, y = pts[i]
        near.setdefault((int((x - ox) / cell), int((y - oy) / cell)), []).append((x, y))

    def snap(p):
        x, y = ox + p[0] * cell, oy + p[1] * cell
        ci, cj = int(p[0]), int(p[1])
        best, best_d = (x, y), (2.5 * cell) ** 2
        for di in (-3, -2, -1, 0, 1, 2):
            for dj in (-3, -2, -1, 0, 1, 2):
                for q in near.get((ci + di, cj + dj), ()):
                    d = (q[0] - x) ** 2 + (q[1] - y) ** 2
                    if d < best_d:
                        best, best_d = q, d
        return best

    loops = []
    for loop in _trace(rows):
        loop = _corners(loop)
        area = sum(loop[k - 1][0] * loop[k][1] - loop[k][0] * loop[k - 1][1] for k in range(len(loop))) / 2
        if abs(area) < 3:
            continue   # a speck, not a shape
        snapped = []
        for p in _simplify(loop, 1.5):   # the traced edge strays up to a cell from the real one
            q = snap(p)
            if not snapped or snapped[-1] != q:
                snapped.append(q)
        if len(snapped) > 1 and snapped[0] == snapped[-1]:
            snapped.pop()
        snapped = _simplify(snapped, 0.05 * cell)
        if len(snapped) >= 3:
            loops.append(snapped)
    return loops


def _outline_data(obj, bd, sf, cam):
    """Outline loops of a polygon object in the same format as _spline_data
    (straight segments; screen loops for camera view, world loops for World
    XY), or None."""
    parts = _polygon_parts(obj, obj.GetMg())
    if not parts:
        return None
    guid = str(obj.GetGUID())
    obj_key = (obj.GetDirty(c4d.DIRTYFLAGS_DATA | c4d.DIRTYFLAGS_MATRIX | c4d.DIRTYFLAGS_CACHE),
               obj.GetHDirty(c4d.HDIRTYFLAGS_OBJECT))
    cam_mg = cam.GetMg()
    view_key = obj_key + (
        tuple(round(v, 4) for vec in (cam_mg.off, cam_mg.v1, cam_mg.v2, cam_mg.v3) for v in (vec.x, vec.y, vec.z)),
        cam.GetDirty(c4d.DIRTYFLAGS_DATA), tuple(sorted(sf.items())))
    world_hit = _outline_cache.get((guid, "world"))
    view_hit = _outline_cache.get((guid, "view"))
    if world_hit and world_hit[0] == obj_key and view_hit and view_hit[0] == view_key:
        return view_hit[1] + world_hit[1] or None

    pts, tris = _mesh(parts)
    if not (world_hit and world_hit[0] == obj_key):
        loops = _outline_loops([(v.x, v.y) for v in pts], tris)
        world_hit = (obj_key, [{"closed": True, "world": [[x, y] * 3 for x, y in loop]} for loop in loops])
        _outline_cache[(guid, "world")] = world_hit
    if not (view_hit and view_hit[0] == view_key):
        fw = max(sf["cr"] - sf["cl"], 1)
        fh = max(sf["cb"] - sf["ct"], 1)
        if cam.CheckType(c4d.Ocamera) and cam[c4d.CAMERA_PROJECTION] == c4d.Pperspective:
            # Faces reaching behind the camera can't be projected.
            cam_inv = ~cam_mg
            ahead = [(cam_inv * v).z > 0 for v in pts]
            view_tris = [t for t in tris if ahead[t[0]] and ahead[t[1]] and ahead[t[2]]]
        else:
            view_tris = tris
        px = []
        for v in pts:
            sx, sy = _to_frame(bd, sf, v)
            px.append((sx * fw, sy * fh))   # square pixels, so the grid isn't stretched
        loops = _outline_loops(px, view_tris)
        view_hit = (view_key, [{"closed": True, "screen": [[x / fw, y / fh] * 3 for x, y in loop]}
                               for loop in loops])
        _outline_cache[(guid, "view")] = view_hit
    return view_hit[1] + world_hit[1] or None


def tag_draw(tag, bd):
    """The bridge tag's draw(): publishes the listed objects (and everything
    inside them) as the active camera sees them."""
    doc = tag.GetDocument()
    if doc is None or bd != doc.GetActiveBaseDraw() or not _ud(tag, UD_ENABLED, True):
        return True
    # Viewport captures and some renders draw a temporary copy of the scene,
    # whose objects have different GUIDs; only publish the real document.
    if doc != c4d.documents.GetActiveDocument():
        return True
    srv = ensure(_ud(tag, UD_HOST, DEFAULT_HOST) or DEFAULT_HOST, int(_ud(tag, UD_PORT, DEFAULT_PORT)))
    inex = _ud(tag, UD_OBJECTS, None)
    if srv is None or inex is None:
        return True

    cam = bd.GetSceneCamera(doc) or bd.GetEditorCamera()
    cam_inv = ~cam.GetMg()
    sf = bd.GetSafeFrame()
    frame_w = max(sf["cr"] - sf["cl"], 1)
    frame_h = max(sf["cb"] - sf["ct"], 1)
    # Front, Top and other flat views see everything, wherever the view's
    # camera happens to sit; only a perspective camera has a "behind".
    perspective = not cam.CheckType(c4d.Ocamera) or cam[c4d.CAMERA_PROJECTION] == c4d.Pperspective

    def entry(obj, parent):
        """The object itself: geometry for splines, a transform otherwise."""
        mg = obj.GetMg()
        p = mg.off
        base = {"id": str(obj.GetGUID()), "name": obj.GetName(), "parent": parent,
                "visible": obj.GetEditorMode() != c4d.MODE_OFF,
                "depth": (cam_inv * p).z if perspective else 1.0}
        # Splines travel as their curves, polygon objects as their outline as
        # the camera sees it; Cavalry draws both as editable shapes.
        spline = _spline_data(obj, bd, sf)
        if spline is None:
            spline = _outline_data(obj, bd, sf, cam)
        if spline is not None:
            base.update(kind="spline", spline=spline)
            return base
        hpb = c4d.utils.MatrixToHPB(mg)
        sx, sy = _to_frame(bd, sf, p)
        # Screen-space rotation/size from the projected local X axis
        # (100 scene units long), so perspective is taken into account.
        ax, ay = _to_frame(bd, sf, p + mg.v1.GetNormalized() * 100.0)
        dx, dy = (ax - sx) * frame_w, (ay - sy) * frame_h
        base.update(kind="object",
                    pos=[p.x, p.y, p.z],
                    rot=[math.degrees(hpb.x), math.degrees(hpb.y), math.degrees(hpb.z)],
                    scale=[mg.v1.GetLength(), mg.v2.GetLength(), mg.v3.GetLength()],
                    screen=[sx, sy],
                    screenRot=math.degrees(math.atan2(-dy, dx)),
                    screenScale=math.hypot(dx, dy) / frame_w)  # frame widths per 100 units
        return base

    # Linked objects with children travel as folders: Cavalry rebuilds the
    # hierarchy as groups. A folder is purely structural (no transform of its
    # own), because the geometry already arrives in world / screen space. A
    # spline that has children shows up inside its own folder.
    objects = []
    seen = set()

    def walk(obj, parent):
        guid = str(obj.GetGUID())
        if guid in seen:   # linked twice, or also inside another linked folder
            return
        seen.add(guid)
        children = obj.GetChildren()
        if not children:
            objects.append(entry(obj, parent))
            return
        folder = guid + "#folder"
        objects.append({"id": folder, "name": obj.GetName(), "parent": parent, "kind": "folder",
                        "visible": obj.GetEditorMode() != c4d.MODE_OFF})
        if obj.GetRealSpline() is not None or (not obj.CheckType(c4d.Onull) and _polygon_parts(obj, obj.GetMg())):
            # A spline or a shape with children shows up inside its own
            # folder; Nulls need no layer of their own.
            objects.append(entry(obj, folder))
        for child in children:
            walk(child, folder)

    for i in range(inex.GetObjectCount()):
        obj = inex.ObjectFromIndex(doc, i)
        if obj is not None:
            walk(obj, None)

    state = json.dumps({
        "frame": doc.GetTime().GetFrame(doc.GetFps()),
        "fps": doc.GetFps(),
        "aspect": frame_w / frame_h,
        "objects": objects,
    }, separators=(",", ":")).encode("utf-8")
    srv.state = state
    publish_state(state)
    return True


_published = None


def publish_state(state):
    """Writes the state file - only when it changed, so Cavalry has nothing
    to do while nothing changes. Replaced in one step, so Cavalry never reads
    half a file; if Cavalry is reading it right then, the next redraw retries."""
    global _published
    if state == _published:
        return
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(state)
        os.replace(tmp, STATE_FILE)
        _published = state
    except OSError:
        pass


def cavalry_connected():
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_FILE) < HEARTBEAT_SECONDS
    except OSError:
        return False


def _all_objects(op):
    while op:
        yield op
        yield from _all_objects(op.GetDown())
        op = op.GetNext()


def find_bridge_tag(doc):
    """The document's bridge tag (also one made by an older version), or None."""
    for obj in _all_objects(doc.GetFirstObject()):
        for tag in obj.GetTags():
            if tag.CheckType(c4d.Tpython) and "Cavalry Bridge" in (tag[c4d.TPYTHON_CODE] or ""):
                return tag
    return None


def _add_ud(tag, dtype, name, value):
    bc = c4d.GetCustomDataTypeDefault(dtype)
    bc[c4d.DESC_NAME] = name
    bc[c4d.DESC_SHORT_NAME] = name
    tag[tag.AddUserData(bc)] = value


def ensure_bridge_tag(doc):
    """The bridge tag, creating its Null (with undo) if the scene has none,
    and bringing an older version's tag code up to date."""
    tag = find_bridge_tag(doc)
    if tag is not None:
        if tag[c4d.TPYTHON_CODE] != TAG_CODE:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, tag)
            tag[c4d.TPYTHON_CODE] = TAG_CODE
        return tag
    null = c4d.BaseObject(c4d.Onull)
    null.SetName(BRIDGE_NAME)
    null[c4d.NULLOBJECT_DISPLAY] = c4d.NULLOBJECT_DISPLAY_NONE
    tag = c4d.BaseTag(c4d.Tpython)
    tag.SetName(BRIDGE_NAME)
    tag[c4d.TPYTHON_CODE] = TAG_CODE
    null.InsertTag(tag)
    # IDs come out as 1..4 = UD_ENABLED, UD_HOST, UD_PORT, UD_OBJECTS.
    _add_ud(tag, c4d.DTYPE_BOOL, "Enabled", True)
    _add_ud(tag, c4d.DTYPE_STRING, "Host", DEFAULT_HOST)
    _add_ud(tag, c4d.DTYPE_LONG, "Port", DEFAULT_PORT)
    _add_ud(tag, c4d.CUSTOMDATATYPE_INEXCLUDE_LIST, "Objects", c4d.InExcludeData())
    doc.InsertObject(null)
    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, null)
    return tag


def linked_objects(doc, tag):
    inex = _ud(tag, UD_OBJECTS, None) if tag else None
    if inex is None:
        return []
    return [o for o in (inex.ObjectFromIndex(doc, i) for i in range(inex.GetObjectCount())) if o is not None]


# ============================================================ Cavalry Spline

def _to_plane(v, plane):
    """XY-plane vector -> the chosen plane (same mapping as C4D primitives)."""
    if plane == c4d.PRIM_PLANE_ZY:
        return c4d.Vector(-v.z, v.y, v.x)
    if plane == c4d.PRIM_PLANE_XZ:
        return c4d.Vector(v.x, -v.z, v.y)
    return v


def _wanted(shape, names):
    return not names or shape.get("name", "").strip().lower() in names


def natural_size(shapes):
    """Width and height of the outlines' points, as sent (before Width /
    Height resize them)."""
    xs = [p[0] for s in shapes for c in s.get("contours", []) for p in c.get("points", [])]
    ys = [p[1] for s in shapes for c in s.get("contours", []) for p in c.get("points", [])]
    if not xs:
        return 0.0, 0.0
    return max(xs) - min(xs), max(ys) - min(ys)


class CavalrySpline(plugins.ObjectData):

    def __init__(self):
        self._version = None  # shapes_version last seen (Follow Latest Send)
        self._parsed_src = None
        self._parsed = []

    def Init(self, op, isCloneInit=False):
        self.InitAttr(op, str, c4d.CAVALRYSPLINE_LAYERS)
        self.InitAttr(op, float, c4d.CAVALRYSPLINE_WIDTH)
        self.InitAttr(op, float, c4d.CAVALRYSPLINE_HEIGHT)
        self.InitAttr(op, bool, c4d.CAVALRYSPLINE_FOLLOW)
        self.InitAttr(op, int, c4d.PRIM_PLANE)
        self.InitAttr(op, bool, c4d.PRIM_REVERSE)
        self.InitAttr(op, int, c4d.SPLINEOBJECT_INTERPOLATION)
        self.InitAttr(op, int, c4d.SPLINEOBJECT_SUB)
        self.InitAttr(op, float, c4d.SPLINEOBJECT_ANGLE)
        self.InitAttr(op, float, c4d.SPLINEOBJECT_MAXIMUMLENGTH)
        if not isCloneInit:
            op[c4d.CAVALRYSPLINE_LAYERS] = ""
            op[c4d.CAVALRYSPLINE_WIDTH] = 0.0    # 0 = as sent
            op[c4d.CAVALRYSPLINE_HEIGHT] = 0.0
            op[c4d.CAVALRYSPLINE_FOLLOW] = True
            op[c4d.PRIM_PLANE] = c4d.PRIM_PLANE_XY
            op[c4d.PRIM_REVERSE] = False
            op[c4d.SPLINEOBJECT_INTERPOLATION] = c4d.SPLINEOBJECT_INTERPOLATION_ADAPTIVE
            op[c4d.SPLINEOBJECT_SUB] = 8
            op[c4d.SPLINEOBJECT_ANGLE] = c4d.utils.DegToRad(5.0)
            op[c4d.SPLINEOBJECT_MAXIMUMLENGTH] = 5.0
        return True

    def CheckDirty(self, op, doc):
        if not op[c4d.CAVALRYSPLINE_FOLLOW]:
            return  # own copy of the shapes; changes to it dirty the object anyway
        version = _server.shapes_version if _server else None
        if version != self._version:
            self._version = version
            op.SetDirty(c4d.DIRTYFLAGS_DATA)

    def _shapes(self, op):
        if op[c4d.CAVALRYSPLINE_FOLLOW]:
            return _server.shapes if _server else []
        src = op.GetDataInstance().GetString(SHAPES_DATA)
        if src != self._parsed_src:  # parse once per change, not every rebuild
            self._parsed = json.loads(src) if src else []
            self._parsed_src = src
        return self._parsed

    def GetContour(self, op, doc, lod, bt):
        try:
            return self._contour(op)
        except Exception as e:  # never let a bad packet break the object
            print("[Cavalry Bridge] Cavalry Spline: %s" % e)
            return None

    def _contour(self, op):
        shapes = self._shapes(op)
        names = {n.strip().lower() for n in (op[c4d.CAVALRYSPLINE_LAYERS] or "").split(",") if n.strip()}
        contours = [c for shape in shapes if _wanted(shape, names)
                    for c in shape.get("contours", []) if c.get("points")]
        if not contours:
            return None

        # Width / Height resize the outlines as sent (all layers, so picking
        # layers doesn't change the size). 0 = as sent; scenes from 1.0.x
        # used a plain Scale factor instead.
        old_scale = op.GetDataInstance().GetFloat(c4d.CAVALRYSPLINE_SCALE, 1.0) or 1.0
        nat_w, nat_h = natural_size(shapes)
        width, height = op[c4d.CAVALRYSPLINE_WIDTH], op[c4d.CAVALRYSPLINE_HEIGHT]
        fx = width / nat_w if width > 0 and nat_w > 1e-9 else old_scale
        fy = height / nat_h if height > 0 and nat_h > 1e-9 else old_scale

        plane = op[c4d.PRIM_PLANE]
        reverse = op[c4d.PRIM_REVERSE]
        count = sum(len(c["points"]) for c in contours)
        spline = c4d.SplineObject(count, c4d.SPLINETYPE_BEZIER)
        if spline is None:
            return None
        spline.ResizeObject(count, len(contours))

        i = 0
        for seg, contour in enumerate(contours):
            points = contour["points"][::-1] if reverse else contour["points"]
            spline.SetSegment(seg, len(points), bool(contour.get("closed")))
            for x, y, ix, iy, ox, oy in points:
                if reverse:  # walking backwards swaps in and out handles
                    ix, iy, ox, oy = ox, oy, ix, iy
                pos = c4d.Vector(x * fx, y * fy, 0)
                left = (c4d.Vector(ix * fx, iy * fy, 0) - pos) * TANGENT_SCALE
                right = (c4d.Vector(ox * fx, oy * fy, 0) - pos) * TANGENT_SCALE
                spline.SetPoint(i, _to_plane(pos, plane))
                spline.SetTangent(i, _to_plane(left, plane), _to_plane(right, plane))
                i += 1

        spline[c4d.SPLINEOBJECT_CLOSED] = all(c.get("closed") for c in contours)
        spline[c4d.SPLINEOBJECT_INTERPOLATION] = int(op[c4d.SPLINEOBJECT_INTERPOLATION])
        spline[c4d.SPLINEOBJECT_SUB] = int(op[c4d.SPLINEOBJECT_SUB])
        spline[c4d.SPLINEOBJECT_ANGLE] = op[c4d.SPLINEOBJECT_ANGLE]
        spline[c4d.SPLINEOBJECT_MAXIMUMLENGTH] = op[c4d.SPLINEOBJECT_MAXIMUMLENGTH]
        spline.Message(c4d.MSG_UPDATE)
        return spline


# ====================================================== sends -> objects

def _new_cavalry_spline(name, shapes):
    """A Cavalry Spline holding its own copy of `shapes` (not inserted),
    sized to them and coloured like the Cavalry fill."""
    op = c4d.BaseObject(PLUGIN_ID)
    if op is None:
        return None
    op.SetName(name or "Cavalry Spline")
    op[c4d.CAVALRYSPLINE_FOLLOW] = False
    keep = ("id", "name", "contours", "color")
    stored = [{k: s[k] for k in keep if k in s} for s in shapes]
    op.GetDataInstance().SetString(SHAPES_DATA, json.dumps(stored, separators=(",", ":")))
    op[c4d.CAVALRYSPLINE_WIDTH], op[c4d.CAVALRYSPLINE_HEIGHT] = natural_size(stored)
    colors = [s["color"] for s in shapes if s.get("color")]
    if colors:
        op[c4d.ID_BASEOBJECT_USECOLOR] = c4d.ID_BASEOBJECT_USECOLOR_ALWAYS
        op[c4d.ID_BASEOBJECT_COLOR] = c4d.Vector(*colors[0])
    return op


def _build_node(node):
    """One sent layer (and its children) as C4D objects: folders become
    Nulls, shapes Cavalry Splines; each gets its layer's transform and keys."""
    if node.get("kind") == "folder":
        op = c4d.BaseObject(c4d.Onull)
        op.SetName(node.get("name") or "Group")
    else:
        op = _new_cavalry_spline(node.get("name"), [node])
        if op is None:
            return None
    if "transform" in node:
        apply_static(op, node["transform"])
    if "anim" in node:
        apply_animation(op, node["anim"])
    for child in reversed(node.get("children", [])):  # InsertUnder prepends
        child_op = _build_node(child)
        if child_op is not None:
            child_op.InsertUnder(op)
    return op


def _add_send(doc, send):
    """Turns one send into objects and inserts them at the top of `doc`."""
    if "tree" in send:
        roots = [op for op in (_build_node(n) for n in send["tree"]) if op is not None]
        if not roots:
            return None
        if len(roots) == 1:
            top = roots[0]
        else:
            top = c4d.BaseObject(c4d.Onull)
            top.SetName("Cavalry (%d layers)" % len(roots))
            for op in reversed(roots):
                op.InsertUnder(top)
    else:
        shapes = send.get("shapes", [])
        if not shapes:
            return None
        name = shapes[0].get("name") if len(shapes) == 1 else "Cavalry Spline (%d layers)" % len(shapes)
        top = _new_cavalry_spline(name, shapes)
    if top is None:
        return None
    doc.StartUndo()
    doc.InsertObject(top)
    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, top)
    doc.EndUndo()
    doc.SetActiveObject(top, c4d.SELECTION_NEW)
    return top


class ShapesReceiver(plugins.MessageData):
    """Turns each send from Cavalry into new objects (main thread)."""

    def CoreMessage(self, id, bc):
        if id == RECEIVER_ID:
            sends = take_pending()
            doc = c4d.documents.GetActiveDocument()
            if sends and doc is not None:
                for send in sends:
                    try:
                        _add_send(doc, send)
                    except Exception as e:
                        print("[Cavalry Bridge] could not add shapes: %s" % e)
                c4d.EventAdd()
        return True


# ================================================================== panel

ID_STATUS = 1000
ID_PORT = 1001
ID_SEND = 1002
ID_SENDING = 1003
ID_RECEIVED = 1004
ID_ADD_SPLINE = 1005


def _describe(objs):
    """'Logo (folder, 4 inside), Plane' - what is being sent, in one line."""
    names = ["%s (folder, %d inside)" % (o.GetName(), _count_inside(o)) if o.GetDown() else o.GetName()
             for o in objs]
    if len(names) > 3:
        names = names[:3] + ["%d more" % (len(names) - 3)]
    return ", ".join(names)


class BridgePanel(gui.GeDialog):
    """Extensions > Cavalry Bridge: both directions in one place.
    Select objects in the Object Manager and click Send Selected to Cavalry:
    they (and everything inside them) are sent live from then on. Sending
    again replaces them; sending with nothing selected stops."""

    def CreateLayout(self):
        self.SetTitle("Cavalry Bridge")
        self.GroupBorderSpace(10, 8, 10, 8)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Connection")
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(8, 6, 8, 6)
        self.AddStaticText(ID_STATUS, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_NONE)
        self.GroupBegin(0, c4d.BFH_LEFT, 2, 1)
        self.AddStaticText(0, c4d.BFH_LEFT, 0, 0, "Port", c4d.BORDER_NONE)
        self.AddEditNumberArrows(ID_PORT, c4d.BFH_LEFT, 80, 0)
        self.GroupEnd()
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Cinema 4D to Cavalry")
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(8, 6, 8, 6)
        self.AddButton(ID_SEND, c4d.BFH_SCALEFIT, 0, 0, "Send Selected to Cavalry")
        self.AddStaticText(ID_SENDING, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_NONE)
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Cavalry to Cinema 4D")
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(8, 6, 8, 6)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0,
                           "In Cavalry, click Send Selected to C4D.", c4d.BORDER_NONE)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0,
                           "Shapes arrive here as Cavalry Splines.", c4d.BORDER_NONE)
        self.AddStaticText(ID_RECEIVED, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_NONE)
        self.AddButton(ID_ADD_SPLINE, c4d.BFH_SCALEFIT, 0, 0, "Add Cavalry Spline (follows the latest send)")
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetInt32(ID_PORT, DEFAULT_PORT, 1024, 65535)
        self.Refresh()
        self.SetTimer(1000)
        return True

    def Timer(self, msg):
        self.Refresh()

    def CoreMessage(self, id, msg):
        if id in (c4d.EVMSG_CHANGE, RECEIVER_ID):
            self.Refresh()
        return gui.GeDialog.CoreMessage(self, id, msg)

    def Refresh(self):
        doc = c4d.documents.GetActiveDocument()
        tag = find_bridge_tag(doc) if doc else None
        srv = get()
        if srv is None:
            status = "Not running - port %d may be in use by another program" % self.GetInt32(ID_PORT)
        elif cavalry_connected() or time.time() - srv.last_get < 2.0:
            status = "Connected to Cavalry (port %d)" % srv.server_address[1]
        else:
            status = "Waiting for Cavalry: open Window > Scripts > C4D Bridge there"
        self.SetString(ID_STATUS, status)

        objs = linked_objects(doc, tag) if doc else []
        if objs and _ud(tag, UD_ENABLED, True):
            sending = "Sending live: " + _describe(objs)
        else:
            sending = "Select splines, objects or folders in the Object Manager, then click Send."
        self.SetString(ID_SENDING, sending)
        if tag is not None:
            port = int(_ud(tag, UD_PORT, DEFAULT_PORT))
            if self.GetInt32(ID_PORT) != port:
                self.SetInt32(ID_PORT, port, 1024, 65535)

        last = srv.last_send if srv else None
        if last:
            self.SetString(ID_RECEIVED, "Last received: %d shape%s at %s" % (
                last[1], "" if last[1] == 1 else "s", time.strftime("%H:%M:%S", time.localtime(last[0]))))
        else:
            self.SetString(ID_RECEIVED, "Nothing received yet.")

    def Command(self, id, msg):
        doc = c4d.documents.GetActiveDocument()
        if doc is None:
            return True
        if id == ID_SEND:
            self._send_selected(doc)
        elif id == ID_PORT:
            tag = find_bridge_tag(doc)
            if tag is not None:
                doc.StartUndo()
                doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
                tag[c4d.ID_USERDATA, UD_PORT] = self.GetInt32(ID_PORT)
                doc.EndUndo()
                c4d.EventAdd()
        elif id == ID_ADD_SPLINE:
            op = c4d.BaseObject(PLUGIN_ID)   # Follow Latest Send is on by default
            doc.StartUndo()
            doc.InsertObject(op)
            doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, op)
            doc.EndUndo()
            doc.SetActiveObject(op, c4d.SELECTION_NEW)
            c4d.EventAdd()
        return True

    def _send_selected(self, doc):
        """The selection becomes what is sent live (replacing what was sent
        before); nothing selected stops sending."""
        tag = find_bridge_tag(doc)
        host = tag.GetObject() if tag else None   # the bridge's own Null is never sent
        selected = [o for o in doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_SELECTIONORDER) if o != host]
        if not selected and tag is None:
            return
        doc.StartUndo()
        tag = ensure_bridge_tag(doc)
        doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
        inex = c4d.InExcludeData()
        for o in selected:
            inex.InsertObject(o, 1)
        tag[c4d.ID_USERDATA, UD_OBJECTS] = inex
        tag[c4d.ID_USERDATA, UD_ENABLED] = True
        doc.EndUndo()
        c4d.EventAdd()
        self.Refresh()


def _count_inside(op):
    return sum(1 for _ in _all_objects(op.GetDown()))


class PanelCommand(plugins.CommandData):
    dialog = None

    def Execute(self, doc):
        if self.dialog is None:
            self.dialog = BridgePanel()
        return self.dialog.Open(c4d.DLG_TYPE_ASYNC, PANEL_ID, defaultw=380, defaulth=420)

    def RestoreLayout(self, sec_ref):
        if self.dialog is None:
            self.dialog = BridgePanel()
        return self.dialog.Restore(PANEL_ID, sec_ref)


# ============================================================= plugin entry

def _register_runtime():
    """What bridge tags call: `cavalry_bridge_runtime` (current tags) and
    `cavalry_bridge_server` (tags saved by 1.0.x / 1.1.0, which import it)."""
    rt = types.ModuleType("cavalry_bridge_runtime")
    rt.tag_draw = tag_draw
    rt.ensure = ensure
    rt.get = get
    rt.DEFAULT_HOST, rt.DEFAULT_PORT = DEFAULT_HOST, DEFAULT_PORT
    sys.modules["cavalry_bridge_runtime"] = rt
    sys.modules["cavalry_bridge_server"] = rt


def PluginMessage(id, data):
    if id == c4d.C4DPL_BUILDMENU:
        # One "Cavalry Bridge" entry with the icon straight in the Extensions
        # menu, instead of a "cavalry_bridge" folder submenu listing every
        # command (the plugins are registered hidden from that menu).
        # SearchPluginMenuResource() hands back an opaque pointer in 2026, so
        # the Extensions submenu (the one holding the plugin list) is looked
        # up directly; iterating a BaseContainer yields references.
        for _, menu in gui.GetMenuResource("M_EDITOR"):
            if isinstance(menu, c4d.BaseContainer) and (
                    menu.GetString(c4d.MENURESOURCE_SUBTITLE) == "IDS_EDITOR_PIPELINE"
                    or "IDM_PLUGINS" in [v for _, v in menu if isinstance(v, str)]):
                menu.InsData(c4d.MENURESOURCE_COMMAND, "PLUGIN_CMD_%d" % PANEL_ID)
                break
    elif id == c4d.C4DPL_ENDACTIVITY:
        stop()
        _remove_state()
    return False


if __name__ == "__main__":
    icon = c4d.bitmaps.BaseBitmap()
    icon.InitWith(os.path.join(PLUGIN_DIR, "res", "icons", "cavalry_spline.png"))
    plugins.RegisterObjectPlugin(id=PLUGIN_ID,
                                 str="Cavalry Spline",
                                 g=CavalrySpline,
                                 description="Ocavalryspline",
                                 icon=icon,
                                 # Added from the panel; the icon still shows in the Object Manager.
                                 info=c4d.OBJECT_GENERATOR | c4d.OBJECT_ISSPLINE | c4d.PLUGINFLAG_HIDEPLUGINMENU)
    plugins.RegisterMessagePlugin(id=RECEIVER_ID,
                                  str="Cavalry Bridge Receiver",
                                  info=0,
                                  dat=ShapesReceiver())
    plugins.RegisterCommandPlugin(id=PANEL_ID,
                                  str="Cavalry Bridge",
                                  info=c4d.PLUGINFLAG_HIDEPLUGINMENU,   # placed in Extensions by C4DPL_BUILDMENU
                                  icon=icon,
                                  help="Send splines and folders to Cavalry, and see what Cavalry sent",
                                  dat=PanelCommand())
    _register_runtime()
    _remove_state()
    ensure()
