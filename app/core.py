"""应用装配层：把配置、数据服务、小组件、主窗口、托盘串起来。"""
from __future__ import annotations

import os
import tkinter as tk
from typing import Optional

from .autostart import (
    set_enabled as set_autostart_enabled,
    state as read_autostart_state,
    sync as sync_autostart,
)
from .config import CONFIG_PATH, ICON_PATH, ConfigStore
from .icon import ensure_icon
from .market import MarketService
from .platform_win import work_area
from .tray import TrayIcon
from .trading import CALENDAR
from .ui_common import Fonts, px, pick_font_family
from .ui_settings import SettingsWindow
from .ui_widget import WidgetWindow
from .util import log


class App:
    def __init__(self, root: tk.Tk, store: ConfigStore) -> None:
        self.root = root
        self.store = store
        self.cfg = store.cfg
        self.fonts = Fonts(root, pick_font_family())
        self.service = MarketService(self.cfg)
        self.widget: Optional[WidgetWindow] = None
        self.settings: Optional[SettingsWindow] = None
        self.tray: Optional[TrayIcon] = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._sync_autostart()
        try:
            CALENDAR.ensure_refresh()
        except Exception:
            log("交易日历刷新失败", exc=True)
        self.service.start()
        self.widget = WidgetWindow(self)
        self.widget.show()
        self._start_tray()
        log("小组件已启动")

    def _sync_autostart(self) -> None:
        """以注册表为准同步配置镜像，并修复 exe 被移动后遗留的旧路径。"""
        try:
            before = bool(self.cfg.get("system", {}).get("autostart", False))
            st = sync_autostart(self.cfg)
            if st.get("error"):
                log(f"开机自启动：{st['error']}")
            if bool(self.cfg.get("system", {}).get("autostart", False)) != before:
                self.store.save()
        except Exception:
            log("开机自启动状态同步失败", exc=True)

    def _start_tray(self) -> None:
        accent = self._accent_rgb()
        try:
            icon = ensure_icon(ICON_PATH, accent)
            tray = TrayIcon(str(icon), "任务栏股票小组件 · 右键菜单", self.widget.post)
            ok = tray.start()
            if ok:
                self.tray = tray
            else:
                log("托盘图标创建失败，小组件仍可正常使用")
        except Exception:
            log("托盘图标初始化异常", exc=True)

    def _accent_rgb(self) -> tuple:
        if self.cfg["display"]["color_style"] == "intl":
            return (32, 178, 104)
        return (245, 69, 92)

    # --------------------------------------------------------------- settings
    def open_settings(self) -> None:
        try:
            self.service.refresh_now()
        except Exception:
            pass
        if self.settings is None:
            self.settings = SettingsWindow(self)
        self.settings.show()

    def apply_config(self, save: bool = True) -> None:
        if save:
            self.store.save()
        self.service.apply(self.cfg)
        if self.widget:
            self.widget.apply_config()

    def set_display(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if key in self.cfg["display"]:
                self.cfg["display"][key] = value
        self.apply_config(save=True)

    def set_provider(self, provider: str) -> None:
        self.cfg["data"]["provider"] = provider
        self.apply_config(save=True)

    # ------------------------------------------------------------- autostart
    def autostart_state(self) -> dict:
        """读取当前真实的自启动状态（注册表），供界面显示。"""
        try:
            return read_autostart_state()
        except Exception:
            log("读取开机自启动状态失败", exc=True)
            return {"supported": False, "enabled": False, "error": "状态读取失败"}

    def set_autostart(self, enabled: bool) -> tuple[bool, str]:
        """开关自启动。注册表写入成功后才更新配置；失败时保留原状态。"""
        ok, msg = set_autostart_enabled(enabled)
        if ok:
            self.cfg.setdefault("system", {})["autostart"] = bool(enabled)
            self.store.save()
            log(f"开机自启动：{msg}")
        else:
            log(f"开机自启动设置失败：{msg}")
        return ok, msg

    def reset_position(self) -> None:
        if not self.widget:
            return
        self.widget.reset_position()

    def open_config_file(self) -> None:
        try:
            if not CONFIG_PATH.exists():
                self.store.save()
            os.startfile(str(CONFIG_PATH))
        except Exception:
            log("打开配置文件失败", exc=True)

    def quit_app(self) -> None:
        log("用户退出")
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.service.stop()
        except Exception:
            pass
        try:
            self.store.save()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
