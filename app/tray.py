"""任务栏通知区（托盘）图标，直接用 Win32 API 实现，不依赖第三方库。

托盘图标的点击事件通过 `post` 回调丢回 Tk 线程（Tk 只能在主线程操作）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable, Optional

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

WM_APP = 0x8000
WM_QUIT = 0x0012
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wt.BOOL


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


class TrayIcon:
    def __init__(self, icon_path: str, tooltip: str, post: Callable[[str], None]) -> None:
        self.icon_path = icon_path
        self.tooltip = tooltip[:120]
        self.post = post
        self.hwnd: Optional[int] = None
        self.tid: Optional[int] = None
        self._ready = threading.Event()
        self._ok = False
        self._thread: Optional[threading.Thread] = None
        self._wndproc = None  # 防止被 GC
        self._nid = None

    # ------------------------------------------------------------------ API
    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self._ok

    def stop(self) -> None:
        try:
            if self.tid:
                user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
        except Exception:
            pass

    # -------------------------------------------------------------- internals
    def _run(self) -> None:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc = WNDPROC(self._on_message)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "TaskbarStockTrayWnd"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                # 已注册（极少见）也继续
                pass

            self.hwnd = user32.CreateWindowExW(
                0, "TaskbarStockTrayWnd", "", 0, 0, 0, 0, 0, None, None, hinst, None
            )
            if not self.hwnd:
                self._ready.set()
                return

            icon = None
            try:
                icon = user32.LoadImageW(
                    None, self.icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
                )
            except Exception:
                icon = None
            if not icon:
                icon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_APP + 1
            nid.hIcon = icon
            nid.szTip = self.tooltip
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.cast(ctypes.pointer(nid),
                                                                 ctypes.c_void_p))
            if not ok:
                from .util import log
                log("Shell_NotifyIconW(NIM_ADD) 返回失败")
            self._nid = nid
            self._ok = bool(ok)
            self.tid = kernel32.GetCurrentThreadId()
            self._ready.set()

            msg = wt.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            # 清理
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE,
                                          ctypes.cast(ctypes.pointer(nid), ctypes.c_void_p))
            except Exception:
                pass
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:
                pass
        except Exception:
            from .util import log
            log("托盘线程异常", exc=True)
        finally:
            self._ready.set()

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP + 1:
                if lparam == WM_LBUTTONUP:
                    self.post("tray_left")
                elif lparam == WM_RBUTTONUP:
                    self.post("tray_right")
                elif lparam == WM_LBUTTONDBLCLK:
                    self.post("tray_left")
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
