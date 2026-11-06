"""开机自启动管理（Windows 当前用户，无需管理员权限）。

实现：在 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 下维护名为
``TaskbarStock`` 的 REG_SZ 值，指向当前 exe（源码运行时为 pythonw + main.py）。
不写启动文件夹、不建计划任务、不申请提权；任务管理器的「启动应用」里也能看到。

状态以**注册表为准**：配置文件里的 ``system.autostart`` 只是它的镜像。这样用户在
任务管理器里手动禁用后，程序下次启动不会擅自把启动项改回来。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 平台
    winreg = None  # type: ignore[assignment]

VALUE_NAME = "TaskbarStock"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\StartupApproved\Run")
STARTUP_FLAG = "--startup"
FROZEN = bool(getattr(sys, "frozen", False))


def _q(path) -> str:
    """给路径加双引号，兼容「Program Files」这类带空格的目录。"""
    return '"' + str(path) + '"'


def launch_target() -> str:
    """实际启动目标：冻结后是 exe，源码运行时是工程根目录的 main.py。"""
    if FROZEN:
        return str(Path(sys.executable).resolve())
    return str(Path(__file__).resolve().parent.parent / "main.py")


def launch_command() -> str:
    """写入注册表的完整命令行。源码运行优先用 pythonw，避免开机弹控制台。"""
    if FROZEN:
        return f"{_q(launch_target())} {STARTUP_FLAG}"
    pythonw = Path(sys.executable).resolve().with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable).resolve()
    return f"{_q(runner)} {_q(launch_target())} {STARTUP_FLAG}"


def is_startup_launch() -> bool:
    """本次进程是否由开机自启动拉起（用于日志）。"""
    return STARTUP_FLAG in sys.argv


def _read_run() -> Tuple[Optional[str], Optional[str]]:
    """返回 (启动命令, 错误信息)；没有该值返回 (None, None)。"""
    if winreg is None:
        return None, "仅 Windows 支持开机自启动"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
    except OSError as exc:
        return None, f"读取启动项失败：{exc}"
    try:
        try:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return None, None
        return str(value), None
    except OSError as exc:
        return None, f"读取启动项失败：{exc}"
    finally:
        winreg.CloseKey(key)


def _disabled_by_task_manager() -> bool:
    """任务管理器「启动应用」禁用后会在 StartupApproved\\Run 留 12 字节标记。

    首字节偶数（02/06）= 启用，奇数（03/07）= 禁用；无此值 = 默认启用。
    """
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                             winreg.KEY_READ)
    except OSError:
        return False
    try:
        data, _kind = winreg.QueryValueEx(key, VALUE_NAME)
        return bool(data) and (int(data[0]) & 1) == 1
    except (OSError, TypeError, ValueError, IndexError):
        return False
    finally:
        winreg.CloseKey(key)


def _clear_approval() -> None:
    """清掉「已禁用」标记，让任务管理器重新按启用显示。"""
    if winreg is None:
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                             winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        try:
            winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)


def state() -> Dict[str, Any]:
    """当前自启动状态；``enabled=False`` 可能是未设置，也可能是被系统禁用。"""
    command, error = _read_run()
    exists = command is not None
    blocked = exists and _disabled_by_task_manager()
    return {
        "supported": winreg is not None,
        "enabled": bool(exists and not blocked and not error),
        "exists": exists,
        "blocked": blocked,
        "command": command,
        "current": command == launch_command(),
        "target": launch_target(),
        "error": error,
    }


def set_enabled(enabled: bool) -> Tuple[bool, str]:
    """开启/关闭自启动，返回 (是否成功, 提示文本)。失败不改动配置。"""
    if winreg is None:
        return False, "仅 Windows 支持开机自启动"
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                 winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                                  launch_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except OSError as exc:
        return False, f"写入启动项失败：{exc}"
    _clear_approval()
    return True, "已开启开机自启动" if enabled else "已关闭开机自启动"


def sync(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """启动时调用：把注册表状态镜像进配置，并修复 exe 被移动后的旧路径。

    只在「已启用且路径已过期」时才回写注册表；被任务管理器禁用或读取失败时
    保持现状，不擅自替用户做决定。
    """
    st = state()
    if st["error"]:
        return st
    if st["exists"] and not st["blocked"] and not st["current"]:
        ok, _msg = set_enabled(True)
        if ok:
            st["command"] = launch_command()
            st["current"] = True
    system = cfg.setdefault("system", {})
    system["autostart"] = bool(st["enabled"])
    return st
