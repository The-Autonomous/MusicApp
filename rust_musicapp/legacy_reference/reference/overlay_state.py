"""Persist overlay preferences and keep the whole player on a connected monitor."""
import ctypes
import json
from math import isfinite
import os
from pathlib import Path
import tempfile
from threading import Event, Lock, Thread


def valid_position(value):
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and isfinite(v) and abs(v) <= 10 ** 7 for v in value)):
        return tuple(int(v) for v in value)
    return None


def clamp_position(position, width, height, areas):
    """Use monitor work areas, including monitors left of the primary display."""
    width, height = max(1, int(width)), max(1, int(height))
    areas = [area for area in areas if area[2] > area[0] and area[3] > area[1]]
    if not areas:
        areas = [(0, 0, 1920, 1080)]
    position = valid_position(position)
    if position is None:
        left, top, right, bottom = areas[0]
        position = (left + max(0, (right - left - width) // 2), top + 20)
    x, y = position
    candidates = [(max(left, min(x, max(left, right - width))),
                   max(top, min(y, max(top, bottom - height))))
                  for left, top, right, bottom in areas]
    return min(candidates, key=lambda point: (point[0] - x) ** 2 + (point[1] - y) ** 2)


def monitor_work_areas(root):
    if os.name == "nt":
        from ctypes import wintypes

        class Rect(ctypes.Structure):
            _fields_ = [(name, wintypes.LONG) for name in ('left', 'top', 'right', 'bottom')]

        class Info(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('monitor', Rect), ('work', Rect), ('flags', wintypes.DWORD)]

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                          ctypes.POINTER(Rect), wintypes.LPARAM)
        user32 = ctypes.windll.user32
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(Info)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(Rect), callback_type, wintypes.LPARAM]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        areas = []

        def inspect(handle, dc, rect, parameter):
            info = Info()
            info.size = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                r = info.work
                areas.append((bool(info.flags & 1), (r.left, r.top, r.right, r.bottom)))
            return True

        if user32.EnumDisplayMonitors(None, None, callback_type(inspect), 0) and areas:
            return [area for primary, area in sorted(areas, key=lambda item: not item[0])]
    return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]


def place_window(window, position, width=None, height=None):
    x, y = position
    if width is not None and height is not None:
        window.geometry(f'{int(width)}x{int(height)}')
    if os.name == 'nt':
        from ctypes import wintypes
        window.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        handle = user32.GetParent(window.winfo_id()) or window.winfo_id()
        if not user32.SetWindowPos(handle, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010):
            raise ctypes.WinError()
    else:
        window.geometry(f'{x:+d}{y:+d}')


class OverlayPreferences:
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        self.visible = data.get('visible') if isinstance(data.get('visible'), bool) else True
        self.player_enabled = data.get('player_enabled') if isinstance(data.get('player_enabled'), bool) else True
        self.position = valid_position(data.get('position'))
        self.lock = Lock()
        self.wake = Event()
        self.pending = None
        self.thread = None
        self.closed = False

    def save(self, visible, player_enabled, position):
        with self.lock:
            if self.closed:
                return
            self.pending = {'visible': bool(visible), 'player_enabled': bool(player_enabled),
                            'position': valid_position(position)}
            if self.thread is None:
                self.thread = Thread(target=self._write, daemon=True, name='overlay-settings')
                self.thread.start()
            self.wake.set()

    def _write(self):
        while True:
            self.wake.wait()
            with self.lock:
                data, self.pending = self.pending, None
                self.wake.clear()
                closed = self.closed
            if data is not None:
                temporary = None
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                                     prefix=self.path.name, suffix='.tmp', delete=False) as handle:
                        temporary = Path(handle.name)
                        json.dump(data, handle)
                    os.replace(temporary, self.path)
                except OSError:
                    pass
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
            with self.lock:
                if self.closed and self.pending is None:
                    return
            if closed:
                self.wake.set()

    def close(self):
        with self.lock:
            self.closed = True
            self.wake.set()
        if self.thread:
            self.thread.join(2)
