// C4D Bridge - live link between Cavalry and Cinema 4D.
//
// Cinema 4D -> Cavalry: polls the "Cavalry Bridge" tag in Cinema 4D (served
// by the cavalry_bridge plugin at http://127.0.0.1:<port>/state) and drives
// one Cavalry layer per C4D object. Splines become editable shapes that
// follow the spline's geometry, other objects drive a layer's position,
// rotation and scale, and C4D folders (objects with children) become groups,
// so whole hierarchies come across.
//
// Cavalry -> Cinema 4D: "Send Selected to C4D" sends the selected layers.
//   Group          folders become Nulls and every shape its own Cavalry
//                  Spline, each with its layer's position / rotation / scale
//                  (and, with "Animation (PSR)", their keyframes)
//   Single object  everything merged into one Cavalry Spline
//
// The script connects by itself: the plugin keeps a presence file in the
// temp folder while Cinema 4D runs, so the script knows when (and on which
// port) to connect without blocking Cavalry on a refused connection.
//
// Layers are matched to C4D objects by the object's GUID, stored in the
// layer's user data, so renaming either side doesn't break the link. Use
// "Bind Selected" to drive your own layer instead of the one the bridge makes.

var BRIDGE_KEY = "c4dBridgeId";      // user data: C4D GUID this layer follows
var AUTO_KEY = "c4dBridgeAuto";      // user data: layer was created by the bridge
var GROUP_KEY = "c4dBridgeGroup";    // user data: the "C4D Bridge" group
var PRESENCE_FILE = api.getTempFolder() + "/cavalry_bridge_c4d.json";

// Layers that are folders even without children.
var FOLDER_TYPES = { group: 1, "null": 1, layoutGroup: 1, gridLayoutGroup: 1, schedulingGroup: 1 };

// ---------------------------------------------------------------- core

var bridge = {
    layerFor: {},       // C4D GUID -> Cavalry layer id
    names: {},          // C4D GUID -> C4D object name (last seen)
    hiddenByBridge: {}, // C4D GUID -> true when hidden because it left the C4D scene
    groupId: null,
    packets: 0,
    lastFrame: null,
    options: {
        mode: 0,          // 0 = Screen (camera view), 1 = World XY
        worldScale: 1,    // World mode: Cavalry px per C4D unit
        rotation: true,
        perspectiveScale: true,
        create: true,
        record: false
    }
};

// Cinema 4D's presence file ({port, pid, started}), or null when it isn't running.
bridge.presence = function () {
    try {
        if (!api.filePathExists(PRESENCE_FILE)) return null;
        var p = JSON.parse(api.readFromFile(PRESENCE_FILE));
        return p && p.port ? p : null;
    } catch (e) {
        return null;
    }
};

bridge.findGroup = function () {
    if (bridge.groupId && api.layerExists(bridge.groupId)) return bridge.groupId;
    var all = api.getAllSceneLayers();
    for (var i = 0; i < all.length; i++) {
        if (api.hasUserDataKey(all[i], GROUP_KEY)) return (bridge.groupId = all[i]);
    }
    bridge.groupId = api.create("group", "C4D Bridge");
    api.setUserData(bridge.groupId, GROUP_KEY, true);
    return bridge.groupId;
};

bridge.rescan = function () {
    bridge.layerFor = {};
    var all = api.getAllSceneLayers();
    for (var i = 0; i < all.length; i++) {
        if (api.hasUserDataKey(all[i], BRIDGE_KEY)) {
            var guid = api.getUserDataKey(all[i], BRIDGE_KEY);
            if (guid) bridge.layerFor[guid] = all[i];
        }
    }
};

bridge.isAuto = function (id) {
    return api.hasUserDataKey(id, AUTO_KEY) && api.getUserDataKey(id, AUTO_KEY);
};

bridge.layerForObject = function (obj) {
    var id = bridge.layerFor[obj.id];
    if (id && api.layerExists(id)) return id;
    bridge.rescan();
    id = bridge.layerFor[obj.id];
    if (id) return id;
    if (!bridge.options.create) return null;
    if (obj.kind === "folder") {
        id = api.create("group", obj.name);
    } else if (obj.spline) {
        id = api.create("editableShape", obj.name);
    } else {
        id = api.primitive("ellipse", obj.name);
        api.set(id, { "generator.radius": [20, 20] });
    }
    api.setUserData(id, BRIDGE_KEY, obj.id);
    api.setUserData(id, AUTO_KEY, true);
    api.parent(id, bridge.findGroup());
    bridge.layerFor[obj.id] = id;
    return id;
};

bridge.bind = function (guid, layerId) {
    // Unbind whatever was driven by this object before.
    var old = bridge.layerFor[guid];
    if (old && api.layerExists(old) && old !== layerId) api.setUserData(old, BRIDGE_KEY, "");
    api.setUserData(layerId, BRIDGE_KEY, guid);
    bridge.layerFor[guid] = layerId;
};

// C4D spline -> path of an editable shape, in world space. Points come as
// [x, y, inX, inY, outX, outY] with absolute handles, either in C4D world XY
// or in normalised screen coordinates (0..1, y down).
bridge.splinePath = function (segments, res) {
    var o = bridge.options;
    var map = o.mode === 0
        ? function (u, v) { return { x: (u - 0.5) * res.x, y: (0.5 - v) * res.y }; }
        : function (u, v) { return { x: u * o.worldScale, y: v * o.worldScale }; };
    var path = [];
    for (var s = 0; s < segments.length; s++) {
        var pts = o.mode === 0 ? segments[s].screen : segments[s].world;
        var points = [];
        for (var p = 0; p < pts.length; p++) {
            var q = pts[p];
            points.push({ position: map(q[0], q[1]), inHandle: map(q[2], q[3]), outHandle: map(q[4], q[5]) });
        }
        path.push({ isClosed: segments[s].closed, points: points });
    }
    return path;
};

bridge.pathCache = {};   // layer id -> last path JSON, to skip unchanged splines

bridge.applySpline = function (id, obj, res) {
    if (bridge.options.mode === 0 && obj.depth <= 0) return;   // behind the camera
    var path = bridge.splinePath(obj.spline, res);
    var json = JSON.stringify(path);
    if (bridge.pathCache[id] === json) return;
    try {
        api.setEditablePath(id, true, path);
        bridge.pathCache[id] = json;
    } catch (e) {
        console.warn("C4D Bridge: " + api.getNiceName(id) + " is not an editable shape, " +
            "so it can't follow the spline " + obj.name);
    }
};

bridge.applyTransform = function (id, obj, res, frame) {
    var o = bridge.options;
    var x, y, rot, sx, sy;
    if (o.mode === 0) {
        if (obj.depth <= 0) return;   // behind the camera
        x = (obj.screen[0] - 0.5) * res.x;
        y = (0.5 - obj.screen[1]) * res.y;
        rot = obj.screenRot;
        var k = o.perspectiveScale ? obj.screenScale * res.x / 100 : 1;
        sx = obj.scale[0] * k;
        sy = obj.scale[1] * k;
    } else {
        x = obj.pos[0] * o.worldScale;
        y = obj.pos[1] * o.worldScale;
        rot = -obj.rot[2];   // C4D bank is clockwise seen from the default camera
        sx = obj.scale[0];
        sy = obj.scale[1];
    }
    var values = { "position.x": x, "position.y": y, "scale.x": sx, "scale.y": sy };
    if (o.rotation) values["rotation.z"] = rot;
    if (o.record) {
        api.keyframe(id, frame, values);
    } else {
        var set = { position: [x, y], scale: [sx, sy] };
        if (o.rotation) set.rotation = rot;
        api.set(id, set);
    }
};

bridge.isRootParent = function (parent) {
    return !parent || parent === api.getActiveComp() || api.getLayerType(parent) === "compNode";
};

bridge.apply = function (packet) {
    var res = api.get(api.getActiveComp(), "resolution");
    var objects = packet.objects || [];
    var seen = {};
    for (var i = 0; i < objects.length; i++) {
        var obj = objects[i];
        seen[obj.id] = true;
        bridge.names[obj.id] = obj.name;
        var id = bridge.layerForObject(obj);
        if (!id) continue;
        if (bridge.isAuto(id)) {
            // Layers the bridge made mirror the C4D object's name and visibility.
            if (api.getNiceName(id) !== obj.name) api.rename(id, obj.name);
            var hide = obj.visible === false;
            if (!!api.get(id, "hidden") !== hide) api.set(id, { hidden: hide });
            delete bridge.hiddenByBridge[obj.id];
        }
        if (obj.kind === "folder") continue;
        if (obj.spline) bridge.applySpline(id, obj, res);
        else bridge.applyTransform(id, obj, res, packet.frame);
    }
    // Rebuild the C4D hierarchy: each layer the bridge made goes into the
    // group of its C4D parent (or the "C4D Bridge" group at the top).
    for (var j = 0; j < objects.length; j++) {
        var lid = bridge.layerFor[objects[j].id];
        if (!lid || !api.layerExists(lid) || !bridge.isAuto(lid)) continue;
        var parentLayer = objects[j].parent && bridge.layerFor[objects[j].parent];
        var want = parentLayer && api.layerExists(parentLayer) ? parentLayer : bridge.findGroup();
        if (api.getParent(lid) !== want) api.parent(lid, want);
    }
    // Objects removed from the C4D scene: hide (never delete) their layers.
    for (var guid in bridge.layerFor) {
        if (seen[guid] || bridge.hiddenByBridge[guid]) continue;
        var gone = bridge.layerFor[guid];
        if (api.layerExists(gone) && bridge.isAuto(gone) && !api.get(gone, "hidden")) {
            api.set(gone, { hidden: true });
            bridge.hiddenByBridge[guid] = true;
        }
    }
    bridge.packets++;
    bridge.lastFrame = packet.frame;
};

// ------------------------------------------------- Cavalry -> C4D shapes

// C4D stores each send inside its Cavalry Splines, so keep the numbers short.
function round3(v) {
    return Math.round(v * 1000) / 1000;
}

var PSR_ATTRS = ["position.x", "position.y", "rotation.z", "scale.x", "scale.y"];

bridge.transformValues = function (id) {
    var p = api.get(id, "position"), r = api.get(id, "rotation"), s = api.get(id, "scale");
    return { "position.x": p.x, "position.y": p.y, "rotation.z": r.z, "scale.x": s.x, "scale.y": s.y };
};

bridge.isFolder = function (id) {
    return !!FOLDER_TYPES[api.getLayerType(id)] || (!api.isShape(id) && api.getChildren(id).length > 0);
};

// Outline of one shape layer: a temporary editable copy gives the final
// geometry for any shape type (primitives, text, duplicators...). In world
// space by default; with `local`, in the layer's own space relative to its
// pivot (world = position + rotation * scale * (local - pivot)).
bridge.contours = function (id, local) {
    var wasHidden = api.get(id, "hidden");
    var copy = api.makeEditable(id, true);
    var path;
    try {
        path = api.getEditablePath(copy, !local);
    } finally {
        api.deleteLayer(copy);
        api.set(id, { hidden: wasHidden });   // makeEditable hides the source
    }
    var pv = local ? api.get(id, "pivot") : { x: 0, y: 0 };
    var contours = [];
    for (var c = 0; c < path.length; c++) {
        var points = [];
        for (var p = 0; p < path[c].points.length; p++) {
            var pt = path[c].points[p];
            var pos = pt.position;
            var inH = pt.hasInHandle ? pt.inHandle : pos;
            var outH = pt.hasOutHandle ? pt.outHandle : pos;
            points.push([pos.x - pv.x, pos.y - pv.y, inH.x - pv.x, inH.y - pv.y,
                         outH.x - pv.x, outH.y - pv.y].map(round3));
        }
        contours.push({ closed: !!path[c].isClosed, points: points });
    }
    return contours;
};

bridge.color = function (id) {
    var col = api.get(id, "material.materialColor");
    return col && col.r !== undefined ? [col.r / 255, col.g / 255, col.b / 255] : null;
};

// One layer as a node of the sent hierarchy. Position is relative to the
// parent's pivot, so C4D can rebuild the layer as a child object:
// world = parent * (position - parent pivot + rotation * scale * (local - pivot)).
// `shallow`: just a folder with the layer's transform (for unselected parents).
bridge.node = function (id, parentPivot, shallow) {
    if (api.get(id, "hidden")) return null;
    var t = bridge.transformValues(id);
    var node = {
        id: id,
        name: api.getNiceName(id),
        kind: shallow || bridge.isFolder(id) ? "folder" : "shape",
        parentPivot: [parentPivot.x, parentPivot.y],
        transform: {
            pos: [t["position.x"] - parentPivot.x, t["position.y"] - parentPivot.y],
            rot: t["rotation.z"],
            scale: [t["scale.x"], t["scale.y"]]
        },
        children: []
    };
    if (shallow) return node;
    if (node.kind === "shape") {
        if (!api.isShape(id)) return null;   // behaviours, utilities...
        node.contours = bridge.contours(id, true);
        var col = bridge.color(id);
        if (col) node.color = col;
    }
    var pivot = api.get(id, "pivot") || { x: 0, y: 0 };
    var children = api.getChildren(id);
    for (var i = 0; i < children.length; i++) {
        var child = bridge.node(children[i], pivot);
        if (child) node.children.push(child);
    }
    return node;
};

// The selected layers as a hierarchy. A layer selected inside a group keeps
// its parent groups as folders, so it lands in C4D exactly where it is.
bridge.tree = function (ids) {
    var selected = {};
    for (var i = 0; i < ids.length; i++) selected[ids[i]] = true;
    var forest = [];
    for (var k = 0; k < ids.length; k++) {
        var id = ids[k];
        var chain = [], skip = false;
        for (var p = api.getParent(id); !bridge.isRootParent(p); p = api.getParent(p)) {
            if (selected[p]) { skip = true; break; }   // already sent with its parent
            chain.unshift(p);
        }
        if (skip) continue;
        var parentPivot = chain.length ? api.get(chain[chain.length - 1], "pivot") : { x: 0, y: 0 };
        var node = bridge.node(id, parentPivot);
        if (!node) continue;
        // Wrap in the parent groups, sharing them between selected siblings.
        var level = forest, pv = { x: 0, y: 0 };
        for (var c = 0; c < chain.length; c++) {
            var found = null;
            for (var f = 0; f < level.length; f++) if (level[f].id === chain[c]) found = level[f];
            if (!found) {
                found = bridge.node(chain[c], pv, true);
                if (!found) { level = null; break; }   // a hidden parent hides it
                level.push(found);
            }
            pv = api.get(chain[c], "pivot") || { x: 0, y: 0 };
            level = found.children;
        }
        if (level) level.push(node);
    }
    return forest;
};

bridge.eachNode = function (nodes, fn) {
    for (var i = 0; i < nodes.length; i++) {
        fn(nodes[i]);
        bridge.eachNode(nodes[i].children || [], fn);
    }
};

// Keyframes of one attribute as [[frame, value], ...] (empty if none).
bridge.keysOf = function (id, attr) {
    var ids;
    try {
        ids = api.getKeyframeIdsForAttribute(id, attr);
    } catch (e) {
        return [];
    }
    var keys = [];
    for (var i = 0; i < ids.length; i++) {
        var data = api.get(ids[i], "data");
        if (data && data.numValue !== undefined) keys.push([api.get(ids[i], "frame"), data.numValue]);
    }
    keys.sort(function (a, b) { return a[0] - b[0]; });
    return keys;
};

// Adds `anim` to every node with PSR keyframes: the keys, plus the value on
// every frame between the first and last key, so C4D can fit its curves to
// the real motion whatever easing Cavalry used. Returns the animated count.
bridge.addAnimation = function (nodes) {
    var comp = api.getActiveComp();
    var fps = api.get(comp, "fps");
    var first = Infinity, last = -Infinity, animated = [];
    bridge.eachNode(nodes, function (node) {
        var attrs = {}, any = false;
        for (var a = 0; a < PSR_ATTRS.length; a++) {
            var keys = bridge.keysOf(node.id, PSR_ATTRS[a]);
            if (keys.length < 1) continue;
            attrs[PSR_ATTRS[a]] = { keys: keys, first: keys[0][0], samples: [] };
            first = Math.min(first, keys[0][0]);
            last = Math.max(last, keys[keys.length - 1][0]);
            any = true;
        }
        if (any) {
            node.anim = { fps: fps, attrs: attrs };
            animated.push(node);
        }
    });
    if (!animated.length) return 0;

    // setFrame clamps to the comp's range, so widen it while sampling.
    var start = api.get(comp, "startFrame"), end = api.get(comp, "endFrame");
    var now = api.getFrame();
    api.set(comp, { startFrame: Math.min(start, first), endFrame: Math.max(end, last) });
    try {
        for (var f = first; f <= last; f++) {
            api.setFrame(f);
            for (var j = 0; j < animated.length; j++) {
                var values = bridge.transformValues(animated[j].id);
                var anim = animated[j].anim.attrs;
                for (var attr in anim) {
                    var k = anim[attr].keys;
                    if (f >= k[0][0] && f <= k[k.length - 1][0]) anim[attr].samples.push(round3(values[attr]));
                }
            }
        }
    } finally {
        api.set(comp, { startFrame: start, endFrame: end });
        api.setFrame(now);
    }
    // Positions are sent relative to the parent's pivot, keys included.
    for (var n = 0; n < animated.length; n++) {
        var node = animated[n], at = node.anim.attrs;
        for (var axis = 0; axis < 2; axis++) {
            var a2 = at[axis ? "position.y" : "position.x"];
            if (!a2) continue;
            var off = node.parentPivot[axis];
            a2.keys = a2.keys.map(function (kv) { return [kv[0], kv[1] - off]; });
            a2.samples = a2.samples.map(function (v) { return round3(v - off); });
        }
    }
    return animated.length;
};

// Visible shapes in the given layers (walking into groups), world space.
bridge.flatShapes = function (ids) {
    var shapes = [];
    var visit = function (id) {
        if (api.get(id, "hidden")) return;
        if (!bridge.isFolder(id) && api.isShape(id)) {
            var s = { id: id, name: api.getNiceName(id), contours: bridge.contours(id, false) };
            var col = bridge.color(id);
            if (col) s.color = col;
            shapes.push(s);
        }
        var children = api.getChildren(id);
        for (var i = 0; i < children.length; i++) visit(children[i]);
    };
    for (var i = 0; i < ids.length; i++) visit(ids[i]);
    return shapes;
};

// Sends the selected layers to Cinema 4D; returns a status message.
//   asGroup    folders -> Nulls, one Cavalry Spline per shape, transforms kept
//   !asGroup   one Cavalry Spline with every outline (world space)
bridge.send = function (ids, asGroup, animation) {
    var c4d = bridge.presence();
    if (!c4d) return "Cinema 4D isn't running (or the Cavalry Bridge plugin isn't installed)";
    var selection = api.getSelection();
    var payload, summary;
    try {
        if (asGroup) {
            // (A single shape sent as a group is just one Cavalry Spline,
            // with its transform and keys.)
            var tree = bridge.tree(ids);
            var shapes = 0, folders = 0;
            bridge.eachNode(tree, function (n) { if (n.kind === "folder") folders++; else shapes++; });
            if (!shapes) return "Nothing to send - select visible shapes or groups";
            var animated = animation ? bridge.addAnimation(tree) : 0;
            payload = { tree: tree };
            summary = "Sent " + shapes + " shape" + (shapes === 1 ? "" : "s") +
                (folders ? " in " + folders + " folder" + (folders === 1 ? "" : "s") : "") +
                (animation ? " (" + animated + " animated)" : "");
        } else {
            var flat = bridge.flatShapes(ids);
            if (!flat.length) return "Nothing to send - select visible shapes or groups";
            payload = { shapes: flat };
            summary = "Sent " + flat.length + " shape" + (flat.length === 1 ? "" : "s") + " as one Cavalry Spline" +
                (animation ? " (keyframes need Send as: Group)" : "");
        }
    } finally {
        api.select(selection);
    }
    var client = new api.WebClient("http://127.0.0.1:" + c4d.port);
    client.post("/shapes", JSON.stringify(payload), "application/json");
    if (client.status() !== 200) return "Cinema 4D didn't answer on port " + c4d.port;
    return summary;
};

// ------------------------------------------------------------------ UI
// (Skipped when this file is evaluated outside a Script Window.)

if (typeof ui.Button === "function") {
    var POLL_MS = 33;        // ~30 updates per second while connected
    var WATCH_MS = 1000;     // look for Cinema 4D once a second while not
    var client = null;
    var lastBody = null;
    var port = null;
    var stamp = null;        // which Cinema 4D run we're talking to
    var failedStamp = null;  // a run that didn't answer: don't retry it
    var autoConnect = true;  // off after Disconnect, until Connect

    var connectButton = new ui.Button("Connect");
    var statusLabel = new ui.Label("Looking for Cinema 4D...");

    var modeDrop = new ui.DropDown();
    modeDrop.addEntry("Screen (camera view)");
    modeDrop.addEntry("World XY");
    var worldScaleField = new ui.NumericField(1);
    worldScaleField.setType(1);
    worldScaleField.setStep(0.1);
    worldScaleField.setToolTip("World XY mode: Cavalry pixels per C4D unit");

    var rotationCheck = new ui.Checkbox(true);
    var perspectiveCheck = new ui.Checkbox(true);
    perspectiveCheck.setToolTip("Screen mode: scale layers with distance from the camera");
    var createCheck = new ui.Checkbox(true);
    var recordCheck = new ui.Checkbox(false);
    recordCheck.setToolTip("Keyframe incoming values at the C4D frame instead of just setting them");

    var objectDrop = new ui.DropDown();
    var bindButton = new ui.Button("Bind Selected");
    bindButton.setToolTip("Drive the selected Cavalry layer with the chosen C4D object");
    var objectGuids = [];

    var sendAsDrop = new ui.DropDown();
    sendAsDrop.addEntry("Group (keep folders)");
    sendAsDrop.addEntry("Single object");
    sendAsDrop.setToolTip("Group: folders become Nulls and every shape its own Cavalry Spline, with its " +
        "position / rotation / scale. Single object: all outlines in one Cavalry Spline.");
    var animationCheck = new ui.Checkbox(false);
    animationCheck.setToolTip("Also send position / rotation / scale keyframes (with Send as: Group)");
    var sendButton = new ui.Button("Send Selected to C4D");
    sendButton.setToolTip("Send the selected layers (or everything in selected groups) to Cinema 4D");
    var sendLabel = new ui.Label("");

    sendButton.onClick = function () {
        var sel = api.getSelection();
        if (!sel.length) {
            sendLabel.setText("Select layers or groups first");
            return;
        }
        // Read the choice by its text: it doesn't depend on how the dropdown numbers entries.
        var asGroup = String(sendAsDrop.getText()).indexOf("Single") < 0;
        sendLabel.setText(bridge.send(sel, asGroup, !!animationCheck.getValue()));
    };

    function refreshOptions() {
        var o = bridge.options;
        o.mode = String(modeDrop.getText()).indexOf("World") < 0 ? 0 : 1;
        o.worldScale = worldScaleField.getValue();
        o.rotation = rotationCheck.getValue();
        o.perspectiveScale = perspectiveCheck.getValue();
        o.create = createCheck.getValue();
        o.record = recordCheck.getValue();
        lastBody = null;   // re-apply the current state with the new options
        bridge.pathCache = {};
    }
    modeDrop.onValueChanged = refreshOptions;
    worldScaleField.onValueChanged = refreshOptions;
    rotationCheck.onValueChanged = refreshOptions;
    perspectiveCheck.onValueChanged = refreshOptions;
    createCheck.onValueChanged = refreshOptions;
    recordCheck.onValueChanged = refreshOptions;

    function refreshObjectList() {
        var guids = Object.keys(bridge.names);
        if (guids.join() === objectGuids.join()) return;
        objectGuids = guids;
        objectDrop.clear();
        for (var i = 0; i < guids.length; i++) objectDrop.addEntry(bridge.names[guids[i]]);
    }

    bindButton.onClick = function () {
        var sel = api.getSelection();
        var guid = objectGuids[objectDrop.getValue()];
        if (sel.length !== 1 || !guid) {
            console.warn("C4D Bridge: select one layer and pick a C4D object first.");
            return;
        }
        bridge.bind(guid, sel[0]);
        lastBody = null;
        console.log("C4D Bridge: " + api.getNiceName(sel[0]) + " now follows " + bridge.names[guid]);
    };

    function disconnect(message) {
        pollTimer.stop();
        client = null;
        connectButton.setText("Connect");
        statusLabel.setText(message);
    }

    function connect(presence) {
        refreshOptions();
        bridge.rescan();
        port = presence.port;
        stamp = presence.pid + "/" + presence.started;
        client = new api.WebClient("http://127.0.0.1:" + port);
        connectButton.setText("Disconnect");
        statusLabel.setText("Connecting to Cinema 4D on port " + port + "...");
        poll();
        if (client) pollTimer.start();
    }

    function poll() {
        // Cinema 4D removes the presence file when it quits: notice that
        // without a (blocking) refused connection.
        if (!api.filePathExists(PRESENCE_FILE)) {
            disconnect("Cinema 4D closed - waiting for it to start");
            return;
        }
        client.get("/state");
        if (client.status() !== 200) {
            failedStamp = stamp;
            disconnect("Cinema 4D isn't answering on port " + port + " - click Connect to retry");
            return;
        }
        var body = client.body();
        if (body === lastBody) return;   // nothing moved in C4D
        lastBody = body;
        try {
            bridge.apply(JSON.parse(body));
        } catch (e) {
            console.error("C4D Bridge: " + e);
            return;
        }
        refreshObjectList();
        statusLabel.setText("Connected to Cinema 4D on port " + port + " - " + bridge.packets +
            " updates, C4D frame " + bridge.lastFrame);
    }

    // Connects by itself whenever Cinema 4D is running, and again after it restarts.
    function watch() {
        if (client || !autoConnect) return;
        var p = bridge.presence();
        if (!p) {
            statusLabel.setText("Waiting for Cinema 4D (with the Cavalry Bridge plugin)...");
            return;
        }
        if (p.pid + "/" + p.started === failedStamp) return;
        connect(p);
    }

    function PollCallbacks() {
        this.onTimeout = function () {
            if (client) poll();
        };
    }
    function WatchCallbacks() {
        this.onTimeout = watch;
    }
    var pollTimer = new api.Timer(new PollCallbacks());
    pollTimer.setInterval(POLL_MS);
    pollTimer.setRepeating(true);
    var watchTimer = new api.Timer(new WatchCallbacks());
    watchTimer.setInterval(WATCH_MS);
    watchTimer.setRepeating(true);

    // Connected: Disconnect (and stop reconnecting). Otherwise: try now,
    // including a Cinema 4D that didn't answer before.
    connectButton.onClick = function () {
        if (client) {
            autoConnect = false;
            disconnect("Disconnected - click Connect to reconnect");
            return;
        }
        autoConnect = true;
        failedStamp = null;
        watch();
    };

    ui.onClose = function () {
        pollTimer.stop();
        watchTimer.stop();
    };

    function row(labelText, widget) {
        var layout = new ui.HLayout();
        var label = new ui.Label(labelText);
        label.setFixedWidth(120);
        layout.add(label, widget);
        layout.addStretch();
        return layout;
    }

    var connectRow = new ui.HLayout();
    connectRow.add(statusLabel);
    connectRow.addStretch();
    connectRow.add(connectButton);

    var bindRow = new ui.HLayout();
    bindRow.add(objectDrop, bindButton);

    ui.setTitle("C4D Bridge");
    ui.add(connectRow);
    ui.add(new ui.Label("Cinema 4D to Cavalry"));
    ui.add(row("Mode", modeDrop));
    ui.add(row("World scale", worldScaleField));
    ui.add(row("Rotation", rotationCheck));
    ui.add(row("Perspective scale", perspectiveCheck));
    ui.add(row("Create layers", createCheck));
    ui.add(row("Record keys", recordCheck));
    ui.add(bindRow);
    ui.add(new ui.Label("Cavalry to Cinema 4D"));
    ui.add(row("Send as", sendAsDrop));
    ui.add(row("Animation (PSR)", animationCheck));
    ui.add(sendButton);
    ui.add(sendLabel);
    ui.addStretch();
    ui.show();

    refreshOptions();
    watchTimer.start();
    watch();   // connect straight away when Cinema 4D is already running
}
