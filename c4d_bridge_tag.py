# Cavalry Bridge - Python Tag code for Cinema 4D.
#
# Publishes the objects in the tag's "Objects" list on the Cavalry Bridge
# server (GET /state), for the "C4D Bridge" script in Cavalry to poll:
# splines (including generators such as Circle, Text or Cavalry Spline) as
# their Bezier geometry, other objects as world and screen-space transforms.
# Objects with children (a Null "folder", say) bring their whole hierarchy
# along; each entry names its parent so Cavalry can rebuild it as groups.
# Needs the cavalry_bridge plugin, which runs that server.
#
# The state is computed in the tag's draw() callback (main thread, active
# viewport only), so screen positions match exactly what the active camera
# sees, framed by the safe frame (render resolution).
#
# User data on the tag (created by install_c4d_bridge.py):
#   1 Enabled (bool)   2 Host (string)   3 Port (int)   4 Objects (InExclude)
import c4d
import json
import math

try:
    import cavalry_bridge_server as server
except ImportError:
    server = None
    print("[Cavalry Bridge] the cavalry_bridge plugin is not installed")

UD_ENABLED, UD_HOST, UD_PORT, UD_OBJECTS = 1, 2, 3, 4


def main():
    pass


def _ud(bc_id, default):
    try:
        v = op[c4d.ID_USERDATA, bc_id]
        return default if v is None else v
    except Exception:
        return default


def _to_frame(bd, sf, p):
    """World point -> normalised safe-frame coords (0..1, y down)."""
    s = bd.WS(p)
    w = max(sf["cr"] - sf["cl"], 1)
    h = max(sf["cb"] - sf["ct"], 1)
    return (s.x - sf["cl"]) / w, (s.y - sf["ct"]) / h


# C4D Bezier tangents are 3/4 of a cubic Bezier control offset (see the
# Cavalry Spline plugin); Cavalry handles are absolute control points.
TANGENT_TO_HANDLE = 1.0 / 0.75
SAMPLES_PER_POINT = 8  # for spline types without Bezier handles


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
                    triples.append((p, p + t["vl"] * TANGENT_TO_HANDLE, p + t["vr"] * TANGENT_TO_HANDLE))
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


def draw(bd):
    doc = op.GetDocument()
    if server is None or doc is None or bd != doc.GetActiveBaseDraw() or not _ud(UD_ENABLED, True):
        return True
    # Viewport captures and some renders draw a temporary copy of the scene,
    # whose objects have different GUIDs; only publish the real document.
    if doc != c4d.documents.GetActiveDocument():
        return True
    srv = server.ensure(_ud(UD_HOST, server.DEFAULT_HOST) or server.DEFAULT_HOST,
                        int(_ud(UD_PORT, server.DEFAULT_PORT)))
    inex = _ud(UD_OBJECTS, None)
    if srv is None or inex is None:
        return True

    cam = bd.GetSceneCamera(doc) or bd.GetEditorCamera()
    cam_inv = ~cam.GetMg()
    sf = bd.GetSafeFrame()
    frame_w = max(sf["cr"] - sf["cl"], 1)
    frame_h = max(sf["cb"] - sf["ct"], 1)

    def entry(obj, parent):
        """The object itself: geometry for splines, a transform otherwise."""
        mg = obj.GetMg()
        p = mg.off
        base = {"id": str(obj.GetGUID()), "name": obj.GetName(), "parent": parent,
                "visible": obj.GetEditorMode() != c4d.MODE_OFF, "depth": (cam_inv * p).z}
        spline = _spline_data(obj, bd, sf)
        if spline is not None:
            # Splines travel as geometry; Cavalry draws them as editable shapes.
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
        if obj.GetRealSpline() is not None:
            # Other parents (Nulls, generators) need no layer of their own.
            objects.append(entry(obj, folder))
        for child in children:
            walk(child, folder)

    for i in range(inex.GetObjectCount()):
        obj = inex.ObjectFromIndex(doc, i)
        if obj is not None:
            walk(obj, None)

    srv.state = json.dumps({
        "frame": doc.GetTime().GetFrame(doc.GetFps()),
        "fps": doc.GetFps(),
        "aspect": frame_w / frame_h,
        "objects": objects,
    }, separators=(",", ":")).encode("utf-8")
    return True
