# VPN Toggle

A tiny always-on-top switch for **Palo Alto GlobalProtect** on Windows. One click connects or disconnects the VPN, and the switch always shows the real connection state. (Original Nat's idea).
## How it works

GlobalProtect has no command line on Windows, so VPN Toggle drives the GlobalProtect app's own window:

1. It opens the GlobalProtect panel (by launching `PanGPA.exe`, which brings up the panel if the app is already running).
2. It clicks **Connect** / **Disconnect**, either on the panel itself or inside the **≡** menu in its top-right corner, depending on your GlobalProtect version.
3. It gives focus back so the panel hides again.

This automation runs in a separate process (`main.py --connect` / `--disconnect`), so the switch window never freezes.

The GlobalProtect panel hides as soon as another window gets the focus. If you click somewhere else during the few seconds it's open, the panel closes before the click can happen. VPN Toggle then reopens the panel and tries again (up to 3 tries). Before showing an error, it also waits a few seconds to check whether the VPN switched anyway. For the most reliable result, leave the mouse alone while the panel is open.

To read the **status**, VPN Toggle watches GlobalProtect's virtual network adapter (`PANGP Virtual Ethernet Adapter`). The VPN counts as connected only when the adapter is up **and** has been given its VPN address. That way the switch says *Connecting…* until the tunnel is actually ready. If you connect or disconnect from the tray icon instead, the switch follows along.

## Requirements

- Windows 10/11
- Python 3.8+ (with Tk, which the standard Windows installer includes)
- GlobalProtect installed in its default location
- Python packages:

```bash
pip install pywinauto psutil
```

## Usage

Start the switch without a console window:

```bash
pythonw main.py
```

| Action                          | Result                                                  |
|---------------------------------|---------------------------------------------------------|
| Click the toggle                | Connect / disconnect                                    |
| Drag anywhere                   | Move the switch (its position is remembered)            |
| Drag the bottom-right corner    | Resize (or hold **Ctrl** and use the mouse wheel)       |
| Right-click                     | Menu: open GlobalProtect, always on top, reset size, quit |

Only the toggle itself switches the VPN, so grabbing the switch to move it never connects or disconnects by accident. The cursor shows what a click will do: a hand over the toggle, a move cursor over the rest, and a resize arrow at the corner.

The switch always stays fully on screen. You can't push it past a screen edge or under the taskbar, but you can drag it onto another monitor. If a monitor is unplugged, the switch moves back onto a remaining one.

Switch colors:

- **Green, knob right**: connected
- **Grey, knob left**: disconnected (or status unknown)
- **Yellow, knob centered**: connecting or disconnecting. Clicks are ignored until the change finishes (the cursor shows as busy).

A blue outline around the switch makes it easy to spot on any background.

Only one copy runs at a time. Settings (position, size, always-on-top) are saved to `%APPDATA%\vpn_toggle.json`.

### Command line

```bash
python main.py --connect       # connect and exit
python main.py --disconnect    # disconnect and exit
python main.py --inspect       # write gp_controls.txt for troubleshooting
pythonw main.py --demo         # test the switch without GlobalProtect
```

`--connect` / `--disconnect` exit with code `0` on success and `3` if the VPN was already in that state. Any other code is an error (see `ERROR_TEXT` in `main.py`).

`--demo` drives a `fake_globalprotect.ps1` stand-in placed next to `main.py`. That script is not included in this repository.

### Start with Windows

Press `Win+R`, run `shell:startup`, and create a shortcut there with this target:

```
"<pythonw.exe path>" "<VPN Toggle folder>\main.py"
```

Replace both placeholders with the paths on your computer:

- **`<pythonw.exe path>`**: run `where pythonw` in a Command Prompt (or `(Get-Command pythonw).Source` in PowerShell).
- **`<VPN Toggle folder>`**: the folder where you saved this project, for example `C:\Users\<you>\Documents\VPNtoggle`.

For example:

```
"C:\Users\<you>\AppData\Local\Programs\Python\Python311\pythonw.exe" "C:\Users\<you>\Documents\VPNtoggle\main.py"
```

## Configuration

The settings block at the top of `main.py` covers the usual differences between GlobalProtect installs:

| Setting | Purpose |
|---------|---------|
| `GP_EXE_CANDIDATES` | Where to look for `PanGPA.exe` |
| `GP_WINDOW_TITLE` | Regex for the GlobalProtect panel's window title |
| `CONNECT_LABELS` / `DISCONNECT_LABELS` | Button/menu captions; add translations for non-English GlobalProtect |
| `MENU_BUTTON_LABELS`, `MENU_BUTTON_OFFSET`, `FORCE_MENU_BUTTON_OFFSET` | How to find the ≡ menu button |
| `ADAPTER_DESCRIPTION` / `ADAPTER_NAME` | Which network adapter is the VPN; set `ADAPTER_NAME` (e.g. `"Ethernet 3"`) to skip auto-detection |
| `CONNECT_TIMEOUT_S` / `DISCONNECT_TIMEOUT_S` | How long the switch waits for the VPN to change state before giving up |
| `CLICK_COOLDOWN_S` | Clicks ignored right after a change finishes, so a double-click doesn't undo it |
| `AUTOMATION_ATTEMPTS` | How many times to reopen the GlobalProtect panel and retry if it closes early |
| `VERIFY_S` | After a failed try, how long to watch the adapter for a change before showing an error |
| `HIDE_PANEL_AFTER_CLICK` | Take focus back so the GlobalProtect panel auto-hides |
| `OUTLINE_COLOR` / `OUTLINE_WIDTH` | Color and thickness of the switch's border |
| `MIN_ZOOM` / `MAX_ZOOM` | How small or large the switch can be resized |

## Troubleshooting

**"No Connect/Disconnect button was found" or "wasn't found in its ≡ menu"**: run

```bash
python main.py --inspect
```

It opens the panel and the ≡ menu, then writes every control it sees to `gp_controls.txt`. Look up the real captions there and update `CONNECT_LABELS` / `DISCONNECT_LABELS`, `MENU_BUTTON_LABELS` or `MENU_BUTTON_OFFSET`.

**"The GlobalProtect window closed before the VPN could be switched"**: the panel kept closing, usually because something was clicked during every try. Click the toggle again and leave the mouse alone for a few seconds.

**"The GlobalProtect window didn't appear, or closed right away"**: if you clicked elsewhere while it was opening, just try again. If it keeps happening, `gp_controls.txt` lists the visible window titles. Adjust `GP_WINDOW_TITLE` to match.

**Switch stays on "Status unknown"**: the VPN adapter wasn't found. In PowerShell, run `Get-NetAdapter -IncludeHidden` and either fix `ADAPTER_DESCRIPTION` or set `ADAPTER_NAME`.

**Switch stays yellow**: GlobalProtect didn't reach the new state (for example, a sign-in prompt is waiting). The switch gives up after the timeout and shows the actual state again.
