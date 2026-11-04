"""托盘图标生成：纯 Python 手绘 32bpp BMP 并打包成 .ico。

不依赖 Pillow，保证在干净机器上也能出图标。
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import List, Tuple

RGBA = Tuple[int, int, int, int]


def _fill_round_rect(buf: bytearray, n: int, box, radius: float, color: RGBA) -> None:
    x0, y0, x1, y1 = box
    r = radius
    for y in range(int(y0), int(y1)):
        py = y + 0.5
        for x in range(int(x0), int(x1)):
            px = x + 0.5
            cx = min(max(px, x0 + r), x1 - r)
            cy = min(max(py, y0 + r), y1 - r)
            if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                _put(buf, n, x, y, color)


def _put(buf: bytearray, n: int, x: int, y: int, color: RGBA) -> None:
    if x < 0 or y < 0 or x >= n or y >= n:
        return
    idx = (y * n + x) * 4
    r, g, b, a = color
    if a >= 255:
        buf[idx:idx + 4] = bytes((r, g, b, 255))
        return
    # 简单 alpha 混合到已有像素
    br, bg, bb, ba = buf[idx], buf[idx + 1], buf[idx + 2], buf[idx + 3]
    alpha = a / 255.0
    buf[idx] = int(r * alpha + br * (1 - alpha))
    buf[idx + 1] = int(g * alpha + bg * (1 - alpha))
    buf[idx + 2] = int(b * alpha + bb * (1 - alpha))
    buf[idx + 3] = max(ba, a)


def _draw_line(buf: bytearray, n: int, p0, p1, width: float, color: RGBA) -> None:
    x0, y0 = p0
    x1, y1 = p1
    half = width / 2.0
    left = int(min(x0, x1) - half - 1)
    right = int(max(x0, x1) + half + 1)
    top = int(min(y0, y1) - half - 1)
    bottom = int(max(y0, y1) + half + 1)
    dx, dy = x1 - x0, y1 - y0
    seg_len2 = dx * dx + dy * dy
    for y in range(top, bottom):
        for x in range(left, right):
            px, py = x + 0.5, y + 0.5
            if seg_len2 == 0:
                dist2 = (px - x0) ** 2 + (py - y0) ** 2
            else:
                t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg_len2))
                cx, cy = x0 + t * dx, y0 + t * dy
                dist2 = (px - cx) ** 2 + (py - cy) ** 2
            if dist2 <= half * half:
                _put(buf, n, x, y, color)


def _fill_triangle(buf: bytearray, n: int, p0, p1, p2, color: RGBA) -> None:
    xs = [p0[0], p1[0], p2[0]]
    ys = [p0[1], p1[1], p2[1]]

    def sign(a, b, c):
        return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])

    for y in range(int(min(ys)), int(max(ys)) + 1):
        for x in range(int(min(xs)), int(max(xs)) + 1):
            p = (x + 0.5, y + 0.5)
            d1 = sign(p, p0, p1)
            d2 = sign(p, p1, p2)
            d3 = sign(p, p2, p0)
            if not (d1 < 0 or d2 < 0 or d3 < 0) or not (d1 > 0 or d2 > 0 or d3 > 0):
                _put(buf, n, x, y, color)


def _render_icon(size: int, accent: Tuple[int, int, int]) -> bytes:
    """返回 size*size 的 RGBA（自上而下）像素。"""
    s = 4  # 超采样倍数
    n = size * s
    hi = bytearray(n * n * 4)

    margin = n * 0.08
    radius = n * 0.24
    _fill_round_rect(hi, n, (margin, margin, n - margin, n - margin), radius,
                     (19, 21, 27, 255))
    # 1px 高光边框（超采样后约 1*size/16）
    border = max(1.0, s * 0.9)
    _fill_round_rect(hi, n, (margin, margin, n - margin, n - margin), radius,
                     (72, 76, 90, 120))
    _fill_round_rect(hi, n, (margin + border, margin + border,
                             n - margin - border, n - margin - border),
                     max(1.0, radius - border), (19, 21, 27, 255))

    pts = [
        (0.24, 0.74),
        (0.42, 0.55),
        (0.56, 0.63),
        (0.74, 0.34),
    ]
    width = n * 0.085
    for a, b in zip(pts, pts[1:]):
        _draw_line(hi, n, (a[0] * n, a[1] * n), (b[0] * n, b[1] * n), width,
                   (accent[0], accent[1], accent[2], 255))

    head = n * 0.16
    hx, hy = pts[-1][0] * n, pts[-1][1] * n
    _fill_triangle(hi, n, (hx - head, hy - head * 0.15),
                   (hx + head * 0.15, hy - head * 0.95),
                   (hx + head * 0.75, hy + head * 0.35),
                   (accent[0], accent[1], accent[2], 255))

    # 降采样
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(s):
                for dx in range(s):
                    idx = ((y * s + dy) * n + (x * s + dx)) * 4
                    r += hi[idx]
                    g += hi[idx + 1]
                    b += hi[idx + 2]
                    a += hi[idx + 3]
            total = s * s
            oidx = (y * size + x) * 4
            out[oidx] = r // total
            out[oidx + 1] = g // total
            out[oidx + 2] = b // total
            out[oidx + 3] = a // total
    return bytes(out)


def _bmp_entry(rgba_top_down: bytes, size: int) -> bytes:
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0
    )
    rows = []
    for y in range(size - 1, -1, -1):
        row = rgba_top_down[y * size * 4:(y + 1) * size * 4]
        flipped = bytearray(size * 4)
        for x in range(size):
            r, g, b, a = row[x * 4:x * 4 + 4]
            flipped[x * 4:x * 4 + 4] = bytes((b, g, r, a))
        rows.append(bytes(flipped))
    mask_row = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_row * size)
    return header + b"".join(rows) + mask


def build_ico(accent: Tuple[int, int, int], sizes: List[int] | None = None) -> bytes:
    sizes = sizes or [16, 24, 32, 48]
    entries = []
    offset = 6 + 16 * len(sizes)
    body = b""
    for size in sizes:
        data = _bmp_entry(_render_icon(size, accent), size)
        entries.append(
            struct.pack(
                "<BBBBHHII",
                size if size < 256 else 0,
                size if size < 256 else 0,
                0, 0, 1, 32, len(data), offset,
            )
        )
        offset += len(data)
        body += data
    header = struct.pack("<HHH", 0, 1, len(sizes))
    return header + b"".join(entries) + body


def ensure_icon(path: Path, accent: Tuple[int, int, int]) -> Path:
    try:
        if not path.exists() or path.stat().st_size < 100:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(build_ico(accent))
    except Exception:
        pass
    return path
