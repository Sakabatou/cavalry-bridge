# Draws the setup icon (a Bezier curve with its handles, like the Cavalry
# Spline icon) at several sizes and writes them into one .ico file.
param([string]$Out = (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'Setup.ico'))
Add-Type -AssemblyName System.Drawing

function Draw-Icon([int]$size) {
    $s = $size / 64.0
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.Clear([System.Drawing.Color]::Transparent)
    # Rounded dark tile so the icon reads on any background.
    $tile = New-Object System.Drawing.Drawing2D.GraphicsPath
    $r = 14 * $s; $w = $size - 1
    $tile.AddArc(0, 0, $r, $r, 180, 90); $tile.AddArc($w - $r, 0, $r, $r, 270, 90)
    $tile.AddArc($w - $r, $w - $r, $r, $r, 0, 90); $tile.AddArc(0, $w - $r, $r, $r, 90, 90); $tile.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 32, 36, 44))), $tile)
    $curve = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(255, 120, 200, 255)), ([Math]::Max(1.5, 5 * $s))
    $curve.StartCap = 'Round'; $curve.EndCap = 'Round'
    $g.DrawBezier($curve, 14 * $s, 48 * $s, 14 * $s, 14 * $s, 50 * $s, 50 * $s, 50 * $s, 16 * $s)
    $orange = [System.Drawing.Color]::FromArgb(255, 255, 140, 60)
    if ($size -ge 32) {
        $handle = New-Object System.Drawing.Pen $orange, ([Math]::Max(1, 2 * $s))
        $g.DrawLine($handle, 14 * $s, 48 * $s, 14 * $s, 24 * $s); $g.DrawLine($handle, 50 * $s, 16 * $s, 50 * $s, 40 * $s)
    }
    $dot = New-Object System.Drawing.SolidBrush $orange
    $d = [Math]::Max(3, 9 * $s)
    foreach ($p in @(@(14, 48), @(50, 16))) { $g.FillEllipse($dot, $p[0] * $s - $d / 2, $p[1] * $s - $d / 2, $d, $d) }
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    if ($size -ge 256) {
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)   # Vista+ style entry
    } else {
        Write-Dib $bmp $ms   # classic entry, readable by every icon API
    }
    $bmp.Dispose()
    return , $ms.ToArray()
}

# 32-bit BGRA DIB as stored in .ico files: BITMAPINFOHEADER with double
# height, bottom-up pixels, then an all-zero 1-bit AND mask.
function Write-Dib($bmp, $ms) {
    $size = $bmp.Width
    $bw = New-Object System.IO.BinaryWriter $ms
    $bw.Write([uint32]40); $bw.Write([int32]$size); $bw.Write([int32]($size * 2))
    $bw.Write([uint16]1); $bw.Write([uint16]32); $bw.Write([uint32]0)
    $maskStride = [int]([Math]::Ceiling($size / 32.0) * 4)
    $bw.Write([uint32]($size * $size * 4 + $maskStride * $size))
    $bw.Write([int32]0); $bw.Write([int32]0); $bw.Write([uint32]0); $bw.Write([uint32]0)
    $rect = New-Object System.Drawing.Rectangle 0, 0, $size, $size
    $data = $bmp.LockBits($rect, 'ReadOnly', [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $bytes = New-Object byte[] ($data.Stride * $size)
    [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $bytes.Length)
    $bmp.UnlockBits($data)
    for ($y = $size - 1; $y -ge 0; $y--) { $bw.Write($bytes, $y * $data.Stride, $size * 4) }
    $bw.Write((New-Object byte[] ($maskStride * $size)))
    $bw.Flush()
}

# ICO file: header, one directory entry per size, then the PNG images.
$sizes = 16, 24, 32, 48, 64, 128, 256
$images = @($sizes | ForEach-Object { , (Draw-Icon $_) })
$fs = [System.IO.File]::Create($Out)
$bw = New-Object System.IO.BinaryWriter $fs
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $dim = if ($sizes[$i] -ge 256) { 0 } else { $sizes[$i] }
    $bw.Write([byte]$dim); $bw.Write([byte]$dim); $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([uint16]1); $bw.Write([uint16]32)
    $bw.Write([uint32]$images[$i].Length); $bw.Write([uint32]$offset)
    $offset += $images[$i].Length
}
foreach ($img in $images) { $bw.Write($img) }
$bw.Close()
Write-Host "Wrote $Out"
