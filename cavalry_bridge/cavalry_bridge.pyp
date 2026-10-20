# Cavalry Bridge - Cinema 4D plugin.
#
# Registers the "Cavalry Spline" object: a spline generator (like Circle or
# Text) whose contour is shape outlines sent from Cavalry with "Send
# Selected to C4D". Being a real spline object it works directly in Extrude,
# Sweep, Loft, Lathe, MoSpline..., has the usual Plane / Reverse /
# Intermediate Points settings, and Make Editable (C) turns it into a plain
# spline. Its Width / Height are lengths, like a Rectangle's, so the Scale
# tool and the Coordinate Manager resize it in Model mode like a primitive.
#
# Every send from Cavalry creates new objects in the active document, each
# holding its own copy of the shapes (saved with the scene):
#   - "Single object": one Cavalry Spline with all the outlines;
#   - "Group": Cavalry's folders (groups) become Nulls and every shape its own
#     Cavalry Spline, each with its layer's position / rotation / scale and,
#     with "Animation (PSR)" on, their keyframes (cavalry_bridge_anim.py).
# A Cavalry Spline created from the menu has "Follow Latest Send" on instead,
# and always shows whatever was sent last.
#
# Also starts the bridge's local HTTP server (cavalry_bridge_server.py) when
# Cinema 4D starts, so Cavalry can connect and send at any time.
import json
import os
import sys

import c4d

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
import cavalry_bridge_anim as anim  # noqa: E402
import cavalry_bridge_server as server  # noqa: E402

# Plugin IDs registered at developers.maxon.net. The object ID is stored in
# every scene that contains a Cavalry Spline, so it must never change.
PLUGIN_ID = 1071603                          # Cavalry Spline object ("Cavalry Bridge")
RECEIVER_ID = server.SHAPES_RECEIVED         # 1071604, MessageData ("CavalryBridgeReceiver")

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


def natural_size(shapes):
    """Width and height of the outlines' points, as sent (before Width /
    Height resize them)."""
    xs = [p[0] for s in shapes for c in s.get("contours", []) for p in c.get("points", [])]
    ys = [p[1] for s in shapes for c in s.get("contours", []) for p in c.get("points", [])]
    if not xs:
        return 0.0, 0.0
    return max(xs) - min(xs), max(ys) - min(ys)


class CavalrySpline(c4d.plugins.ObjectData):

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


# ------------------------------------------------------- sends -> objects

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
        anim.apply_static(op, node["transform"], 1.0)
    if "anim" in node:
        anim.apply_animation(op, node["anim"], 1.0)
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


class ShapesReceiver(c4d.plugins.MessageData):
    """Turns each send from Cavalry into new objects (main thread)."""

    def CoreMessage(self, id, bc):
        if id == RECEIVER_ID:
            sends = server.take_pending()
            doc = c4d.documents.GetActiveDocument()
            if sends and doc is not None:
                for send in sends:
                    try:
                        _add_send(doc, send)
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
