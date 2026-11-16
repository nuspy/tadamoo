"""Computer control on Windows with the standard library alone: input by SendInput, pictures by GDI (scaled by the system, encoded as PNG by png.py), windows
and screens by the user32 calls, and the accessibility tree by UI Automation through PowerShell and the .NET assemblies Windows ships (uia_tree.ps1).
Nothing to install. Elevated programs ignore input from a program that is not elevated; the error says so."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import List, Tuple

from tadamoo.computer.backend import Backend, ComputerError, Monitor, UiNode, Window
from tadamoo.computer.png import bgra_to_rgb, encode_rgb

ULONG_PTR = ctypes.c_size_t

MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x1, 0x2, 0x4, 0x8, 0x10
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x20, 0x40, 0x800, 0x1000, 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
WHEEL_DELTA = 120
BUTTON_FLAGS = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP), "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP), "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}
NAMED_VK = {"enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "space": 0x20, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "insert": 0x2D, "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
            **{f"f{n}": 0x70 + n - 1 for n in range(1, 13)}}
PUNCTUATION_VK = {"-": 0xBD, "=": 0xBB, "+": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0, "\\": 0xDC}
EXTENDED = {"home", "end", "pageup", "pagedown", "up", "down", "left", "right", "insert", "delete"}
SRCCOPY, CAPTUREBLT, HALFTONE = 0x00CC0020, 0x40000000, 4
SCRIPT = Path(__file__).with_name("uia_tree.ps1")


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def virtual_key(name: str) -> int:
    if name in NAMED_VK:
        return NAMED_VK[name]
    if name in PUNCTUATION_VK:
        return PUNCTUATION_VK[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ComputerError(f"'{name}' is not a key this system can press")


def utf16_units(ch: str) -> List[int]:
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


class WindowsBackend(Backend):
    name = "windows"

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi")
        self._types()
        self._dpi_aware()

    def _types(self) -> None:
        u, g, k = self.user32, self.gdi32, self.kernel32
        u.SendInput.argtypes, u.SendInput.restype = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT
        u.GetDC.argtypes, u.GetDC.restype = [wintypes.HWND], wintypes.HDC
        u.ReleaseDC.argtypes, u.ReleaseDC.restype = [wintypes.HWND, wintypes.HDC], ctypes.c_int
        u.GetCursorPos.argtypes, u.GetCursorPos.restype = [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL
        u.GetSystemMetrics.argtypes, u.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
        u.MapVirtualKeyW.argtypes, u.MapVirtualKeyW.restype = [wintypes.UINT, wintypes.UINT], wintypes.UINT
        u.IsWindowVisible.argtypes = u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowLongW.argtypes, u.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], wintypes.LONG
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
        g.CreateCompatibleBitmap.argtypes, g.CreateCompatibleBitmap.restype = [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP
        g.SelectObject.argtypes, g.SelectObject.restype = [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        g.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
        g.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]

    def _dpi_aware(self) -> None:
        """Positions are real pixels, not the scaled ones a program that is not DPI aware is given."""
        try:
            self.user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            if self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):       # per-monitor v2
                return
        except (AttributeError, OSError):
            pass
        try:
            self.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass

    # -- screens and pictures --------------------------------------------------------------------------------------

    def monitors(self) -> List[Monitor]:
        found: List[Tuple[int, int, int, int, bool]] = []
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def each(handle: int, _dc: int, _rect: object, _data: int) -> bool:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if self.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                r = info.rcMonitor
                found.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1)))
            return True

        self.user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, proc_type, wintypes.LPARAM]
        self.user32.EnumDisplayMonitors(None, None, proc_type(each), 0)
        found.sort(key=lambda m: (m[0], m[1]))
        return [Monitor(i + 1, *m[:4], primary=m[4]) for i, m in enumerate(found)]

    def capture(self, left: int, top: int, width: int, height: int, out_width: int, out_height: int) -> bytes:
        g, u = self.gdi32, self.user32
        screen = u.GetDC(None)
        mem = g.CreateCompatibleDC(screen)
        bitmap = g.CreateCompatibleBitmap(screen, out_width, out_height)
        try:
            previous = g.SelectObject(mem, bitmap)
            g.SetStretchBltMode(mem, HALFTONE)
            ok = g.StretchBlt(mem, 0, 0, out_width, out_height, screen, left, top, width, height, SRCCOPY | CAPTUREBLT)
            g.SelectObject(mem, previous)                          # GetDIBits wants the bitmap out of every device context
            if not ok:
                raise ComputerError("Windows did not give a picture of the screen (a locked screen or a protected window)")
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), out_width, -out_height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(out_width * out_height * 4)
            if g.GetDIBits(mem, bitmap, 0, out_height, buffer, ctypes.byref(header), 0) != out_height:
                raise ComputerError("Windows did not give the pixels of the picture")
        finally:
            g.DeleteObject(bitmap)
            g.DeleteDC(mem)
            u.ReleaseDC(None, screen)
        return encode_rgb(bgra_to_rgb(buffer.raw), out_width, out_height)

    # -- windows -----------------------------------------------------------------------------------------------------

    def _app(self, hwnd: int) -> str:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(520)
            buf = ctypes.create_unicode_buffer(size.value)
            return os.path.basename(buf.value) if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
        finally:
            self.kernel32.CloseHandle(handle)

    def _frame(self, hwnd: int) -> Tuple[int, int, int, int]:
        rect = wintypes.RECT()
        if self.dwm.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:     # the frame without the invisible shadow border
            self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def windows(self) -> List[Window]:
        found: List[Window] = []
        front = self.user32.GetForegroundWindow() or 0
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd: int, _data: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            cloaked = wintypes.DWORD()
            self.dwm.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))     # a window of another virtual desktop, or a hidden app
            if cloaked.value or self.user32.GetWindowLongW(hwnd, -20) & 0x80:                                                # a tool window (a tooltip, a palette)
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buf, length + 1)
            minimized = bool(self.user32.IsIconic(hwnd))
            left, top, width, height = self._frame(hwnd)
            found.append(Window(int(hwnd), buf.value, self._app(hwnd), left, top, width, height, foreground=int(hwnd) == int(front), minimized=minimized))
            return True

        self.user32.EnumWindows.argtypes = [proc_type, wintypes.LPARAM]
        self.user32.EnumWindows(proc_type(each), 0)
        return found

    def focus(self, window_id: int) -> None:
        if self.user32.IsIconic(window_id):
            self.user32.ShowWindow(window_id, 9)                 # SW_RESTORE
        self.key("alt", True)                                    # a program that just pressed a key may take the foreground
        self.key("alt", False)
        self.user32.SetForegroundWindow(window_id)
        time.sleep(0.15)
        if int(self.user32.GetForegroundWindow() or 0) != int(window_id):
            raise ComputerError("Windows did not bring the window to the front (a program running as administrator cannot be taken over from here)")

    def ui_tree(self, window_id: int, limit: int) -> List[UiNode]:
        if not SCRIPT.exists():
            raise ComputerError("the accessibility reader (uia_tree.ps1) is missing from the installation")
        try:
            done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Hwnd", str(window_id), "-Limit", str(limit)],
                                  capture_output=True, timeout=45, creationflags=0x08000000)
        except subprocess.TimeoutExpired:
            raise ComputerError("the window took too long to describe: ask for fewer elements (limit) or use a picture") from None
        except OSError as e:
            raise ComputerError(f"PowerShell could not be started to read the window: {e}") from None
        if done.returncode != 0:
            raise ComputerError("the window could not be read: " + done.stderr.decode("utf-8", "replace").strip()[:200])
        try:
            rows = json.loads(done.stdout.decode("utf-8-sig", "replace") or "[]")
        except ValueError:
            raise ComputerError("the window's description could not be understood") from None
        return [UiNode(i + 1, str(r.get("r") or ""), str(r.get("n") or ""), int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"]), int(r.get("d") or 0), bool(r.get("e", True)), str(r.get("v") or ""))
                for i, r in enumerate(rows)]

    # -- the pointer and the keyboard -----------------------------------------------------------------------------------

    def _send(self, item: INPUT) -> None:
        if self.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT)) != 1:
            raise ComputerError("Windows did not accept the input: the program in front may run as administrator, or the screen is locked")

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        item = INPUT(type=0)
        item.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        self._send(item)

    def cursor(self) -> Tuple[int, int]:
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise ComputerError("Windows did not tell where the mouse is")
        return point.x, point.y

    def move_to(self, x: int, y: int) -> None:
        vx, vy, vw, vh = (self.user32.GetSystemMetrics(n) for n in (76, 77, 78, 79))          # the virtual screen
        self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, round((x - vx) * 65535 / max(1, vw - 1)), round((y - vy) * 65535 / max(1, vh - 1)))

    def button(self, name: str, down: bool) -> None:
        self._mouse(BUTTON_FLAGS[name][0 if down else 1])

    def wheel(self, dx: int, dy: int) -> None:
        if dy:
            self._mouse(MOUSEEVENTF_WHEEL, data=-dy * WHEEL_DELTA)
        if dx:
            self._mouse(MOUSEEVENTF_HWHEEL, data=dx * WHEEL_DELTA)

    def _keyboard(self, vk: int, scan: int, flags: int) -> None:
        item = INPUT(type=1)
        item.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        self._send(item)

    def key(self, name: str, down: bool) -> None:
        vk = virtual_key(name)
        flags = (KEYEVENTF_EXTENDEDKEY if name in EXTENDED else 0) | (0 if down else KEYEVENTF_KEYUP)
        self._keyboard(vk, self.user32.MapVirtualKeyW(vk, 0), flags)

    def char(self, ch: str) -> None:
        for unit in utf16_units(ch):
            self._keyboard(0, unit, KEYEVENTF_UNICODE)
            self._keyboard(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
