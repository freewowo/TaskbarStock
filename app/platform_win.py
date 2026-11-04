"""Windows 平台相关的小封装：DPI、工作区、工具窗口样式、单实例。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Optional, Tuple

from PIL import Image, ImageChops

from .util import log

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shcore = None
try:
    _shcore = ctypes.windll.shcore
except OSError:
    _shcore = None

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00010000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01
SPI_GETWORKAREA = 0x0030
ERROR_ALREADY_EXISTS = 183


def enable_dpi_awareness() -> None:
    """必须在创建 Tk 窗口之前调用，否则坐标/字体都会被系统缩放欺骗。"""
    try:
        # PER_MONITOR_AWARE_V2 = -4
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        if _shcore and _shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


def dpi_scale() -> float:
    try:
        dpi = _user32.GetDpiForSystem()
        if dpi:
            return float(dpi) / 96.0
    except Exception:
        pass
    try:
        dc = _user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        _user32.ReleaseDC(0, dc)
        return float(dpi) / 96.0 if dpi else 1.0
    except Exception:
        return 1.0


def work_area(point: Optional[Tuple[int, int]] = None) -> Tuple[int, int, int, int]:
    """返回任务栏以外的可用区域 (left, top, right, bottom)。

    传入 point 时返回该点所在显示器的可用区域；不传时保持旧行为（主屏）。
    """
    if point:
        mon = monitor_from_point(*point)
        if mon:
            return mon["work"]
    rect = wt.RECT()
    if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    width, height = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return 0, 0, width, height


def virtual_screen() -> Tuple[int, int, int, int]:
    """所有显示器组成的虚拟桌面矩形 (left, top, right, bottom)，物理像素。"""
    left = _user32.GetSystemMetrics(76)    # SM_XVIRTUALSCREEN
    top = _user32.GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
    width = _user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    height = _user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if width <= 0 or height <= 0:
        width, height = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
        left = top = 0
    return left, top, left + width, top + height


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(wt.RECT), wt.LPARAM,
)


def _dpi_for_hmon(hmon: int) -> float:
    """某个显示器的有效 DPI 倍率（PMv2 进程下每屏可能不同）。"""
    try:
        if _shcore:
            dx = ctypes.c_uint(96)
            dy = ctypes.c_uint(96)
            res = _shcore.GetDpiForMonitor(
                ctypes.c_void_p(hmon), 0, ctypes.byref(dx), ctypes.byref(dy)
            )
            if res == 0 and dx.value:
                return float(dx.value) / 96.0
    except Exception:
        pass
    return dpi_scale()


def _monitor_info(hmon: int) -> Optional[dict]:
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
    if not _user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(info)):
        return None
    rect = (info.rcMonitor.left, info.rcMonitor.top,
            info.rcMonitor.right, info.rcMonitor.bottom)
    work = (info.rcWork.left, info.rcWork.top,
            info.rcWork.right, info.rcWork.bottom)
    return {
        "hmon": int(hmon),
        "rect": rect,
        "work": work,
        "primary": bool(info.dwFlags & 1),
        "device": info.szDevice or "",
        "dpi": _dpi_for_hmon(int(hmon)),
    }


def monitors() -> list:
    """枚举所有显示器：rect/work 为物理像素，dpi 为该屏有效 DPI 倍率。"""
    found: list = []

    def _cb(hmon, _hdc, _lprect, _lparam):
        info = _monitor_info(hmon)
        if info:
            found.append(info)
        return 1

    try:
        _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    except Exception:
        found = []
    if not found:
        width, height = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
        found = [{
            "hmon": 0, "rect": (0, 0, width, height), "work": work_area(),
            "primary": True, "device": "", "dpi": dpi_scale(),
        }]
    found.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
    return found


def monitor_from_point(x: int, y: int, mons: Optional[list] = None) -> Optional[dict]:
    """点所在的显示器；点不在任何屏幕内时取最近的显示器。"""
    items = mons if mons is not None else monitors()
    if not items:
        return None
    for mon in items:
        r = mon["rect"]
        if r[0] <= x < r[2] and r[1] <= y < r[3]:
            return mon
    fallback = next((m for m in items if m.get("primary")), items[0])
    return fallback


def taskbar_rect() -> Optional[Tuple[int, int, int, int]]:
    hwnd = _user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return None
    return window_rect(hwnd)


TASKBAR_BUTTON_CLASSES = (
    "ReBarWindow32", "MSTaskSwWClass", "MSTaskListWClass",
    "Start", "TrayDummySearchControl", "Search", "SearchUI",
)


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    rect = wt.RECT()
    if hwnd and _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def _enum_windows_by_class(class_name: str) -> list:
    found: list = []

    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == class_name:
            found.append(hwnd)
        return 1

    try:
        _user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_int, wt.HWND, wt.LPARAM)(_cb), 0)
    except Exception:
        found = []
    return found


def _layout_from_hwnd(hwnd: int, primary: bool, mons: list) -> Optional[dict]:
    tb = window_rect(hwnd)
    if not tb:
        return None
    center = ((tb[0] + tb[2]) // 2, (tb[1] + tb[3]) // 2)
    mon = monitor_from_point(*center, mons=mons) or {}
    mon_rect = mon.get("rect") or (0, 0, _user32.GetSystemMetrics(0),
                                   _user32.GetSystemMetrics(1))
    width, height = tb[2] - tb[0], tb[3] - tb[1]
    base = {
        "rect": tb, "tray": None, "occupied_right": tb[0],
        "autohide": False, "primary": primary,
        "monitor_rect": mon_rect,
        "monitor_device": mon.get("device") or "",
        "dpi": float(mon.get("dpi") or 1.0),
        "screen": (mon_rect[2] - mon_rect[0], mon_rect[3] - mon_rect[1]),
    }
    if width < 20 or height < 20:
        base["autohide"] = True
        base["orientation"] = "bottom"
        return base

    tray = window_rect(_user32.FindWindowExW(hwnd, None, "TrayNotifyWnd", None))
    occupied_right = tb[0]
    child = _user32.FindWindowExW(hwnd, None, None, None)
    guard = 0
    while child and guard < 40:
        guard += 1
        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(child, cls_buf, 256)
        if cls_buf.value in TASKBAR_BUTTON_CLASSES and _user32.IsWindowVisible(child):
            r = window_rect(child)
            if r and r[0] < tb[2]:
                occupied_right = max(occupied_right, r[2])
        child = _user32.FindWindowExW(hwnd, child, None, None)
    if occupied_right <= tb[0]:
        occupied_right = tb[0] + 8

    if width >= height:
        orientation = "top" if tb[1] <= mon_rect[1] else "bottom"
    else:
        orientation = "left" if tb[0] <= mon_rect[0] else "right"
    base.update({
        "tray": tray,
        "occupied_right": occupied_right,
        "orientation": orientation,
    })
    return base


def taskbar_layouts() -> list:
    """枚举所有任务栏：主屏 Shell_TrayWnd + 各副屏 Shell_SecondaryTrayWnd。"""
    mons = monitors()
    hwnds: list = []
    primary = _user32.FindWindowW("Shell_TrayWnd", None)
    if primary:
        hwnds.append((primary, True))
    for hwnd in _enum_windows_by_class("Shell_SecondaryTrayWnd"):
        hwnds.append((hwnd, False))
    layouts = []
    for hwnd, is_primary in hwnds:
        layout = _layout_from_hwnd(hwnd, is_primary, mons)
        if layout:
            layouts.append(layout)
    return layouts


def taskbar_layout(point: Optional[Tuple[int, int]] = None) -> Optional[dict]:
    """读取任务栏/托盘的真实布局（物理像素）。

    返回 rect=任务栏矩形, tray=通知区域矩形, occupied_right=任务栏按钮/搜索框
    占用的最右边界, orientation=bottom|top|left|right, autohide=是否自动隐藏,
    monitor_device/monitor_rect/dpi=所属显示器信息。

    传入 point 时返回该点所在显示器自己的任务栏；该屏没有任务栏窗口时返回
    None，由调用方按“工作区悬浮”降级。
    """
    layouts = taskbar_layouts()
    if not layouts:
        return None
    if point is None:
        for layout in layouts:
            if layout.get("primary"):
                return layout
        return layouts[0]
    x, y = int(point[0]), int(point[1])
    for layout in layouts:
        r = layout.get("monitor_rect") or ()
        if len(r) == 4 and r[0] <= x < r[2] and r[1] <= y < r[3]:
            return layout
    mon = monitor_from_point(x, y)
    if mon:
        for layout in layouts:
            if layout.get("monitor_device") == mon["device"]:
                return layout
    return None


def screen_luminance(x: int, y: int) -> Optional[float]:
    """取屏幕某点的亮度（0~1），用于判断任务栏是亮色还是暗色。"""
    try:
        hdc = _user32.GetDC(0)
        if not hdc:
            return None
        color = ctypes.windll.gdi32.GetPixel(hdc, int(x), int(y))
        _user32.ReleaseDC(0, hdc)
        if color == 0xFFFFFFFF:
            return None
        r = color & 0xFF
        g = (color >> 8) & 0xFF
        b = (color >> 16) & 0xFF
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    except Exception:
        return None


def raise_topmost(widget) -> None:
    """把窗口放到 topmost 组最前面，确保盖在任务栏之上但不抢焦点。"""
    try:
        hwnd = hwnd_of_toplevel(widget)
        # 注意：hwndInsertAfter 必须按 64 位指针传 HWND_TOPMOST，
        # 直接传 -1 会被 ctypes 当成 32 位整数，调用失败且不报错。
        _user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(-1), 0, 0, 0, 0,
                             0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE
    except Exception:
        pass


def hwnd_of_toplevel(widget) -> int:
    """Tk Toplevel 的外层窗口句柄。"""
    try:
        return int(widget.frame(), 16)
    except Exception:
        return int(widget.winfo_id())


def apply_tool_window(widget) -> None:
    """加 WS_EX_TOOLWINDOW：不进入 Alt+Tab，也不显示在任务栏按钮区。"""
    try:
        hwnd = hwnd_of_toplevel(widget)
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


def apply_no_activate(widget) -> None:
    """加 WS_EX_NOACTIVATE：点击小组件不会抢走当前窗口的焦点。"""
    try:
        hwnd = hwnd_of_toplevel(widget)
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_NOACTIVATE
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


# ---------------------------------------------------------------- mouse hook
WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_QUIT = 0x0012

_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               ctypes.c_size_t, ctypes.c_ssize_t)


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_set_hook = _user32.SetWindowsHookExW
_set_hook.restype = wt.HANDLE
_set_hook.argtypes = [ctypes.c_int, _HOOKPROC, wt.HINSTANCE, wt.DWORD]
_unhook = _user32.UnhookWindowsHookEx
_unhook.argtypes = [wt.HANDLE]
_unhook.restype = wt.BOOL
_next_hook = _user32.CallNextHookEx
_next_hook.restype = ctypes.c_ssize_t
_next_hook.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
_get_module = _kernel32.GetModuleHandleW
_get_module.restype = wt.HMODULE
_get_module.argtypes = [wt.LPCWSTR]
_get_tid = _kernel32.GetCurrentThreadId
_get_tid.restype = wt.DWORD
_post_thread = _user32.PostThreadMessageW
_post_thread.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_post_thread.restype = wt.BOOL
_get_message = _user32.GetMessageW
_get_message.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_get_message.restype = ctypes.c_int
_translate = _user32.TranslateMessage
_translate.argtypes = [ctypes.POINTER(wt.MSG)]
_dispatch = _user32.DispatchMessageW
_dispatch.argtypes = [ctypes.POINTER(wt.MSG)]
_dispatch.restype = ctypes.c_ssize_t


class MouseHook:
    """全局低级鼠标钩子（WH_MOUSE_LL），只在隐藏模式开启期间安装。

    钩子运行在独立线程的消息循环里；回调应尽量轻量，只做矩形判断并投递事件。
    鼠标静止时没有消息，CPU 占用接近 0；stop() 会卸载钩子并结束线程。
    """

    def __init__(self, on_move) -> None:
        self._on_move = on_move
        self._proc = None
        self._thread = None
        self._tid = 0
        self._ready = threading.Event()
        self._ok = False
        self._err = 0

    @property
    def ok(self) -> bool:
        return self._ok

    @property
    def error(self) -> int:
        return self._err

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return self._ok
        self._thread = threading.Thread(target=self._run, name="mouse-hook", daemon=True)
        self._thread.start()
        self._ready.wait(1.0)
        return self._ok

    def stop(self) -> None:
        if not self._thread:
            return
        try:
            if self._tid:
                _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        except Exception:
            pass
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass
        self._thread = None
        self._ok = False

    def _run(self) -> None:
        self._tid = int(_get_tid())
        self._proc = _HOOKPROC(self._callback)
        try:
            handle = _set_hook(WH_MOUSE_LL, self._proc, _get_module(None), 0)
        except Exception:
            handle = None
        if not handle:
            self._err = int(_kernel32.GetLastError())
            self._ok = False
            self._ready.set()
            return
        self._ok = True
        self._ready.set()
        msg = wt.MSG()
        try:
            while True:
                ret = _get_message(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                _translate(ctypes.byref(msg))
                _dispatch(ctypes.byref(msg))
        except Exception:
            pass
        try:
            _unhook(handle)
        except Exception:
            pass

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and w_param == WM_MOUSEMOVE:
                info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                self._on_move(int(info.pt.x), int(info.pt.y))
        except Exception:
            pass
        return _next_hook(None, n_code, w_param, l_param)


_busy_cache = (0.0, False)


def desktop_busy() -> bool:
    """全屏应用或锁屏时返回 True，隐藏模式的鼠标钩子应忽略这次移动。

    判定：前台窗口矩形几乎铺满所在显示器，且不是桌面/任务栏本身；锁屏时
    没有前台窗口。结果缓存 0.5 秒，避免每次鼠标移动都枚举窗口。
    """
    global _busy_cache
    now = time.time()
    if now - _busy_cache[0] < 0.5:
        return _busy_cache[1]
    busy = True
    try:
        hwnd = _user32.GetForegroundWindow()
        if hwnd:
            rect = window_rect(hwnd)
            if rect:
                mon = monitor_from_point((rect[0] + rect[2]) // 2,
                                         (rect[1] + rect[3]) // 2)
                if mon:
                    m = mon["rect"]
                    area = max(1, (m[2] - m[0]) * (m[3] - m[1]))
                    covered = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                    # 最大化窗口只占工作区（高度少一条任务栏），约 96%；真正的
                    # 全屏/无边框全屏才会铺满整个显示器，阈值取 98% 可区分两者。
                    # 任务栏自动隐藏时最大化窗口也能铺满，但带标题栏样式，不算全屏。
                    style = int(_user32.GetWindowLongW(hwnd, -16) or 0)
                    if covered < area * 0.98 or (style & 0x00C00000):
                        busy = False
                    else:
                        buf = ctypes.create_unicode_buffer(256)
                        _user32.GetClassNameW(hwnd, buf, 256)
                        busy = buf.value not in (
                            "Progman", "WorkerW", "Shell_TrayWnd",
                            "Shell_SecondaryTrayWnd",
                        )
                else:
                    busy = False
            else:
                busy = False
        else:
            busy = True          # 锁屏/切换用户时没有前台窗口
    except Exception:
        busy = False
    _busy_cache = (now, busy)
    return busy


_MUTEX = None

def single_instance_lock(name: str = "TaskbarStockWidget") -> bool:
    """返回 True 表示当前是唯一实例；False 表示已有实例在运行。"""
    global _MUTEX
    try:
        _MUTEX = _kernel32.CreateMutexW(None, False, name)
        return _kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def cursor_pos() -> Tuple[int, int]:
    point = wt.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]


def premultiplied_bgra(image: Image.Image) -> bytes:
    """RGBA → 预乘 alpha 的 BGRA 字节。

    UpdateLayeredWindow 在 AC_SRC_ALPHA 下按“预乘 alpha”合成；直接提交直通
    RGBA 会让抗锯齿边缘的 RGB 保持满强度，在 Win11 的 DWM 上表现为彩色描边/
    光晕。这里先把每个通道乘上 alpha 再转字节序。
    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    r, g, b, a = image.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (r, g, b, a)).tobytes("raw", "BGRA")


_WIN32_READY = False


def _declare_win32() -> None:
    """显式声明 GDI/User32 的句柄参数类型。

    64 位下 HDC/HBITMAP 的高位常为 0xFFFFFFFF（如 0xFFFFFFFFC4F7xxxx），
    ctypes 默认按 c_int 转换会抛 OverflowError；一旦在提交位图时抛出，窗口就
    会停留在 Tk 的黑色底上（看起来像整个组件消失/变黑）。
    """
    global _WIN32_READY
    if _WIN32_READY:
        return
    gdi = ctypes.windll.gdi32
    gdi.CreateCompatibleDC.restype = wt.HDC
    gdi.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi.CreateDIBSection.restype = wt.HBITMAP
    gdi.CreateDIBSection.argtypes = [
        wt.HDC, ctypes.POINTER(_BITMAPINFO), wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD,
    ]
    gdi.SelectObject.restype = wt.HGDIOBJ
    gdi.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi.DeleteObject.argtypes = [wt.HGDIOBJ]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
    _user32.GetDC.restype = wt.HDC
    _user32.GetDC.argtypes = [wt.HWND]
    _user32.UpdateLayeredWindow.restype = wt.BOOL
    _user32.UpdateLayeredWindow.argtypes = [
        wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
        wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION), wt.DWORD,
    ]
    _WIN32_READY = True


_declare_win32()


class LayeredSurface:
    """用 UpdateLayeredWindow 提交 32 位 RGBA 位图：真彩色 + 逐像素透明。"""

    def __init__(self, hwnd: int) -> None:
        _declare_win32()
        self.hwnd = int(hwnd)
        self.ok = False
        self.premultiply = True   # 关闭仅供花屏自检对比，正式运行必须为 True
        self._screen_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._old_bitmap = None
        self._bits = None
        self._size = (0, 0)
        self._warned = False
        try:
            style = _user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            self.ok = self._open_dc()
        except Exception:
            self.ok = False

    def _open_dc(self) -> bool:
        """取屏幕 DC 与内存 DC；失败时返回 False，由下一次 update 继续重试。

        锁屏/切换会话时 GetDC(0) 可能返回 0，若只在构造时取一次，窗口会一直是
        Tk 的黑色底（看起来像“整个组件消失”）。
        """
        gdi = ctypes.windll.gdi32
        try:
            if not self._screen_dc:
                self._screen_dc = _user32.GetDC(None)
            if not self._mem_dc:
                self._mem_dc = gdi.CreateCompatibleDC(self._screen_dc)
            self.ok = bool(self._mem_dc)
        except Exception:
            self.ok = False
        return self.ok

    def _ensure(self, width: int, height: int) -> None:
        if self._bitmap and self._size == (width, height):
            return
        gdi = ctypes.windll.gdi32
        if self._bitmap:
            try:
                gdi.SelectObject(self._mem_dc, self._old_bitmap)
                gdi.DeleteObject(self._bitmap)
            except Exception:
                pass
            self._bitmap = None
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 负数 = 自上而下，与 Pillow 行序一致
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        self._bitmap = gdi.CreateDIBSection(
            self._screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
        )
        if not self._bitmap:
            raise OSError("CreateDIBSection failed")
        self._old_bitmap = gdi.SelectObject(self._mem_dc, self._bitmap)
        self._bits = bits
        self._size = (width, height)

    def update(self, image, x: int, y: int) -> None:
        """提交一帧 RGBA 图像，同时把窗口移动到 (x, y)。"""
        if not self.ok:
            self._open_dc()
        if not self.ok:
            if not self._warned:
                log("分层窗口初始化失败（GetDC/CreateCompatibleDC），稍后重试")
                self._warned = True
            return
        self._warned = False
        width, height = image.size
        self._ensure(width, height)
        data = premultiplied_bgra(image) if self.premultiply \
            else image.tobytes("raw", "BGRA")
        ctypes.memmove(self._bits, data, len(data))
        # 直接写 DIB 位后刷新 GDI 批次，避免 ULW 读到缓存的旧内容（偶发横条/残影）
        ctypes.windll.gdi32.GdiFlush()
        blend = _BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
        ok = bool(_user32.UpdateLayeredWindow(
            self.hwnd,
            self._screen_dc,
            ctypes.byref(wt.POINT(int(x), int(y))),
            ctypes.byref(wt.SIZE(width, height)),
            self._mem_dc,
            ctypes.byref(wt.POINT(0, 0)),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        ))
        if not ok:
            err = _kernel32.GetLastError()
            try:
                rect = wt.RECT()
                _user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
                log(f"UpdateLayeredWindow 失败 hwnd={self.hwnd} "
                    f"bitmap={width}x{height} rect=({rect.left},{rect.top},"
                    f"{rect.right},{rect.bottom}) err={err}")
            except Exception:
                log(f"UpdateLayeredWindow 失败 err={err}")
