# Clawdmeter on Android

A phone version of [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter): Claude Code session/weekly usage,
the computers and projects Claude is working on. It also shows over the lock screen.

## Install

Download the latest files from the [Releases page](https://github.com/protik09/clawdmeter-android/releases/latest).

| Device | File | How |
|---|---|---|
| Android phone | `clawdmeter-protik09-<version>.apk` | sideload (Android 13+) |
| Linux | `clawdbridge-install-linux.sh` | `sh clawdbridge-install-linux.sh` |
| macOS | `clawdbridge-install-macos.command` | double-click (or `sh clawdbridge-install-macos.command`) |
| Windows | `clawdbridge-install-windows.cmd` | double-click |

## Links

- **Platform notes:**
  - macOS bridges are Wi-Fi only: Bluetooth needs an app bundle with a usage description.
  - Windows asks once whether Python may use the network; allow Private networks.
  - Linux: if `ufw` or `firewalld` is on, the installer offers the command that opens TCP 8787 and mDNS.
  - Linux without systemd (Alpine, Void, Devuan…): the bridge starts from an `@reboot` crontab line when a cron
    daemon runs, otherwise from a desktop autostart entry. The installer says which, or that nothing will.
  - Headless Linux with systemd (a Pi over SSH): run `loginctl enable-linger` once, or the bridge stops at logout.
  - WSL and ChromeOS's Linux sit behind NAT with no Bluetooth, so the phone can't reach them as is.

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
- **Logs:** `journalctl --user -u clawdbridge -f` (Linux with systemd), `~/.config/clawdmeter/bridge.log` (macOS,
  Linux without systemd) or
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
python3 bridge/build_installers.py               # dist/: the three installers + clawdmeter-protik09-dev.apk
```

## License

The code is under Apache-2.0 ([LICENSE](LICENSE)), except for these files:

- The Clawd sprites (`android/app/src/main/assets/clawd/*.png`) and the launcher icon are Anthropic's Clawd
  character. The sprites were extracted from upstream Clawdmeter, which has no license.
- `DejaVuSansMono.ttf` is under the Bitstream Vera license, which is embedded in the font file.
