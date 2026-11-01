<#
.SYNOPSIS
    Installs webcam-mcp: creates .venv, installs dependencies, probes the
    cameras and (optionally) registers the server with the Claude desktop app.

.DESCRIPTION
    Run from the folder that contains server.py:

        powershell -ExecutionPolicy Bypass -File .\install.ps1

    Add -RegisterClaudeDesktop to merge the server into
    %APPDATA%\Claude\claude_desktop_config.json automatically (the file is
    backed up first and every other entry is left untouched).

    Nothing is registered unless you ask for it; by default the script only
    prints the config block to paste into whatever MCP client you use.

.PARAMETER RegisterClaudeDesktop
    Merge the "webcam" entry into the Claude desktop app config.

.PARAMETER ConfigPath
    Alternate path to claude_desktop_config.json (useful for testing).

.PARAMETER Python
    Python launcher to build the venv with. Defaults to "py -3", then "python".
#>
param(
    [switch]$RegisterClaudeDesktop,
    [string]$ConfigPath = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# ---------------------------------------------------------------------------
# 1. venv
# ---------------------------------------------------------------------------
if ($Python) {
    $pyCmd = $Python
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $pyCmd = "py -3" } else { $pyCmd = "python" }
}

if (-not (Test-Path (Join-Path $root ".venv"))) {
    Write-Host "==> creating venv with '$pyCmd'"
    Invoke-Expression "$pyCmd -m venv .venv"
} else {
    Write-Host "==> reusing existing .venv"
}
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv python not found at $venvPy" }

# ---------------------------------------------------------------------------
# 2. dependencies
# ---------------------------------------------------------------------------
Write-Host "==> installing dependencies"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet "mcp>=2.0" "opencv-python>=4.9"
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

# ---------------------------------------------------------------------------
# 3. camera probe
# ---------------------------------------------------------------------------
Write-Host "==> probing cameras"
# Written to a temp .py file rather than inlined as `python -c @"..."@`:
# Windows PowerShell 5.1 mangles double quotes and spaces when a multi-line
# here-string is passed as a native command argument, which makes the inline
# version fail with "SyntaxError: unterminated string literal".
$probeScript = Join-Path $env:TEMP "webcam-probe-$PID.py"
Set-Content -Path $probeScript -Encoding utf8 -Value @'
import cv2
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
found = False
for i in range(6):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    if cap.isOpened():
        ok, _ = cap.read()
        state = "ok" if ok else "FAIL"
        print(f"  index {i}: {int(cap.get(3))}x{int(cap.get(4))} read={state}")
        found = True
    cap.release()
if not found:
    print("  no camera opened - check Windows Settings > Privacy & security > Camera > \"Let desktop apps access your camera\"")
'@
try {
    & $venvPy $probeScript
} finally {
    Remove-Item -Force $probeScript -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# 4. the config block
# ---------------------------------------------------------------------------
$serverPath = Join-Path $root "server.py"
# Let python format the block: PowerShell's ConvertTo-Json pads oddly. The
# inline here-string holds no double quotes on purpose - Windows PowerShell 5.1
# mangles them when a multi-line string is passed to a native command.
$blockJson = & $venvPy -c @'
import json, sys
print(json.dumps({'command': sys.argv[1], 'args': [sys.argv[2]],
                  'env': {'WEBCAM_DEVICE': 'auto'}}, indent=2))
'@ $venvPy $serverPath

Write-Host ""
Write-Host "==> MCP client config for this copy (add under 'mcpServers'):"
Write-Host ""
foreach ($line in @($blockJson)) { Write-Host ("    " + $line) }
Write-Host ""

if (-not $RegisterClaudeDesktop) {
    Write-Host "Claude desktop config: $env:APPDATA\Claude\claude_desktop_config.json"
    Write-Host "Re-run with -RegisterClaudeDesktop to merge it automatically,"
    Write-Host "then restart the desktop app."
    return
}

# ---------------------------------------------------------------------------
# 5. optional registration (python does the merge so the file keeps its shape)
# ---------------------------------------------------------------------------
if (-not $ConfigPath) { $ConfigPath = Join-Path $env:APPDATA "Claude\claude_desktop_config.json" }
if (-not (Test-Path $ConfigPath)) { throw "config not found: $ConfigPath" }

Write-Host "==> registering with the Claude desktop app"
$mergeScript = Join-Path $env:TEMP "webcam-merge-$PID.py"
Set-Content -Path $mergeScript -Encoding utf8 -Value @'
import json, shutil, sys
from datetime import datetime
from pathlib import Path

cfg = Path(sys.argv[1])
command = sys.argv[2]
server = sys.argv[3]

data = json.loads(cfg.read_text(encoding="utf-8"))
servers = data.setdefault("mcpServers", {})
entry = {"command": command, "args": [server], "env": {"WEBCAM_DEVICE": "auto"}}

if "webcam" in servers:
    if servers["webcam"] == entry:
        print("   already registered with the same paths - nothing to do")
        raise SystemExit(0)
    print("   'webcam' exists with different paths:")
    print("   ", json.dumps(servers["webcam"]))
    print("   leaving it alone; edit it by hand if this copy should replace it")
    raise SystemExit(0)

backup = cfg.with_name(cfg.name + ".bak-" + datetime.now().strftime("%Y%m%d"))
shutil.copy2(cfg, backup)
print("   backup:", backup)
servers["webcam"] = entry
cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
json.loads(cfg.read_text(encoding="utf-8"))  # prove it still parses
print("   registered;", len(servers), "servers now configured")
'@
try {
    & $venvPy $mergeScript $ConfigPath $venvPy $serverPath
    if ($LASTEXITCODE -ne 0) { throw "merge failed" }
} finally {
    Remove-Item -Force $mergeScript -ErrorAction SilentlyContinue
}
Write-Host "==> restart the Claude desktop app to pick it up"
