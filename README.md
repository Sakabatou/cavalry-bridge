<p align="center"><img src="docs/img/logo.png" width="96" alt=""></p>

<h1 align="center">Cavalry Bridge</h1>

<p align="center">A live link between <b>Cavalry</b> and <b>Cinema&nbsp;4D</b>.<br>
Send Cavalry shapes, with their animation, to Cinema&nbsp;4D as real splines. Stream Cinema&nbsp;4D splines into Cavalry as you edit them.</p>

<p align="center">
  <a href="../../releases/latest"><b>Download for Windows</b></a> ·
  <a href="https://sakabatou.github.io/cavalry-bridge/">Project page</a>
</p>

<p align="center"><img src="docs/img/cav2c4d-extrude.png" width="640" alt="Cavalry text, star and ellipse extruded in Cinema 4D"></p>

## Features

| Direction | What happens |
|---|---|
| **Cavalry → Cinema 4D** | *Send Selected to C4D* creates a **Cavalry Spline**, a real spline object that works directly in Extrude, Sweep, Loft and Lathe. Holes in letters stay holes, and the shapes are saved with the scene. |
| **…with animation** | With *Animation (PSR)* ticked, each layer arrives as its own Cavalry Spline with its position, rotation and scale **keyframes**. Eases are rebuilt as curve handles on the original keys. |
| **Cinema 4D → Cavalry** | Splines linked to the bridge appear in Cavalry as **editable shapes that follow every change**, seen through the C4D camera or in world XY. Other objects drive a layer's position, rotation and scale. |

Everything runs on your own computer, over `127.0.0.1:1234`.

## Install

1. Download `CavalryBridgeSetup-x.y.z.exe` from the [latest release](../../releases/latest) and run it.
   It finds each Cinema 4D version (R23 or newer) that has been started at least once, and Cavalry, then installs into their user folders. No admin rights are needed.
2. Restart Cinema 4D.
3. In Cavalry, open **Window › Scripts › C4D Bridge**. The first time, Cavalry asks whether to trust the script, because it makes local network calls. **Choose to trust it**, or it can't connect.

If Windows shows "Windows protected your PC" (the setup isn't code-signed yet), click **More info › Run anyway**.

| App | Installed into |
|---|---|
| Cinema 4D | `%APPDATA%\Maxon\<version>\plugins\cavalry_bridge\` and `…\library\scripts\Cavalry Bridge Setup.py` |
| Cavalry | `%APPDATA%\Cavalry\Scripts\C4D Bridge.js` |

## Use

**Cavalry → Cinema 4D:** select layers or groups in Cavalry. Optionally tick *Animation (PSR)*. Then click **Send Selected to C4D**.

**Cinema 4D → Cavalry:** select splines in Cinema 4D, run **Extensions › User Scripts › Cavalry Bridge Setup**, then click **Connect** in Cavalry.

The [project page](https://sakabatou.github.io/cavalry-bridge/) explains every setting, how the bridge works, and how it was built.

## Build from source

Requires Windows and nothing else: it uses the .NET Framework 4.x C# compiler that comes with Windows.

```
powershell -ExecutionPolicy Bypass -File build_installer.ps1
```

This builds `dist\CavalryBridgeSetup-<version>.exe`, with every file it installs packed inside. The setup also runs from the command line with `/silent`, `/uninstall`, `/log <file>` and `/appdata <folder>`.

```
cavalry_bridge/            Cinema 4D plugin: Cavalry Spline, local server, keyframe fitting
c4d_bridge_tag.py          Cavalry Bridge tag (publishes C4D splines and transforms)
install_c4d_bridge.py      "Cavalry Bridge Setup" script for Cinema 4D
C4D Bridge.js              Cavalry script window
installer/                 setup program source (C#), manifest, icon
build_installer.ps1        builds the setup .exe
docs/                      project page (GitHub Pages)
```

## Limits

- Only outlines transfer. Fills, gradients, stroke widths and effects don't, and strokes arrive as their centre line.
- Animation covers each layer's own position, rotation and scale keyframes. Parent groups' transforms and behaviour-driven motion aren't included.
- If the two apps run at different frame rates, keyframes keep their timing in seconds, so they can land between frames.
- Windows only for now.
- The plugin uses Maxon's **test plugin IDs** (1000001, 1000002). Register unique IDs at developers.maxon.net before distributing it widely.

---

Cinema 4D is a trademark of Maxon. Cavalry is made by Scene Group / Canva. This project isn't affiliated with either.
