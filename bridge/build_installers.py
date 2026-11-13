#!/usr/bin/env python3
"""Build the single-file installers in ../dist from clawdbridge.py (the only copy of the bridge).

    dist/clawdbridge-install-linux.sh        sh clawdbridge-install-linux.sh
    dist/clawdbridge-install-macos.command   double-click (or: sh clawdbridge-install-macos.command)
    dist/clawdbridge-install-windows.cmd     double-click

Each embeds the bridge and installs it for the current user only (no admin): Python + packages come
from uv (x86-64 and ARM64 on all three), kept with the bridge in one folder.
"""
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE.parent / "dist"

SH = r"""#!/bin/sh
# Clawdmeter bridge installer for Linux and macOS. Run: sh <this file>
# Installs for this user only, into one folder: the bridge (Wi-Fi + Bluetooth link to the Clawdmeter
# phone app), the Claude Code hooks it needs, and start at login. Uninstall: see the last lines printed.
set -eu
say() { printf '%s\n' "$@"; }
if [ "$(id -u)" = 0 ] && [ -z "${CLAWDBRIDGE_AS_ROOT:-}" ]; then
  say "Run this as your normal user, not root or sudo: it installs for whoever runs it (their Claude Code, their login)."
  exit 1
fi
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"
  else return 1; fi
}
case "$(uname -s)" in
  Darwin) os=mac; dir="$HOME/Library/Application Support/clawdbridge" ;;
  *) os=linux; dir="${XDG_DATA_HOME:-$HOME/.local/share}/clawdbridge" ;;
esac
mkdir -p "$dir"
say "Installing the Clawdmeter bridge into $dir"
cat > "$dir/clawdbridge.py" <<'CLAWDBRIDGE_PY'
@@BRIDGE@@
CLAWDBRIDGE_PY

# Python + packages from uv, all inside $dir (no PATH or shell-profile changes).
export UV_UNMANAGED_INSTALL="$dir/bin" UV_PYTHON_INSTALL_DIR="$dir/python" UV_CACHE_DIR="$dir/cache"
uv="$dir/bin/uv"
if [ ! -x "$uv" ]; then
  say "Downloading uv (Python manager)..."
  fetch https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || say "  uv download failed; trying this system's Python"
fi
py=""
if [ -x "$uv" ]; then
  # uv's own Python, kept in $dir: a distro upgrade (3.14 -> 3.15) can't strand the packages, and on macOS
  # Apple's python3 stub (which pops up an Xcode install dialog) is never touched
  if "$uv" venv -q --allow-existing --python-preference only-managed --python 3.12 "$dir/venv"; then
    py="$dir/venv/bin/python"
    "$uv" pip install -q --python "$py" -U zeroconf || say "  (zeroconf didn't install: enter this computer's address on the phone)"
    if [ "$os" = linux ]; then  # macOS: Bluetooth needs an app bundle with a usage description; Wi-Fi only
      "$uv" pip install -q --python "$py" -U bleak || say "  (bleak didn't install: Bluetooth link off, Wi-Fi works)"
    fi
  fi
fi
if [ -z "$py" ] && command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  if python3 -m venv "$dir/venv" >/dev/null 2>&1; then
    py="$dir/venv/bin/python"
    "$py" -m pip install -q -U zeroconf bleak >/dev/null 2>&1 || say "  (extras didn't install: Wi-Fi with a typed address only)"
  else
    py="$(command -v python3)"; say "  (no venv module: Wi-Fi with a typed address only)"
  fi
fi
[ -n "$py" ] || { say "Couldn't get Python 3.10+. Check the internet connection (or install python3) and run this again."; exit 1; }
"$py" "$dir/clawdbridge.py" install

if [ "$os" = linux ]; then  # the phone needs TCP 8787 (and mDNS, UDP 5353) to reach this computer
  fw=""
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    fw="sudo firewall-cmd --permanent --add-port=8787/tcp --add-service=mdns && sudo firewall-cmd --reload"
  elif grep -qs '^ENABLED=yes' /etc/ufw/ufw.conf; then
    fw="sudo ufw allow 8787/tcp && sudo ufw allow 5353/udp"
  fi
  if [ -n "$fw" ]; then
    say "" "A firewall is on. If the phone can't find or reach this computer, run:" "  $fw"
    if [ -t 0 ]; then
      printf 'Run it now? (asks for your password) [y/N] '
      read -r answer || answer=""
      case "$answer" in y|Y|yes) sh -c "$fw" || say "That didn't work; run the command above yourself." ;; esac
    fi
  fi
else
  say "" "If macOS asks whether Python may accept incoming connections, choose Allow."
fi
say "Remove everything: run the uninstall line above, then delete \"$dir\"."
"""

CMD = r"""<# : Clawdmeter bridge installer for Windows: double-click this file.
@echo off
set "CLAWD_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "iex ([IO.File]::ReadAllText($env:CLAWD_SELF))"
echo.
pause
exit /b
#>
# Installs for this user only (no admin), into one folder: the bridge (Wi-Fi + Bluetooth link to the
# Clawdmeter phone app), the Claude Code hooks it needs, and start at sign-in.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072  # TLS 1.2
$dir = Join-Path $env:LOCALAPPDATA 'clawdbridge'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Write-Host "Installing the Clawdmeter bridge into $dir"
try {  # a running bridge holds its files open: ask it to exit
  Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 2 http://127.0.0.1:8787/quit | Out-Null
  Start-Sleep -Seconds 2
} catch {}
$bridge = @'
@@BRIDGE@@
'@
[IO.File]::WriteAllText((Join-Path $dir 'clawdbridge.py'), $bridge, (New-Object Text.UTF8Encoding $false))

# Python + packages from uv, all inside $dir (no PATH changes).
$env:UV_UNMANAGED_INSTALL = "$dir\bin"; $env:UV_PYTHON_INSTALL_DIR = "$dir\python"; $env:UV_CACHE_DIR = "$dir\cache"
$uv = "$dir\bin\uv.exe"
if (-not (Test-Path $uv)) {
  Write-Host 'Downloading uv (Python manager)...'
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
}
$python = $null
if (Test-Path $uv) {
  & $uv venv -q --allow-existing --python-preference only-managed --python 3.12 "$dir\venv"
  if ($LASTEXITCODE -eq 0) {
    $python = "$dir\venv\Scripts\python.exe"
    & $uv pip install -q --python $python -U zeroconf
    if ($LASTEXITCODE) { Write-Host "  (zeroconf didn't install: enter this computer's address on the phone)" }
    & $uv pip install -q --python $python -U bleak
    if ($LASTEXITCODE) { Write-Host "  (bleak didn't install: Bluetooth link off, Wi-Fi works)" }
  }
}
if (-not $python -and (Get-Command py -ErrorAction SilentlyContinue)) {
  Write-Host "uv unavailable; using this system's Python"
  & py -3 -m venv "$dir\venv"
  if ($LASTEXITCODE -eq 0) {
    $python = "$dir\venv\Scripts\python.exe"
    & $python -m pip install -q -U zeroconf bleak
  }
}
if (-not $python) {
  Write-Host "Couldn't get Python. Check the internet connection and run this again."
  return
}
& $python (Join-Path $dir 'clawdbridge.py') install
Write-Host ''
Write-Host 'If Windows asks whether Python may use the network, allow Private networks so the phone can connect.'
Write-Host "Remove everything: run the uninstall line above, then delete $dir"
"""


def main():
    bridge = (HERE / "clawdbridge.py").read_text(encoding="utf-8").rstrip("\n")
    assert bridge.isascii(), "keep the bridge ASCII (Windows PowerShell 5.1 reads the .cmd)"
    assert "\nCLAWDBRIDGE_PY\n" not in bridge and "\n'@" not in bridge, "the bridge would end its heredoc early"
    DIST.mkdir(exist_ok=True)
    sh = SH.replace("@@BRIDGE@@", bridge)
    for name in ("clawdbridge-install-linux.sh", "clawdbridge-install-macos.command"):
        (DIST / name).write_text(sh, encoding="utf-8", newline="\n")
        (DIST / name).chmod(0o755)
    cmd = CMD.replace("@@BRIDGE@@", bridge)
    (DIST / "clawdbridge-install-windows.cmd").write_bytes(cmd.replace("\n", "\r\n").encode("ascii"))  # cmd.exe wants CRLF
    apk = HERE.parent / "android/app/build/outputs/apk/debug/app-debug.apk"
    if apk.exists():
        shutil.copy2(apk, DIST / "clawdmeter.apk")
    print("\n".join(sorted(p.name for p in DIST.iterdir())))


if __name__ == "__main__":
    main()
