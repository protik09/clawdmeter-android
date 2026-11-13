# Clawdmeter on Android

A phone version of [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter): Claude Code session/weekly usage,
the computers and projects Claude is working on, Clawd (upstream sprites), the status line, and attention alerts
with a chime. The screen never sleeps while the app is in front (`FLAG_KEEP_SCREEN_ON`), and it shows over the
lock screen.

## Install (what to hand to users): `dist/`

| Device | File | How |
|---|---|---|
| Android phone | `clawdmeter.apk` | sideload (Android 13+) |
| Linux | `clawdbridge-install-linux.sh` | `sh clawdbridge-install-linux.sh` |
| macOS | `clawdbridge-install-macos.command` | double-click (or `sh clawdbridge-install-macos.command`) |
| Windows | `clawdbridge-install-windows.cmd` | double-click |

Each installer is one self-contained file. It carries the bridge itself and installs it for the current user,
with no admin, into one folder:
- Python and packages come from [uv](https://docs.astral.sh/uv/) (x86-64 and ARM64), kept inside that folder.
  If uv can't be fetched, the system's Python 3.10+ is used.
- It adds the Claude Code hooks to `~/.claude/settings.json` (backup: `settings.json.bak-clawdbridge`). These
  replace upstream Clawdmeter's hooks if they're there.
- It sets the bridge to start at login and starts it now: a systemd user service, else XDG autostart, on Linux; a
  LaunchAgent on macOS; an `HKCU\...\Run` entry on Windows.
- It prints the uninstall command at the end.

Tested on Ubuntu 24.04, Debian 12, Fedora, openSUSE Tumbleweed, Arch and Ubuntu ARM64 (containers), and on CachyOS
with a real phone. The Windows and macOS installers haven't been run on those systems yet.

## Links

- **Wi-Fi:** every bridge announces itself over mDNS (`_clawdmeter._tcp`), and the phone shows all of them at once:
  each computer's projects, alerts named by computer, and meters from the account whose usage rose most recently.
  You can also add a computer by address (long-press the clock).
- **Bluetooth:** the phone acts as a Clawdmeter board (upstream's GATT protocol), and each bridge connects when the
  phone is in Bluetooth mode. When several computers are connected, the phone asks which one to show, and
  remembers the choice.
  - The bridge sends the computer and project names too.
  - Upstream's desktop app also works over Bluetooth, but without project names. Don't run it alongside the bridge.
- **Platform notes:**
  - macOS bridges are Wi-Fi only: Bluetooth needs an app bundle with a usage description.
  - Windows asks once whether Python may use the network; allow Private networks.
  - Linux: if `ufw` or `firewalld` is on, the installer offers the command that opens TCP 8787 and mDNS.
  - If port 8787 is taken (e.g. by `wrangler dev` or RStudio), the bridge uses the next free port. Discovery finds
    it either way; a typed address then needs that port.

## Running it unattended

- **Kiosk mode (recommended for a spare phone):** set Clawdmeter as the Home app (Settings → Apps → Default apps →
  Home app). Android then relaunches it after a reboot, and after a WebView update, which closes every app that
  uses WebView.
- **Self-recovery, without kiosk mode:**
  - A Bluetooth off/on restarts the phone's Bluetooth link.
  - A killed WebView renderer rebuilds the page.
  - A computer whose address changed is found again.
- **Power:** the screen redraws only when something changes and runs at 60 Hz. When idle, the app uses about 2–4%
  of a CPU core.
- **Burn-in:** the layout shifts a few pixels every minute.

## Troubleshooting

- **Bluetooth connects, then drops, or never connects**, and the bridge log mentions pairing: the phone is paired
  with that computer (for example from upstream's app). Forget the pairing on both sides: the phone's Bluetooth
  settings, and the computer's (`bluetoothctl remove <phone address>` on Linux). The bridge doesn't need pairing,
  and Linux often can't reach a paired Android phone's private address.
- **Logs:** `journalctl --user -u clawdbridge -f` (Linux), `~/.config/clawdmeter/bridge.log` (macOS) or
  `%LOCALAPPDATA%\Clawdmeter\bridge.log` (Windows).

## Phone controls

- Tap the clock: brightness (system → 50% → 20% → 5%).
- Long-press the clock: Wi-Fi/Bluetooth, the list of computers (add or remove by address), the Bluetooth computer
  choice, and a test alert.
- Tap anywhere: dismiss an alert (the computer that raised it is told), or make Clawd hop.
- Status line (bottom):
  - While any session is working (a prompt or tool ran and it hasn't stopped), a spinner and Claude's verbs.
  - When Claude has finished or is waiting on you, `Done 3:12 AM`, the time the last turn ended.
  - `Idle` after 12 hours.
  - An interrupted turn sends no Stop, so it also counts as finished after 15 quiet minutes.

## Build

```sh
python3 bridge/clawdbridge.py selftest           # bridge checks (usage parsing, activity, hooks, settings merge)
export JAVA_HOME=~/.local/opt/jdk17 ANDROID_HOME=~/Android/Sdk
(cd android && ~/.local/opt/gradle-8.14.3/bin/gradle assembleDebug)
python3 bridge/build_installers.py               # dist/: the three installers + clawdmeter.apk
```

- `bridge/clawdbridge.py` is the only copy of the bridge; the installers embed it.
- The UI is `android/app/src/main/assets/index.html`. With a bridge running from the repo, `http://localhost:8787/`
  previews it in a browser.
- Clawd frames were extracted from upstream `firmware/src/splash_animations.h`, and the fonts are upstream's. Both
  belong to Anthropic and are unlicensed upstream, so keep this to personal use.
