# -*- coding: utf-8 -*-
"""Win11 分层窗口文字花屏自检。

做三件事：
1. 在 2 种背景 × 3 档缩放 × 3 档 DPI × 3 个落点（共 54 个位置）上连续移动窗口，
   每个位置分别用「直通 RGBA」和「预乘 alpha」提交，自动截图并与正确合成结果逐
   像素比对，统计异常彩色像素 / 低亮度边缘。
2. 模拟拖动：拖动期间不应有任何位图提交，松手后恰好提交一次且尺寸/位置一致。
3. 模拟 DWM/Tk 擅自改窗口尺寸，确认下一次 tick 能把窗口拉回位图尺寸并重画。

用法：python tools/check_layered_glitch.py
输出：控制台 PASS/FAIL；工程根目录 compare_glitch.png（修复前后 6 倍放大对比）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMP = Path(os.environ.get("TEMP", REPO / "build_probe"))
HOME = TEMP / "tbglitch_check"
HOME.mkdir(parents=True, exist_ok=True)
os.environ["TASKBARSTOCK_HOME"] = str(HOME)
sys.path.insert(0, str(REPO))

CONFIG = {
    "watchlist": [{"symbol": "sh000001", "alias": "上证指数"}],
    "display": {"mode": "trend", "visible_count": 1, "show_breadth": True,
                "rotate_enabled": False, "rotate_seconds": 0, "color_style": "cn"},
    "data": {"provider": "auto", "interval_seconds": 3, "timeout_seconds": 6,
             "stale_after_seconds": 30, "intraday_refresh_seconds": 60},
    "window": {"offset_x": 0, "offset_y": 0, "monitor": "", "monitor_rect": [],
               "max_width": 0, "text_theme": "dark", "always_on_top": True,
               "scale": 0.8, "scale_version": 2},
}
(HOME / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

import tkinter as tk

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageGrab

from app.config import ConfigStore
from app.core import App
from app.market import Quote, Spark
from app.platform_win import (enable_dpi_awareness, hwnd_of_toplevel, monitors,
                              raise_topmost, virtual_screen, window_rect)
from app import ui_widget as W

SHOTS = TEMP / "glitch_check_shots"
SHOTS.mkdir(parents=True, exist_ok=True)
BG_COLORS = [("#101318", "dark"), ("#e3e6ec", "light")]
SCALES = [0.6, 0.8, 1.4]
DPIS = [1.0, 1.25, 1.5]
POSITIONS = [(60, 80), (700, 360), (1180, 640)]
FAILS: list[str] = []

enable_dpi_awareness()
root = tk.Tk()
root.withdraw()
root.tk.call("tk", "scaling", 1.0)
store = ConfigStore()
app = App(root, store)

QUOTE = Quote(symbol="sh000001", name="上证指数", price=3601.55, prev_close=3607.4,
              open=3605.2, high=3612.0, low=3594.8, change=-5.85,
              change_pct=-0.16, quote_time="15:00:00", stale=False, source="probe")
SPARK = Spark(symbol="sh000001",
              points=[3606.0, 3604.2, 3607.1, 3603.4, 3605.0, 3601.2, 3602.8,
                      3607.4, 3603.0, 3596.1, 3598.6, 3595.2, 3600.1, 3602.0,
                      3599.8, 3601.5, 3604.3, 3603.2, 3601.9, 3600.6, 3602.4,
                      3603.7, 3602.0, 3601.1, 3601.55],
              times=[], prev_close=3607.4, source="probe")
META = {"version": 7, "error": "", "names": {"sh000001": "上证指数"},
        "breadth": {"up": 2482, "down": 3821, "flat": 366, "stale": False}}
app.service.snapshot = lambda: ({"sh000001": QUOTE}, {"sh000001": SPARK}, dict(META))

CALLS: list = []
W._orig_update = W.LayeredSurface.update


def _record(self, image, x, y):
    CALLS.append((image.copy(), int(x), int(y)))
    return W._orig_update(self, image, x, y)


W.LayeredSurface.update = _record

widget = W.WidgetWindow(app)
app.widget = widget
widget.show()
try:
    widget.win.after_cancel(widget._tick_id)
except Exception:
    pass

vx, vy, vr, vb = virtual_screen()
backdrop = tk.Toplevel(root)
backdrop.overrideredirect(True)
backdrop.configure(bg=BG_COLORS[0][0])
backdrop.geometry(f"{vr - vx}x{vb - vy}+{vx}+{vy}")
backdrop.attributes("-topmost", True)
backdrop.deiconify()
root.update()
raise_topmost(backdrop)
raise_topmost(widget.win)
raise_topmost(widget.hit)
root.update()


def spin(seconds=0.06):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.005)


def check(name: str, cond: bool, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(name)


def setup_case(scale: float, dpi: float, theme: str):
    widget.cfg["window"]["scale"] = scale
    widget.cfg["window"]["text_theme"] = theme
    real = next(m for m in monitors() if m.get("primary"))
    widget._apply_monitor({**real, "dpi": dpi})
    widget._replan()
    widget._pick_theme()
    widget._size = widget._preferred_size()
    return widget._size


def place(x: int, y: int):
    widget._x, widget._y = int(x), int(y)
    widget._apply_geometry(*widget._size)
    root.update()
    widget._last_sig = None
    widget._render_now()
    spin()


def grab():
    w, h = widget._size
    return ImageGrab.grab(bbox=(widget._x, widget._y, widget._x + w, widget._y + h),
                          all_screens=True).convert("RGB")


def unique_colors(image: Image.Image) -> int:
    return len(image.getcolors(image.width * image.height) or [])


def metrics(shot: Image.Image, frame: Image.Image, bg_rgb) -> dict:
    """实际截图 vs “正确的预乘合成”标准答案。"""
    actual = np.asarray(shot.convert("RGB"), dtype=np.int32)
    rgba = np.asarray(frame.convert("RGBA"), dtype=np.int32)
    src, alpha = rgba[:, :, :3], rgba[:, :, 3:4]
    bg = np.array(bg_rgb, dtype=np.int32)[None, None, :]
    truth = (src * alpha + bg * (255 - alpha)) // 255
    diff = np.abs(actual - truth)
    chroma_a = actual.max(axis=2) - actual.min(axis=2)
    chroma_t = truth.max(axis=2) - truth.min(axis=2)
    lum = lambda px: (px[:, :, 0] * 299 + px[:, :, 1] * 587 + px[:, :, 2] * 114) // 1000
    return {
        "bad": int((diff.max(axis=2) > 24).sum()),
        "fringe": int((chroma_a - chroma_t > 20).sum()),
        "dark": int((lum(truth) - lum(actual) > 24).sum()),
        "uniq": len({tuple(v) for v in actual.reshape(-1, 3).tolist()}),
        "mean": float(diff.mean()),
    }


totals = {"before": {"bad": 0, "fringe": 0, "dark": 0, "uniq": []},
          "after": {"bad": 0, "fringe": 0, "dark": 0, "uniq": []}}
samples = {}
count = 0

# 预检：锁屏/远程桌面里 GetDC 会失败，组件画不出来，此时统计没有意义。
setup_case(0.8, 1.0, "dark")
place(300, 120)
if unique_colors(grab()) <= 2:
    print("环境不可用：桌面被锁或没有桌面合成，请在已登录的桌面上运行。", flush=True)
    widget.destroy()
    root.destroy()
    raise SystemExit(2)

for bg_hex, theme in BG_COLORS:
    backdrop.configure(bg=bg_hex)
    root.update()
    raise_topmost(backdrop)
    raise_topmost(widget.win)
    raise_topmost(widget.hit)
    spin(0.15)
    for scale in SCALES:
        for dpi in DPIS:
            w, h = setup_case(scale, dpi, theme)
            for index, (x, y) in enumerate(POSITIONS):
                for mode in ("before", "after"):
                    widget.surface.premultiply = (mode == "after")
                    place(x, y)
                    n = len(CALLS)
                    widget._last_sig = None
                    widget._render_now()
                    spin(0.04)
                    frame, sx, sy = CALLS[n]
                    shot = grab()
                    rect = window_rect(hwnd_of_toplevel(widget.win))
                    if not rect or (rect[2] - rect[0], rect[3] - rect[1]) != (w, h):
                        FAILS.append(f"窗口/位图尺寸不一致 {scale}@{dpi}")
                    m = metrics(shot, frame, shot.getpixel((0, 0)))
                    totals[mode]["bad"] += m["bad"]
                    totals[mode]["fringe"] += m["fringe"]
                    totals[mode]["dark"] += m["dark"]
                    totals[mode]["uniq"].append(m["uniq"])
                    count += 1
                    if index == 0 and scale == 0.8 and dpi == 1.0:
                        samples[(mode, theme)] = shot.copy()
            print(f"[{theme} scale={scale} dpi={dpi}] {w}x{h} 完成", flush=True)

for mode in ("before", "after"):
    u = totals[mode]["uniq"]
    print(f"{mode}: 异常像素={totals[mode]['bad']} 彩色异常={totals[mode]['fringe']} "
          f"低亮度边缘={totals[mode]['dark']} 平均色阶数={sum(u) / max(1, len(u)):.1f}",
          flush=True)

print(f"\n共 {count} 张截图（{count // 2} 个位置 × 2 种提交方式）", flush=True)
check("修复前（直通 RGBA）确实花屏", totals["before"]["bad"] > 200, totals["before"]["bad"])
check("修复后异常像素 < 40", totals["after"]["bad"] < 40, totals["after"]["bad"])
check("修复后无彩色异常", totals["after"]["fringe"] < 10, totals["after"]["fringe"])
check("修复后无低亮度边缘", totals["after"]["dark"] < 10, totals["after"]["dark"])

# ------------------------------------------------------------- 拖动冻结
print("\n--- 拖动冻结 ---", flush=True)
setup_case(0.8, 1.0, "dark")
widget.surface.premultiply = True
place(300, 120)
start = len(CALLS)
widget._drag = (0, 0, widget._x, widget._y)
widget._drag_moved = True
for step in range(12):
    widget._x, widget._y = 300 + step * 14, 120 + step * 5
    widget.win.geometry(f"+{widget._x}+{widget._y}")
    widget.hit.geometry(f"+{widget._x}+{widget._y}")
    widget._render_now()
    root.update()
    time.sleep(0.01)
during = len(CALLS) - start
widget._on_release(None)
spin(0.10)
after = len(CALLS) - start - during
frame, sx, sy = CALLS[-1]
check("拖动期间 0 次提交", during == 0, during)
check("松手后恰好提交 1 次", after == 1, after)
check("提交尺寸/位置与窗口一致", frame.size == widget._size
      and (sx, sy) == (widget._x, widget._y), (frame.size, sx, sy))
shot_after = grab()
m = metrics(shot_after, frame, shot_after.getpixel((0, 0)))
check("松手后画面正常", m["bad"] < 40, m)

# ------------------------------------------------------- DPI/尺寸漂移自愈
print("\n--- 尺寸漂移自愈 ---", flush=True)
w1, h1 = widget._size
widget.win.geometry(f"{w1 + 50}x{h1 + 12}+{widget._x}+{widget._y}")
spin(0.10)
widget._ensure_geometry()
widget._render_now()
spin(0.10)
rect = window_rect(hwnd_of_toplevel(widget.win))
check("窗口自动回到位图尺寸",
      bool(rect) and (rect[2] - rect[0], rect[3] - rect[1]) == (w1, h1),
      None if not rect else (rect[2] - rect[0], rect[3] - rect[1]))
frame = CALLS[-1][0]
healed = grab()
m = metrics(healed, frame, healed.getpixel((0, 0)))
check("自愈后画面正常", m["bad"] < 40, m)

# ------------------------------------------------------------- 对比图
zoom = 6
font = ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) /
                              "Fonts/msyhbd.ttc"), 20, index=0)
small = ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) /
                                "Fonts/msyh.ttc"), 14, index=0)
rows = [
    ("修复前：直通 RGBA + AC_SRC_ALPHA", "边缘保留满强度 RGB → 彩色描边/笔画粘连", "before"),
    ("修复后：RGBA 先预乘 alpha 再提交", "边缘按 alpha 衰减 → 平滑抗锯齿、无彩色噪点", "after"),
]
crops = []
for title, note, mode in rows:
    shot = samples[(mode, "dark")]
    crop = shot.crop((0, 0, min(250, shot.width), shot.height))
    crops.append((title, note, crop.resize((crop.width * zoom, crop.height * zoom),
                                           Image.NEAREST)))
panel = max(c.width for _, _, c in crops)
canvas = Image.new("RGB", (panel + 32,
                           14 + sum(52 + c.height + 16 for _, _, c in crops) + 8),
                   (13, 16, 22))
draw = ImageDraw.Draw(canvas)
y = 14
for title, note, crop in crops:
    draw.text((16, y), title, font=font, fill=(236, 240, 248))
    draw.text((16, y + 28), note, font=small, fill=(150, 158, 172))
    y += 52
    canvas.paste(crop, (16, y))
    y += crop.height + 16
canvas.save(REPO / "compare_glitch.png")
print("\n对比图已写入 compare_glitch.png", flush=True)
print("结果：" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项 {FAILS}"),
      flush=True)

widget.surface.premultiply = True
try:
    backdrop.destroy()
    widget.destroy()
except Exception:
    pass
root.update()
root.destroy()
