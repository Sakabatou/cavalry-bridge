// C4D Bridge - live link from Cinema 4D.
//
// Polls the "Cavalry Bridge" Python Tag in Cinema 4D (which serves its
// objects at http://127.0.0.1:<port>/state) and drives one Cavalry layer per
// C4D object: C4D splines become editable shapes that follow the spline's
// geometry, other objects drive a layer's position, rotation and scale. Open from Window > Scripts (it must run in a Script Window -
// polling lasts as long as this window is open).
//
// Layers are matched to C4D objects by the object's GUID, stored in the
// layer's user data, so renaming either side doesn't break the link. Missing
// layers are created as small placeholder circles inside a "C4D Bridge"
// group; use "Bind Selected" to drive your own layer instead.
//
// The other way round, "Send Selected to C4D" posts the outlines of the
// selected shapes to C4D, where each send becomes a new Cavalry Spline
// object. With "Animation (PSR)" on, every layer becomes its own Cavalry
// Spline carrying the layer's position / rotation / scale keyframes.

var BRIDGE_KEY = "c4dBridgeId";
var GROUP_KEY = "c4dBridgeGroup";

// ---------------------------------------------------------------- core

var bridge = {
    layerFor: {},     // C4D GUID -> Cavalry layer id
    names: {},        // C4D GUID -> C4D object name (last seen)
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

bridge.findGroup = function () {
    var all = api.getAllSceneLayers();
    for (var i = 0; i < all.length; i++) {
        if (api.hasUserDataKey(all[i], GROUP_KEY)) return all[i];
    }
    var g = api.create("group", "C4D Bridge");
    api.setUserData(g, GROUP_KEY, true);
    return g;
};

bridge.rescan = function () {
    bridge.layerFor = {};
    var all = api.getAllSceneLayers();
    for (var i = 0; i < all.length; i++) {
        if (api.hasUserDataKey(all[i], BRIDGE_KEY)) {
            bridge.layerFor[api.getUserDataKey(all[i], BRIDGE_KEY)] = all[i];
        }
    }
};

bridge.layerForObject = function (obj) {
    var id = bridge.layerFor[obj.id];
    if (id && api.layerExists(id)) return id;
    bridge.rescan();
    id = bridge.layerFor[obj.id];
    if (id) return id;
    if (!bridge.options.create) return null;
    if (obj.spline) {
        id = api.create("editableShape", obj.name);
    } else {
        id = api.primitive("ellipse", obj.name);
        api.set(id, { "generator.radius": [20, 20] });
    }
    api.setUserData(id, BRIDGE_KEY, obj.id);
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

bridge.apply = function (packet) {
    var o = bridge.options;
    var res = api.get(api.getActiveComp(), "resolution");
    var objects = packet.objects || [];
    for (var i = 0; i < objects.length; i++) {
        var obj = objects[i];
        bridge.names[obj.id] = obj.name;
        var id = bridge.layerForObject(obj);
        if (!id) continue;
        if (obj.spline) {
            bridge.applySpline(id, obj, res);
            continue;
        }

        var x, y, rot, sx, sy;
        if (o.mode === 0) {
            if (obj.depth <= 0) continue;   // behind the camera
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
            api.keyframe(id, packet.frame, values);
        } else {
            var set = { position: [x, y], scale: [sx, sy] };
            if (o.rotation) set.rotation = rot;
            api.set(id, set);
        }
    }
    bridge.packets++;
    bridge.lastFrame = packet.frame;
};

// ------------------------------------------------- Cavalry -> C4D shapes

// C4D stores each send inside its Cavalry Spline, so keep the numbers short.
function round3(v) {
    return Math.round(v * 1000) / 1000;
}

// Outline of one shape layer: a temporary editable copy gives the final
// geometry for any shape type (primitives, text, duplicators...). In world
// space by default; with `local`, in the layer's own space relative to its
// pivot (world = position + rotation * scale * (local - pivot)), for sending
// together with the layer's transform and its animation.
bridge.shapeData = function (id, local) {
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
    var shape = { id: id, name: api.getNiceName(id), contours: contours };
    var col = api.get(id, "material.materialColor");
    if (col && col.r !== undefined) shape.color = [col.r / 255, col.g / 255, col.b / 255];
    if (local) {
        var t = bridge.transformValues(id);
        shape.transform = {
            pos: [t["position.x"], t["position.y"]],
            rot: t["rotation.z"],
            scale: [t["scale.x"], t["scale.y"]]
        };
    }
    return shape;
};

// Visible shapes in the given layers, walking into groups.
bridge.collectShapes = function (ids, local) {
    var shapes = [];
    var visit = function (id) {
        if (api.get(id, "hidden")) return;
        if (api.getLayerType(id) !== "group" && api.isShape(id)) shapes.push(bridge.shapeData(id, local));
        var children = api.getChildren(id);
        for (var i = 0; i < children.length; i++) visit(children[i]);
    };
    var selection = api.getSelection();
    for (var i = 0; i < ids.length; i++) visit(ids[i]);
    api.select(selection);
    return shapes;
};

// ----------------------------------------------- PSR animation -> C4D keys

var PSR_ATTRS = ["position.x", "position.y", "rotation.z", "scale.x", "scale.y"];

bridge.transformValues = function (id) {
    var p = api.get(id, "position"), r = api.get(id, "rotation"), s = api.get(id, "scale");
    return { "position.x": p.x, "position.y": p.y, "rotation.z": r.z, "scale.x": s.x, "scale.y": s.y };
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

// Adds `anim` to each shape: the PSR keyframes, plus the value on every
// frame between the first and last key, so C4D can fit its tangents to the
// real curve whatever easing Cavalry used.
bridge.addAnimation = function (shapes) {
    var comp = api.getActiveComp();
    var fps = api.get(comp, "fps");
    var first = Infinity, last = -Infinity, animated = [];
    for (var i = 0; i < shapes.length; i++) {
        var attrs = {}, any = false;
        for (var a = 0; a < PSR_ATTRS.length; a++) {
            var keys = bridge.keysOf(shapes[i].id, PSR_ATTRS[a]);
            if (keys.length < 1) continue;
            attrs[PSR_ATTRS[a]] = { keys: keys, first: keys[0][0], samples: [] };
            first = Math.min(first, keys[0][0]);
            last = Math.max(last, keys[keys.length - 1][0]);
            any = true;
        }
        if (any) {
            shapes[i].anim = { fps: fps, attrs: attrs };
            animated.push(shapes[i]);
        }
    }
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
    return animated.length;
};

// Sends shapes to the C4D bridge; returns a status message. With
// `animation`, every layer becomes its own Cavalry Spline carrying the
// layer's position / rotation / scale and their keyframes.
bridge.sendShapes = function (port, ids, animation) {
    var shapes = bridge.collectShapes(ids, animation);
    if (!shapes.length) return "Nothing to send - select visible shapes or groups";
    var animated = animation ? bridge.addAnimation(shapes) : 0;
    var client = new api.WebClient("http://127.0.0.1:" + port);
    client.post("/shapes", JSON.stringify({ shapes: shapes }), "application/json");
    if (client.status() !== 200) return "C4D not reachable on port " + port;
    var n = shapes.length + " shape" + (shapes.length === 1 ? "" : "s");
    if (!animation) return "Sent " + n + " - added as a new Cavalry Spline in C4D";
    return "Sent " + n + " (" + animated + " animated) - one Cavalry Spline per layer in C4D";
};

// ------------------------------------------------------------------ UI
// (Skipped when this file is evaluated outside a Script Window.)

if (typeof ui.Button === "function") {
    var POLL_MS = 33;      // ~30 updates per second
    var client = null;
    var lastBody = null;
    var port = 1234;

    var portField = new ui.NumericField(1234);
    portField.setType(0);
    portField.setMin(1024);
    portField.setMax(65535);

    var connectButton = new ui.Button("Connect");
    var statusLabel = new ui.Label("Disconnected");

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

    var sendButton = new ui.Button("Send Selected to C4D");
    sendButton.setToolTip("Send the selected shapes (or everything in selected groups) to the " +
        "'Cavalry Shapes' generator in C4D as Bezier splines");
    var sendLabel = new ui.Label("");
    var animationCheck = new ui.Checkbox(false);
    animationCheck.setToolTip("Send each layer's position / rotation / scale and their keyframes; " +
        "every layer becomes its own Cavalry Spline in C4D");

    sendButton.onClick = function () {
        var sel = api.getSelection();
        if (!sel.length) {
            sendLabel.setText("Select shapes or groups first");
            return;
        }
        sendLabel.setText(bridge.sendShapes(Math.round(portField.getValue()), sel, animationCheck.getValue()));
    };

    function refreshOptions() {
        var o = bridge.options;
        o.mode = modeDrop.getValue();
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
        timer.stop();
        client = null;
        connectButton.setText("Connect");
        statusLabel.setText(message);
    }

    function poll() {
        client.get("/state");
        if (client.status() !== 200) {
            // A refused connection can block for a moment on Windows, so stop
            // polling rather than retrying in a loop.
            disconnect("C4D not reachable on port " + port + " - click Connect to retry");
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
        statusLabel.setText("Connected to C4D on " + port + " - " + bridge.packets +
            " updates, C4D frame " + bridge.lastFrame);
    }

    function TimerCallbacks() {
        this.onTimeout = function () {
            if (client) poll();
        };
    }
    var timer = new api.Timer(new TimerCallbacks());
    timer.setInterval(POLL_MS);
    timer.setRepeating(true);

    connectButton.onClick = function () {
        if (client) {
            disconnect("Disconnected");
            return;
        }
        refreshOptions();
        bridge.rescan();
        port = Math.round(portField.getValue());
        client = new api.WebClient("http://127.0.0.1:" + port);
        connectButton.setText("Disconnect");
        statusLabel.setText("Connecting to C4D on " + port + "...");
        poll();
        if (client) timer.start();
    };

    ui.onClose = function () {
        timer.stop();
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
    connectRow.add(new ui.Label("C4D port"), portField, connectButton);
    connectRow.addStretch();

    var bindRow = new ui.HLayout();
    bindRow.add(objectDrop, bindButton);

    ui.setTitle("C4D Bridge");
    ui.add(connectRow);
    ui.add(statusLabel);
    ui.add(row("Mode", modeDrop));
    ui.add(row("World scale", worldScaleField));
    ui.add(row("Rotation", rotationCheck));
    ui.add(row("Perspective scale", perspectiveCheck));
    ui.add(row("Create layers", createCheck));
    ui.add(row("Record keys", recordCheck));
    ui.add(bindRow);
    ui.add(new ui.Label("Cavalry to C4D"));
    ui.add(row("Animation (PSR)", animationCheck));
    ui.add(sendButton);
    ui.add(sendLabel);
    ui.addStretch();
    ui.show();
}
