"""任务栏悬浮小组件窗口（无边框、置顶、逐像素 alpha 的分层窗口）。"""
from __future__ import annotations

import json
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from typing import List, Optional

from .config import RUNTIME_PATH
from .market import GOLD_MANUAL_PREFIX, GOLD_NOTE
from .platform_win import (
    LayeredSurface,
    MouseHook,
    apply_no_activate,
    apply_tool_window,
    cursor_pos,
    desktop_busy,
    hwnd_of_toplevel,
    monitor_from_point,
    monitors,
    raise_topmost,
    screen_luminance,
    taskbar_layout,
    virtual_screen,
    window_rect,
    work_area,
)
from .ui_common import FONT_FAMILY, SCALE, palette, px
from .ui_render import Frame, draw_arrow, draw_spark, text_width
from .trading import calendar_info, status_for
from .util import log

TREND_W, TREND_H = 220, 40
MINI_W, MINI_H = 104, 38
GAP = 8
TRAY_GAP = 6          # 与托盘之间留的间距
EDGE_GAP = 4          # 与任务栏按钮区之间留的间距
BUTTON_SAFE = 44      # Win11 按钮有 XAML 悬停/点击区，ReBar 矩形偏窄，额外让出
MIN_TREND_W = 168     # 走势卡片再窄就放不下曲线与价格
MIN_MINI_W = 78
BREADTH_W = 132       # 涨跌家数卡片理想宽度
MIN_BREADTH_W = 96    # 三列数字放得下的最小宽度
MAX_BREADTH_W = 200

# 100% 基准字号（最终值见 README「字号表」）
NAME_FS = 11.5        # 名称（两种模式共用）
MINI_PCT_FS = 16      # 迷你模式涨跌幅（主信息，加粗）
PRICE_FS = 18         # 走势模式现价（最大）
PCT_FS = 12.5         # 走势模式涨跌幅（次大）
BREADTH_LABEL_FS = 10.5  # 涨/跌/平 标签（单字 CJK 在 8px 以下会糊成色块）
BREADTH_NUM_FS = 11.5   # 家数数字，与名称同级

SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.6, 1.4, 0.1

POPUP_W, POPUP_H = 232, 104
STATUS_FS = 8.5


class WidgetWindow:
    def __init__(self, app) -> None:
        self.app = app
        self.store = app.store
        self.cfg = app.cfg
        self.events: deque = deque()
        self.page = 0
        self._size = (0, 0)
        self._visible: List[str] = []
        self._last_sig = None
        self._drag = None
        self._drag_moved = False
        self._next_rotate = time.time() + max(1, int(self.cfg["display"]["rotate_seconds"]))
        self._tick_id = None
        self._layout = None
        self._mon: Optional[dict] = None
        self._dpi_mul = 1.0
        self._dark = True
        self._pal = palette(True)
        self._dock_ok = False
        self._count_limit = 3
        self.scale = max(0.6, min(1.4, float(self.cfg["window"].get("scale", 0.8))))
        self._card_w = self.s(TREND_W)
        self._breadth_w = self.s(BREADTH_W)
        self._show_breadth = True
        self._ch = self.s(TREND_H)
        self._x = self._y = 0
        self._anchor = (0, 0)
        self._last_geo_check = 0.0
        self._last_theme_check = 0.0
        self._menu = None
        self._toast: Optional[tuple] = None
        self.surface: Optional[LayeredSurface] = None

        # 隐藏模式
        self._hidden = False
        self._hook: Optional[MouseHook] = None
        self._hide_at = 0.0
        self._hook_inside = False
        self._busy_since = 0.0
        self._hidden_reason = ""
        self._hover_zone = None
        self._last_status_key = None
        # 迷你详情浮层
        self._popup = None
        self._popup_sig = None
        self._popup_at = 0.0
        self._popup_leave_at = 0.0
        self._popup_index = 0
        self._last_click_at = 0.0
        self._last_runtime_at = 0.0

        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        # 视觉完全由 UpdateLayeredWindow 提交的 RGBA 位图决定，窗口自身不透明
        self.win.configure(bg="#000000")
        self.win.attributes("-topmost", bool(self.cfg["window"]["always_on_top"]))

        self.canvas = tk.Canvas(
            self.win, bg="#000000", bd=0, highlightthickness=0, cursor="hand2"
        )
        self.canvas.pack(fill="both", expand=True)

        # 分层窗口在 alpha=0 的像素上会被系统判定为点击穿透，另叠一层
        # alpha≈0.4% 的命中层来接收拖动/点击/右键/滚轮，视觉上不可见。
        self.hit = tk.Toplevel(app.root)
        self.hit.withdraw()
        self.hit.overrideredirect(True)
        self.hit.configure(bg="#000000", cursor="hand2")
        self.hit.attributes("-topmost", bool(self.cfg["window"]["always_on_top"]))
        try:
            self.hit.attributes("-alpha", 0.004)
        except Exception:
            pass
        for seq, handler in (
            ("<Button-1>", self._on_press),
            ("<B1-Motion>", self._on_drag),
            ("<ButtonRelease-1>", self._on_release),
            ("<Button-3>", self._on_right_click),
            ("<MouseWheel>", self._on_wheel),
            ("<Enter>", lambda _e: self.events.append("hover_enter")),
            ("<Leave>", lambda _e: self.events.append("hover_leave")),
        ):
            self.hit.bind(seq, handler)

        self._mode_var = tk.StringVar(value=self.cfg["display"]["mode"])
        self._count_var = tk.IntVar(value=self.cfg["display"]["visible_count"])
        self._provider_var = tk.StringVar(value=self.cfg["data"]["provider"])

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    # ------------------------------------------------------------- lifecycle
    def s(self, value: float, minimum: int = 1) -> int:
        """显示器 DPI + 用户整体缩放后的像素值。"""
        return max(minimum, int(round(px(value) * self.scale * self._dpi_mul)))

    def _p(self, value: float) -> int:
        """只按显示器 DPI 换算的物理像素（间距/边距用）。"""
        return max(1, int(round(px(value) * self._dpi_mul)))

    def _fs(self, base: float) -> float:
        return max(7.0, round(base * self.scale * self._dpi_mul, 1))

    # ------------------------------------------------------------ multi-screen
    def _primary_monitor(self, mons: list) -> Optional[dict]:
        return next((m for m in mons if m.get("primary")), mons[0] if mons else None)

    @staticmethod
    def _same_monitor(a: Optional[dict], b: Optional[dict]) -> bool:
        if not a or not b:
            return False
        return (a.get("device") == b.get("device")
                and a.get("rect") == b.get("rect")
                and abs(float(a.get("dpi") or 1.0) - float(b.get("dpi") or 1.0)) < 0.01)

    def _apply_monitor(self, mon: Optional[dict]) -> None:
        self._mon = mon
        dpi = float((mon or {}).get("dpi") or SCALE)
        self._dpi_mul = max(0.5, min(3.0, dpi / max(0.1, SCALE)))

    def _monitor_center(self) -> tuple:
        if not self._mon:
            return cursor_pos()
        r = self._mon["rect"]
        return ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)

    def _pick_saved_monitor(self, mons: list) -> tuple:
        """按配置里的设备名/显示器矩形还原；都没有则主屏。

        返回 (显示器, 是否原显示器已断开)。
        """
        win = self.cfg["window"]
        device = str(win.get("monitor") or "")
        hint = win.get("monitor_rect") or []
        if device:
            for mon in mons:
                if mon.get("device") == device:
                    return mon, False
        if isinstance(hint, (list, tuple)) and len(hint) == 4:
            try:
                rect = [int(v) for v in hint]
            except (TypeError, ValueError):
                rect = []
            for mon in mons:
                if list(mon["rect"]) == rect:
                    return mon, False
        lost = bool(device or hint)
        return self._primary_monitor(mons), lost

    def _forget_monitor(self) -> None:
        """配置的显示器不在了：清掉记忆并回到主屏的安全落点。"""
        win = self.cfg["window"]
        win["monitor"] = ""
        win["monitor_rect"] = []
        win["offset_x"] = 0
        win["offset_y"] = 0
        try:
            self.store.save()
        except Exception:
            pass

    def _monitor_for_start(self) -> None:
        mons = monitors()
        mon, lost = self._pick_saved_monitor(mons)
        self._apply_monitor(mon)
        if lost:
            self._forget_monitor()

    def _follow_monitor(self) -> bool:
        """轮询时确认当前显示器仍在；断开则回主屏。返回屏幕是否发生变化。"""
        mons = monitors()
        device = (self._mon or {}).get("device") or ""
        mon = None
        if device:
            mon = next((m for m in mons if m.get("device") == device), None)
        disconnected = bool(device) and mon is None
        if mon is None:
            mon = self._primary_monitor(mons)
        old = self._mon
        changed = not self._same_monitor(old, mon)
        self._apply_monitor(mon)
        if disconnected:
            self._forget_monitor()
            changed = True
        return changed

    def show(self) -> None:
        self._monitor_for_start()
        self._replan()
        w, h = self._preferred_size()
        ax, ay = self._dock_anchor(w, h)
        self._anchor = (ax, ay)
        self._x = ax + int(self.cfg["window"].get("offset_x") or 0)
        self._y = ay + int(self.cfg["window"].get("offset_y") or 0)
        self._clamp_position(w, h)
        self._size = (w, h)
        self._pick_theme()
        self.win.geometry(f"{w}x{h}+{self._x}+{self._y}")
        self.win.deiconify()
        self.win.update_idletasks()
        self.win.update()
        apply_tool_window(self.win)
        apply_no_activate(self.win)
        apply_tool_window(self.hit)
        apply_no_activate(self.hit)
        self.surface = LayeredSurface(hwnd_of_toplevel(self.win))
        self._sync_hit(w, h)
        if self.cfg["window"]["always_on_top"]:
            raise_topmost(self.win)
            raise_topmost(self.hit)
        self._render_now()
        self._write_runtime()
        self._sync_hidden_mode(startup=True)
        self._tick()

    def destroy(self) -> None:
        self._close_menu()
        self._close_popup()
        self._stop_hook()
        for window in (self.hit, self.win):
            try:
                window.destroy()
            except Exception:
                pass

    def _sync_hit(self, w: int, h: int) -> None:
        """命中层与可见层保持同一矩形；隐藏模式下不显示命中层。"""
        try:
            self.hit.geometry(f"{w}x{h}+{self._x}+{self._y}")
            if self.hit.state() == "withdrawn" and not self._hidden:
                self.hit.deiconify()
        except Exception:
            pass

    def _apply_geometry(self, w: int, h: int) -> None:
        """改尺寸后先让 Tk 真正应用新几何，再提交分层位图。"""
        self.win.geometry(f"{w}x{h}+{self._x}+{self._y}")
        self.canvas.configure(width=w, height=h)
        self._sync_hit(w, h)
        try:
            self.win.update_idletasks()
        except Exception:
            pass

    # ---------------------------------------------------------------- config
    def apply_config(self) -> None:
        self.cfg = self.app.cfg
        display = self.cfg["display"]
        self._mode_var.set(display["mode"])
        self._count_var.set(display["visible_count"])
        self._provider_var.set(self.cfg["data"]["provider"])
        self.win.attributes("-topmost", bool(self.cfg["window"]["always_on_top"]))
        self.page = 0
        self._next_rotate = time.time() + max(1, display["rotate_seconds"])
        self._last_sig = None
        if self._mon is None:
            self._monitor_for_start()
        else:
            self._follow_monitor()   # 显示器还在就留在原屏，不抢回主屏
        self._replan()
        w, h = self._preferred_size()
        ax, ay = self._dock_anchor(w, h)
        self._anchor = (ax, ay)
        self._x = ax + int(self.cfg["window"].get("offset_x") or 0)
        self._y = ay + int(self.cfg["window"].get("offset_y") or 0)
        self._clamp_position(w, h)
        self._size = (w, h)
        self._apply_geometry(w, h)
        self._pick_theme()
        self._close_popup()
        self._render_now()
        self._write_runtime()
        self._sync_hidden_mode()

    # ----------------------------------------------------------------- layout
    def _band(self) -> Optional[tuple]:
        """托盘左侧的可用条带 (left, right, taskbar_rect)，不可用则 None。"""
        layout = self._layout
        if not layout or layout.get("autohide"):
            return None
        if layout.get("orientation") not in ("bottom", "top"):
            return None
        tb = layout["rect"]
        tray = layout.get("tray")
        right = (tray[0] if tray else tb[2]) - self._p(TRAY_GAP)
        left = layout["occupied_right"] + self._p(EDGE_GAP + BUTTON_SAFE)
        if right - left < self._p(120):
            return None
        return left, right, tb

    def _replan(self) -> None:
        """按缩放值、显示模式与任务栏可用空间决定尺寸。

        迷你模式只显示「名称 + 涨跌幅」，涨跌家数卡片完全不参与布局，
        腾出来的宽度还给窗口；走势模式才会挂上涨跌家数。
        """
        if self._mon is None:
            self._apply_monitor(self._primary_monitor(monitors()))
        self._layout = taskbar_layout(point=self._monitor_center())
        band = self._band()
        cfg = self.cfg
        mode = cfg["display"]["mode"]
        self.scale = max(0.6, min(1.4, float(cfg["window"].get("scale", 0.8))))
        gap = self.s(GAP, 2)
        total = len(self.store.cfg["watchlist"])
        rotate = bool(cfg["display"].get("rotate_enabled", True))
        wanted = max(1, min(int(cfg["display"]["visible_count"]), total, 3)) if total else 0
        if not rotate and wanted:
            wanted = 1
        show_b = bool(cfg["display"].get("show_breadth", True)) and mode == "trend"
        self._show_breadth = show_b
        base_h = MINI_H if mode == "mini" else TREND_H

        if band:
            left, right, tb = band
            self._dock_ok = True
            self._ch = min(tb[3] - tb[1], self.s(base_h, 22))
            avail = right - left
            max_width = int(cfg["window"].get("max_width") or 0)
            if max_width > 0:
                avail = min(avail, self._p(max_width))
            base_w = self.s(MINI_W if mode == "mini" else TREND_W)
            if mode == "mini":
                base_w += self._mini_status_extra()
            min_w = self.s(MIN_MINI_W if mode == "mini" else MIN_TREND_W)
            base_b = self.s(BREADTH_W)
            min_b = self.s(MIN_BREADTH_W)
            if total == 0:
                self._count_limit = 0
                self._card_w = 0
                self._breadth_w = min(base_b, avail) if show_b else 0
                if not show_b or self._breadth_w < min_b:
                    self._show_breadth = False
                    self._breadth_w = 0
                return
            if avail < min_w:
                self._dock_ok = False
            else:
                count = wanted
                card_w = base_w
                breadth_w = base_b if show_b else 0
                while True:
                    if show_b:
                        need = count * base_w + base_b + count * gap
                        if need <= avail:
                            card_w, breadth_w = base_w, base_b
                            break
                        candidate = (avail - base_b - count * gap) // count
                        if candidate >= min_w:
                            card_w, breadth_w = candidate, base_b
                            break
                    else:
                        need = count * base_w + (count - 1) * gap
                        if need <= avail:
                            card_w = base_w
                            break
                        candidate = (avail - (count - 1) * gap) // count
                        if candidate >= min_w:
                            card_w = candidate
                            break
                    if count > 1:
                        count -= 1
                        continue
                    # 只剩 1 只：优先保住股票卡，剩多少给涨跌卡
                    if show_b and avail >= base_w + gap + min_b:
                        card_w = base_w
                        breadth_w = max(min_b, min(avail - card_w - gap,
                                                    self.s(MAX_BREADTH_W)))
                    elif show_b and avail >= min_w + gap + min_b:
                        card_w = avail - gap - min_b
                        breadth_w = min_b
                    elif show_b:
                        # 空间连"股票 + 最小涨跌卡"都放不下 → 隐藏涨跌卡
                        self._show_breadth = False
                        show_b = False
                        breadth_w = 0
                        card_w = max(min_w, avail)
                    else:
                        card_w = max(min_w, avail)
                    break
                self._count_limit = count
                self._card_w = card_w
                self._breadth_w = breadth_w
                return

        # 降级：侧边/顶部任务栏、自动隐藏、空间过窄 → 浮在工作区右下角
        self._dock_ok = False
        self._count_limit = wanted
        self._card_w = self.s(MINI_W if mode == "mini" else TREND_W)
        if mode == "mini":
            self._card_w += self._mini_status_extra()
        self._breadth_w = self.s(BREADTH_W) if show_b else 0
        self._ch = self.s(base_h, 22)

    def _card_size(self) -> tuple:
        return self._card_w, self._ch

    def _preferred_size(self, count: Optional[int] = None) -> tuple:
        cw, ch = self._card_size()
        gap = self.s(GAP, 2)
        count = self._visible_count() if count is None else count
        count = max(0, count)
        if count == 0:
            if self._show_breadth and self._breadth_w > 0:
                return self._breadth_w, max(self.s(36), ch)
            return self.s(200), max(self.s(36), ch)
        width = count * cw + (count - 1) * gap
        if self._show_breadth and self._breadth_w > 0:
            width += gap + self._breadth_w
        return width, ch

    def _visible_count(self) -> int:
        total = len(self.store.cfg["watchlist"])
        if total == 0:
            return 0
        if not bool(self.cfg["display"].get("rotate_enabled", True)):
            return 1
        return max(1, min(int(self.cfg["display"]["visible_count"]), total, 3, self._count_limit))

    def _pages(self) -> int:
        count = self._visible_count()
        if not count:
            return 1
        if not bool(self.cfg["display"].get("rotate_enabled", True)):
            return 1
        total = len(self.store.cfg["watchlist"])
        return max(1, (total + count - 1) // count)

    def _compute_visible(self) -> List[str]:
        symbols = self.store.symbols()
        count = self._visible_count()
        if not count:
            return []
        pages = self._pages()
        self.page %= pages
        start = self.page * count
        return symbols[start:start + count]

    def _dock_anchor(self, w: int, h: int) -> tuple:
        """默认落点：任务栏内、托盘左侧；不可贴靠时退回本屏工作区右下角。"""
        band = self._band()
        if self._dock_ok and band:
            _left, right, tb = band
            x = right - w
            y = tb[1] + (tb[3] - tb[1] - h) // 2
            return x, y
        if self._mon:
            left, top, right, bottom = self._mon["work"]
        else:
            left, top, right, bottom = work_area()
        return right - w - self._p(18), bottom - h - self._p(6)

    def _reposition(self, w: int, h: int) -> None:
        """按当前偏移重新贴靠（换页/缩放导致尺寸变化时右边缘仍贴住托盘）。"""
        ax, ay = self._dock_anchor(w, h)
        self._anchor = (ax, ay)
        self._x = ax + int(self.cfg["window"].get("offset_x") or 0)
        self._y = ay + int(self.cfg["window"].get("offset_y") or 0)
        self._clamp_position(w, h)

    def _clamp_position(self, w: int, h: int) -> None:
        band = self._band()
        if self._dock_ok and band:
            left, right, tb = band
            if self._y + h > tb[1] + self._p(4) and self._y < tb[3] - self._p(4):
                self._x = max(left, min(self._x, right - w))
        vx, vy, vr, vb = virtual_screen()
        self._x = max(vx - w + self._p(40), min(self._x, vr - self._p(40)))
        self._y = max(vy, min(self._y, vb - self._p(20)))

    def _clamp_virtual(self, w: int, h: int) -> None:
        """拖动中只限制在整个虚拟桌面内，允许直接跨到副屏，不吸回主屏。"""
        vx, vy, vr, vb = virtual_screen()
        self._x = max(vx, min(self._x, vr - w))
        self._y = max(vy, min(self._y, vb - h))

    def _theme_luminance(self) -> Optional[float]:
        """采样组件旁边的任务栏背景亮度（不能采组件自身，否则会读到文字）。"""
        tb = (self._layout or {}).get("rect")
        w, h = self._size
        points = []
        if tb:
            for dy in (0.25, 0.5, 0.75):
                for dx in (6, 14, 22, 30, 40):
                    points.append((self._x - self._p(dx), int(self._y + h * dy)))
            points.append((tb[0] + self._p(10), (tb[1] + tb[3]) // 2))
            points.append((tb[0] + self._p(74), (tb[1] + tb[3]) // 2))
        values = sorted(v for v in (screen_luminance(*p) for p in points) if v is not None)
        return values[len(values) // 2] if values else None

    def _pick_theme(self, point: Optional[tuple] = None) -> None:
        mode = self.cfg["window"].get("text_theme", "auto")
        if mode in ("dark", "light"):
            dark = mode == "dark"
        else:
            lum = screen_luminance(*point) if point else self._theme_luminance()
            dark = True if lum is None else lum < 0.5
        if dark != self._dark:
            self._last_sig = None
        self._dark = dark
        self._pal = palette(dark)

    # ------------------------------------------------------------ hidden mode
    def _hot_rect(self) -> tuple:
        """隐藏模式热区：每次都由当前窗口真实矩形推导。

        不能用启动时缓存的任务栏锚点：拖动/缩放/换屏后窗口的 _x/_y/_size
        都会变，热区必须跟着窗口走。贴靠任务栏时纵向覆盖整条任务栏高度，
        其余位置按窗口上下各留一点余量。
        """
        w, h = self._size
        pad = self._p(4)
        width = max(w, self._p(float(self.cfg["display"].get("hotspot_width") or 160)))
        band = self._band()
        if self._dock_ok and band:
            tb = band[2]
            if tb[1] <= self._y and self._y + h <= tb[3]:
                return (self._x - (width - w), tb[1], self._x + w + pad, tb[3])
        return (self._x - (width - w), self._y - pad, self._x + w + pad, self._y + h + pad)

    @staticmethod
    def _point_in(rect, x: int, y: int) -> bool:
        return bool(rect) and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def _on_hook_move(self, x: int, y: int) -> None:
        """WH_MOUSE_LL 回调（钩子线程）：只做判断，事件交给 Tk 线程。

        显示/隐藏都用同一个热区做边界，避免 0 延时下光标停在热区边缘
        时“显示→离开→再显示”的闪烁。
        """
        if not bool(self.cfg["display"].get("hidden_mode")):
            return
        if desktop_busy():
            # 全屏/锁屏期间先忽略移动；是否收起由主线程按“持续 busy”判断，
            # 避免全屏判定瞬时抖动把刚显示的组件又藏起来。
            return
        inside = self._point_in(self._hot_rect(), x, y)
        if inside != self._hook_inside:
            self._hook_inside = inside
            self.events.append("hover_enter" if inside else "hover_leave")

    def _sync_hidden_mode(self, startup: bool = False) -> None:
        enabled = bool(self.cfg["display"].get("hidden_mode"))
        was_on = bool(getattr(self, "_hidden_mode_on", False))
        self._hidden_mode_on = enabled
        if not enabled:
            self._stop_hook()
            if self._hidden:
                self._set_hidden(False)
            return
        if self._hook is None:
            self._hook = MouseHook(self._on_hook_move)
            if not self._hook.start():
                log(f"全局鼠标钩子安装失败(error={self._hook.error})，隐藏模式使用轮询兜底")
                self._hook = None
        if startup or not was_on:
            self._set_hidden(True, "startup")

    def _stop_hook(self) -> None:
        hook = self._hook
        self._hook = None
        self._hook_inside = False
        if hook is not None:
            try:
                hook.stop()
            except Exception:
                pass

    def _paint_hidden(self) -> None:
        """隐藏时提交全透明位图：窗口保留但不遮挡、不拦截任何点击。"""
        if not self.surface:
            return
        try:
            w, h = self._size
            self.surface.update(Frame(w, h).image(), self._x, self._y)
        except Exception:
            pass

    def _set_hidden(self, hidden: bool, reason: str = "") -> None:
        if hidden == self._hidden:
            return
        self._hidden = hidden
        self._hidden_reason = reason
        self._hide_at = 0.0
        self._busy_since = 0.0
        if hidden:
            # 重新隐藏后，下一次移动进入热区要能立刻触发显示
            self._hook_inside = False
            self._close_menu()
            self._close_popup()
            try:
                self.hit.withdraw()
            except Exception:
                pass
            self._paint_hidden()
        else:
            self._last_sig = None
            self._render_now()
            self._sync_hit(*self._size)
            now = time.time()
            if now - getattr(self, "_last_show_refresh", 0.0) > 10.0:
                # 重新显示组件时强制请求一次，避免隐藏期间数据过旧
                self._last_show_refresh = now
                try:
                    self.app.service.refresh_now()
                except Exception:
                    pass
            if self.cfg["window"]["always_on_top"]:
                raise_topmost(self.win)
                raise_topmost(self.hit)
        self._write_runtime()

    # ----------------------------------------------------------------- events
    def post(self, event: str) -> None:
        self.events.append(event)

    def _drain_events(self) -> None:
        while self.events:
            event = self.events.popleft()
            if event == "tray_left":
                self.app.open_settings()
            elif event == "tray_right":
                self._show_menu(*cursor_pos())
            elif event == "hover_enter":
                if bool(self.cfg["display"].get("hidden_mode")):
                    self._hide_at = 0.0
                    self._set_hidden(False)
            elif event == "hover_leave":
                if bool(self.cfg["display"].get("hidden_mode")) and not self._hidden:
                    # 详情浮层打开期间保持可见：鼠标移到浮层上不算“离开组件”。
                    if (self._drag is None and self._menu is None
                            and self._popup is None):
                        delay = self._hide_delay()
                        if delay <= 0:
                            self._set_hidden(True, "leave")
                        else:
                            self._hide_at = time.time() + delay

    def _hide_delay(self) -> float:
        try:
            return max(0.0, float(self.cfg["display"].get("hidden_delay") or 0))
        except (TypeError, ValueError):
            return 0.0

    def _on_press(self, event) -> None:
        self._close_menu()
        self._drag = (*cursor_pos(), self._x, self._y)
        self._drag_moved = False

    def _on_drag(self, event) -> None:
        if not self._drag:
            return
        sx, sy, wx, wy = self._drag
        dx, dy = cursor_pos()[0] - sx, cursor_pos()[1] - sy
        if abs(dx) > self._p(4) or abs(dy) > self._p(4):
            self._drag_moved = True
        if self._drag_moved:
            self._x, self._y = wx + dx, wy + dy
            self._clamp_virtual(*self._size)
            self.win.geometry(f"+{self._x}+{self._y}")
            try:
                self.hit.geometry(f"+{self._x}+{self._y}")
            except Exception:
                pass

    def _on_release(self, event) -> None:
        moved = self._drag_moved
        self._drag = None
        if moved:
            self._adopt_drop_monitor()
            self._save_position()
            return
        if self.cfg["display"]["mode"] == "mini":
            # 迷你模式：单击展开详情，双击进入设置
            now = time.time()
            if now - self._last_click_at < 0.35:
                self._last_click_at = 0.0
                self._close_popup()
                self.app.open_settings()
            else:
                self._last_click_at = now
                self._show_popup(self._mini_click_index(event.x))
            return
        self.app.open_settings()

    def _adopt_drop_monitor(self) -> None:
        """松手时按组件中心所在屏幕重新选任务栏，并按该屏 DPI 重排尺寸。"""
        w, h = self._size
        center = (self._x + w // 2, self._y + h // 2)
        mon = monitor_from_point(*center)
        if mon is None:
            mon = self._mon
        changed = not self._same_monitor(self._mon, mon)
        self._apply_monitor(mon)
        if changed:
            self._replan()
            w, h = self._preferred_size()
            self._size = (w, h)
            self._last_sig = None
        self._clamp_position(w, h)
        self._apply_geometry(w, h)
        self._pick_theme()
        # 拖动期间画面是冻结的，松手必须强制提交一帧（签名没变也要提交），
        # 保证窗口位置、尺寸与位图在 ULW 里一次性对齐。
        self._last_sig = None
        self._render_now()
        self._write_runtime()

    def _on_right_click(self, event) -> None:
        self._close_menu()
        self._show_menu(event.x_root, event.y_root)

    def _on_wheel(self, event) -> None:
        # Ctrl 或 Shift + 滚轮 = 整体缩放；普通滚轮 = 翻页
        if event.state & 0x0005:
            self._zoom(SCALE_STEP if event.delta > 0 else -SCALE_STEP)
            return
        if not bool(self.cfg["display"].get("rotate_enabled", True)):
            self._set_toast("轮播已关闭")
            self._last_sig = None
            self._render_now()
            return
        if self._pages() <= 1:
            return
        self.page = (self.page + (1 if event.delta < 0 else -1)) % self._pages()
        self._next_rotate = time.time() + max(1, self.cfg["display"]["rotate_seconds"])
        self._last_sig = None
        self._render_now()

    def _set_toast(self, text: str, seconds: float = 1.6) -> None:
        self._toast = (text, time.time() + seconds)

    def _toast_text(self) -> Optional[str]:
        if not self._toast:
            return None
        text, until = self._toast
        if time.time() >= until:
            self._toast = None
            return None
        return text

    def _zoom(self, delta: float) -> None:
        """Ctrl/Shift+滚轮缩放：实时预览、立即落盘，触顶时提示限制。"""
        old = self.scale
        new = round(max(SCALE_MIN, min(SCALE_MAX, old + delta)), 2)
        if new == old:
            limit = "上限" if old >= SCALE_MAX else "下限"
            self._set_toast(f"已达{limit} {int(old * 100)}%")
            self._last_sig = None
            self._render_now()
            return
        self.scale = new
        self.cfg["window"]["scale"] = new
        self.store.save()
        self._set_toast(f"{int(new * 100)}%")
        self._last_sig = None
        self._replan()
        w, h = self._preferred_size()
        self._reposition(w, h)
        self._size = (w, h)
        self._apply_geometry(w, h)
        self._pick_theme()
        self._render_now()
        self._write_runtime()
        self._sync_settings_scale()

    def _sync_settings_scale(self) -> None:
        settings = getattr(self.app, "settings", None)
        if settings is None:
            return
        try:
            settings.scale_var.set(f"{int(round(self.scale * 100))}%")
        except Exception:
            pass

    def _save_position(self) -> None:
        """拖动结束后保存「显示器标识 + 相对该屏托盘默认落点的偏移」。"""
        ax, ay = self._dock_anchor(*self._size)
        self.cfg["window"]["offset_x"] = int(self._x - ax)
        self.cfg["window"]["offset_y"] = int(self._y - ay)
        mon = self._mon or {}
        self.cfg["window"]["monitor"] = str(mon.get("device") or "")
        self.cfg["window"]["monitor_rect"] = list(mon.get("rect") or [])
        self.store.save()
        self._write_runtime()

    def reset_position(self) -> None:
        self.cfg["window"]["offset_x"] = 0
        self.cfg["window"]["offset_y"] = 0
        self.cfg["window"]["monitor"] = ""
        self.cfg["window"]["monitor_rect"] = []
        self.store.save()
        self._apply_monitor(self._primary_monitor(monitors()))
        self._replan()
        w, h = self._preferred_size()
        ax, ay = self._dock_anchor(w, h)
        self._anchor = (ax, ay)
        self._x, self._y = ax, ay
        self._size = (w, h)
        self._apply_geometry(w, h)
        self._write_runtime()
        self._last_sig = None
        self._pick_theme()
        self._render_now()

    # ------------------------------------------------------------------ menu
    def _close_menu(self) -> None:
        menu = getattr(self, "_menu", None)
        self._menu = None
        if menu is not None:
            try:
                menu.grab_release()
            except Exception:
                pass
            try:
                menu.destroy()
            except Exception:
                pass

    def _show_menu(self, x: int, y: int) -> None:
        """自绘右键菜单（原生菜单在 NOACTIVATE 窗口里不会自动收起）。"""
        self._close_menu()
        bg, hover = "#20232b", "#343a47"
        fg, dim, accent = "#f1f4fa", "#9aa1b0", "#79a6ff"

        menu = tk.Toplevel(self.app.root)
        self._menu = menu
        menu.withdraw()
        menu.overrideredirect(True)
        menu.configure(bg=bg)
        try:
            menu.attributes("-topmost", True)
        except Exception:
            pass
        outer = tk.Frame(menu, bg=bg, padx=px(4), pady=px(4),
                         highlightthickness=1, highlightbackground="#3a4152")
        outer.pack(fill="both", expand=True)

        def add_row(label, command, color=None, bold=False):
            holder = tk.Frame(outer, bg=bg, cursor="hand2")
            holder.pack(fill="x", pady=px(1))
            lab = tk.Label(holder, text=label, bg=bg, fg=color or fg,
                           font=(FONT_FAMILY, px(11), "bold" if bold else "normal"),
                           anchor="w", padx=px(10), pady=px(4))
            lab.pack(fill="x")

            def enter(_e):
                holder.configure(bg=hover)
                lab.configure(bg=hover)

            def leave(_e):
                holder.configure(bg=bg)
                lab.configure(bg=bg)

            def click(_e):
                self._close_menu()
                command()
                return "break"

            for widget in (holder, lab):
                widget.bind("<Enter>", enter)
                widget.bind("<Leave>", leave)
                widget.bind("<Button-1>", click)

        def add_sep():
            tk.Frame(outer, bg="#3a4152", height=1).pack(fill="x", padx=px(8), pady=px(4))

        def add_group(title, options, current, on_pick):
            tk.Label(outer, text=title, bg=bg, fg=dim,
                     font=(FONT_FAMILY, px(10)), anchor="w",
                     padx=px(10), pady=px(2)).pack(fill="x")
            line = tk.Frame(outer, bg=bg)
            line.pack(fill="x", padx=px(10), pady=px(2))
            for index, (value, lab_text) in enumerate(options):
                active = value == current
                chip = tk.Label(
                    line, text=lab_text,
                    bg="#2b3140" if active else bg,
                    fg=accent if active else fg,
                    font=(FONT_FAMILY, px(10)),
                    padx=px(8), pady=px(3), cursor="hand2",
                )
                chip.pack(side="left", padx=(0 if index == 0 else px(4), 0))

                def pick(_e, v=value):
                    self._close_menu()
                    on_pick(v)
                    return "break"

                chip.bind("<Button-1>", pick)

        add_row("打开设置…", self.app.open_settings)
        add_sep()
        add_group(
            "显示模式",
            (("trend", "走势"), ("mini", "迷你")),
            self.cfg["display"]["mode"],
            lambda v: self.app.set_display(mode=v),
        )
        add_group(
            "同时显示",
            tuple((n, str(n)) for n in (1, 2, 3)),
            int(self.cfg["display"]["visible_count"]),
            lambda v: self.app.set_display(visible_count=v),
        )
        add_group(
            "涨跌家数",
            (("on", "显示"), ("off", "隐藏")),
            "on" if self.cfg["display"].get("show_breadth", True) else "off",
            lambda v: self.app.set_display(show_breadth=(v == "on")),
        )
        add_group(
            "轮播",
            (("on", "开启"), ("off", "关闭")),
            "on" if bool(self.cfg["display"].get("rotate_enabled", True)) else "off",
            lambda v: self.app.set_display(rotate_enabled=(v == "on")),
        )
        add_group(
            "隐藏模式",
            (("on", "开启"), ("off", "关闭")),
            "on" if bool(self.cfg["display"].get("hidden_mode", False)) else "off",
            lambda v: self.app.set_display(hidden_mode=(v == "on")),
        )
        add_group(
            "数据源",
            (("auto", "自动"), ("tencent", "腾讯"), ("sina", "新浪")),
            self.cfg["data"]["provider"],
            lambda v: self.app.set_provider(v),
        )
        add_sep()
        add_row("立即刷新", self.app.service.refresh_now)
        add_row("打开配置文件", self.app.open_config_file)
        add_sep()
        add_row("退出小组件", self.app.quit_app, color="#ff8a9a")

        menu.update_idletasks()
        mw, mh = menu.winfo_reqwidth(), menu.winfo_reqheight()
        vx, vy, vr, vb = virtual_screen()
        mx = min(max(vx + px(8), x - mw // 2), max(vx + px(8), vr - mw - px(8)))
        my = min(max(vy + px(8), y - mh - px(8)), max(vy + px(8), vb - mh - px(8)))
        menu.geometry(f"{mw}x{mh}+{mx}+{my}")
        menu.deiconify()
        menu.update_idletasks()
        apply_tool_window(menu)
        raise_topmost(menu)
        try:
            menu.grab_set()
        except Exception:
            pass
        try:
            menu.focus_force()
        except Exception:
            pass
        menu.bind("<FocusOut>", lambda _e: self._close_menu())
        menu.bind("<Escape>", lambda _e: self._close_menu())
        menu.bind("<Button-1>", lambda _e: self._close_menu())
        menu.bind("<Destroy>", lambda _e: setattr(self, "_menu", None))

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        try:
            self._drain_events()
            self._hover_guard()
            self._maybe_rotate()
            self._ensure_geometry()
            self._render_now()
            self._update_popup()
            if time.time() - self._last_runtime_at > 5.0:
                self._write_runtime()
        except Exception:
            log("小组件刷新异常", exc=True)
        finally:
            interval = 100 if bool(self.cfg["display"].get("hidden_mode")) else 400
            self._tick_id = self.win.after(interval, self._tick)

    def _hover_guard(self) -> None:
        if not bool(self.cfg["display"].get("hidden_mode")):
            return
        hook_ok = self._hook is not None and self._hook.ok
        busy = desktop_busy()
        if self._hidden:
            # 全屏应用/锁屏期间不因光标停在热区而弹出
            if busy:
                return
            if not hook_ok and self._point_in(self._hot_rect(), *cursor_pos()):
                self._set_hidden(False)
            return
        if busy:
            # 持续 0.8s 仍处于全屏/锁屏才收起，避开切换窗口瞬间的误判。
            if not self._busy_since:
                self._busy_since = time.time()
            if (time.time() - self._busy_since >= 0.8
                    and self._drag is None and self._menu is None):
                self._set_hidden(True, "busy")
            return
        self._busy_since = 0.0
        if (self._popup is not None or self._drag is not None
                or self._menu is not None):
            self._hide_at = 0.0
            return
        if self._hide_at and time.time() >= self._hide_at:
            self._set_hidden(True, "timeout")
            return
        if self._point_in(self._hot_rect(), *cursor_pos()):
            self._hide_at = 0.0
            return
        # 即使钩子正常也做一次兜底：钩子事件丢失/光标瞬移时仍能及时收起。
        delay = self._hide_delay()
        if delay <= 0:
            self._set_hidden(True, "leave")
        elif not self._hide_at:
            self._hide_at = time.time() + delay

    @staticmethod
    def _layout_key(layout) -> tuple:
        if not layout:
            return ()
        return (layout.get("rect"), layout.get("tray"), layout.get("occupied_right"),
                layout.get("orientation"), layout.get("autohide"),
                layout.get("monitor_device"), layout.get("monitor_rect"),
                round(float(layout.get("dpi") or 0), 2))

    def _ensure_geometry(self) -> None:
        # 拖动中完全冻结：不抢 z-order、不查布局、不重绘，避免与 Tk 的移动竞争。
        if self._drag:
            return
        # Win11 在跨屏/DPI 变化时，DWM/Tk 可能擅自改窗口尺寸，此时旧位图会被
        # 拉伸成“花字/字形错位”。每次 tick 校验真实矩形，不一致就按当前尺寸重画。
        actual = window_rect(hwnd_of_toplevel(self.win))
        if actual:
            size = (actual[2] - actual[0], actual[3] - actual[1])
            if size != self._size:
                self._apply_geometry(*self._size)
                self._last_sig = None
                if self._hidden:
                    self._paint_hidden()
        now = time.time()
        if now - self._last_geo_check < 2.0:
            return
        self._last_geo_check = now
        if self.cfg["window"]["always_on_top"]:
            # 每 tick 抢 z-order 会和拖动/合成打架，2 秒一次足够压住任务栏。
            if not self._hidden:
                raise_topmost(self.win)
                raise_topmost(self.hit)
            if self._menu is not None:
                raise_topmost(self._menu)
        mon_changed = self._follow_monitor()
        fresh = taskbar_layout(point=self._monitor_center())
        if mon_changed or self._layout_key(fresh) != self._layout_key(self._layout):
            self._replan()
            w, h = self._preferred_size()
            ax, ay = self._dock_anchor(w, h)
            self._anchor = (ax, ay)
            self._x = ax + int(self.cfg["window"].get("offset_x") or 0)
            self._y = ay + int(self.cfg["window"].get("offset_y") or 0)
            self._clamp_position(w, h)
            self._size = (w, h)
            self._apply_geometry(w, h)
            self._last_sig = None
            self._pick_theme()
            if self._hidden:
                self._paint_hidden()

        if now - self._last_theme_check > 8.0:
            self._last_theme_check = now
            self._pick_theme()

    def _maybe_rotate(self) -> None:
        if not bool(self.cfg["display"].get("rotate_enabled", True)):
            return
        seconds = int(self.cfg["display"]["rotate_seconds"])
        if seconds <= 0 or self._pages() <= 1:
            return
        now = time.time()
        if now >= self._next_rotate:
            self.page = (self.page + 1) % self._pages()
            self._next_rotate = now + seconds

    def _render_now(self) -> None:
        # 拖动中冻结位图：Tk 只负责移动窗口，不再提交画面，松手后统一提交一次。
        if self._drag is not None or self._hidden:
            return
        quotes, sparks, meta = self.app.service.snapshot()
        visible = self._compute_visible()
        self._visible = visible
        mode = self.cfg["display"]["mode"]
        breadth = meta.get("breadth") if self._show_breadth else None
        toast = self._toast_text()
        now_dt = datetime.now()
        status_keys = tuple(
            (st.get("phase"), st.get("label"), bool(st.get("countdown")))
            for st in (self._status_for(symbol, now_dt) for symbol in visible)
        )
        if mode == "mini" and status_keys != self._last_status_key:
            self._last_status_key = status_keys
            self._replan()
        signature = (
            meta.get("version"),
            tuple(visible),
            self.page,
            mode,
            self.cfg["display"]["color_style"],
            self._size,
            self.scale,
            self._dark,
            self._show_breadth,
            self._breadth_w,
            toast,
        )
        if breadth:
            signature += (breadth.get("up"), breadth.get("down"),
                          breadth.get("flat"), breadth.get("stale"))
        for symbol in visible:
            quote = quotes.get(symbol)
            spark = sparks.get(symbol)
            points = spark.points if spark and spark.points else ()
            status = self._status_for(symbol, now_dt)
            signature += (
                symbol,
                self._name_for(symbol, quote, meta),
                round(quote.price, 3) if quote else None,
                round(quote.change_pct, 3) if quote else None,
                quote.stale if quote else True,
                quote.source if quote else "",
                len(points),
                round(points[-1], 3) if points else None,
                status.get("phase"),
                status.get("label"),
                status.get("countdown"),
            )
        if signature == self._last_sig:
            return
        self._last_sig = signature

        w, h = self._preferred_size()
        if (w, h) != self._size:
            self._size = (w, h)
            self._reposition(w, h)
            # 尺寸变了必须先让窗口真正调整到位，再 UpdateLayeredWindow；
            # 否则新尺寸的位图会提交到旧尺寸窗口，界面会变成一块黑板。
            self._apply_geometry(w, h)
            self._write_runtime()
        if w <= 0 or h <= 0:
            return

        cw, ch = self._card_size()
        gap = self.s(GAP, 2)
        frame = Frame(w, h)
        if not visible and not self._show_breadth:
            frame.text(w / 2, h / 2, "右键 → 设置，添加自选股", self._fs(10.5),
                       self._pal["muted"], anchor="center")
        else:
            for index, symbol in enumerate(visible):
                self._draw_card(frame, index * (cw + gap), 0, cw, ch, symbol,
                                quotes.get(symbol), sparks.get(symbol), meta, mode)
            if self._show_breadth and self._breadth_w > 0:
                x0 = len(visible) * (cw + gap) if visible else 0
                self._draw_breadth(frame, x0, 0, self._breadth_w, ch, breadth)
        if toast:
            frame.text(w - self.s(5), ch * 0.26, toast, self._fs(9.5),
                       self._pal["amber"], bold=True, anchor="e")

        if self.surface:
            self.surface.update(frame.image(), self._x, self._y)

    def _name_for(self, symbol: str, quote, meta) -> str:
        """用户别名优先，其次行情接口中文名，最后本地缓存/代码。"""
        alias = self.store.alias_of(symbol)
        if alias:
            return alias
        name = (quote.name or "").strip() if quote else ""
        if name:
            return name
        return (meta.get("names") or {}).get(symbol) or symbol

    def _status_for(self, symbol: str, now: Optional[datetime] = None) -> dict:
        if self.store.kind_of(symbol) == "gold":
            return {"phase": "gold", "label": "", "countdown": ""}
        return status_for("stock", now)

    def _gold_tag(self, symbol: str, short: bool = False) -> str:
        if self.store.kind_of(symbol) != "gold":
            return ""
        if symbol.startswith(GOLD_MANUAL_PREFIX):
            return "手动"
        return "参考" if short else "参考金价"

    def _mini_status_extra(self) -> int:
        """迷你卡在收盘/午休时给状态标签多留一点宽度（倒计时替代涨跌幅）。"""
        symbols = self._visible or self.store.symbols()[:1]
        if not symbols:
            return 0
        status = self._status_for(symbols[0])
        if status.get("label") and not status.get("countdown"):
            return int(text_width(status["label"], self._fs(STATUS_FS))) + self.s(6)
        return 0

    def _fit(self, text: str, max_w: float, size: float, bold: bool = False) -> str:
        """按真实字体宽度截断，避免长名称撑破卡片。"""
        text = text or ""
        max_w = max(self.s(16), float(max_w))
        if not text or text_width(text, size, bold) <= max_w:
            return text
        for length in range(len(text) - 1, 0, -1):
            candidate = text[:length] + "…"
            if text_width(candidate, size, bold) <= max_w:
                return candidate
        return "…"

    # ------------------------------------------------------------------ draw
    def _dir_color(self, pct: float) -> str:
        p = self._pal
        up = p["red"] if self.cfg["display"]["color_style"] == "cn" else p["green"]
        down = p["green"] if self.cfg["display"]["color_style"] == "cn" else p["red"]
        if pct > 0:
            return up
        if pct < 0:
            return down
        return p["muted"]

    def _draw_card(self, frame: Frame, x0: float, y0: float, cw: int, ch: int,
                   symbol: str, quote, spark, meta, mode: str) -> None:
        name = self._name_for(symbol, quote, meta)
        stale = bool(quote.stale) if quote else True
        pending = quote is None and not meta.get("error")
        error = quote is None and bool(meta.get("error"))
        p = self._pal
        tag = self._gold_tag(symbol, short=(mode == "mini"))
        status = self._status_for(symbol)
        tag_w = 0
        if tag:
            tag_w = text_width(tag, self._fs(STATUS_FS)) + self.s(6)
            frame.text(x0 + cw - self.s(10), y0 + ch * 0.26, tag,
                       self._fs(STATUS_FS), p["muted"], anchor="e")

        if stale and not pending:
            frame.dot(x0 + cw - self.s(7) - tag_w, y0 + ch * 0.28,
                      max(1.6, self.s(1.8)), p["amber"])

        if mode == "mini":
            self._draw_mini(frame, x0, y0, cw, ch, name, quote, pending, error,
                            status, tag_w)
        else:
            self._draw_trend(frame, x0, y0, cw, ch, name, quote, spark, pending,
                             error, status)

    def _price_text(self, quote) -> str:
        if not quote:
            return "—"
        if quote.price >= 10000:
            return f"{quote.price:,.0f}"
        return f"{quote.price:.2f}"

    def _pct_text(self, quote) -> str:
        if not quote:
            return ""
        return f"{quote.change_pct:+.2f}%"

    def _draw_trend(self, frame: Frame, x0, y0, cw, ch, name, quote, spark,
                    pending, error, status) -> None:
        p = self._pal
        color = self._dir_color(quote.change_pct) if quote else p["muted"]
        pad = self.s(10)
        left = x0 + pad
        name_size = self._fs(NAME_FS)
        price_size = self._fs(PRICE_FS)
        pct_size = self._fs(PCT_FS)
        name_y = y0 + ch * 0.26
        # 现价与涨跌幅共用一个基线，字号不同也不会上下错位
        base_y = self._baseline(y0, ch)

        countdown = str((status or {}).get("countdown") or "")
        countdown_text = f"距开盘 {countdown}" if countdown else ""
        countdown_w = (text_width(countdown_text, self._fs(STATUS_FS)) + self.s(8)
                       if countdown_text else 0)
        name = self._fit(name, cw - self.s(46) - countdown_w, name_size)
        frame.text(left, name_y, name, name_size,
                   p["name"] if quote else p["dim"])

        price = "—" if pending or error else self._price_text(quote)
        frame.text(left, base_y, price, price_size, color, bold=True, anchor="ls")

        text = self._pct_text(quote)
        pct_w = 0.0
        pct_x = left
        if text:
            price_w = text_width(price, price_size, True)
            pct_x = left + price_w + self.s(6)
            pct_w = text_width(text, pct_size)
            frame.text(pct_x, base_y, text, pct_size, color, anchor="ls")

        label = str((status or {}).get("label") or "")
        label_w = 0.0
        if label and quote:
            label_size = self._fs(STATUS_FS)
            label_x = pct_x + pct_w + self.s(4)
            frame.text(label_x, base_y, label, label_size, p["muted"], anchor="ls")
            label_w = text_width(label, label_size) + self.s(4)
        if countdown_text and not pending and not error:
            frame.text(x0 + cw - pad, name_y, countdown_text,
                       self._fs(STATUS_FS), p["amber"], anchor="e")

        spark_right = x0 + cw - pad
        spark_left = max(left + text_width(price, price_size, True) + pct_w + label_w
                         + self.s(10), x0 + cw * 0.50)
        if spark_right - spark_left >= self.s(22):
            if spark and len(spark.points) >= 2:
                prev = spark.prev_close or (quote.prev_close if quote else 0)
                draw_spark(frame, (spark_left, y0 + ch * 0.25, spark_right, y0 + ch * 0.76),
                           spark.points, prev, color, p["guide"],
                           width=max(1.0, 1.3 * self.scale))
            else:
                mid = y0 + ch * 0.50
                frame.dashed(spark_left, mid, spark_right, mid, p["guide"],
                             width=max(1.0, self.scale))

        if pending:
            frame.text(x0 + cw - pad, y0 + ch * 0.26, "读取中",
                       self._fs(9), p["dim"], anchor="e")
        elif error:
            frame.text(x0 + cw - pad, y0 + ch * 0.26, "离线",
                       self._fs(9), p["amber"], anchor="e")

    def _baseline(self, y0: float, ch: float) -> float:
        """第二行文字（现价/涨跌幅/家数）的共用基线，底部留 3px 呼吸。"""
        return y0 + ch - max(3, self.s(3))

    def _draw_mini(self, frame: Frame, x0, y0, cw, ch, name, quote,
                   pending, error, status, tag_w: float = 0) -> None:
        """迷你模式：只画「名称 + 涨跌幅」；非交易时段追加状态或开盘倒计时。"""
        p = self._pal
        pad = self.s(8)
        left = x0 + pad
        name_size = self._fs(NAME_FS)
        pct_size = self._fs(MINI_PCT_FS)
        base_y = self._baseline(y0, ch)
        name = self._fit(name, cw - pad * 2 - tag_w, name_size)
        frame.text(left, y0 + ch * 0.26, name, name_size,
                   p["name"] if quote else p["dim"])

        if pending:
            frame.text(left, base_y, "读取中", self._fs(10), p["dim"], anchor="ls")
            return
        if error or not quote:
            frame.text(left, base_y, "离线", self._fs(10), p["amber"], anchor="ls")
            return

        countdown = str((status or {}).get("countdown") or "")
        if countdown:
            frame.text(left, base_y, f"距开盘 {countdown}", self._fs(9.5),
                       p["amber"], bold=True, anchor="ls")
            return

        color = self._dir_color(quote.change_pct)
        if quote.change_pct != 0:
            # 三角与涨跌幅数字的光学中心对齐（数字高约 0.7em）
            draw_arrow(frame, left + self.s(5), base_y - pct_size * 0.34,
                       quote.change_pct > 0, color, max(2.5, 4.4 * self.scale))
        text_x = left + (self.s(14) if quote.change_pct != 0 else 0)
        pct_text = self._pct_text(quote)
        frame.text(text_x, base_y, pct_text, pct_size, color, bold=True, anchor="ls")
        label = str((status or {}).get("label") or "")
        if label:
            label_size = self._fs(STATUS_FS)
            label_x = text_x + text_width(pct_text, pct_size, True) + self.s(3)
            frame.text(label_x, base_y, label, label_size, p["muted"], anchor="ls")

    def _draw_breadth(self, frame: Frame, x0, y0, cw, ch, breadth) -> None:
        """上涨/下跌/平盘家数三列；无数据时显示“—”，失败保留旧值加琥珀点。"""
        p = self._pal
        label_size = self._fs(BREADTH_LABEL_FS)
        num_size = self._fs(BREADTH_NUM_FS)
        up_color = p["red"] if self.cfg["display"]["color_style"] == "cn" else p["green"]
        down_color = p["green"] if self.cfg["display"]["color_style"] == "cn" else p["red"]
        status_label = str(status_for("stock").get("label") or "")
        status_size = self._fs(7.5)
        status_w = (text_width(status_label, status_size) if status_label else 0.0)
        columns = (
            ("涨", up_color, breadth.get("up") if breadth else None),
            ("跌", down_color, breadth.get("down") if breadth else None),
            ("平", p["muted"], breadth.get("flat") if breadth else None),
        )
        col_w = cw / 3.0
        base_y = self._baseline(y0, ch)
        for index, (label, color, value) in enumerate(columns):
            cx = x0 + col_w * (index + 0.5)
            if status_label and index == 2:
                # 给右上角状态标签让出宽度，避免“平已收盘”挤在一起
                cx -= (status_w + self.s(3)) / 2.0
            frame.text(cx, y0 + ch * 0.28, label, max(7.5, label_size),
                       color if value is not None else p["dim"], anchor="center")
            number = "—" if value is None else f"{value}"
            # 三列数字与现价同基线，跨卡片看起来齐平
            frame.text(cx, base_y, number, num_size,
                       color if value is not None else p["dim"],
                       bold=True, anchor="ms")
        if status_label:
            frame.text(x0 + cw - self.s(2), y0 + ch * 0.28, status_label,
                       status_size, p["muted"], anchor="e")
        if breadth and breadth.get("stale"):
            frame.dot(x0 + cw - self.s(5), y0 + ch * 0.78,
                      max(1.6, self.s(1.8)), p["amber"])

    # ---------------------------------------------------------- mini popup
    def _mini_click_index(self, event_x: int) -> int:
        cw, _ch = self._card_size()
        step = max(1, cw + self.s(GAP, 2))
        index = max(0, int(event_x) // step)
        return min(index, max(0, len(self._visible) - 1))

    def _show_popup(self, index: int = 0) -> None:
        if self.cfg["display"]["mode"] != "mini" or not self._visible:
            return
        self._popup_index = max(0, min(index, len(self._visible) - 1))
        self._popup_at = time.time() + 4.0
        self._popup_leave_at = 0.0
        if self._popup is None:
            self._create_popup()
        self._popup_sig = None
        self._update_popup(force=True)
        self._write_runtime()

    def _create_popup(self) -> None:
        self._close_popup()
        win = tk.Toplevel(self.app.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg="#000000")
        win.attributes("-topmost", True)
        hit = tk.Toplevel(self.app.root)
        hit.withdraw()
        hit.overrideredirect(True)
        hit.configure(bg="#000000")
        hit.attributes("-topmost", True)
        try:
            hit.attributes("-alpha", 0.004)
        except Exception:
            pass
        apply_tool_window(win)
        apply_no_activate(win)
        apply_tool_window(hit)
        apply_no_activate(hit)
        hit.bind("<Enter>", lambda _e: self._popup_enter())
        hit.bind("<Leave>", lambda _e: self._popup_leave())
        hit.bind("<Button-1>", lambda _e: self._close_popup())
        surface = LayeredSurface(hwnd_of_toplevel(win))
        self._popup = {"win": win, "hit": hit, "surface": surface}

    def _popup_enter(self) -> None:
        if self._popup:
            self._popup_at = time.time() + 4.0
            self._popup_leave_at = 0.0

    def _popup_leave(self) -> None:
        if self._popup:
            self._popup_leave_at = time.time() + 0.8

    def _close_popup(self) -> None:
        state = self._popup
        self._popup = None
        self._popup_sig = None
        self._popup_leave_at = 0.0
        if not state:
            return
        for key in ("hit", "win"):
            try:
                state[key].destroy()
            except Exception:
                pass
        # 浮层关闭时如果鼠标已不在组件上，补一次隐藏倒计时（隐藏模式下
        # 浮层打开期间故意不隐藏，关闭后需要恢复自动收起）。
        if (bool(self.cfg["display"].get("hidden_mode")) and not self._hidden
                and self._drag is None and self._menu is None
                and not self._point_in(self._hot_rect(), *cursor_pos())):
            delay = self._hide_delay()
            if delay <= 0:
                self._set_hidden(True, "leave")
            else:
                self._hide_at = time.time() + delay
        self._write_runtime()

    def _popup_pos(self, pw: int, ph: int) -> tuple:
        cw, _ch = self._card_size()
        gap = self.s(GAP, 2)
        index = max(0, min(self._popup_index, max(0, len(self._visible) - 1)))
        cx = self._x + index * (cw + gap) + cw // 2
        x = cx - pw // 2
        band = self._band()
        layout = self._layout or {}
        if self._dock_ok and band and layout.get("orientation") == "top":
            y = layout["rect"][3] + self.s(8)
        elif self._dock_ok and band:
            y = layout["rect"][1] - ph - self.s(8)
        else:
            y = self._y - ph - self.s(8)
        if self._mon:
            left, top, right, bottom = self._mon["work"]
        else:
            left, top, right, bottom = work_area()
        x = max(left + self.s(6), min(x, right - pw - self.s(6)))
        if y < top + self.s(4) or y + ph > bottom - self.s(4):
            y = min(max(top + self.s(4), y), bottom - ph - self.s(4))
        return int(x), int(y)

    def _update_popup(self, force: bool = False) -> None:
        if not self._popup:
            return
        if self.cfg["display"]["mode"] != "mini":
            self._close_popup()
            return
        now = time.time()
        if self._popup_leave_at and now >= self._popup_leave_at:
            self._close_popup()
            return
        if now >= self._popup_at:
            self._close_popup()
            return
        visible = self._visible or self._compute_visible()
        if not visible:
            self._close_popup()
            return
        symbol = visible[min(self._popup_index, len(visible) - 1)]
        quotes, sparks, meta = self.app.service.snapshot()
        quote = quotes.get(symbol)
        spark = sparks.get(symbol)
        pw, ph = self.s(POPUP_W), self.s(POPUP_H, 40)
        x, y = self._popup_pos(pw, ph)
        status = self._status_for(symbol)
        points = spark.points if spark and spark.points else ()
        signature = (
            symbol, self._name_for(symbol, quote, meta),
            round(quote.price, 3) if quote else None,
            round(quote.change_pct, 3) if quote else None,
            quote.stale if quote else True, quote.source if quote else "",
            len(points), round(points[-1], 3) if points else None,
            status.get("phase"), status.get("label"), status.get("countdown"),
            self.scale, self._dark, pw, ph,
        )
        state = self._popup
        if signature == self._popup_sig and not force:
            if (state["win"].winfo_x(), state["win"].winfo_y()) != (x, y):
                state["win"].geometry(f"{pw}x{ph}+{x}+{y}")
                state["hit"].geometry(f"{pw}x{ph}+{x}+{y}")
            return
        self._popup_sig = signature

        frame = Frame(pw, ph)
        self._draw_popup(frame, pw, ph, symbol, quote, spark, meta, status)
        try:
            state["win"].geometry(f"{pw}x{ph}+{x}+{y}")
            state["hit"].geometry(f"{pw}x{ph}+{x}+{y}")
            state["win"].update_idletasks()
            state["surface"].update(frame.image(), x, y)
            if state["win"].state() == "withdrawn":
                state["win"].deiconify()
            if state["hit"].state() == "withdrawn":
                state["hit"].deiconify()
            raise_topmost(state["win"])
            raise_topmost(state["hit"])
        except Exception:
            log("迷你详情浮层渲染失败", exc=True)

    def _draw_popup(self, frame: Frame, pw, ph, symbol, quote, spark, meta, status) -> None:
        p = self._pal
        # 浮层悬浮在桌面/窗口之上，必须有实底，否则文字会与下方内容叠在一起。
        radius = self.s(8)
        frame.rounded_rect(0.5, 0.5, pw - 0.5, ph - 0.5,
                           "#3a4152" if self._dark else "#d8dbe3", radius)
        frame.rounded_rect(1.5, 1.5, pw - 1.5, ph - 1.5,
                           "#1b1e26" if self._dark else "#f4f5f8", radius - 1)
        pad = self.s(12)
        name = self._name_for(symbol, quote, meta)
        color = self._dir_color(quote.change_pct) if quote else p["muted"]
        tag = self._gold_tag(symbol)
        unit = "元/克" if self.store.kind_of(symbol) == "gold" else ""
        tag_text = " · ".join(t for t in (tag, unit) if t)
        status_label = str((status or {}).get("label") or "")
        countdown = str((status or {}).get("countdown") or "")

        right_text = tag_text
        if countdown:
            right_text = f"距开盘 {countdown}"
        right_w = text_width(right_text, self._fs(9), True) + self.s(8)
        name_text = self._fit(name, pw - pad * 2 - right_w, self._fs(12))
        frame.text(pad, ph * 0.18, name_text, self._fs(12), p["name"], bold=True)
        if right_text:
            frame.text(pw - pad, ph * 0.18, right_text, self._fs(9),
                       p["amber"] if countdown else p["muted"], anchor="e")

        price = self._price_text(quote)
        price_size = self._fs(20)
        base_y = ph * 0.52
        frame.text(pad, base_y, price, price_size, color, bold=True, anchor="ls")
        pct_text = self._pct_text(quote)
        if pct_text:
            pct_x = pad + text_width(price, price_size, True) + self.s(8)
            frame.text(pct_x, base_y, pct_text, self._fs(11), color, anchor="ls")
            if status_label:
                frame.text(pct_x + text_width(pct_text, self._fs(11)) + self.s(4),
                           base_y, status_label, self._fs(STATUS_FS),
                           p["muted"], anchor="ls")
        if quote and not quote.stale:
            frame.text(pw - pad, base_y, quote.quote_time or "",
                       self._fs(8), p["dim"], anchor="rs")
        elif quote is not None:
            frame.text(pw - pad, base_y, "数据异常", self._fs(8), p["amber"], anchor="rs")

        box = (pad, ph * 0.62, pw - pad, ph - self.s(8))
        if spark and len(spark.points) >= 2:
            prev = spark.prev_close or (quote.prev_close if quote else 0)
            draw_spark(frame, box, spark.points, prev, color, p["guide"],
                       width=max(1.0, 1.2 * self.scale))
        else:
            mid = (box[1] + box[3]) / 2
            frame.dashed(box[0], mid, box[2], mid, p["guide"],
                         width=max(1.0, self.scale))
            if self.store.kind_of(symbol) == "gold":
                frame.text(pw / 2, mid, GOLD_NOTE, self._fs(8), p["dim"],
                           anchor="center")

    # ---------------------------------------------------------------- runtime
    def _write_runtime(self) -> None:
        try:
            self._last_runtime_at = time.time()
            w, h = self._size
            _quotes, _sparks, meta = self.app.service.snapshot()
            payload = {
                "x": int(self._x),
                "y": int(self._y),
                "w": int(w),
                "h": int(h),
                "mode": self.cfg["display"]["mode"],
                "scale": round(self.scale, 2),
                "symbols": self._visible or [],
                "docked": bool(self._dock_ok),
                "dark": bool(self._dark),
                "offset_x": int(self.cfg["window"].get("offset_x") or 0),
                "offset_y": int(self.cfg["window"].get("offset_y") or 0),
                "monitor": (self._mon or {}).get("device") or "",
                "monitor_rect": (self._mon or {}).get("rect"),
                "dpi": round(self._dpi_mul, 2),
                "rotate_enabled": bool(self.cfg["display"].get("rotate_enabled", True)),
                "taskbar": (self._layout or {}).get("rect"),
                "tray": (self._layout or {}).get("tray"),
                "capacity": self._visible_count(),
                "show_breadth": bool(self._show_breadth),
                "names": meta.get("names") or {},
                "breadth": meta.get("breadth"),
                "hidden_mode": bool(self.cfg["display"].get("hidden_mode")),
                "hidden": bool(self._hidden),
                "hidden_reason": getattr(self, "_hidden_reason", ""),
                "desktop_busy": desktop_busy(),
                "mouse": list(cursor_pos()),
                "hotzone": list(self._hot_rect()),
                "popup": ({"symbol": (self._visible[min(self._popup_index,
                                                       len(self._visible) - 1)]
                            if self._visible else ""),
                           "open": True} if self._popup else None),
                "status": {symbol: self._status_for(symbol)
                            for symbol in (self._visible or [])},
                "gold_error": meta.get("gold_error") or "",
                "adaptive": meta.get("adaptive") or {},
                "poll_log": (meta.get("poll_log") or [])[-20:],
                "gold": {
                    symbol: {
                        "price": quote.price,
                        "change_pct": round(quote.change_pct, 3),
                        "time": quote.quote_time,
                        "source": quote.source,
                        "stale": quote.stale,
                    }
                    for symbol, quote in _quotes.items()
                    if str(symbol).startswith("gold:")
                },
                "calendar": calendar_info(),
                "pid": __import__("os").getpid(),
            }
            RUNTIME_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                                    encoding="utf-8")
        except Exception:
            pass
