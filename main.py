from __future__ import annotations

"""
List Scanner
Captures a screen region with OCR and checks each list item against found text.
Items turn green when found, red when not found.
Found text is highlighted on screen via a transparent overlay.
"""

import os
import sys
import ctypes
import ctypes.wintypes
import json
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk
import mss
from PIL import Image, ImageGrab
import pytesseract
from pynput import mouse as _pynput_mouse
from pynput import keyboard as _pynput_keyboard
from ocr_engine import (
    OcrEngine,
    locate_prepared as _locate_prepared,
    preprocess_search_term as _preprocess_search_term,
    is_numeric_item as _is_numeric_item,
)

# ── DPI awareness (must be before any window creation) ────────────────────────
# Without this the selector window is half-size on high-DPI displays and
# coordinates are off by the DPI scale factor when running as an exe.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Tesseract detection ────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle — use the Tesseract we bundled at build time
    _bundled_tess = os.path.join(sys._MEIPASS, "tesseract", "tesseract.exe")
    pytesseract.pytesseract.tesseract_cmd = _bundled_tess
    os.environ["TESSDATA_PREFIX"] = os.path.join(sys._MEIPASS, "tesseract", "tessdata")
elif sys.platform == "win32":
    # Running from source — try common system install locations
    _candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Programs", "Tesseract-OCR", "tesseract.exe",
        ),
    ]
    for _p in _candidates:
        if os.path.exists(_p):
            pytesseract.pytesseract.tesseract_cmd = _p
            break

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Chroma-key color used as the transparent background on the overlay window.
# Must not appear in any drawn rectangle outline.
_CHROMA = "#020203"


class _GlobalWheelHook:
    """Windows low-level mouse wheel hook (captures wheel + precision touchpad scroll)."""

    WH_MOUSE_LL = 14
    WM_MOUSEWHEEL = 0x020A
    WM_MOUSEHWHEEL = 0x020E
    HC_ACTION = 0

    class POINT(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_long),
            ("y", ctypes.c_long),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", ctypes.wintypes.POINT),
            ("mouseData", ctypes.wintypes.DWORD),
            ("flags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    def __init__(self, on_scroll):
        self._on_scroll = on_scroll
        self._hook = None
        self._cb = None
        self._thread = None
        self._thread_id = 0
        self._running = False
        self._started_ok = False
        self._started_evt = threading.Event()

    def start(self):
        if self._running:
            return self._started_ok
        self._running = True
        self._started_ok = False
        self._started_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started_evt.wait(timeout=0.35)
        return self._started_ok

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self._thread = None

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.wintypes.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG), ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]

        def low_level_mouse_proc(n_code, w_param, l_param):
            if n_code == self.HC_ACTION and w_param in (self.WM_MOUSEWHEEL, self.WM_MOUSEHWHEEL):
                try:
                    self._on_scroll()
                except Exception:
                    pass
            return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

        self._cb = HOOKPROC(low_level_mouse_proc)
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(self.WH_MOUSE_LL, self._cb, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            self._started_ok = False
            self._started_evt.set()
            self._running = False
            return

        self._started_ok = True
        self._started_evt.set()

        msg = ctypes.wintypes.MSG()
        while self._running:
            gm = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if gm <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._thread_id = 0
        self._running = False


class _GlobalScrollEventHook:
    """Windows global scroll-start WinEvent hook (works across focused apps)."""

    EVENT_SYSTEM_SCROLLINGSTART = 0x0012
    WINEVENT_OUTOFCONTEXT = 0x0000
    WINEVENT_SKIPOWNPROCESS = 0x0002

    def __init__(self, on_scroll):
        self._on_scroll = on_scroll
        self._hook = None
        self._cb = None
        self._thread = None
        self._thread_id = 0
        self._running = False
        self._started_ok = False
        self._started_evt = threading.Event()

    def start(self):
        if self._running:
            return self._started_ok
        self._running = True
        self._started_ok = False
        self._started_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started_evt.wait(timeout=0.35)
        return self._started_ok

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self._thread = None

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        HWINEVENTHOOK = ctypes.c_void_p
        WINEVENTPROC = ctypes.WINFUNCTYPE(
            None,
            HWINEVENTHOOK,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LONG,
            ctypes.wintypes.LONG,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
        )

        user32.SetWinEventHook.restype = HWINEVENTHOOK
        user32.SetWinEventHook.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            WINEVENTPROC,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
        ]
        user32.UnhookWinEvent.restype = ctypes.wintypes.BOOL
        user32.UnhookWinEvent.argtypes = [HWINEVENTHOOK]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG), ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

        def win_event_proc(hook, event, hwnd, obj_id, child_id, event_thread, event_time):
            if event == self.EVENT_SYSTEM_SCROLLINGSTART:
                try:
                    self._on_scroll()
                except Exception:
                    pass

        self._cb = WINEVENTPROC(win_event_proc)
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWinEventHook(
            self.EVENT_SYSTEM_SCROLLINGSTART,
            self.EVENT_SYSTEM_SCROLLINGSTART,
            0,
            self._cb,
            0,
            0,
            self.WINEVENT_OUTOFCONTEXT | self.WINEVENT_SKIPOWNPROCESS,
        )

        if not self._hook:
            self._started_ok = False
            self._started_evt.set()
            self._running = False
            return

        self._started_ok = True
        self._started_evt.set()

        msg = ctypes.wintypes.MSG()
        while self._running:
            gm = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if gm <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            user32.UnhookWinEvent(self._hook)
            self._hook = None
        self._thread_id = 0
        self._running = False


class _RawInputWheelSink:
    """Global raw-input wheel sink (receives wheel input without app focus)."""

    WM_INPUT = 0x00FF
    WM_CLOSE = 0x0010
    RID_INPUT = 0x10000003
    RIM_TYPEMOUSE = 0
    RIM_TYPEHID = 2
    RI_MOUSE_WHEEL = 0x0400
    RI_MOUSE_HWHEEL = 0x0800
    RIDEV_INPUTSINK = 0x00000100
    RIDEV_DEVNOTIFY = 0x00002000
    HWND_MESSAGE = ctypes.c_void_p(-3).value

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [
            ("usUsagePage", ctypes.wintypes.USHORT),
            ("usUsage", ctypes.wintypes.USHORT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("hwndTarget", ctypes.wintypes.HWND),
        ]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [
            ("dwType", ctypes.wintypes.DWORD),
            ("dwSize", ctypes.wintypes.DWORD),
            ("hDevice", ctypes.wintypes.HANDLE),
            ("wParam", ctypes.wintypes.WPARAM),
        ]

    class RAWMOUSE(ctypes.Structure):
        _fields_ = [
            ("usFlags", ctypes.wintypes.USHORT),
            ("ulButtons", ctypes.wintypes.ULONG),
            ("ulRawButtons", ctypes.wintypes.ULONG),
            ("lLastX", ctypes.wintypes.LONG),
            ("lLastY", ctypes.wintypes.LONG),
            ("ulExtraInformation", ctypes.wintypes.ULONG),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p),
            ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.wintypes.LPCWSTR),
            ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ]

    def __init__(self, on_scroll, on_touchpad_activity=None):
        self._on_scroll = on_scroll
        self._on_touchpad_activity = on_touchpad_activity
        self._thread = None
        self._thread_id = 0
        self._running = False
        self._started_ok = False
        self._started_evt = threading.Event()
        self._hwnd = None
        self._class_name = None
        self._wndproc = None

    def start(self):
        if self._running:
            return self._started_ok
        self._running = True
        self._started_ok = False
        self._started_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started_evt.wait(timeout=0.5)
        return self._started_ok

    def stop(self):
        if not self._running:
            return
        self._running = False
        user32 = ctypes.windll.user32
        if self._hwnd:
            user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread is not None:
            self._thread.join(timeout=0.75)
        self._thread = None

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

        user32.RegisterClassW.restype = ctypes.wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(self.WNDCLASSW)]
        user32.CreateWindowExW.restype = ctypes.wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.HWND,
            ctypes.wintypes.HMENU,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.RegisterRawInputDevices.restype = ctypes.wintypes.BOOL
        user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(self.RAWINPUTDEVICE), ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        user32.GetRawInputData.restype = ctypes.wintypes.UINT
        user32.GetRawInputData.argtypes = [ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.UINT), ctypes.wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG), ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]

        def wnd_proc(hwnd, msg, w_param, l_param):
            if msg == self.WM_INPUT:
                size = ctypes.wintypes.UINT(0)
                hdr_size = ctypes.sizeof(self.RAWINPUTHEADER)
                user32.GetRawInputData(ctypes.c_void_p(l_param), self.RID_INPUT, None, ctypes.byref(size), hdr_size)
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(
                        ctypes.c_void_p(l_param),
                        self.RID_INPUT,
                        buf,
                        ctypes.byref(size),
                        hdr_size,
                    )
                    if got == size.value:
                        header = ctypes.cast(buf, ctypes.POINTER(self.RAWINPUTHEADER)).contents
                        min_mouse_bytes = ctypes.sizeof(self.RAWINPUTHEADER) + ctypes.sizeof(self.RAWMOUSE)
                        if header.dwType == self.RIM_TYPEMOUSE and size.value >= min_mouse_bytes:
                            raw_ptr = ctypes.c_void_p(ctypes.addressof(buf) + ctypes.sizeof(self.RAWINPUTHEADER))
                            raw = ctypes.cast(raw_ptr, ctypes.POINTER(self.RAWMOUSE)).contents
                            flags = raw.ulButtons & 0xFFFF
                            if flags & (self.RI_MOUSE_WHEEL | self.RI_MOUSE_HWHEEL):
                                try:
                                    self._on_scroll()
                                except Exception:
                                    pass
                        elif header.dwType == self.RIM_TYPEHID and self._on_touchpad_activity is not None:
                            try:
                                self._on_touchpad_activity()
                            except Exception:
                                pass
                return 0
            if msg == self.WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            return user32.DefWindowProcW(hwnd, msg, w_param, l_param)

        self._wndproc = WNDPROC(wnd_proc)
        self._thread_id = kernel32.GetCurrentThreadId()
        self._class_name = f"ListScannerRawInputSink_{self._thread_id}"

        wc = self.WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            self._started_evt.set()
            self._running = False
            return

        self._hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            "",
            0,
            0,
            0,
            0,
            0,
            self.HWND_MESSAGE,
            0,
            wc.hInstance,
            0,
        )
        if not self._hwnd:
            self._started_evt.set()
            self._running = False
            return

        rid = (self.RAWINPUTDEVICE * 2)(
            self.RAWINPUTDEVICE(
                usUsagePage=0x01,  # Generic desktop controls
                usUsage=0x02,      # Mouse
                dwFlags=self.RIDEV_INPUTSINK | self.RIDEV_DEVNOTIFY,
                hwndTarget=self._hwnd,
            ),
            self.RAWINPUTDEVICE(
                usUsagePage=0x0D,  # Digitizers
                usUsage=0x05,      # Touchpad (Windows Precision Touchpad TLC)
                dwFlags=self.RIDEV_INPUTSINK | self.RIDEV_DEVNOTIFY,
                hwndTarget=self._hwnd,
            ),
        )
        if not user32.RegisterRawInputDevices(rid, 2, ctypes.sizeof(self.RAWINPUTDEVICE)):
            self._started_evt.set()
            self._running = False
            return

        self._started_ok = True
        self._started_evt.set()

        msg = ctypes.wintypes.MSG()
        while self._running:
            gm = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if gm <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._hwnd = None
        self._thread_id = 0
        self._running = False




# =============================================================================
# Scan-area selector
# =============================================================================

class ScanAreaSelector:
    """Full-screen semi-transparent overlay spanning all monitors for drag-to-select."""

    def __init__(self, root: tk.Misc, callback):
        self._callback = callback
        self._sx = self._sy = 0
        self._rect_id = None

        # Virtual desktop bounds — covers every connected monitor
        u32 = ctypes.windll.user32
        vx = u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vw = u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        vh = u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        self._vx, self._vy = vx, vy

        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self._win.attributes("-alpha", 0.35)
        self._win.attributes("-topmost", True)
        self._win.configure(bg="#111111")
        self._win.lift()
        self._win.focus_force()

        self._canvas = tk.Canvas(
            self._win, bg="#111111", cursor="crosshair",
            highlightthickness=0, width=vw, height=vh,
        )
        self._canvas.pack(fill="both", expand=True)
        self._canvas.create_text(
            vw // 2, 40,
            text="Drag to select the scan area   \u2022   Esc to cancel",
            fill="white",
            font=("Segoe UI", 16, "bold"),
        )

        self._canvas.bind("<ButtonPress-1>", self._press)
        self._canvas.bind("<B1-Motion>", self._drag)
        self._canvas.bind("<ButtonRelease-1>", self._release)
        self._win.bind("<Escape>", lambda _: self._win.destroy())

    def _press(self, e: tk.Event):
        self._sx, self._sy = e.x, e.y
        if self._rect_id:
            self._canvas.delete(self._rect_id)
            self._rect_id = None

    def _drag(self, e: tk.Event):
        if self._rect_id:
            self._canvas.delete(self._rect_id)
        self._rect_id = self._canvas.create_rectangle(
            self._sx, self._sy, e.x, e.y, outline="#00e676", width=2
        )

    def _release(self, e: tk.Event):
        x1 = min(self._sx, e.x)
        y1 = min(self._sy, e.y)
        x2 = max(self._sx, e.x)
        y2 = max(self._sy, e.y)
        self._win.destroy()
        if (x2 - x1) >= 20 and (y2 - y1) >= 20:
            # Offset by virtual-screen origin for absolute screen coordinates
            self._callback({
                "left":   x1 + self._vx,
                "top":    y1 + self._vy,
                "width":  x2 - x1,
                "height": y2 - y1,
            })


# =============================================================================
# Highlight overlay
# =============================================================================

class HighlightOverlay:
    """
    Transparent always-on-top window that draws green boxes around
    found text positions within the scan area.
    The window is created once and reused; only the canvas contents are redrawn.
    """

    def __init__(self, root: tk.Misc):
        self._root = root
        self._win: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._current_area: dict | None = None

    def show(self, scan_area: dict, boxes: list[tuple]):
        if not boxes:
            # No hits — clear canvas but leave the window open to avoid flash
            if self._canvas:
                self._canvas.delete("all")
            return

        # Create window once, or reposition if the scan area changed
        if self._win is None:
            self._win = tk.Toplevel(self._root)
            self._win.overrideredirect(True)
            self._win.attributes("-topmost", True)
            self._win.attributes("-transparentcolor", _CHROMA)
            self._win.configure(bg=_CHROMA)
            self._win.geometry(
                f"{scan_area['width']}x{scan_area['height']}"
                f"+{scan_area['left']}+{scan_area['top']}"
            )
            # Exclude the overlay from screen capture so OCR is never affected.
            # WDA_EXCLUDEFROMCAPTURE = 0x11 (Windows 10 v2004+)
            try:
                hwnd = self._win.winfo_id()
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
            except Exception:
                pass
            self._canvas = tk.Canvas(
                self._win,
                width=scan_area["width"],
                height=scan_area["height"],
                bg=_CHROMA,
                highlightthickness=0,
            )
            self._canvas.pack()
            self._current_area = scan_area
        elif scan_area != self._current_area:
            self._win.geometry(
                f"{scan_area['width']}x{scan_area['height']}"
                f"+{scan_area['left']}+{scan_area['top']}"
            )
            self._canvas.configure(
                width=scan_area["width"], height=scan_area["height"]
            )
            self._current_area = scan_area

        # Redraw boxes in place — no window destroy/recreate
        self._canvas.delete("all")
        for (x, y, w, h) in boxes:
            self._canvas.create_rectangle(
                x - 3, y - 3, x + w + 3, y + h + 3,
                outline="#00e676", width=2,
            )

    def hide(self):
        self._destroy()

    def _destroy(self):
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None
            self._canvas = None
            self._current_area = None


# =============================================================================
# Main application
# =============================================================================

class ListScannerApp(ctk.CTk):
    _SCAN_INTERVAL = 0.75  # seconds between OCR passes
    _WINDOW_SIZE   = 4     # rolling window of recent scans for majority vote
    _HIDE_HIGHLIGHT_WIDTH = 520
    _OCR_TILE_TARGET_PX = 500
    _OCR_TILE_MIN_PX = 250
    _OCR_TILE_OVERLAP_PX = 30
    _OCR_DYNAMIC_OVERLAP_BASE_CHARS = 6
    _OCR_DYNAMIC_OVERLAP_AVG_CHAR_WIDTH_PX = 9
    _OCR_DYNAMIC_OVERLAP_CHAR_WIDTH_RATIO = 0.75
    _OCR_TILE_MAX = 16
    _ADDITIVE_LOCK_FRAMES = 3

    def __init__(self):
        super().__init__()
        self.title("List Scanner")
        self.geometry("540x760")
        self.minsize(400, 540)

        self._scan_area: dict | None = None
        self._scanning = False
        self._show_overlay = True
        self._thread: threading.Thread | None = None
        self._overlay = HighlightOverlay(self)

        self._items: list[dict] = []       # {'text': str, 'status': str}
        self._scan_gen = 0                  # incremented on reset; discards in-flight results
        self._scan_pass = 0
        self._ocr_scale = 0.9               # screenshot upscale factor
        self._ocr_digits_only = False       # if all list items are numeric, constrain OCR charset
        self._ctrl_click = False            # whether click_found uses Ctrl+click
        self._always_on_top = False         # keep window above all others
        self._highlight_control_visible = True
        self._show_debug_screenshot = False
        self._show_debug_ocr_frames = False
        self._ocr_tile_max = self._OCR_TILE_MAX    # user-configurable, persisted to config
        self._ocr_tile_target_px = self._OCR_TILE_TARGET_PX  # user-configurable, persisted to config
        self._ocr_tile_overlap_px = self._OCR_TILE_OVERLAP_PX  # user-configurable, persisted to config
        self._additive_mode = False               # additive scan mode — accumulates evidence per frame
        self._ocr_engine = OcrEngine()

        # Persistent config file next to the exe / script
        _base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
        self._config_path = _base / "list_scanner_config.json"

        # Hotkey config: action -> key name string
        self._hotkeys: dict[str, str] = {
            "start_stop":      "f12",
            "toggle_overlay":  "f11",
            "toggle_debug":    "f9",
            "set_area":        "f10",
            "click_found":     "f8",
            "toggle_additive": "f7",
        }
        self._hk_listener = None
        self._closing = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_window_configure)
        self._load_config()
        self.after(200, self._check_tesseract)
        self._start_mouse_listener()
        self._apply_hotkeys()

    # ── Tesseract check ───────────────────────────────────────────────────────

    def _check_tesseract(self):
        # When running as a built exe, Tesseract is bundled — no check needed.
        if getattr(sys, "frozen", False):
            return
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            messagebox.showerror(
                "Tesseract Not Found",
                "Tesseract OCR is required but was not found.\n\n"
                "Please install it from:\n"
                "https://github.com/UB-Mannheim/tesseract/wiki\n\n"
                "Install to the default location, then restart the app.",
                parent=self,
            )

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(20, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text="List Scanner",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        self._config_btn = ctk.CTkButton(
            header, text="\u2699",
            command=self._open_config,
            fg_color="transparent",
            hover_color=("gray82", "gray22"),
            text_color=("gray45", "gray62"),
            width=28, height=28,
            corner_radius=14,
            border_width=0,
            font=ctk.CTkFont(size=18),
        )
        self._additive_btn = ctk.CTkButton(
            header,
            text="+",
            command=self._toggle_additive_mode,
            fg_color="transparent",
            hover_color=("gray82", "gray22"),
            text_color=("gray45", "gray62"),
            width=28,
            height=28,
            corner_radius=14,
            border_width=0,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self._mode_lbl = ctk.CTkLabel(
            header, text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#ff9900",
        )
        self._mode_lbl.grid(row=0, column=1, sticky="e", padx=(0, 4))
        self._additive_btn.grid(row=0, column=2, sticky="e", padx=(0, 4))
        self._config_btn.grid(row=0, column=3, sticky="e")
        self._update_additive_indicator()

        # ── Combined input / results textbox ──
        box_card = ctk.CTkFrame(self)
        box_card.grid(row=1, column=0, padx=20, pady=4, sticky="nsew")
        box_card.grid_columnconfigure(0, weight=1)
        box_card.grid_rowconfigure(1, weight=1)

        self._box_label = ctk.CTkLabel(
            box_card,
            text="Paste items to scan for (one per line):",
            font=ctk.CTkFont(size=12),
        )
        self._box_label.grid(row=0, column=0, padx=12, pady=(12, 4), sticky="w")

        # Use the underlying tk.Text for tag-based coloring
        self._text_box = ctk.CTkTextbox(box_card, font=ctk.CTkFont(size=13))
        self._text_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")

        # Grab the internal tk.Text widget so we can use tags
        self._tk_text: tk.Text = self._text_box._textbox

        self._tk_text.tag_configure("found",     foreground="#6dff6d", background="#1a4d1a")
        self._tk_text.tag_configure("not_found", foreground="#ff7070", background="#4d1a1a")
        self._tk_text.tag_configure("pending",   foreground="#a0a0a0", background="")
        self._tk_text.tag_configure("additive",  foreground="#ffcc00", background="#3d2b00")

        # ── Control bar ──
        ctrl = ctk.CTkFrame(self)
        ctrl.grid(row=2, column=0, padx=20, pady=4, sticky="ew")

        self._area_btn = ctk.CTkButton(
            ctrl, text="",
            command=self._set_area,
            fg_color=("gray50", "gray30"),
            hover_color=("gray60", "gray40"),
            width=170, height=36,
        )
        self._area_btn.pack(side="left", padx=(8, 6), pady=10)

        self._overlay_toggle = ctk.CTkCheckBox(
            ctrl, text="",
            command=self._toggle_overlay,
            font=ctk.CTkFont(size=12),
        )
        self._overlay_toggle.select()  # on by default
        self._overlay_toggle.pack(side="left", padx=(2, 6), pady=10)

        self._start_btn = ctk.CTkButton(
            ctrl, text="",
            command=self._toggle_scan,
            fg_color="#2a5c2a",
            hover_color="#357535",
            width=170, height=36,
        )
        self._start_btn.pack(side="right", padx=(6, 4), pady=10)
        self._refresh_start_button()
        self.after_idle(lambda: self._update_highlight_control_visibility(self.winfo_width()))

        # ── Status strip ──
        strip = ctk.CTkFrame(self, fg_color="transparent")
        strip.grid(row=3, column=0, padx=20, pady=(0, 16), sticky="ew")
        strip.grid_columnconfigure(1, weight=1)

        self._area_lbl = ctk.CTkLabel(
            strip, text="Scan area: not set",
            font=ctk.CTkFont(size=11), text_color="gray50", anchor="w",
        )
        self._area_lbl.grid(row=0, column=0, sticky="w")

        self._status_lbl = ctk.CTkLabel(
            strip, text="Ready",
            font=ctk.CTkFont(size=11), text_color="gray50", anchor="e",
        )
        self._status_lbl.grid(row=0, column=1, sticky="e")

    def _toggle_overlay(self):
        self._show_overlay = bool(self._overlay_toggle.get())
        if not self._show_overlay:
            self._overlay.hide()

    def _on_window_configure(self, event):
        if event.widget is self:
            self._update_highlight_control_visibility(event.width)

    def _update_highlight_control_visibility(self, width: int):
        should_show = width >= self._HIDE_HIGHLIGHT_WIDTH
        if should_show == self._highlight_control_visible:
            return
        self._highlight_control_visible = should_show
        if should_show:
            self._overlay_toggle.pack(side="left", padx=(2, 6), pady=10)
        else:
            self._overlay_toggle.pack_forget()

    def _format_hotkey(self, action: str) -> str:
        key = self._hotkeys.get(action, "").strip()
        return key.upper() if key else "UNSET"

    def _refresh_start_button(self):
        if hasattr(self, "_area_btn"):
            self._area_btn.configure(text=f"Set Scan Area ({self._format_hotkey('set_area')})")
        if hasattr(self, "_overlay_toggle"):
            self._overlay_toggle.configure(text=f"Highlight ({self._format_hotkey('toggle_overlay')})")
        if not hasattr(self, "_start_btn"):
            return
        action = "Stop" if self._scanning else "Start"
        icon = "\u23f9" if self._scanning else "\u25b6"
        hotkey = self._format_hotkey("start_stop")
        self._start_btn.configure(text=f"{icon}  {action} ({hotkey})")

    # ── Persist config ──────────────────────────────────────────────────────

    def _load_config(self):
        try:
            data = json.loads(self._config_path.read_text())
            if "scan_area" in data and data["scan_area"]:
                self._scan_area = data["scan_area"]
                a = self._scan_area
                info = f"{a['width']} × {a['height']}  at  ({a['left']}, {a['top']})"
                self._area_lbl.configure(text=f"Scan area: {info}", text_color="#4caf50")
            if "hotkeys" in data:
                self._hotkeys.update(data["hotkeys"])
            if "ocr_scale" in data:
                self._ocr_scale = float(data["ocr_scale"])
            if "ctrl_click" in data:
                self._ctrl_click = bool(data["ctrl_click"])
            if "always_on_top" in data:
                self._always_on_top = bool(data["always_on_top"])
                self._sync_window_stack()
            if "ocr_tile_max" in data:
                self._ocr_tile_max = max(1, int(data["ocr_tile_max"]))
            if "ocr_tile_min_px" in data:
                self._ocr_tile_target_px = max(1, int(data["ocr_tile_min_px"]))
            if "ocr_tile_target_px" in data:
                self._ocr_tile_target_px = max(1, int(data["ocr_tile_target_px"]))
            if "ocr_tile_overlap_px" in data:
                self._ocr_tile_overlap_px = max(0, int(data["ocr_tile_overlap_px"]))
            self._refresh_start_button()
        except Exception:
            pass

    def _sync_window_stack(self):
        if self._closing:
            return
        self.wm_attributes("-topmost", self._always_on_top)

        debug = getattr(self, "_debug_win", None)
        if debug and debug.winfo_exists():
            debug.transient(self)
            debug.attributes("-topmost", self._always_on_top)
            debug.lift(self)

        config = getattr(self, "_config_win", None)
        if config and config.winfo_exists():
            config.transient(self)
            config.attributes("-topmost", self._always_on_top)
            if debug and debug.winfo_exists():
                config.lift(debug)
            else:
                config.lift(self)

        self.after(50, self._restack_popups)

    def _restack_popups(self):
        debug = getattr(self, "_debug_win", None)
        config = getattr(self, "_config_win", None)
        if debug and debug.winfo_exists():
            debug.lift(self)
        if config and config.winfo_exists():
            if debug and debug.winfo_exists():
                config.lift(debug)
            else:
                config.lift(self)

    def _save_config(self):
        try:
            data = {
                "scan_area": self._scan_area,
                "hotkeys":   self._hotkeys,
                "ocr_scale": self._ocr_scale,
                "ctrl_click": self._ctrl_click,
                "always_on_top": self._always_on_top,
                "ocr_tile_max": self._ocr_tile_max,
                "ocr_tile_target_px": self._ocr_tile_target_px,
                "ocr_tile_overlap_px": self._ocr_tile_overlap_px,
            }
            self._config_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    # ── Keyboard hotkeys ─────────────────────────────────────────────────────

    def _apply_hotkeys(self):
        """(Re)register global hotkeys via pynput Listener (fires on key release — no repeats)."""
        if self._hk_listener is not None:
            try:
                self._hk_listener.stop()
            except Exception:
                pass
            self._hk_listener = None

        actions = {
            "start_stop":      self._toggle_scan,
            "toggle_overlay":  self._toggle_overlay_hotkey,
            "toggle_debug":    self._toggle_debug,
            "set_area":        self._set_area,
            "click_found":     self._do_click_found,
            "toggle_additive": self._toggle_additive_mode,
        }
        # Build a plain name -> fn map  (e.g. "f9" -> _toggle_debug)
        key_to_fn: dict[str, object] = {}
        for action_name, fn in actions.items():
            key = self._hotkeys.get(action_name, "").lower()
            if key:
                key_to_fn[key] = fn

        def on_release(key):
            if self._closing:
                return
            try:
                name = key.name.lower()   # Key enum  e.g. Key.f9  -> "f9"
            except AttributeError:
                try:
                    name = key.char.lower() if key.char else None
                except AttributeError:
                    name = None
            if name:
                fn = key_to_fn.get(name)
                if fn:
                    self.after(0, fn)

        from pynput import keyboard as _pynput_kb
        self._hk_listener = _pynput_kb.Listener(on_release=on_release)
        self._hk_listener.start()

    def _toggle_overlay_hotkey(self):
        """Toggle overlay checkbox and state via hotkey."""
        if self._overlay_toggle.get():
            self._overlay_toggle.deselect()
        else:
            self._overlay_toggle.select()
        self._toggle_overlay()
        self._debug_event(
            f"Overlay toggled via hotkey: {'on' if self._show_overlay else 'off'}",
            "info",
        )

    def _do_click_found(self):
        """Click the center of every currently-found box."""
        area = self._scan_area
        if not area:
            return
        boxes = [b for item in self._items if item["status"] == "found"
                 for b in item["last_boxes"]]
        if not boxes:
            return

        def _click():
            mc = _pynput_mouse.Controller()
            kc = _pynput_keyboard.Controller()
            for (x, y, w, h) in boxes:
                sx = area["left"] + x + w // 2
                sy = area["top"]  + y + h // 2
                mc.position = (sx, sy)
                if self._ctrl_click:
                    with kc.pressed(_pynput_keyboard.Key.ctrl):
                        mc.click(_pynput_mouse.Button.left)
                else:
                    mc.click(_pynput_mouse.Button.left)
                time.sleep(0.05)

        threading.Thread(target=_click, daemon=True).start()

    # ── Additive mode ─────────────────────────────────────────────────────────

    def _toggle_additive_mode(self):
        """Toggle additive scanning mode on/off via hotkey or programmatically."""
        self._additive_mode = not self._additive_mode
        if not self._additive_mode:
            self._restore_item_texts()
        self._update_additive_indicator()
        self._debug_event(
            f"Additive mode {'enabled' if self._additive_mode else 'disabled'} "
            f"(hotkey: {self._hotkeys.get('toggle_additive', 'f7').upper()})",
            "info",
        )

    def _update_additive_indicator(self):
        """Show/hide additive label and sync header + button styling."""
        is_on = self._additive_mode
        self._mode_lbl.configure(text="additive" if is_on else "")
        if hasattr(self, "_additive_btn"):
            if is_on:
                self._additive_btn.configure(
                    fg_color="#ff9900",
                    hover_color="#ffad33",
                    text_color="#1a1a1a",
                )
            else:
                self._additive_btn.configure(
                    fg_color="transparent",
                    hover_color=("gray82", "gray22"),
                    text_color=("gray45", "gray62"),
                )

    def _update_row_additive(self, idx: int, count: int, locked: bool):
        """Update a single item row: appends stars (1-3) or shows green at 4."""
        if idx >= len(self._items):
            return
        item = self._items[idx]
        line_start = f"{idx + 1}.0"
        line_end   = f"{idx + 1}.end"

        if locked:
            new_text = item["text"]
        elif count > 0:
            new_text = item["text"] + " " + "*" * count
        else:
            new_text = item["text"]

        # Temporarily enable the underlying tk.Text to modify content
        was_disabled = self._tk_text.cget("state") == "disabled"
        if was_disabled:
            self._tk_text.configure(state="normal")
        self._tk_text.delete(line_start, line_end)
        self._tk_text.insert(line_start, new_text)
        if was_disabled:
            self._tk_text.configure(state="disabled")

        # Apply colour tag
        self._tk_text.tag_remove("found",     line_start, line_end)
        self._tk_text.tag_remove("not_found", line_start, line_end)
        self._tk_text.tag_remove("pending",   line_start, line_end)
        self._tk_text.tag_remove("additive",  line_start, line_end)
        if locked:
            self._tk_text.tag_add("found",     line_start, line_end)
            item["status"] = "found"
        elif count > 0:
            self._tk_text.tag_add("additive",  line_start, line_end)
            item["status"] = "pending"
        else:
            self._tk_text.tag_add("not_found", line_start, line_end)
            item["status"] = "not_found"

    def _restore_item_texts(self):
        """Remove any additive star text from all item rows, resetting additive state."""
        has_changes = any(
            item.get("additive_count", 0) > 0 or item.get("additive_locked", False)
            for item in self._items
        )
        if not has_changes:
            return
        self._tk_text.configure(state="normal")
        for i, item in enumerate(self._items):
            if item.get("additive_count", 0) > 0 or item.get("additive_locked", False):
                line_start = f"{i + 1}.0"
                line_end   = f"{i + 1}.end"
                self._tk_text.delete(line_start, line_end)
                self._tk_text.insert(line_start, item["text"])
                item["additive_count"] = 0
                item["additive_locked"] = False
        self._tk_text.configure(state="disabled")

    # ── Config popup ──────────────────────────────────────────────────────────

    _ACTION_LABELS = {
        "start_stop":      "Start / Stop",
        "toggle_overlay":  "Toggle Overlay",
        "toggle_debug":    "Toggle Debug Window",
        "set_area":        "Set Scan Area",
        "click_found":     "Click Found Items",
        "toggle_additive": "Toggle Additive Mode",
    }

    def _open_config(self):
        if hasattr(self, "_config_win") and self._config_win and self._config_win.winfo_exists():
            self._sync_window_stack()
            self._config_win.focus_force()
            return

        win = tk.Toplevel(self)
        self._config_win = win
        win.title("Hotkey Config")
        win.resizable(False, False)
        win.configure(bg="#1a1a1a")
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_config_win())
        win.grab_set()  # modal
        self._sync_window_stack()

        tk.Label(
            win, text="Hotkeys", bg="#1a1a1a", fg="#ffffff",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(16, 8), sticky="w")

        tk.Label(
            win, text="Press a key in each field, then click Save.",
            bg="#1a1a1a", fg="#888888", font=("Segoe UI", 10),
        ).grid(row=1, column=0, columnspan=2, padx=20, pady=(0, 12), sticky="w")

        entries: dict[str, tk.Entry] = {}
        for row_idx, (action, label) in enumerate(self._ACTION_LABELS.items(), start=2):
            tk.Label(
                win, text=label, bg="#1a1a1a", fg="#cccccc",
                font=("Segoe UI", 11), width=22, anchor="w",
            ).grid(row=row_idx, column=0, padx=(20, 8), pady=6, sticky="w")

            entry = tk.Entry(
                win, bg="#2b2b2b", fg="#ffffff", insertbackground="white",
                font=("Consolas", 11), width=12, relief="flat",
            )
            entry.insert(0, self._hotkeys[action].upper())
            entry.grid(row=row_idx, column=1, padx=(0, 20), pady=6, sticky="w")
            entries[action] = entry

            # On focus, clear and capture next keypress
            def on_focus(e, ent=entry):
                ent.delete(0, "end")
                ent.configure(fg="#ffcc00")

            def on_key(e, ent=entry):
                key = e.keysym.lower()
                ent.delete(0, "end")
                ent.insert(0, key.upper())
                ent.configure(fg="#ffffff")
                return "break"

            entry.bind("<FocusIn>", on_focus)
            entry.bind("<KeyPress>", on_key)

        btn_row = 2 + len(self._ACTION_LABELS)

        # Ctrl+click toggle
        ctrl_var = tk.BooleanVar(value=self._ctrl_click)
        tk.Checkbutton(
            win, text="Use Ctrl+Click instead of Click",
            variable=ctrl_var,
            bg="#1a1a1a", fg="#cccccc", selectcolor="#2b2b2b",
            activebackground="#1a1a1a", activeforeground="#ffffff",
            font=("Segoe UI", 11),
        ).grid(row=btn_row, column=0, columnspan=2, padx=20, pady=(4, 4), sticky="w")
        btn_row += 1

        # Always on top toggle
        on_top_var = tk.BooleanVar(value=self._always_on_top)
        tk.Checkbutton(
            win, text="Always keep window on top",
            variable=on_top_var,
            bg="#1a1a1a", fg="#cccccc", selectcolor="#2b2b2b",
            activebackground="#1a1a1a", activeforeground="#ffffff",
            font=("Segoe UI", 11),
        ).grid(row=btn_row, column=0, columnspan=2, padx=20, pady=(4, 4), sticky="w")
        btn_row += 1

        # Max OCR frames spinbox
        max_tile_row = tk.Frame(win, bg="#1a1a1a")
        max_tile_row.grid(row=btn_row, column=0, columnspan=2, padx=20, pady=(4, 8), sticky="w")
        tk.Label(
            max_tile_row, text="Max OCR frames:", bg="#1a1a1a", fg="#cccccc",
            font=("Segoe UI", 11),
        ).pack(side="left")
        max_tile_var = tk.IntVar(value=self._ocr_tile_max)
        tk.Spinbox(
            max_tile_row, from_=1, to=64, textvariable=max_tile_var, width=4,
            bg="#2b2b2b", fg="#ffffff", insertbackground="white", relief="flat",
            buttonbackground="#333333", font=("Consolas", 11),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            max_tile_row, text="(1 = no subdivision)", bg="#1a1a1a", fg="#555555",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(8, 0))
        btn_row += 1

        # OCR frame target size spinbox
        min_tile_row = tk.Frame(win, bg="#1a1a1a")
        min_tile_row.grid(row=btn_row, column=0, columnspan=2, padx=20, pady=(4, 8), sticky="w")
        tk.Label(
            min_tile_row, text="OCR frame size (px):", bg="#1a1a1a", fg="#cccccc",
            font=("Segoe UI", 11),
        ).pack(side="left")
        min_tile_var = tk.IntVar(value=self._ocr_tile_target_px)
        tk.Spinbox(
            min_tile_row, from_=50, to=2000, textvariable=min_tile_var, width=5,
            bg="#2b2b2b", fg="#ffffff", insertbackground="white", relief="flat",
            buttonbackground="#333333", font=("Consolas", 11),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            min_tile_row, text="px (smaller = more tiles)", bg="#1a1a1a", fg="#555555",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(8, 0))
        btn_row += 1

        # OCR frame overlap spinbox
        overlap_row = tk.Frame(win, bg="#1a1a1a")
        overlap_row.grid(row=btn_row, column=0, columnspan=2, padx=20, pady=(4, 8), sticky="w")
        tk.Label(
            overlap_row, text="OCR frame overlap (px):", bg="#1a1a1a", fg="#cccccc",
            font=("Segoe UI", 11),
        ).pack(side="left")
        overlap_var = tk.IntVar(value=self._ocr_tile_overlap_px)
        tk.Spinbox(
            overlap_row, from_=0, to=500, textvariable=overlap_var, width=5,
            bg="#2b2b2b", fg="#ffffff", insertbackground="white", relief="flat",
            buttonbackground="#333333", font=("Consolas", 11),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            overlap_row, text="px (helps text near tile edges)", bg="#1a1a1a", fg="#555555",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(8, 0))
        btn_row += 1

        def save():
            for action, ent in entries.items():
                val = ent.get().strip().lower()
                if val:
                    self._hotkeys[action] = val
            self._ctrl_click = ctrl_var.get()
            self._always_on_top = on_top_var.get()
            self._ocr_tile_max = max(1, max_tile_var.get())
            self._ocr_tile_target_px = max(1, min_tile_var.get())
            self._ocr_tile_overlap_px = max(0, overlap_var.get())
            self._sync_window_stack()
            self._apply_hotkeys()
            self._refresh_start_button()
            self._save_config()
            self._close_config_win()

        btn_frame = tk.Frame(win, bg="#1a1a1a")
        btn_frame.grid(row=btn_row, column=0, columnspan=2, pady=(8, 16))

        tk.Button(
            btn_frame, text="Save", command=save,
            bg="#2a5c2a", fg="white", relief="flat",
            font=("Segoe UI", 11), padx=20, pady=6, cursor="hand2",
        ).pack(side="left", padx=8)

        tk.Button(
            btn_frame, text="Debug Window", command=lambda: (self._close_config_win(), self._toggle_debug()),
            bg="#333333", fg="white", relief="flat",
            font=("Segoe UI", 11), padx=20, pady=6, cursor="hand2",
        ).pack(side="left", padx=8)

        tk.Button(
            btn_frame, text="Cancel", command=self._close_config_win,
            bg="#3a2a2a", fg="white", relief="flat",
            font=("Segoe UI", 11), padx=20, pady=6, cursor="hand2",
        ).pack(side="left", padx=8)

    # ── Mouse listener ────────────────────────────────────────────────────────

    def _start_mouse_listener(self):
        def on_click(x, y, button, pressed):
            if pressed and self._scanning:
                self._queue_input_reset("mouse_click")

        def on_scroll(x, y, dx, dy):
            if self._scanning:
                self._queue_input_reset("mouse_wheel")

        self._mouse_listener = _pynput_mouse.Listener(
            on_click=on_click, on_scroll=on_scroll
        )
        self._mouse_listener.daemon = True
        self._mouse_listener.start()

        def on_tk_scroll(_event):
            if self._scanning:
                self._queue_input_reset("tk_wheel")

        # Fallback path for trackpad gestures delivered to the app window.
        self.bind_all("<MouseWheel>", on_tk_scroll, add="+")
        self.bind_all("<Shift-MouseWheel>", on_tk_scroll, add="+")

        # Backup wheel hook for precision trackpads that may not emit pynput scroll events.
        self._wheel_hook = None
        if sys.platform == "win32":
            try:
                self._wheel_hook = _GlobalWheelHook(
                    on_scroll=lambda: self._queue_input_reset("trackpad_hook") if self._scanning else None
                )
                if not self._wheel_hook.start():
                    self._debug_event("Trackpad wheel hook failed to start; using tk/pynput fallbacks", "warn")
                    self._wheel_hook = None
            except Exception:
                self._debug_event("Trackpad wheel hook failed to initialize; using tk/pynput fallbacks", "warn")
                self._wheel_hook = None

        # Additional global scroll-start event hook to catch precision touchpad scrolling
        # in other focused applications.
        self._scroll_event_hook = None
        if sys.platform == "win32":
            try:
                self._scroll_event_hook = _GlobalScrollEventHook(
                    on_scroll=lambda: self._queue_input_reset("win_scroll_event") if self._scanning else None
                )
                if not self._scroll_event_hook.start():
                    self._debug_event("Global scroll event hook failed to start", "warn")
                    self._scroll_event_hook = None
            except Exception:
                self._debug_event("Global scroll event hook failed to initialize", "warn")
                self._scroll_event_hook = None

        # Raw input sink catches wheel-style HID input globally, including many
        # precision touchpad drivers that do not surface via classic wheel hooks.
        self._raw_input_hook = None
        if sys.platform == "win32":
            try:
                self._raw_input_hook = _RawInputWheelSink(
                    on_scroll=lambda: self._queue_input_reset("raw_input_wheel") if self._scanning else None,
                    on_touchpad_activity=lambda: self._queue_input_reset("raw_input_touchpad") if self._scanning else None,
                )
                if not self._raw_input_hook.start():
                    self._debug_event("Raw input wheel hook failed to start", "warn")
                    self._raw_input_hook = None
            except Exception:
                self._debug_event("Raw input wheel hook failed to initialize", "warn")
                self._raw_input_hook = None

    def _queue_input_reset(self, source: str):
        """Schedule a vote reset and include the trigger source in debug output."""
        self.after(0, self._reset_votes, source)

    def _reset_item_for_input(self, idx: int, item: dict, preserve_locked_additive: bool) -> bool:
        """Reset one row for a click/scroll event. Returns True when a locked additive row was kept."""
        item["votes"].clear()

        if preserve_locked_additive and item.get("additive_locked", False):
            item["status"] = "found"
            item["additive_count"] = max(self._ADDITIVE_LOCK_FRAMES, item.get("additive_count", 0))
            # Preserve locked-green state, but clear stale overlay boxes.
            item["last_boxes"] = []
            self._update_row_additive(idx, item["additive_count"], True)
            return True

        item["status"] = "pending"
        item["last_boxes"] = []
        item["additive_count"] = 0
        item["additive_locked"] = False
        if self._additive_mode:
            self._update_row_additive(idx, 0, False)
        return False

    def _reset_votes(self, source: str = "input"):
        """Clear all vote histories and hide overlay — called on mouse interaction."""
        self._scan_gen += 1  # invalidate any in-flight scan results

        if self._additive_mode:
            locked_count = 0
            for i, item in enumerate(self._items):
                if self._reset_item_for_input(i, item, preserve_locked_additive=True):
                    locked_count += 1

            self._overlay.hide()
            self._debug_event(
                f"Votes reset ({source}); generation={self._scan_gen}; additive_locked_preserved={locked_count}",
                "info",
            )
            return

        for i, item in enumerate(self._items):
            self._reset_item_for_input(i, item, preserve_locked_additive=False)
        self._restore_item_texts()
        self._reset_colors()
        self._overlay.hide()
        self._debug_event(f"Votes reset ({source}); generation={self._scan_gen}", "info")

    # ── Debug panel ───────────────────────────────────────────────────────────

    def _toggle_debug(self):
        if hasattr(self, "_debug_win") and self._debug_win and self._debug_win.winfo_exists():
            self._close_debug_win()
            return
        win = tk.Toplevel(self)
        win.title("Debug")
        win.geometry("520x760")
        win.configure(bg="#1a1a1a")
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_debug_win())
        self._debug_win = win
        self._sync_window_stack()

        # ── Scale slider ──
        scale_row = tk.Frame(win, bg="#1a1a1a")
        scale_row.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(scale_row, text="OCR Scale:", bg="#1a1a1a", fg="#cccccc",
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        self._scale_val_lbl = tk.Label(
            scale_row, text=f"{self._ocr_scale:.1f}x",
            bg="#1a1a1a", fg="#ffcc00", font=("Consolas", 11, "bold"), width=5,
        )
        self._scale_val_lbl.pack(side="right")

        def on_scale(v):
            self._ocr_scale = round(float(v), 1)
            self._scale_val_lbl.configure(text=f"{self._ocr_scale:.1f}x")
            self._debug_event(f"OCR scale changed to {self._ocr_scale:.1f}x", "info")

        self._scale_slider = tk.Scale(
            win, from_=0.2, to=4.0, resolution=0.1, orient="horizontal",
            command=on_scale, bg="#1a1a1a", fg="#cccccc", troughcolor="#333333",
            highlightthickness=0, sliderrelief="flat", length=480,
        )
        self._scale_slider.set(self._ocr_scale)
        self._scale_slider.pack(fill="x", padx=10, pady=(2, 6))

        def toggle_screenshot():
            self._show_debug_screenshot = bool(self._debug_screenshot_var.get())
            if not self._show_debug_screenshot:
                self._debug_photo = None
                self._debug_img_label.configure(image="", text="", height=1)
            else:
                self._debug_img_label.configure(height=220)
            self._debug_event(
                f"Last screenshot preview {'enabled' if self._show_debug_screenshot else 'disabled'}",
                "info",
            )

        self._debug_screenshot_var = tk.BooleanVar(value=self._show_debug_screenshot)
        tk.Checkbutton(
            win,
            text="Show last screenshot",
            variable=self._debug_screenshot_var,
            command=toggle_screenshot,
            bg="#1a1a1a",
            fg="#cccccc",
            selectcolor="#2b2b2b",
            activebackground="#1a1a1a",
            activeforeground="#ffffff",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 2))
        self._debug_img_label = tk.Label(
            win,
            bg="#1a1a1a",
            anchor="nw",
            height=1 if not self._show_debug_screenshot else 220,
        )
        self._debug_img_label.pack(fill="x", padx=10)

        def toggle_ocr_frames():
            self._show_debug_ocr_frames = bool(self._debug_ocr_frames_var.get())
            self._debug_event(
                f"OCR frame overlay {'enabled' if self._show_debug_ocr_frames else 'disabled'}",
                "info",
            )

        self._debug_ocr_frames_var = tk.BooleanVar(value=self._show_debug_ocr_frames)
        tk.Checkbutton(
            win,
            text="Show OCR frames",
            variable=self._debug_ocr_frames_var,
            command=toggle_ocr_frames,
            bg="#1a1a1a",
            fg="#cccccc",
            selectcolor="#2b2b2b",
            activebackground="#1a1a1a",
            activeforeground="#ffffff",
            font=("Segoe UI", 11),
        ).pack(anchor="w", padx=10, pady=(2, 6))

        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Debug.TNotebook", background="#1a1a1a", borderwidth=0)
        style.configure(
            "Debug.TNotebook.Tab",
            background="#2b2b2b",
            foreground="#cccccc",
            padding=(12, 6),
        )
        style.map(
            "Debug.TNotebook.Tab",
            background=[("selected", "#111111")],
            foreground=[("selected", "#ffffff")],
        )

        tabs = ttk.Notebook(win, style="Debug.TNotebook")
        tabs.pack(fill="both", expand=True, padx=10, pady=(10, 10))
        scan_tab = tk.Frame(tabs, bg="#1a1a1a")
        debug_tab = tk.Frame(tabs, bg="#1a1a1a")
        tabs.add(scan_tab, text="Scan Log")
        tabs.add(debug_tab, text="Debug Log")

        self._debug_text = self._make_debug_text(scan_tab)
        self._debug_event_text = self._make_debug_text(debug_tab)
        self._debug_event(
            f"Debug panel opened; screenshot preview {'enabled' if self._show_debug_screenshot else 'disabled'}; "
            f"scale={self._ocr_scale:.1f}x; overlay={'on' if self._show_overlay else 'off'}",
            "info",
        )

    def _make_debug_text(self, parent: tk.Misc) -> tk.Text:
        frame = tk.Frame(parent, bg="#1a1a1a")
        frame.pack(fill="both", expand=True)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        text = tk.Text(
            frame,
            bg="#111111",
            fg="#dddddd",
            font=("Consolas", 10),
            yscrollcommand=sb.set,
            state="disabled",
            wrap="word",
        )
        text.pack(fill="both", expand=True)
        sb.config(command=text.yview)
        text.tag_configure("found", foreground="#6dff6d")
        text.tag_configure("not_found", foreground="#ff7070")
        text.tag_configure("info", foreground="#aaaaaa")
        text.tag_configure("warn", foreground="#ffcc00")
        text.tag_configure("error", foreground="#ff7070")
        return text

    def _debug_log(self, lines: list[tuple[str, str]]):
        """Append lines to the scan log. Each entry is (text, tag)."""
        self._append_debug_lines("_debug_text", lines, max_lines=300)

    def _debug_event(self, message: str, tag: str = "info"):
        """Append a timestamped message to the debug log."""
        stamp = time.strftime("%H:%M:%S")
        self._append_debug_lines("_debug_event_text", [(f"[{stamp}] {message}", tag)], max_lines=500)

    def _append_debug_lines(self, attr: str, lines: list[tuple[str, str]], max_lines: int):
        try:
            if not (hasattr(self, "_debug_win") and self._debug_win and self._debug_win.winfo_exists()):
                return
            t = getattr(self, attr, None)
            if t is None:
                return
            t.configure(state="normal")
            for text, tag in lines:
                t.insert("end", text + "\n", tag)
            # Keep the widget bounded so a long debug session stays responsive.
            line_count = int(t.index("end-1c").split(".")[0])
            if line_count > max_lines:
                t.delete("1.0", f"{line_count - max_lines}.0")
            t.configure(state="disabled")
            t.see("end")
        except Exception:
            import traceback; traceback.print_exc()

    def _debug_update_image(self, img: Image.Image, tiles: list[dict] | None = None):
        """Show a thumbnail of img in the debug panel, optionally overlaying OCR tile frames."""
        try:
            if not (hasattr(self, "_debug_win") and self._debug_win and self._debug_win.winfo_exists()):
                return
            if not self._show_debug_screenshot:
                return
            from PIL import ImageTk, ImageDraw
            MAX_W, MAX_H = 500, 220
            thumb = img.copy().convert("RGB")
            if self._show_debug_ocr_frames and tiles and len(tiles) > 1:
                draw = ImageDraw.Draw(thumb)
                for tile in tiles:
                    x0 = tile["left"]
                    y0 = tile["top"]
                    x1 = x0 + tile["width"] - 1
                    y1 = y0 + tile["height"] - 1
                    draw.rectangle([x0, y0, x1, y1], outline=(255, 140, 0), width=2)
            thumb.thumbnail((MAX_W, MAX_H), Image.BILINEAR)
            padded = Image.new("RGB", (max(thumb.width, 1), MAX_H), (26, 26, 26))
            padded.paste(thumb, (0, (MAX_H - thumb.height) // 2))
            self._debug_photo = ImageTk.PhotoImage(padded)
            self._debug_img_label.configure(image=self._debug_photo, height=MAX_H)
        except Exception:
            import traceback; traceback.print_exc()

    # ── Scan area selection ───────────────────────────────────────────────────

    def _set_area(self):
        self._debug_event("Scan-area selector opened", "info")
        self.withdraw()
        self.after(200, lambda: ScanAreaSelector(self, self._area_selected))

    def _area_selected(self, area: dict):
        self._scan_area = area
        self.deiconify()
        info = f"{area['width']} × {area['height']}  at  ({area['left']}, {area['top']})"
        self._area_lbl.configure(text=f"Scan area: {info}", text_color="#4caf50")
        self._set_status("Scan area set")
        self._save_config()
        self._debug_event(f"Scan area set: {info}", "info")

    # ── List loading ──────────────────────────────────────────────────────────

    def _parse_list(self) -> bool:
        """Read the textbox, build self._items. Returns False if empty."""
        raw = self._text_box.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("Empty", "Paste items into the text box first.", parent=self)
            self._debug_event("Start rejected: item list is empty", "warn")
            return False
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        self._items = [
            {
                "text": ln,
                "search_prep": _preprocess_search_term(ln),
                "is_numeric": _is_numeric_item(ln),
                "status": "pending",
                "votes": deque(maxlen=self._WINDOW_SIZE),
                "last_boxes": [],
                "additive_count": 0,
                "additive_locked": False,
            }
            for ln in lines
        ]
        self._ocr_digits_only = bool(self._items) and all(item["is_numeric"] for item in self._items)
        self._debug_event(f"Parsed {len(self._items)} list item(s)", "info")
        self._debug_event(
            f"OCR mode: {'digits-only + English' if self._ocr_digits_only else 'English'}",
            "info",
        )
        return True

    def _update_row(self, idx: int, status: str):
        if idx >= len(self._items):
            return
        self._items[idx]["status"] = status
        # Line numbers in tk.Text are 1-based
        line_start = f"{idx + 1}.0"
        line_end   = f"{idx + 1}.end"
        self._tk_text.tag_remove("found",     line_start, line_end)
        self._tk_text.tag_remove("not_found", line_start, line_end)
        self._tk_text.tag_remove("pending",   line_start, line_end)
        self._tk_text.tag_add(status, line_start, line_end)

    def _reset_colors(self):
        self._tk_text.tag_remove("found",     "1.0", "end")
        self._tk_text.tag_remove("not_found", "1.0", "end")
        self._tk_text.tag_remove("pending",   "1.0", "end")

    # ── Scanning ──────────────────────────────────────────────────────────────

    def _toggle_scan(self):
        if self._scanning:
            self._stop()
        else:
            self._start()

    def _start(self):
        if not self._scan_area:
            messagebox.showwarning("No Area", "Set a scan area first.", parent=self)
            self._debug_event("Start rejected: scan area is not set", "warn")
            return
        if not self._parse_list():
            return

        # Lock the textbox so text can't be edited while scanning
        self._text_box.configure(state="disabled")
        self._box_label.configure(text="Scanning results (stop to edit):")

        self._scanning = True
        self._start_btn.configure(fg_color="#6b1c1c", hover_color="#852222")
        self._refresh_start_button()
        self._set_status("Scanning…")
        self._scan_pass = 0
        area = self._scan_area
        overlap_x_px = self._effective_tile_overlap_x_px()
        overlap_y_px = max(0, int(self._ocr_tile_overlap_px))
        longest_token_chars = self._longest_search_token_chars()
        self._debug_event(
            f"Scanning started: items={len(self._items)}, area={area['width']}x{area['height']} "
            f"at ({area['left']},{area['top']}), scale={self._ocr_scale:.1f}x, "
            f"ocr_mode={'digits-only' if self._ocr_digits_only else 'general'}, lang=eng, "
            f"adaptive_tiles=target{self._ocr_tile_target_px}px/min{self._OCR_TILE_MIN_PX}px/"
            f"overlap(x={overlap_x_px}px,y={overlap_y_px}px;base={self._ocr_tile_overlap_px}px,longest_token={longest_token_chars})/max{self._ocr_tile_max}, "
            f"overlay={'on' if self._show_overlay else 'off'}, screenshot_preview="
            f"{'on' if self._show_debug_screenshot else 'off'}",
            "info",
        )
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._scanning = False
        self._start_btn.configure(fg_color="#2a5c2a", hover_color="#357535")
        self._refresh_start_button()
        self._overlay.hide()
        # Restore any additive-mode star text before re-enabling the textbox for editing
        self._restore_item_texts()
        # Unlock textbox for editing and reset colors
        self._text_box.configure(state="normal")
        self._reset_colors()
        self._box_label.configure(text="Paste items to scan for (one per line):")
        self._set_status("Stopped")
        self._debug_event("Scanning stopped", "info")

    def _update_item_boxes_for_scan(self, item: dict, hits: list[tuple], gen: int):
        """Persist only current-pass hit boxes; clear stale geometry on misses."""
        if self._scan_gen == gen:
            item["last_boxes"] = hits if hits else []

    def _process_additive_item_scan(
        self,
        idx: int,
        item: dict,
        hits: list[tuple],
        gen: int,
    ) -> tuple[int, list[tuple], tuple[str, str], str | None]:
        """Update one row for additive mode and return render/debug outputs."""
        status_change = None

        if not item.get("additive_locked", False):
            if hits:
                new_count = item.get("additive_count", 0) + 1
                item["additive_count"] = new_count
                locked = new_count >= self._ADDITIVE_LOCK_FRAMES
                if locked:
                    item["additive_locked"] = True
                    item["status"] = "found"
                    status_change = f"{item['text']} -> found (additive x{self._ADDITIVE_LOCK_FRAMES})"
                if self._scan_gen == gen:
                    self.after(0, self._update_row_additive, idx, new_count, locked)
            elif item.get("additive_count", 0) == 0:
                # No stars yet and no hit -> show not_found
                if item["status"] != "not_found" and self._scan_gen == gen:
                    item["status"] = "not_found"
                    self.after(0, self._update_row_additive, idx, 0, False)

        count = item.get("additive_count", 0)
        locked = item.get("additive_locked", False)
        boxes = item["last_boxes"] if (locked or (count > 0 and item["last_boxes"])) else []
        tag = "found" if locked else ("additive" if count > 0 else "not_found")
        stars = " " + "*" * count if count > 0 and not locked else ""
        line = f"  {'✓' if hits else '✗'} [additive:{count}/{self._ADDITIVE_LOCK_FRAMES}] {item['text']}{stars}"
        return (1 if locked else 0), boxes, (line, tag), status_change

    def _process_normal_item_scan(
        self,
        idx: int,
        item: dict,
        hits: list[tuple],
        gen: int,
    ) -> tuple[int, list[tuple], tuple[str, str], str | None]:
        """Update one row for normal (majority-vote) mode and return outputs."""
        n = len(item["votes"])
        vote_sum = sum(item["votes"])

        # Found when more than half of accumulated votes are hits.
        new_status = "found" if vote_sum > n / 2 else "not_found"
        status_change = None
        if new_status != item["status"] and self._scan_gen == gen:
            item["status"] = new_status
            self.after(0, self._update_row, idx, new_status)
            status_change = f"{item['text']} -> {new_status}"

        boxes = item["last_boxes"] if item["status"] == "found" else []
        tag = "found" if item["status"] == "found" else "not_found"
        votes_str = "".join("●" if v else "○" for v in item["votes"])
        line = f"  {'✓' if hits else '✗'} [{votes_str}] {vote_sum}/{n} {item['text']}"
        return (1 if item["status"] == "found" else 0), boxes, (line, tag), status_change

    def _loop(self):
        _last_frame_hash: int | None = None
        _cached_prepared_ocr: dict | None = None
        _cached_words: list[str] = []
        _cached_ocr_ms: float = 0.0
        _cached_tile_desc: str = "1x1 (1 tile)"
        _cached_tiles: list[dict] = []
        while self._scanning:
            try:
                pass_start = time.perf_counter()
                self._scan_pass += 1
                scan_id = self._scan_pass
                area = dict(self._scan_area)  # snapshot to avoid race
                gen = self._scan_gen          # capture generation at scan start
                additive_mode = self._additive_mode  # snapshot mode for this pass
                self.after(
                    0,
                    self._debug_event,
                    f"Scan {scan_id} started; generation={gen}; area={area['width']}x{area['height']} "
                    f"at ({area['left']},{area['top']})",
                    "info",
                )

                # Hide overlay just for the capture to avoid OCR reading its own boxes.
                hide_done = threading.Event()
                self.after(0, lambda e=hide_done: (self._overlay.hide(), e.set()))
                hide_done.wait(timeout=0.3)

                # ImageGrab.grab with all_screens=True handles cross-monitor regions
                # correctly via DWM, unlike mss which uses a per-monitor DC.
                bbox = (
                    area["left"],
                    area["top"],
                    area["left"] + area["width"],
                    area["top"]  + area["height"],
                )
                orig_img = ImageGrab.grab(bbox=bbox, all_screens=True)
                grab_ms = (time.perf_counter() - pass_start) * 1000

                # Keep overlay hidden until current-pass OCR completes to avoid ghost boxes.

                # If a reset happened during the grab/OCR, discard this scan entirely
                if self._scan_gen != gen:
                    self.after(0, self._debug_event, f"Scan {scan_id} discarded after capture; generation changed to {self._scan_gen}", "warn")
                    time.sleep(self._SCAN_INTERVAL)
                    continue

                # Convert to grayscale before resizing — 1/3 the pixel data = much faster resize.
                # Tesseract converts to grayscale internally anyway.
                _SCALE = self._ocr_scale
                gray = orig_img.convert("L")
                w, h = gray.size
                img = gray.resize((int(w * _SCALE), int(h * _SCALE)), Image.BILINEAR)

                # Frame-hash skip: if the captured frame is byte-identical to the
                # previous pass, reuse the cached OCR result and skip Tesseract entirely.
                frame_hash = hash(img.tobytes())
                frame_changed = (frame_hash != _last_frame_hash) or (_cached_prepared_ocr is None)

                if not frame_changed:
                    prepared_ocr = _cached_prepared_ocr
                    words = _cached_words
                    ocr_ms = 0.0
                    tile_desc = _cached_tile_desc
                    tiles = _cached_tiles
                    self.after(0, self._debug_event,
                               f"Scan {scan_id}: frame unchanged — reusing cached OCR (saved {_cached_ocr_ms:.0f}ms)",
                               "info")
                else:
                    overlap_x_px = self._effective_tile_overlap_x_px()
                    overlap_y_px = max(0, int(self._ocr_tile_overlap_px))
                    pass_result = self._ocr_engine.run_pass(
                        gray=gray,
                        scale=_SCALE,
                        digits_only=self._ocr_digits_only,
                        target_px=self._ocr_tile_target_px,
                        min_tile_px=self._OCR_TILE_MIN_PX,
                        overlap_x_px=overlap_x_px,
                        overlap_y_px=overlap_y_px,
                        max_tiles=self._effective_tile_max(),
                    )
                    prepared_ocr = pass_result.prepared_ocr
                    words = pass_result.words
                    ocr_ms = pass_result.ocr_ms
                    tile_desc = pass_result.tile_desc
                    tiles = pass_result.tiles
                    _last_frame_hash = frame_hash
                    _cached_prepared_ocr = prepared_ocr
                    _cached_words = words
                    _cached_ocr_ms = ocr_ms
                    _cached_tile_desc = tile_desc
                    _cached_tiles = tiles

                # Push screenshot to debug panel
                if self._show_debug_screenshot:
                    self.after(0, self._debug_update_image, orig_img, tiles)

                all_boxes: list[tuple] = []
                found_count = 0
                items_snapshot = list(self._items)  # snapshot
                debug_lines: list[tuple[str, str]] = []
                changed_statuses: list[str] = []

                for i, item in enumerate(items_snapshot):
                    hits = _locate_prepared(item["text"], prepared_ocr, item.get("search_prep"))
                    item["votes"].append(1 if hits else 0)
                    self._update_item_boxes_for_scan(item, hits, gen)

                    if additive_mode:
                        found_inc, item_boxes, debug_entry, status_change = self._process_additive_item_scan(
                            i, item, hits, gen
                        )
                    else:
                        found_inc, item_boxes, debug_entry, status_change = self._process_normal_item_scan(
                            i, item, hits, gen
                        )

                    found_count += found_inc
                    if item_boxes:
                        all_boxes.extend(item_boxes)
                    debug_lines.append(debug_entry)
                    if status_change:
                        changed_statuses.append(status_change)

                elapsed_ms = (time.perf_counter() - pass_start) * 1000
                debug_lines.insert(0, (f"--- Scan {scan_id} (window={self._WINDOW_SIZE}) ---", "info"))
                self.after(0, self._debug_log, debug_lines)
                total = len(items_snapshot)
                if self._scan_gen != gen:
                    self.after(0, self._debug_event, f"Scan {scan_id} discarded after OCR; generation changed to {self._scan_gen}", "warn")
                    time.sleep(self._SCAN_INTERVAL)
                    continue
                self.after(0, self._set_status, f"Scanning…  {found_count}/{total} found")
                if self._show_overlay:
                    self.after(0, lambda a=area, b=all_boxes, g=gen:
                        self._overlay.show(a, b) if self._scan_gen == g else None)
                self.after(
                    0,
                    self._debug_event,
                    f"Scan {scan_id} complete: found={found_count}/{total}, overlay_boxes={len(all_boxes)}, "
                    f"ocr_words={len(words)}, tiles={tile_desc}, capture={grab_ms:.0f}ms, ocr={ocr_ms:.0f}ms, total={elapsed_ms:.0f}ms, "
                    f"status_changes={len(changed_statuses)}",
                    "info",
                )
                for change in changed_statuses:
                    self.after(0, self._debug_event, f"Scan {scan_id}: {change}", "found")

            except Exception as exc:
                import traceback; traceback.print_exc()
                self.after(0, self._set_status, f"Error: {exc}")
                self.after(0, self._debug_log, [(f"ERROR: {exc}", "not_found")])
                self.after(0, self._debug_event, f"Scan loop error: {exc}", "error")
                time.sleep(3)
                continue

            time.sleep(self._SCAN_INTERVAL)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str):
        self._status_lbl.configure(text=msg)

    def _effective_tile_max(self) -> int:
        cpu = os.cpu_count() or 4
        return max(1, min(self._ocr_tile_max, cpu * 2))

    def _longest_search_token_chars(self) -> int:
        longest = 0
        for item in self._items:
            prep = item.get("search_prep") or {}
            for key in ("query", "sep_query"):
                for token in prep.get(key, []):
                    if token:
                        longest = max(longest, len(token))
        return longest

    def _effective_tile_overlap_x_px(self) -> int:
        configured = max(0, int(self._ocr_tile_overlap_px))
        longest = self._longest_search_token_chars()
        if longest <= 0:
            return configured

        extra_chars = max(0, longest - self._OCR_DYNAMIC_OVERLAP_BASE_CHARS)
        px_per_char = int(round(
            self._OCR_DYNAMIC_OVERLAP_AVG_CHAR_WIDTH_PX * self._OCR_DYNAMIC_OVERLAP_CHAR_WIDTH_RATIO
        ))
        estimated = configured + (extra_chars * px_per_char)
        # Keep overlap below target tile size to avoid degenerate step size.
        max_safe_overlap = max(0, int(self._ocr_tile_target_px) - 1)
        return min(estimated, max_safe_overlap)

    def _close_debug_win(self):
        """Close the debug window and reset tracking state."""
        if hasattr(self, "_debug_win") and self._debug_win:
            try:
                self._debug_win.destroy()
            except Exception:
                pass
            self._debug_win = None
        self._sync_window_stack()

    def _close_config_win(self):
        """Close the config window and reset tracking state."""
        if hasattr(self, "_config_win") and self._config_win:
            try:
                self._config_win.grab_release()
            except Exception:
                pass
            try:
                self._config_win.destroy()
            except Exception:
                pass
            self._config_win = None
        self._sync_window_stack()

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._scanning = False
        # Stop listeners first so no callbacks fire against a destroyed widget
        if hasattr(self, "_hk_listener") and self._hk_listener is not None:
            try:
                self._hk_listener.stop()
            except Exception:
                pass
            self._hk_listener = None
        if hasattr(self, "_mouse_listener"):
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
        if hasattr(self, "_wheel_hook") and self._wheel_hook is not None:
            try:
                self._wheel_hook.stop()
            except Exception:
                pass
            self._wheel_hook = None
        if hasattr(self, "_scroll_event_hook") and self._scroll_event_hook is not None:
            try:
                self._scroll_event_hook.stop()
            except Exception:
                pass
            self._scroll_event_hook = None
        if hasattr(self, "_raw_input_hook") and self._raw_input_hook is not None:
            try:
                self._raw_input_hook.stop()
            except Exception:
                pass
            self._raw_input_hook = None
        self._save_config()
        self._close_config_win()
        self._close_debug_win()
        self._overlay.hide()
        self.destroy()


# =============================================================================
# =============================================================================

if __name__ == "__main__":
    app = ListScannerApp()
    app.mainloop()
