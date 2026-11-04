"""任务栏股票小组件入口。

启动：python main.py    （无控制台可用 pythonw main.py）
参数：--settings 启动时同时打开主窗口
"""
from __future__ import annotations

import sys
import tkinter as tk
import traceback

from app.config import ConfigStore
from app.core import App
from app.platform_win import enable_dpi_awareness, single_instance_lock
from app.util import log


def _install_excepthook(root: tk.Tk) -> None:
    def handler(exc_type, exc_value, exc_tb):
        log("未捕获异常", exc=True)
        try:
            from tkinter import messagebox
            messagebox.showerror(
                "任务栏股票小组件",
                f"程序遇到错误，已写入日志：\n{exc_value}\n\n"
                "可重新启动，或查看 %APPDATA%\\TaskbarStock\\stockbar.log",
            )
        except Exception:
            pass
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = handler

    def callback_handler(exc_type, exc_value, exc_tb):
        # 无控制台（pythonw/exe）时 stderr 为 None，默认处理会让异常继续抛出，
        # 这里改成只写日志，避免单个绘制/轮询回调把界面带崩。
        try:
            log("界面回调异常：" + "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)))
        except Exception:
            pass

    root.report_callback_exception = callback_handler


def _hide_console() -> None:
    """打包为 console 子系统时，立即隐藏控制台窗口（双击运行看不到黑框）。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def main() -> int:
    _hide_console()
    enable_dpi_awareness()
    if not single_instance_lock():
        # 已有实例在运行：静默退出，避免出现两个小组件
        return 0

    store = ConfigStore()
    root = tk.Tk()
    root.withdraw()
    root.title("任务栏股票小组件")
    try:
        # 统一按 1 个点 = 1 像素处理，配合 px() 做 DPI 缩放
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass
    _install_excepthook(root)

    app = App(root, store)
    try:
        app.start()
    except Exception:
        log("启动失败", exc=True)
        try:
            root.destroy()
        except Exception:
            pass
        return 1

    if "--settings" in sys.argv:
        app.open_settings()

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            app.service.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
