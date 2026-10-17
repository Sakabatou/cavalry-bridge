# Cavalry Bridge - Cinema 4D plugin.
#
# Registers the "Cavalry Spline" object: a spline generator (like Circle or
# Text) whose contour is shape outlines sent from Cavalry with "Send
# Selected to C4D". Being a real spline object it works directly in Extrude,
# Sweep, Loft, Lathe, MoSpline..., has the usual Plane / Reverse /
# Intermediate Points settings, and Make Editable (C) turns it into a plain
# spline.
#
# Every send from Cavalry creates a new Cavalry Spline in the active
# document, holding its own copy of the shapes (saved with the scene). With
# "Animation (PSR)" on in Cavalry, every layer becomes its own Cavalry Spline
# with the layer's position / rotation / scale keys (cavalry_bridge_anim.py). A
# Cavalry Spline created from the menu has "Follow Latest Send" on instead,
# and always shows whatever was sent last.
#
# Also starts the bridge's local HTTP server (cavalry_bridge_server.py) when
# Cinema 4D starts, so Cavalry can send shapes at any time.
import json
import os
import sys

import c4d

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
import cavalry_bridge_anim as anim  # noqa: E402
import cavalry_bridge_server as server  # noqa: E402

# 1000001-1000010 is Maxon's range for testing. Get unique IDs from
# developers.maxon.net before giving this plugin to anyone else.
PLUGIN_ID = 1000001                          # Cavalry Spline object
RECEIVER_ID = server.SHAPES_RECEIVED         # 1000002, MessageData

# Private container ID holding a send's shapes as JSON (not in the
# description, so it is saved and copied with the object but not shown).
SHAPES_DATA = 10100

# Cavalry handles are absolute cubic Bezier control points; C4D tangents are
# relative and 3/4 the length of a cubic control offset (C4D's own circle:
# 41.4 for radius 100, not 55.2).
TANGENT_SCALE = 0.75


def _to_plane(v, plane):
    """XY-plane vector -> the chosen plane (same mapping as C4D primitives)."""
    if plane == c4d.PRIM_PLANE_ZY:
        return c4d.Vector(-v.z, v.y, v.x)
    if plane == c4d.PRIM_PLANE_XZ:
        return c4d.Vector(v.x, -v.z, v.y)
    return v


def _wanted(shape, names):
    return not names or shape.get("name", "").strip().lower() in names


class CavalrySpline(c4d.plugins.ObjectData):

    def __init__(self):
        self._version = None  # shapes_version last seen (Follow Latest Send)
        self._parsed_src = None
        self._parsed = []

    def Init(self, op, isCloneInit=False):
        self.InitAttr(op, str, c4d.CAVALRYSPLINE_LAYERS)
        self.InitAttr(op, float, c4d.CAVALRYSPLINE_SCALE)
        self.InitAttr(op, bool, c4d.CAVALRYSPLINE_FOLLOW)
        self.InitAttr(op, int, c4d.PRIM_PLANE)
        self.InitAttr(op, bool, c4d.PRIM_REVERSE)
        self.InitAttr(op, int, c4d.SPLINEOBJECT_INTERPOLATION)
        self.InitAttr(op, int, c4d.SPLINEOBJECT_SUB)
        self.InitAttr(op, float, c4d.SPLINEOBJECT_ANGLE)
        self.InitAttr(op, float, c4d.SPLINEOBJECT_MAXIMUMLENGTH)
        if not isCloneInit:
            op[c4d.CAVALRYSPLINE_LAYERS] = ""
            op[c4d.CAVALRYSPLINE_SCALE] = 1.0
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
        srv = server.get()
        version = srv.shapes_version if srv else None
        if version != self._version:
            self._version = version
            op.SetDirty(c4d.DIRTYFLAGS_DATA)

    def _shapes(self, op):
        if op[c4d.CAVALRYSPLINE_FOLLOW]:
            srv = server.get()
            return srv.shapes if srv else []
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
        names = {n.strip().lower() for n in (op[c4d.CAVALRYSPLINE_LAYERS] or "").split(",") if n.strip()}
        contours = [c for shape in self._shapes(op) if _wanted(shape, names)
                    for c in shape.get("contours", []) if c.get("points")]
        if not contours:
            return None

        scale = op[c4d.CAVALRYSPLINE_SCALE]
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
                pos = c4d.Vector(x, y, 0) * scale
                left = (c4d.Vector(ix, iy, 0) * scale - pos) * TANGENT_SCALE
                right = (c4d.Vector(ox, oy, 0) * scale - pos) * TANGENT_SCALE
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


def _new_cavalry_spline(name, shapes):
    """A Cavalry Spline holding its own copy of `shapes` (not inserted)."""
    op = c4d.BaseObject(PLUGIN_ID)
    if op is None:
        return None
    op.SetName(name or "Cavalry Spline")
    op[c4d.CAVALRYSPLINE_FOLLOW] = False
    # Animation and transform live on the object itself; store outlines only.
    stored = [{k: v for k, v in s.items() if k not in ("anim", "transform")} for s in shapes]
    op.GetDataInstance().SetString(SHAPES_DATA, json.dumps(stored, separators=(",", ":")))
    return op


def _add_send(doc, shapes):
    """Turns one send into objects: a single Cavalry Spline, or with
    animation one per layer (grouped under a null if there are several)."""
    if any("transform" in s for s in shapes):
        objs = []
        for shape in shapes:
            op = _new_cavalry_spline(shape.get("name"), [shape])
            if op is None:
                continue
            scale = op[c4d.CAVALRYSPLINE_SCALE]
            if "transform" in shape:
                anim.apply_static(op, shape["transform"], scale)
            if "anim" in shape:
                anim.apply_animation(op, shape["anim"], scale)
            objs.append(op)
        if len(objs) == 1:
            top = objs[0]
        else:
            top = c4d.BaseObject(c4d.Onull)
            top.SetName("Cavalry Animation (%d layers)" % len(objs))
            for op in reversed(objs):  # InsertUnder prepends; keep Cavalry's order
                op.InsertUnder(top)
    else:
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


class ShapesReceiver(c4d.plugins.MessageData):
    """Turns each send from Cavalry into a new Cavalry Spline (main thread)."""

    def CoreMessage(self, id, bc):
        if id == RECEIVER_ID:
            sends = server.take_pending()
            doc = c4d.documents.GetActiveDocument()
            if sends and doc is not None:
                for shapes in sends:
                    if shapes:
                        try:
                            _add_send(doc, shapes)
                        except Exception as e:
                            print("[Cavalry Bridge] could not add shapes: %s" % e)
                c4d.EventAdd()
        return True


def PluginMessage(id, data):
    if id == c4d.C4DPL_ENDACTIVITY:
        server.stop()
    return False


if __name__ == "__main__":
    icon = c4d.bitmaps.BaseBitmap()
    icon.InitWith(os.path.join(PLUGIN_DIR, "res", "icons", "cavalry_spline.png"))
    c4d.plugins.RegisterObjectPlugin(id=PLUGIN_ID,
                                     str="Cavalry Spline",
                                     g=CavalrySpline,
                                     description="Ocavalryspline",
                                     icon=icon,
                                     info=c4d.OBJECT_GENERATOR | c4d.OBJECT_ISSPLINE)
    c4d.plugins.RegisterMessagePlugin(id=RECEIVER_ID,
                                      str="Cavalry Bridge Receiver",
                                      info=0,
                                      dat=ShapesReceiver())
    server.ensure()
