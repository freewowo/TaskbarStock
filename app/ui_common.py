"""UI 公共部分：配色、字体、圆角与走势图绘制工具。"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Optional, Tuple

from .platform_win import dpi_scale

SCALE = dpi_scale()

# 透明色键（用于 Windows 的 -transparentcolor，做出圆角）
TRANSPARENT_KEY = "#010203"

BG = "#16181d"
BG_HOVER = "#1c1f26"
BORDER = "#2b2f39"
BORDER_SOFT = "#232733"
TEXT = "#e9ebf2"
MUTED = "#8b90a0"
MUTED_DIM = "#666b7a"
AMBER = "#f0a020"
PANEL_BG = "#12141a"
PANEL_CARD = "#1b1e26"

RED = "#f5455c"
GREEN = "#20b268"

# 任务栏亮/暗两套文字配色：组件逐像素透明，浅底用深字、深底用浅字
PALETTES = {
    "dark": {
        "text": "#f4f6fb", "name": "#e7eaf1", "muted": "#a4aab8",
        "dim": "#878e9d", "red": "#ff5f72", "green": "#31d183",
        "amber": "#ffb020", "guide": "#3d424d",
    },
    "light": {
        "text": "#1a1e26", "name": "#151922", "muted": "#48505f",
        "dim": "#6b7280", "red": "#d21f3a", "green": "#0a8a4b",
        "amber": "#9a6100", "guide": "#a8aeb9",
    },
}


def palette(dark: bool) -> dict:
    return PALETTES["dark" if dark else "light"]


def px(value: float) -> int:
    return int(round(value * SCALE))


def blend(color_a: str, color_b: str, t: float) -> str:
    """把 color_a 按比例 t 混到 color_b 上（t=0 为 color_b）。"""
    def rgb(c: str) -> Tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    ar, ag, ab = rgb(color_a)
    br, bg, bb = rgb(color_b)
    r = int(ar * t + br * (1 - t))
    g = int(ag * t + bg * (1 - t))
    b = int(ab * t + bb * (1 - t))
    return f"#{r:02x}{g:02x}{b:02x}"


def pick_font_family() -> str:
    try:
        families = set(tkfont.families())
    except Exception:
        return "Segoe UI"
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
        if name in families:
            return name
    return "TkDefaultFont"


FONT_FAMILY = "Segoe UI"


class Fonts:
    """字体缓存 + 文本宽度测量。"""

    def __init__(self, root: tk.Misc, family: str) -> None:
        self.root = root
        self.family = family
        self._fonts: dict = {}

    def spec(self, size: float, bold: bool = False) -> tuple:
        return (self.family, int(round(size * SCALE)), "bold") if bold else (
            self.family, int(round(size * SCALE))
        )

    def font(self, size: float, bold: bool = False) -> tkfont.Font:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(root=self.root, font=self.spec(size, bold))
        return self._fonts[key]

    def width(self, text: str, size: float, bold: bool = False) -> int:
        return self.font(size, bold).measure(text)


def rounded_rect(cv: tk.Canvas, x0: float, y0: float, x1: float, y1: float,
                 r: float, fill: str, outline: Optional[str] = None,
                 width: int = 1) -> int:
    """Canvas 没有原生圆角矩形，这里用平滑多边形近似。"""
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [
        x0 + r, y0, x1 - r, y0, x1, y0,
        x1, y0 + r, x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1, x0, y1,
        x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    options = {"fill": fill, "smooth": True, "splinesteps": 12}
    if outline:
        options["outline"] = outline
        options["width"] = width
    return cv.create_polygon(pts, **options)


def draw_spark(cv: tk.Canvas, box, points, prev_close: float, color: str,
               guide: str = BORDER_SOFT, smooth: bool = True) -> None:
    """透明背景下画走势线：只有线、昨收虚线和末端圆点，不做任何填充。"""
    x0, y0, x1, y1 = box
    if len(points) < 2:
        mid = (y0 + y1) / 2
        cv.create_line(x0, mid, x1, mid, fill=guide, dash=(3, 4))
        return

    values = list(points)
    lo = min(values)
    hi = max(values)
    if prev_close:
        lo = min(lo, prev_close)
        hi = max(hi, prev_close)
    if hi - lo < 1e-9:
        hi = lo + 0.01 / max(1.0, abs(lo))

    def to_y(v: float) -> float:
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    if prev_close:
        base_y = to_y(prev_close)
        cv.create_line(x0, base_y, x1, base_y, fill=guide, dash=(3, 4))

    step = (x1 - x0) / (len(values) - 1)
    coords = []
    for i, value in enumerate(values):
        coords.extend((x0 + i * step, to_y(value)))

    cv.create_line(coords, fill=color, width=max(1, px(1.4)), smooth=smooth)
    # 末端小圆点
    end_x, end_y = coords[-2], coords[-1]
    r = max(1.5, px(2))
    cv.create_oval(end_x - r, end_y - r, end_x + r, end_y + r, fill=color, outline="")


def draw_arrow(cv: tk.Canvas, x: float, cy: float, up: bool, color: str,
               size: float = 5.0) -> None:
    s = px(size)
    if up:
        cv.create_polygon(x, cy - s, x + s, cy + s * 0.7, x - s, cy + s * 0.7,
                          fill=color, outline="")
    else:
        cv.create_polygon(x, cy + s, x + s, cy - s * 0.7, x - s, cy - s * 0.7,
                          fill=color, outline="")
