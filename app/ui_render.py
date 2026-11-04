"""Pillow 透明画布：逐像素 alpha 绘制，文字/线条按 3 倍超采样后缩放。

为什么不用 Tk Canvas：Tk 的 `-transparentcolor` 是色键透明，文字抗锯齿像素
会和色键颜色混合，边缘出现暗边；只有逐像素 alpha 才能做到真彩色 + 真透明。
"""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
    raise ImportError(
        "渲染需要 Pillow：请执行 python -m pip install pillow"
    ) from exc

SS = 3  # 超采样倍数

_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_FONT_FILES = {
    False: ("msyh.ttc", "msyh.ttf", "segoeui.ttf", "arial.ttf"),
    True: ("msyhbd.ttc", "msyhbd.ttf", "segoeuib.ttf", "arialbd.ttf"),
}
_font_paths: Dict[bool, str] = {}
_fonts: Dict[Tuple[int, bool], ImageFont.FreeTypeFont] = {}

_ANCHOR = {"w": "lm", "e": "rm", "center": "mm", "c": "mm",
           "ls": "ls", "rs": "rs", "ms": "ms"}


def _font_path(bold: bool) -> str:
    if bold not in _font_paths:
        chosen = ""
        for name in _FONT_FILES[bold]:
            candidate = _FONT_DIR / name
            if candidate.exists():
                chosen = str(candidate)
                break
        _font_paths[bold] = chosen
    return _font_paths[bold]


def font(size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (max(1, int(round(size * SS))), bold)
    if key not in _fonts:
        path = _font_path(bold)
        _fonts[key] = (ImageFont.truetype(path, key[0], index=0) if path
                       else ImageFont.load_default())
    return _fonts[key]


def text_width(text: str, size: float, bold: bool = False) -> float:
    if not text:
        return 0.0
    try:
        return float(font(size, bold).getlength(text)) / SS
    except Exception:
        return float(len(text)) * size * 0.6


def rgb(color: str) -> Tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


class Frame:
    """先按颜色分组画到 3 倍遮罩上，最后统一缩放合成，避免暗边。"""

    def __init__(self, width: int, height: int) -> None:
        self.w = max(1, int(width))
        self.h = max(1, int(height))
        self._masks: "OrderedDict[Tuple[int, int, int], Image.Image]" = OrderedDict()

    def _mask(self, color: str) -> Image.Image:
        key = rgb(color)
        if key not in self._masks:
            self._masks[key] = Image.new("L", (self.w * SS, self.h * SS), 0)
        return self._masks[key]

    # ------------------------------------------------------------- primitives
    def text(self, x: float, y: float, text: str, size: float, color: str,
             bold: bool = False, anchor: str = "w") -> None:
        if not text:
            return
        draw = ImageDraw.Draw(self._mask(color))
        draw.text((x * SS, y * SS), text, font=font(size, bold), fill=255,
                  anchor=_ANCHOR.get(anchor, "lm"))

    def line(self, points: Sequence[Tuple[float, float]], color: str,
             width: float = 1.0) -> None:
        if len(points) < 2:
            return
        draw = ImageDraw.Draw(self._mask(color))
        draw.line([(x * SS, y * SS) for x, y in points], fill=255,
                  width=max(1, int(round(width * SS))), joint="curve")

    def dashed(self, x0: float, y0: float, x1: float, y1: float, color: str,
               width: float = 1.0, dash: float = 3.0, gap: float = 4.0) -> None:
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if length <= 0:
            return
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        start = 0.0
        while start < length:
            end = min(length, start + dash)
            self.line(((x0 + ux * start, y0 + uy * start),
                       (x0 + ux * end, y0 + uy * end)), color, width)
            start += dash + gap

    def dot(self, cx: float, cy: float, radius: float, color: str) -> None:
        draw = ImageDraw.Draw(self._mask(color))
        r = max(1.0, radius) * SS
        draw.ellipse((cx * SS - r, cy * SS - r, cx * SS + r, cy * SS + r), fill=255)

    def triangle(self, points: Sequence[Tuple[float, float]], color: str) -> None:
        draw = ImageDraw.Draw(self._mask(color))
        draw.polygon([(px * SS, py * SS) for px, py in points], fill=255)

    # ----------------------------------------------------------------- output
    def image(self) -> Image.Image:
        base = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        for color, mask in self._masks.items():
            small = mask.resize((self.w, self.h), Image.LANCZOS)
            layer = Image.new("RGBA", (self.w, self.h), color + (255,))
            layer.putalpha(small)
            base = Image.alpha_composite(base, layer)
        return base


def draw_spark(frame: Frame, box, points: Iterable[float], prev_close: float,
               color: str, guide: str, width: float = 1.2) -> None:
    """透明背景下画走势线：只有线、昨收虚线和末端圆点。"""
    x0, y0, x1, y1 = box
    values = list(points)
    if len(values) < 2:
        mid = (y0 + y1) / 2
        frame.dashed(x0, mid, x1, mid, guide, 1.0)
        return

    lo, hi = min(values), max(values)
    if prev_close:
        lo = min(lo, prev_close)
        hi = max(hi, prev_close)
    if hi - lo < 1e-9:
        hi = lo + 0.01 / max(1.0, abs(lo))

    def to_y(value: float) -> float:
        return y1 - (value - lo) / (hi - lo) * (y1 - y0)

    if prev_close:
        frame.dashed(x0, to_y(prev_close), x1, to_y(prev_close), guide, 1.0)

    step = (x1 - x0) / (len(values) - 1)
    coords = [(x0 + index * step, to_y(value)) for index, value in enumerate(values)]
    frame.line(coords, color, width)
    end_x, end_y = coords[-1]
    frame.dot(end_x, end_y, max(1.2, width * 1.6), color)


def draw_arrow(frame: Frame, x: float, cy: float, up: bool, color: str,
               size: float = 5.0) -> None:
    s = max(2.0, size)
    if up:
        frame.triangle(((x, cy - s), (x + s, cy + s * 0.7),
                        (x - s, cy + s * 0.7)), color)
    else:
        frame.triangle(((x, cy + s), (x + s, cy - s * 0.7),
                        (x - s, cy - s * 0.7)), color)
