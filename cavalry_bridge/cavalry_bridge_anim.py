# Cavalry Bridge - Cavalry position / rotation / scale animation as C4D keys.
#
# Cavalry sends, per attribute, its keyframes ([frame, value]) and the value
# on every frame from the first to the last key. Cavalry's easing curves
# can't be read directly, so each segment between two keys gets the Bezier
# handles (length and height) that best fit the sampled curve - the way an
# animator shapes an ease in the F-Curve editor. Where no single curve fits
# (Bounce, Spring, Step...), that segment gets a key on every frame instead.
# The result is one C4D key per Cavalry key wherever possible, so it stays
# easy to edit.
import math

import c4d

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


def apply_static(op, transform, scale):
    """Sets the layer's transform at the time of the send."""
    op[c4d.ID_BASEOBJECT_REL_POSITION] = c4d.Vector(transform["pos"][0] * scale, transform["pos"][1] * scale, 0)
    op[c4d.ID_BASEOBJECT_REL_ROTATION] = c4d.Vector(0, 0, -math.radians(transform["rot"]))
    op[c4d.ID_BASEOBJECT_REL_SCALE] = c4d.Vector(transform["scale"][0], transform["scale"][1], 1)


def apply_animation(op, anim, scale):
    """Creates PSR tracks on op from a shape's `anim` block."""
    fps = float(anim.get("fps") or 25)
    for attr, data in anim.get("attrs", {}).items():
        if attr not in TRACKS or not data.get("keys"):
            continue
        param, component, convert, invisible = TRACKS[attr]
        factor = scale if attr.startswith("position") else 1.0
        keys = [(f, convert(v) * factor) for f, v in data["keys"]]
        samples = [convert(v) * factor for v in data.get("samples", [])]
        _build_curve(op, param, component, keys, data.get("first", keys[0][0]), samples, fps,
                     invisible * factor)


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
