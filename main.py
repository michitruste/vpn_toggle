#!/usr/bin/env python3
"""
VPN Toggle - a tiny always-on-top switch for Palo Alto GlobalProtect (Windows).

GlobalProtect has no command line on Windows, so this program drives the app's
own window: it pops open the GlobalProtect panel, opens its menu (the "three
lines" button, top right), picks Connect/Disconnect, and lets the panel hide. The connection status is read from
GlobalProtect's virtual network adapter, so the switch stays correct even if
you connect or disconnect from the tray icon.

Setup:    pip install pywinauto psutil
Run:      pythonw main.py            (no console window)
Debug:    python  main.py --inspect  (writes gp_controls.txt listing the
                                       GlobalProtect window's controls)
Scripts:  python  main.py --connect | --disconnect
Test:     pythonw main.py --demo     (no GlobalProtect needed: drives the
                                       fake_globalprotect.ps1 stand-in that
                                       sits next to this file)

Using the switch:
  click the toggle          -> connect / disconnect the VPN
  drag anywhere             -> move the window (position is remembered)
  drag bottom-right corner  -> resize (or Ctrl + mouse wheel)
  right-click               -> menu (open GlobalProtect, always on top,
                               reset size, quit)
"""

import ctypes
import importlib.util
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox

try:
    import psutil
except ImportError:
    psutil = None


# --------------------------------------------------------------------------
# Settings - adjust these if your GlobalProtect install differs
# --------------------------------------------------------------------------

GP_EXE_CANDIDATES = [
    r"C:\Program Files\Palo Alto Networks\GlobalProtect\PanGPA.exe",
    r"C:\Program Files (x86)\Palo Alto Networks\GlobalProtect\PanGPA.exe",
]

# Regex matched against top-level window titles to find the GlobalProtect panel.
GP_WINDOW_TITLE = r"^GlobalProtect"

# Menu item / button captions to look for (add translations if needed).
CONNECT_LABELS = ["Connect"]
DISCONNECT_LABELS = ["Disconnect"]

# Newer GlobalProtect versions keep Connect/Disconnect inside the menu that
# opens from the "three lines" button in the panel's top-right corner.
# Names that button may have; if none match, the program picks the top-right
# button in the panel, and as a last resort clicks MENU_BUTTON_OFFSET.
MENU_BUTTON_LABELS = ["Global Protect Hamburger Menu", "Main Menu", "Menu",
                      "Open menu", "Toggle menu", "Hamburger", "≡", "☰"]
# Where the menu button sits: (pixels from the panel's right edge, pixels
# from its top edge), at 100% display scaling.
MENU_BUTTON_OFFSET = (24, 24)
# Set True to always click MENU_BUTTON_OFFSET instead of searching for it.
FORCE_MENU_BUTTON_OFFSET = False

# GlobalProtect's virtual adapter, used to read the connection status.
ADAPTER_DESCRIPTION = "PANGP Virtual Ethernet Adapter*"
ADAPTER_NAME = None  # e.g. "Ethernet 3" to skip auto-detection

HIDE_PANEL_AFTER_CLICK = True  # take focus back so the GP panel auto-hides
POLL_MS = 2000                 # how often to re-check the status
CONNECT_TIMEOUT_S = 90         # give up waiting for the VPN to come up after this
DISCONNECT_TIMEOUT_S = 30      # ...or to go down
PANEL_TIMEOUT_S = 10           # how long to wait for the GP panel to appear
AUTOMATION_ATTEMPTS = 3        # tries if the GP panel closes before the click
VERIFY_S = 5                   # after a failed try, wait this long to see if
                               # the VPN changed anyway before showing an error
CLICK_COOLDOWN_S = 1.0         # ignore clicks this long after a toggle finishes

OUTLINE_COLOR = "#3b9eff"      # border that makes the switch easy to spot
OUTLINE_WIDTH = 2              # border thickness, in pixels at normal size
MIN_ZOOM, MAX_ZOOM = 0.75, 3.0 # resize limits (1.0 = normal size)

CONFIG_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "vpn_toggle.json"

# --------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000

# --demo: test without GlobalProtect, using the fake_globalprotect.ps1 stand-in.
DEMO = "--demo" in sys.argv[1:]
HERE = (Path(sys.executable).parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
FAKE_GP_SCRIPT = HERE / "fake_globalprotect.ps1"
DEMO_STATE_FILE = Path(os.environ.get("TEMP", str(Path.home()))) / "fake_globalprotect_state.txt"

EXIT_OK, EXIT_ERROR, EXIT_NO_BUTTON, EXIT_ALREADY = 0, 1, 2, 3
EXIT_NO_WINDOW, EXIT_NO_EXE, EXIT_NO_PYWINAUTO, EXIT_TIMEOUT = 4, 5, 6, 7
EXIT_NO_MENU, EXIT_PANEL_CLOSED = 8, 9
# Failures that may just mean the panel was closed by a click elsewhere.
RETRYABLE = {EXIT_ERROR, EXIT_NO_WINDOW, EXIT_NO_MENU, EXIT_PANEL_CLOSED}

ERROR_TEXT = {
    EXIT_PANEL_CLOSED: "The GlobalProtect window closed before the VPN could be "
                       "switched. It hides as soon as you click somewhere else.\n\n"
                       "Click the toggle to try again, and leave the mouse alone "
                       "for a few seconds while the GlobalProtect window is open.",
    EXIT_ERROR: "Something went wrong while pressing the GlobalProtect button.",
    EXIT_NO_BUTTON: "The GlobalProtect window opened, but no Connect/Disconnect "
                    "button was found.\n\nRun  python main.py --inspect  "
                    "and check gp_controls.txt for the button's caption, then "
                    "update CONNECT_LABELS / DISCONNECT_LABELS.",
    EXIT_NO_WINDOW: "The GlobalProtect window didn't appear, or closed right "
                    "away (it hides if you click somewhere else while it "
                    "opens).\n\nClick the toggle to try again. If it keeps "
                    "happening, run  python main.py --inspect  to see which "
                    "windows are visible, then adjust GP_WINDOW_TITLE.",
    EXIT_NO_EXE: "PanGPA.exe wasn't found. Add its path to GP_EXE_CANDIDATES.",
    EXIT_NO_PYWINAUTO: "pywinauto isn't installed.\n\nRun:  pip install pywinauto",
    EXIT_TIMEOUT: "GlobalProtect didn't respond in time.",
    EXIT_NO_MENU: "The GlobalProtect window opened, but Connect/Disconnect "
                  "wasn't found in its ≡ menu.\n\nRun  python main.py "
                  "--inspect  and send the gp_controls.txt it creates, so the "
                  "settings can be adjusted to your GlobalProtect version.",
}
if DEMO:
    ERROR_TEXT[EXIT_NO_EXE] = ("fake_globalprotect.ps1 wasn't found. Put it in the "
                               "same folder as main.py.")


# ============================ GlobalProtect automation =====================
# Runs in a separate process (main.py --connect / --disconnect) so the
# UI Automation work never blocks or interferes with the switch window.

def find_gp_exe():
    for path in GP_EXE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def gp_command(background=False):
    """Command that starts GlobalProtect (or brings its panel up if running)."""
    if DEMO:
        if not FAKE_GP_SCRIPT.exists():
            return None
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA",
               "-WindowStyle", "Hidden", "-File", str(FAKE_GP_SCRIPT)]
        if background:
            cmd.append("-Background")
        return cmd
    exe = find_gp_exe()
    return [exe] if exe else None


def signal_fake_gp_exit():
    k = ctypes.windll.kernel32
    handle = k.OpenEventW(0x0002, False, "Local\\FakeGlobalProtectExit")  # EVENT_MODIFY_STATE
    if handle:
        k.SetEvent(handle)
        k.CloseHandle(handle)


def gp_windows(desktop):
    found = []
    for w in desktop.windows():
        try:
            if re.search(GP_WINDOW_TITLE, w.window_text() or ""):
                found.append(w)
        except Exception:
            pass
    return found


def norm(text):
    """'&Disconnect...' -> 'disconnect' so menu captions compare cleanly."""
    return (text or "").replace("&", "").strip().rstrip(".…").strip().lower()


def safe_descendants(win):
    try:
        return win.descendants()
    except Exception:
        return []


def find_labeled(windows, labels):
    wanted = {norm(label) for label in labels}
    for w in windows:
        for ctrl in [w] + safe_descendants(w):
            try:
                if norm(ctrl.window_text()) in wanted:
                    return ctrl
            except Exception:
                continue
    return None


def press(ctrl):
    try:
        ctrl.invoke()  # UI Automation "press", no mouse movement
        return
    except Exception:
        pass
    ctrl.click_input()


def open_gp_panel(desktop, cmd):
    # Launching PanGPA.exe while it's already running brings its panel up.
    subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
    deadline = time.time() + PANEL_TIMEOUT_S
    while time.time() < deadline:
        wins = gp_windows(desktop)
        if wins:
            return wins
        time.sleep(0.3)
    return []


def process_windows(desktop, pid):
    """All visible top-level windows of GlobalProtect - its pop-up menu is one."""
    found = []
    for w in desktop.windows():
        try:
            if w.process_id() == pid:
                found.append(w)
        except Exception:
            pass
    return found


TITLE_BAR_NAMES = {"close", "minimize", "maximize", "restore", "help", "system"}
CLICKABLE_TYPES = {"Button", "MenuItem", "SplitButton", "Hyperlink", "Image",
                   "Custom", "MenuBar"}


def find_menu_button(panel):
    """The 'three lines' button: by name if possible, else the top-right button."""
    controls = safe_descendants(panel)
    wanted = {norm(label) for label in MENU_BUTTON_LABELS}
    for c in controls:
        try:
            if norm(c.window_text()) in wanted:
                return c
        except Exception:
            continue

    try:
        pr = panel.rectangle()
    except Exception:
        return None
    best, best_key = None, None
    for c in controls:
        try:
            info = c.element_info
            if info.control_type not in CLICKABLE_TYPES:
                continue
            if norm(c.window_text()) in TITLE_BAR_NAMES:
                continue
            parent = c.parent()
            if parent is not None and parent.element_info.control_type == "TitleBar":
                continue
            r = c.rectangle()
        except Exception:
            continue
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            continue
        if w > pr.width() * 0.3 or h > pr.height() * 0.2:
            continue  # too big to be a small icon button
        if r.top - pr.top > pr.height() * 0.25:
            continue  # not near the top
        key = (r.right, -r.top)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best


def click_menu_offset(panel):
    try:
        scale = ctypes.windll.user32.GetDpiForWindow(panel.handle) / 96.0 or 1.0
    except Exception:
        scale = 1.0
    r = panel.rectangle()
    ox, oy = MENU_BUTTON_OFFSET
    panel.click_input(coords=(int(r.width() - ox * scale), int(oy * scale)))


def wait_for_menu_item(desktop, pid, panel, target, other, timeout):
    """Look for Connect/Disconnect in GlobalProtect's open menu.
    Returns ('target'|'other'|'closed'|None, control)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = process_windows(desktop, pid)
        if panel not in wins:
            return "closed", None  # the panel hid: someone clicked elsewhere
        ctrl = find_labeled(wins, target)
        if ctrl:
            return "target", ctrl
        ctrl = find_labeled(wins, other)
        if ctrl:
            return "other", ctrl
        time.sleep(0.25)
    return None, None


def close_menu(desktop, pid):
    """Press Esc to close GlobalProtect's menu - only while that menu is open,
    so the key never lands in another program."""
    try:
        if not any(w.element_info.class_name == "#32768"  # a pop-up menu
                   for w in process_windows(desktop, pid)):
            return
        from pywinauto.keyboard import send_keys
        send_keys("{ESC}")
    except Exception:
        pass


def open_menu_and_find(desktop, panel, target, other):
    """Click the menu button; returns ('target'|'other'|'closed'|None, control)."""
    pid = panel.process_id()
    button = None if FORCE_MENU_BUTTON_OFFSET else find_menu_button(panel)
    if button is not None:
        for click, wait in ((button.invoke, 1.5), (button.click_input, 3)):
            try:
                click()
                kind, ctrl = wait_for_menu_item(desktop, pid, panel, target, other, wait)
                if kind:
                    return kind, ctrl
            except Exception:
                if not panel_visible(panel):
                    return "closed", None
        close_menu(desktop, pid)
    if not panel_visible(panel):
        return "closed", None
    click_menu_offset(panel)
    return wait_for_menu_item(desktop, pid, panel, target, other, 3)


def panel_visible(panel):
    try:
        return panel.is_visible()
    except Exception:
        return False  # the window is gone


def automation_attempt(desktop, cmd, target, other, refocus):
    """One try at pressing Connect/Disconnect; returns an EXIT_* code."""
    wins = open_gp_panel(desktop, cmd)
    if not wins:
        return EXIT_NO_WINDOW
    pid = wins[0].process_id()
    if refocus:
        # An earlier try lost the panel because another window took the
        # focus; take it back, or the panel's menu won't open.
        try:
            wins[0].set_focus()
        except Exception:
            pass

    try:
        # Wait for the panel to finish drawing. Some versions show the
        # Connect/Disconnect button right on the panel - use it if so.
        deadline = time.time() + 4
        while time.time() < deadline:
            wins = gp_windows(desktop)
            if not wins:
                return EXIT_PANEL_CLOSED
            ctrl = find_labeled(wins, target)
            if ctrl:
                press(ctrl)
                return EXIT_OK
            if find_labeled(wins, other):
                return EXIT_ALREADY
            if FORCE_MENU_BUTTON_OFFSET or find_menu_button(wins[0]) is not None:
                break
            time.sleep(0.4)

        # Otherwise: open the menu, then pick Connect or Disconnect.
        kind, ctrl = open_menu_and_find(desktop, wins[0], target, other)
        if kind == "target":
            press(ctrl)
            return EXIT_OK
        close_menu(desktop, pid)
        if kind == "other":
            return EXIT_ALREADY
        if kind == "closed":
            return EXIT_PANEL_CLOSED
        return EXIT_NO_MENU
    except Exception:
        close_menu(desktop, pid)
        return EXIT_ERROR if panel_visible(wins[0]) else EXIT_PANEL_CLOSED


def automation_main(action):
    try:
        from pywinauto import Desktop
    except ImportError:
        return EXIT_NO_PYWINAUTO

    cmd = gp_command()
    if not cmd:
        return EXIT_NO_EXE

    desktop = Desktop(backend="uia")
    target = CONNECT_LABELS if action == "connect" else DISCONNECT_LABELS
    other = DISCONNECT_LABELS if action == "connect" else CONNECT_LABELS

    # The panel hides whenever another window gets the focus, e.g. when you
    # click elsewhere while this runs. Then just open it again and retry. A
    # retry also notices if the first try got through after all: the menu
    # then offers the opposite action, which reports EXIT_ALREADY.
    results = []
    for attempt in range(AUTOMATION_ATTEMPTS):
        if attempt:
            time.sleep(0.5)  # let the panel finish hiding
        rc = automation_attempt(desktop, cmd, target, other, refocus=attempt > 0)
        if rc not in RETRYABLE:
            return rc
        results.append(rc)
    if EXIT_PANEL_CLOSED in results:
        return EXIT_PANEL_CLOSED  # the real cause, whatever the last try hit
    return results[-1]


def dump_controls(f, win):
    try:
        r = win.rectangle()
        f.write(f"=== Window {win.window_text()!r} type={win.element_info.control_type} "
                f"class={win.element_info.class_name!r} "
                f"rect=({r.left},{r.top},{r.right},{r.bottom}) ===\n")
    except Exception:
        f.write("=== Window (unreadable) ===\n")
    for c in safe_descendants(win):
        try:
            info = c.element_info
            r = c.rectangle()
            f.write(f"  {info.control_type:<12} {c.window_text()!r:<32} "
                    f"id={info.automation_id!r} class={info.class_name!r} "
                    f"rect=({r.left},{r.top},{r.right},{r.bottom})\n")
        except Exception:
            pass


def inspect_main():
    try:
        from pywinauto import Desktop
    except ImportError:
        print(ERROR_TEXT[EXIT_NO_PYWINAUTO])
        return EXIT_NO_PYWINAUTO

    cmd = gp_command()
    if not cmd:
        print(ERROR_TEXT[EXIT_NO_EXE])
        return EXIT_NO_EXE

    desktop = Desktop(backend="uia")
    wins = open_gp_panel(desktop, cmd)
    time.sleep(1.5)  # let the panel finish drawing
    wins = gp_windows(desktop) or wins

    out = Path("gp_controls.txt").resolve()
    with out.open("w", encoding="utf-8") as f:
        if not wins:
            f.write("No window matched GP_WINDOW_TITLE. Visible top-level windows:\n")
            for w in desktop.windows():
                try:
                    f.write(f"  {w.window_text()!r}\n")
                except Exception:
                    pass
            print(f"Wrote {out}")
            return EXIT_NO_WINDOW

        f.write("##### 1. The GlobalProtect panel #####\n")
        for w in wins:
            dump_controls(f, w)

        panel = wins[0]
        button = find_menu_button(panel)
        f.write("\n##### 2. Menu button chosen #####\n")
        if button is not None:
            try:
                r = button.rectangle()
                f.write(f"  {button.element_info.control_type} {button.window_text()!r} "
                        f"rect=({r.left},{r.top},{r.right},{r.bottom})\n")
                button.click_input()
            except Exception as e:
                f.write(f"  click failed: {e!r}\n")
        else:
            f.write(f"  none found - clicking MENU_BUTTON_OFFSET {MENU_BUTTON_OFFSET}\n")
            try:
                click_menu_offset(panel)
            except Exception as e:
                f.write(f"  click failed: {e!r}\n")

        time.sleep(1.5)  # let the menu open
        f.write("\n##### 3. GlobalProtect windows after opening the menu #####\n")
        try:
            pid = panel.process_id()
            for w in process_windows(desktop, pid):
                dump_controls(f, w)
        except Exception as e:
            f.write(f"  failed: {e!r}\n")
        try:
            close_menu(desktop, panel.process_id())
        except Exception:
            pass

    print(f"Wrote {out}")
    return EXIT_OK


# ============================ Status detection =============================

def run_hidden(args, timeout):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, creationflags=CREATE_NO_WINDOW)


def find_adapter_name():
    ps = (f"Get-NetAdapter -IncludeHidden -InterfaceDescription '{ADAPTER_DESCRIPTION}' "
          f"| Select-Object -First 1 -ExpandProperty Name")
    try:
        result = run_hidden(["powershell", "-NoProfile", "-Command", ps], timeout=20)
        return result.stdout.strip() or None
    except Exception:
        return None


def has_tunnel_address(name):
    """GlobalProtect gives its adapter the VPN address only once the tunnel is
    up, so a link that is up but still has no address is still connecting."""
    for a in psutil.net_if_addrs().get(name, []):
        if a.family == socket.AF_INET:
            if a.address and not a.address.startswith(("169.254.", "0.")):
                return True
        elif a.family == socket.AF_INET6:
            addr = (a.address or "").split("%")[0].lower()
            if addr and addr != "::" and not addr.startswith("fe80"):
                return True
    return False


# ============================ Helpers ======================================

def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32 = ctypes.WinDLL("user32")
_user32.MonitorFromPoint.restype = wintypes.HMONITOR
_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]


def clamp_to_screen(x, y, w, h, point=None):
    """Keep a w*h window at (x, y) fully inside the work area (screen minus
    taskbar) of the monitor nearest `point` (default: the window's center)."""
    px, py = point or (x + w // 2, y + h // 2)
    try:
        hmon = _user32.MonitorFromPoint(wintypes.POINT(int(px), int(py)),
                                        2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return x, y
    except Exception:
        return x, y
    r = info.rcWork
    return (max(r.left, min(x, r.right - w)),
            max(r.top, min(y, r.bottom - h)))


def already_running():
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VpnToggleSingleInstance")
        return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception:
        pass


def self_command():
    extra = ["--demo"] if DEMO else []
    if getattr(sys, "frozen", False):  # packaged with PyInstaller
        return [sys.executable] + extra
    return [sys.executable, os.path.abspath(__file__)] + extra


# ============================ The switch window ============================

KEY = "#010203"  # transparent color, gives the window rounded corners
BG = "#1f2023"
FG = "#ececec"
SUB = "#9aa0a6"
TRACK = {"on": "#2ea043", "off": "#55585e", "unknown": "#55585e", "busy": "#d29922"}


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("VPN Toggle")
        self.root.overrideredirect(True)
        self.root.configure(bg=KEY)
        try:
            self.root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        cfg = load_config()
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.zoom = min(max(float(cfg.get("zoom", 1.0)), MIN_ZOOM), MAX_ZOOM)
        self.w, self.h = self.px(178), self.px(46)

        sw = self.root.winfo_screenwidth()
        x = cfg.get("x", sw - self.w - self.px(40))
        y = cfg.get("y", self.px(40))
        # A saved position can be off-screen if a monitor was unplugged.
        x, y = clamp_to_screen(x, y, self.w, self.h)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")

        self.topmost = tk.BooleanVar(value=cfg.get("topmost", True))
        self.root.attributes("-topmost", self.topmost.get())

        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h, bg=KEY,
                                highlightthickness=0, cursor="fleur")
        self.canvas.pack()

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open GlobalProtect", command=self.open_gp)
        self.menu.add_checkbutton(label="Always on top", variable=self.topmost,
                                  command=self.apply_topmost)
        self.menu.add_command(label="Reset size", command=lambda: self.set_zoom(1.0, save=True))
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)
        self.menu_open = False

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_hover)
        self.canvas.bind("<Control-MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-3>", self.on_menu)

        self.state = "unknown"   # "on" / "off" / "unknown"
        self.busy = None         # {"target", "since", "timeout", "label", ["error"]}
        self.worker = None       # thread running the GlobalProtect automation
        self.cooldown_until = 0.0
        self.adapter = ADAPTER_NAME
        self.adapter_checked = bool(ADAPTER_NAME)  # a lookup has finished
        self.link_up = False     # adapter is up, maybe still without VPN address
        self.lookup_running = False
        self.last_lookup = 0.0
        self.results = queue.Queue()
        self.drag = None         # (mouse x, mouse y, window x, window y, zoom, zone)
        self.dragging = False
        self.hover = None        # last mouse position over the switch
        self.drawn = None

        if DEMO:
            # Start the stand-in in the background, like GlobalProtect in the tray.
            cmd = gp_command(background=True)
            if cmd:
                subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)

        self.draw()
        self.root.after(50, self.poll)
        self.root.after(150, self.check_results)

    def px(self, n):
        return int(round(n * self.scale * self.zoom))

    def toggle_box(self):
        """(x1, y1, x2, y2) of the switch's track."""
        tw, th = self.px(44), self.px(24)
        tx2 = self.w - self.px(14)
        ty1 = (self.h - th) / 2
        return tx2 - tw, ty1, tx2, ty1 + th

    def zone(self, x, y):
        """What's under (x, y): 'toggle', 'grip' (resize corner) or 'body'."""
        tx1, ty1, tx2, ty2 = self.toggle_box()
        r = (ty2 - ty1) / 2
        nearest_x = min(max(x, tx1 + r), tx2 - r)
        if (x - nearest_x) ** 2 + (y - (ty1 + r)) ** 2 <= (r + self.px(2)) ** 2:
            return "toggle"
        g = self.px(18)
        if x >= self.w - g and y >= self.h - g:
            return "grip"
        return "body"

    # ---------------- drawing ----------------

    def rounded(self, x1, y1, x2, y2, r, fill):
        c = self.canvas
        c.create_oval(x1, y1, x1 + 2 * r, y1 + 2 * r, fill=fill, outline=fill)
        c.create_oval(x2 - 2 * r, y1, x2, y1 + 2 * r, fill=fill, outline=fill)
        c.create_oval(x1, y2 - 2 * r, x1 + 2 * r, y2, fill=fill, outline=fill)
        c.create_oval(x2 - 2 * r, y2 - 2 * r, x2, y2, fill=fill, outline=fill)
        c.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill)
        c.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=fill)

    def draw(self):
        sig = (self.state, self.busy["label"] if self.busy else None)
        if sig == self.drawn:
            return
        self.drawn = sig

        c, p, w, h = self.canvas, self.px, self.w, self.h
        c.delete("all")
        bw = max(1, p(OUTLINE_WIDTH))
        self.rounded(0, 0, w - 1, h - 1, p(12), OUTLINE_COLOR)
        self.rounded(bw, bw, w - 1 - bw, h - 1 - bw, p(12) - bw, BG)

        # resize grip: three short diagonal lines in the bottom-right corner
        gx, gy = w - p(6), h - p(6)
        for i in (1, 2, 3):
            c.create_line(gx - i * p(3), gy, gx, gy - i * p(3),
                          fill=SUB, width=max(1, p(1)))

        if self.busy:
            sub, look = self.busy["label"], "busy"
        else:
            sub = {"on": "Connected", "off": "Disconnected",
                   "unknown": "Status unknown"}[self.state]
            look = self.state

        # negative font sizes are pixels, so the text scales with the switch
        c.create_text(p(14), h / 2 - p(8), text="VPN (test)" if DEMO else "VPN", anchor="w",
                      fill=FG, font=("Segoe UI", -p(13), "bold"))
        c.create_text(p(14), h / 2 + p(8), text=sub, anchor="w",
                      fill=SUB, font=("Segoe UI", -p(11)))

        tx1, ty1, tx2, ty2 = self.toggle_box()
        th = ty2 - ty1
        color = TRACK[look]
        r = th / 2
        c.create_oval(tx1, ty1, tx1 + th, ty2, fill=color, outline=color)
        c.create_oval(tx2 - th, ty1, tx2, ty2, fill=color, outline=color)
        c.create_rectangle(tx1 + r, ty1, tx2 - r, ty2, fill=color, outline=color)

        kr = r - p(3)
        if look == "on":
            kx = tx2 - r
        elif look == "busy":
            kx = (tx1 + tx2) / 2
        else:
            kx = tx1 + r
        ky = (ty1 + ty2) / 2
        c.create_oval(kx - kr, ky - kr, kx + kr, ky + kr, fill="#ffffff", outline="")
        self.update_cursor()

    def update_cursor(self):
        if not self.hover:
            return
        where = self.zone(*self.hover)
        if where == "toggle":
            cursor = "watch" if self.busy else "hand2"
        elif where == "grip":
            cursor = "size_nw_se"
        else:
            cursor = "fleur"
        if self.canvas.cget("cursor") != cursor:
            self.canvas.configure(cursor=cursor)

    def set_zoom(self, zoom, save=False):
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if abs(zoom - self.zoom) < 0.005:
            return
        self.zoom = zoom
        self.w, self.h = self.px(178), self.px(46)
        self.canvas.configure(width=self.w, height=self.h)
        x, y = clamp_to_screen(self.root.winfo_x(), self.root.winfo_y(), self.w, self.h)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.drawn = None
        self.draw()
        if save:
            self.save()

    def keep_on_screen(self):
        """Pull the switch back if a monitor was unplugged or rearranged."""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        nx, ny = clamp_to_screen(x, y, self.w, self.h)
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")
            self.save()

    # ---------------- mouse ----------------
    # Only the toggle itself switches the VPN. Dragging anywhere moves the
    # window, dragging the bottom-right corner (or Ctrl+wheel) resizes it.

    def on_hover(self, e):
        self.hover = (e.x, e.y)
        self.update_cursor()

    def on_press(self, e):
        self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(),
                     self.zoom, self.zone(e.x, e.y))
        self.dragging = False

    def on_motion(self, e):
        if not self.drag:
            return
        mx, my, wx, wy, zoom, where = self.drag
        dx, dy = e.x_root - mx, e.y_root - my
        if not self.dragging and abs(dx) + abs(dy) < self.px(4):
            return
        self.dragging = True
        if where == "grip":
            # Grow or shrink with the corner, keeping the proportions.
            w0 = 178 * self.scale * zoom
            h0 = 46 * self.scale * zoom
            self.set_zoom(zoom * max((w0 + dx) / w0, (h0 + dy) / h0))
            return
        # Stay inside the monitor under the mouse: the switch can't be pushed
        # past a screen edge, but follows the mouse onto another monitor.
        x, y = clamp_to_screen(wx + dx, wy + dy, self.w, self.h,
                               point=(e.x_root, e.y_root))
        self.root.geometry(f"+{x}+{y}")

    def on_release(self, e):
        if self.dragging:
            self.save()
        elif self.drag and self.drag[5] == "toggle" and self.zone(e.x, e.y) == "toggle":
            self.toggle()
        self.drag = None
        self.dragging = False
        self.hover = (e.x, e.y)
        self.update_cursor()

    def on_wheel(self, e):
        self.set_zoom(self.zoom * (1.1 if e.delta > 0 else 1 / 1.1), save=True)

    def on_menu(self, e):
        # The menu runs its own loop while open; meanwhile poll() must not
        # re-apply "always on top", or the switch jumps in front of the menu.
        self.menu_open = True
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
            self.menu_open = False

    # ---------------- actions ----------------

    def save(self):
        save_config({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                     "zoom": round(self.zoom, 3), "topmost": self.topmost.get()})

    def apply_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        self.save()

    def open_gp(self):
        cmd = gp_command()
        if cmd:
            self.allow_foreground()
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        else:
            self.error(ERROR_TEXT[EXIT_NO_EXE])

    def quit(self):
        if DEMO:
            try:
                signal_fake_gp_exit()
            except Exception:
                pass
        self.root.destroy()

    @staticmethod
    def allow_foreground():
        # Let the GlobalProtect panel come to the front when it opens.
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass

    def toggle(self):
        # One toggle at a time: ignore clicks until GlobalProtect has finished
        # and the adapter shows the new state, plus a short cooldown so a
        # double-click doesn't immediately undo it.
        if self.busy or self.worker or time.time() < self.cooldown_until:
            return
        self.allow_foreground()
        want_on = self.state != "on"
        self.busy = {"target": want_on, "since": time.time(),
                     "timeout": CONNECT_TIMEOUT_S if want_on else DISCONNECT_TIMEOUT_S,
                     "label": "Connecting…" if want_on else "Disconnecting…"}
        self.draw()
        self.worker = threading.Thread(target=self.run_automation, args=(want_on,),
                                       daemon=True)
        self.worker.start()

    def finish(self, state=None):
        """End a toggle; optionally force the shown state."""
        if state:
            self.state = state
        self.busy = None
        self.cooldown_until = time.time() + CLICK_COOLDOWN_S

    def run_automation(self, want_on):
        cmd = self_command() + ["--connect" if want_on else "--disconnect"]
        try:
            rc = run_hidden(cmd, timeout=(PANEL_TIMEOUT_S + 20) * AUTOMATION_ATTEMPTS
                            ).returncode
        except subprocess.TimeoutExpired:
            rc = EXIT_TIMEOUT
        except Exception:
            rc = EXIT_ERROR
        self.results.put((want_on, rc))

    def check_results(self):
        try:
            while True:
                want_on, rc = self.results.get_nowait()
                self.handle_result(want_on, rc)
        except queue.Empty:
            pass
        self.root.after(150, self.check_results)

    def handle_result(self, want_on, rc):
        self.worker = None
        no_adapter = not DEMO and not self.adapter and self.adapter_checked
        if rc in (EXIT_OK, EXIT_ALREADY):
            if HIDE_PANEL_AFTER_CLICK:
                self.root.after(700 if rc == EXIT_OK else 300, self.root.focus_force)
            if no_adapter:
                # GlobalProtect's adapter doesn't exist, so there's nothing to
                # confirm with - trust GlobalProtect.
                self.finish("on" if want_on else "off")
            else:
                # poll() ends the toggle once the adapter shows the new state,
                # which for a connect is when the tunnel is actually up
                self.busy["since"] = time.time()
        elif (rc in RETRYABLE or rc == EXIT_TIMEOUT) and not no_adapter:
            # The click may have gone through just before the panel closed.
            # Let poll() check the adapter for a moment before complaining.
            self.busy["error"] = rc
            self.busy["since"] = time.time()
        else:
            self.finish()
            self.draw()
            self.error(ERROR_TEXT.get(rc, f"Unexpected error (code {rc})."))
        self.draw()

    def error(self, text):
        messagebox.showerror("VPN Toggle", text, parent=self.root)

    # ---------------- status polling ----------------

    def lookup_adapter(self, min_interval=30):
        if self.lookup_running or time.time() - self.last_lookup < min_interval:
            return
        self.lookup_running = True
        self.last_lookup = time.time()

        def work():
            name = find_adapter_name()
            if name:
                self.adapter = name
            self.adapter_checked = True
            self.lookup_running = False

        threading.Thread(target=work, daemon=True).start()

    def read_status(self):
        if DEMO:
            try:
                text = DEMO_STATE_FILE.read_text(encoding="ascii").strip()
            except Exception:
                return None
            return (text == "1") if text else None
        if not self.adapter:
            self.lookup_adapter()
            return None
        stats = psutil.net_if_stats().get(self.adapter)
        self.link_up = bool(stats and stats.isup)
        if stats is None:
            # GlobalProtect disables or removes its adapter while disconnected,
            # so a missing adapter means "off". Now and then re-check the name
            # in case the adapter was renamed or reinstalled.
            if not ADAPTER_NAME:
                self.lookup_adapter(min_interval=300)
            return False
        return bool(stats.isup) and has_tunnel_address(self.adapter)

    def poll(self):
        status = self.read_status()
        if status is not None:
            self.state = "on" if status else "off"
        failed = None
        busy = self.busy
        if busy and not self.worker:
            waited = time.time() - busy["since"]
            if status is not None and status == busy["target"]:
                self.finish()  # done, even if the automation reported a problem
            elif "error" in busy:
                if busy["target"] and self.link_up:
                    # The adapter is coming up: the connect did go through
                    # and is still in progress, so wait for it as usual.
                    del busy["error"]
                    busy["since"] = time.time()
                elif waited > VERIFY_S:
                    failed = busy["error"]
                    self.finish()
            elif waited > busy["timeout"]:
                self.finish()
        if self.topmost.get() and not self.menu_open:
            self.root.attributes("-topmost", True)
        if not self.drag and not self.menu_open and self.root.winfo_ismapped():
            self.keep_on_screen()
        self.draw()
        fast = self.busy or self.state == "unknown"
        self.root.after(700 if fast else POLL_MS, self.poll)
        if failed is not None:
            self.error(ERROR_TEXT.get(failed, f"Unexpected error (code {failed})."))

    def run(self):
        self.root.mainloop()


# ============================ Entry point ==================================

def main():
    args = sys.argv[1:]
    if "--connect" in args:
        sys.exit(automation_main("connect"))
    if "--disconnect" in args:
        sys.exit(automation_main("disconnect"))
    if "--inspect" in args:
        sys.exit(inspect_main())

    set_dpi_aware()
    if already_running():
        return

    if DEMO and not FAKE_GP_SCRIPT.exists():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("VPN Toggle", ERROR_TEXT[EXIT_NO_EXE])
        return

    missing = []
    if psutil is None:
        missing.append("psutil")
    if importlib.util.find_spec("pywinauto") is None:
        missing.append("pywinauto")
    if missing:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("VPN Toggle", "Missing packages. Run:\n\n"
                             f"pip install {' '.join(missing)}")
        return

    App().run()


if __name__ == "__main__":
    main()