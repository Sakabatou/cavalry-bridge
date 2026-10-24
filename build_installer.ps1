# Builds dist\CavalryBridgeSetup-<version>.exe from the project files.
#
# The exe is compiled from installer\Setup.cs with the C# compiler that comes
# with .NET Framework 4.x (part of Windows), with the files to install
# embedded as payload.zip:
#   cinema4d/plugins/cavalry_bridge/...          the Cinema 4D plugin (one .pyp + res)
#   cavalry/C4D Bridge.js                        the Cavalry script
# Run: powershell -ExecutionPolicy Bypass -File build_installer.ps1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $root 'installer'
$version = (Select-String -Path "$installer\Setup.cs" -Pattern 'Version = "(.+)"').Matches[0].Groups[1].Value
$dist = Join-Path $root 'dist'
$build = Join-Path $dist 'build'
$exe = Join-Path $dist "CavalryBridgeSetup-$version.exe"
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw ".NET Framework 4 C# compiler not found: $csc" }

if (Test-Path $build) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Force $build | Out-Null

# payload.zip - entries named with forward slashes, whatever the OS.
$files = [ordered]@{}
foreach ($f in Get-ChildItem "$root\cavalry_bridge" -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' }) {
    $files['cinema4d/plugins/cavalry_bridge/' + $f.FullName.Substring("$root\cavalry_bridge\".Length).Replace('\', '/')] = $f.FullName
}
$files['cavalry/C4D Bridge.js'] = "$root\C4D Bridge.js"
$payload = Join-Path $build 'payload.zip'
$zip = [System.IO.Compression.ZipFile]::Open($payload, 'Create')
foreach ($entry in $files.Keys) {
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $files[$entry], $entry) | Out-Null
}
$zip.Dispose()

# Icon (regenerated if missing)
if (-not (Test-Path "$installer\Setup.ico")) { & "$installer\make_icon.ps1" }

if (Test-Path $exe) { Remove-Item $exe -Force }
& $csc /nologo /target:winexe /optimize+ /platform:anycpu `
    "/out:$exe" "/win32icon:$installer\Setup.ico" "/win32manifest:$installer\Setup.manifest" `
    "/resource:$payload,payload.zip" `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
    /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll `
    "$installer\Setup.cs"
if ($LASTEXITCODE -ne 0) { throw "Compile failed" }
Remove-Item $build -Recurse -Force

Write-Host "Built $exe ($([Math]::Round((Get-Item $exe).Length / 1KB)) KB) with $($files.Count) files:"
$files.Keys | ForEach-Object { "  $_" }
