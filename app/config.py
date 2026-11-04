"""配置读写。

配置文件位置：%APPDATA%\\TaskbarStock\\config.json
首次运行会把工程根目录的 config.json 作为模板复制过去，之后用户的修改都写在
%APPDATA% 里，重新拉取代码不会覆盖用户配置。
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

APP_NAME = "TaskbarStock"
FROZEN = bool(getattr(sys, "frozen", False))
# 打包成 onefile 后，代码从临时解包目录加载，配置/日志要落到 exe 所在目录或用户目录
PROJECT_ROOT = Path(sys.executable).resolve().parent if FROZEN else \
    Path(__file__).resolve().parent.parent
_BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
TEMPLATE_CONFIG = _BUNDLE_DIR / "config.json"


def _resolve_config_dir() -> Path:
    """配置目录：TASKBARSTOCK_HOME > %APPDATA% > %LOCALAPPDATA% > 工程/exe 内 data。

    前两条失败（例如权限/沙箱限制）时逐级退到仍可写的目录，保证程序能启动。
    """
    override = os.environ.get("TASKBARSTOCK_HOME", "").strip()
    candidates = [Path(override)] if override else []
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / APP_NAME)
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / APP_NAME)
    candidates.append(PROJECT_ROOT / "data")
    candidates.append(Path.home() / APP_NAME)
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return PROJECT_ROOT


CONFIG_DIR = _resolve_config_dir()
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "stockbar.log"
RUNTIME_PATH = CONFIG_DIR / "runtime.json"
ICON_PATH = CONFIG_DIR / "stockbar.ico"

DEFAULTS: Dict[str, Any] = {
    "watchlist": [
        {"symbol": "sh600519", "alias": "贵州茅台"},
        {"symbol": "sz000001", "alias": "平安银行"},
        {"symbol": "sh000001", "alias": "上证指数"},
    ],
    "display": {
        "mode": "trend",              # trend=走势模式, mini=迷你模式
        "visible_count": 1,            # 一次显示 1~3 只，默认 1
        "show_breadth": True,         # 是否显示涨跌家数卡片
        "rotate_enabled": True,       # 是否启用轮播；关闭时固定显示自选第一只
        "rotate_seconds": 10,          # 轮播间隔，0 = 不轮播
        "color_style": "cn",           # cn=红涨绿跌（A股习惯），intl=绿涨红跌
        "hidden_mode": False,          # 隐藏模式：平时不可见，鼠标进贴靠热区才显示
        "hidden_delay": 0.0,           # 移出热区后自动隐藏的延时（秒），0=立即
        "hotspot_width": 160,          # 隐藏模式热区最小宽度（物理像素）
    },
    "data": {
        "provider": "auto",            # auto | tencent | sina
        "interval_seconds": 3,
        "timeout_seconds": 6,
        "stale_after_seconds": 30,
        "intraday_refresh_seconds": 60,
    },
    "window": {
        # 相对“托盘左侧默认落点”的偏移，拖动后按此记忆
        "offset_x": 0,
        "offset_y": 0,
        "monitor": "",                 # 显示器设备名（\\.\DISPLAY2），空=主屏
        "monitor_rect": [],            # 设备名失效时用显示器矩形辅助还原
        "max_width": 0,                # 卡片区最大宽度(px)，0=自动
        "text_theme": "auto",          # auto | dark | light
        "always_on_top": True,
        "scale": 0.8,                  # 整体缩放 60%~140%，默认 80%
    },
}

_SYMBOL_RE = re.compile(r"^(sh|sz|bj|hk|us)[A-Za-z0-9._]+$")
_MODES = ("trend", "mini")
_PROVIDERS = ("auto", "tencent", "sina")

# 000 开头的上交所指数（与深市同号个股冲突时优先识别成指数，例如 000905 中证500）
SSE_INDEX_CODES = {
    "000009", "000010", "000016", "000300", "000688",
    "000852", "000903", "000905", "000906",
}


def _market_for_digits(code: str) -> str:
    head = code[0]
    if head in "69":
        return "sh"
    if head == "5":
        return "sh"          # 5xxxxx 沪市基金/ETF
    if head in "48":
        return "bj"
    if head == "3":
        return "sz"
    if code in SSE_INDEX_CODES:
        return "sh"
    return "sz"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def clean_code(raw: str) -> str:
    """去掉空白、全角空格，统一大写，便于后续解析。"""
    text = str(raw or "")
    for ch in (" ", "\t", "\r", "\n", "\u3000"):
        text = text.replace(ch, "")
    return text.upper()


def normalize_symbol(raw: str) -> str:
    """把各种常见写法规范化为腾讯/新浪风格的 symbol。

    支持：000905 / sh000905 / sh.000905 / 000905.SH / SH600519 /
    1.000905（东财 secid）/ 1B0905（同花顺指数内部代码）/ AAPL。
    解析不出来时返回空串，由调用方再走搜索接口。
    """
    text = clean_code(raw)
    if not text:
        return ""

    # 1) 后缀式：000905.SH / 600519.SH / AAPL.US
    match = re.match(r"^([A-Z0-9]+)\.(SH|SZ|BJ|HK|US)$", text)
    if match:
        code, market = match.group(1), match.group(2).lower()
        if re.fullmatch(r"\d+", code):
            return market + code
        if market == "us" and code.isalpha():
            return "us" + code
        return ""

    # 2) 东方财富 secid：1.000905 / 0.399001
    match = re.match(r"^([01])\.(\d{6})$", text)
    if match:
        return ("sh" if match.group(1) == "1" else "sz") + match.group(2)

    # 3) 同花顺/券商终端的指数内部代码：1B0905（1B=沪市指数）、0B399905
    match = re.match(r"^([01])B(\d{1,6})$", text)
    if match:
        market = "sh" if match.group(1) == "1" else "sz"
        return market + match.group(2).zfill(6)

    # 4) 前缀式：sh000905 / sh.000905 / SH600519
    match = re.match(r"^(SH|SZ|BJ|HK|US)\.?([\w.]+)$", text)
    if match:
        prefix, code = match.group(1).lower(), match.group(2).replace(".", "")
        if prefix == "us":
            return "us" + code if code.isalnum() else ""
        return prefix + code if code.isdigit() else ""

    # 5) 纯数字
    if text.isdigit():
        if len(text) == 6:
            return _market_for_digits(text) + text
        if len(text) == 5:
            return "hk" + text
        return ""

    # 6) 纯字母按美股处理（只认 ASCII，避免把中文名当成美股代码）
    if text.isascii() and text.isalpha() and len(text) <= 6:
        return "us" + text
    return ""


class ConfigStore:
    def __init__(self) -> None:
        self.dir = CONFIG_DIR
        self.path = CONFIG_PATH
        self.cfg: Dict[str, Any] = {}
        self.first_run = False
        self._migrated = False
        self.load()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        raw: Dict[str, Any] = {}
        if self.path.exists():
            raw = self._read_json(self.path)
        elif TEMPLATE_CONFIG.exists():
            self.first_run = True
            raw = self._read_json(TEMPLATE_CONFIG)
            self.cfg = _deep_merge(DEFAULTS, raw)
            self._sanitize()
            self.save()
            return
        else:
            self.first_run = True
        self.cfg = _deep_merge(DEFAULTS, raw)
        self._sanitize()
        if self._migrated:
            self.save()

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _sanitize(self) -> None:
        cfg = self.cfg
        # watchlist
        items = []
        for entry in cfg.get("watchlist") or []:
            if isinstance(entry, str):
                entry = {"symbol": entry}
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get("alias") or "").strip()
            kind = "gold" if str(entry.get("kind") or "").lower() == "gold" else "stock"
            if kind == "gold":
                sym = str(entry.get("symbol") or "").strip()
                if not sym.startswith("gold:"):
                    continue
                changed = entry.get("manual_updated")
                try:
                    changed = float(changed or 0)
                except (TypeError, ValueError):
                    changed = 0.0
                items.append({
                    "symbol": sym,
                    "alias": alias,
                    "kind": "gold",
                    "manual_price": round(
                        _clamp_float(entry.get("manual_price"), 0, 100000, 0), 3),
                    "manual_prev": round(
                        _clamp_float(entry.get("manual_prev"), 0, 100000, 0), 3),
                    "manual_updated": changed,
                })
                continue
            sym = normalize_symbol(entry.get("symbol", ""))
            if not sym:
                continue
            items.append({"symbol": sym, "alias": alias})
        cfg["watchlist"] = items

        display = cfg.setdefault("display", {})
        if display.get("mode") not in _MODES:
            display["mode"] = DEFAULTS["display"]["mode"]
        display["visible_count"] = _clamp(display.get("visible_count"), 1, 3, 1)
        display["show_breadth"] = bool(display.get("show_breadth", True))
        display["rotate_enabled"] = bool(display.get("rotate_enabled", True))
        display["rotate_seconds"] = _clamp(display.get("rotate_seconds"), 0, 600, 10)
        if display.get("color_style") not in ("cn", "intl"):
            display["color_style"] = "cn"
        display["hidden_mode"] = bool(display.get("hidden_mode", False))
        # 0.2.0 的默认延时是 1.5s；升级到 0.2.1 时把旧默认值迁成 0（立即隐藏），
        # 用户手选过的其它值（如 2.5）保留。
        if int(display.get("hidden_delay_version") or 0) < 1:
            legacy = _clamp_float(display.get("hidden_delay"), 0.0, 5.0, 1.5)
            if abs(legacy - 1.5) < 0.001:
                display["hidden_delay"] = 0.0
            display["hidden_delay_version"] = 1
            self._migrated = True
        display["hidden_delay"] = round(
            _clamp_float(display.get("hidden_delay"), 0.0, 5.0, 0.0), 2)
        display["hotspot_width"] = _clamp(
            display.get("hotspot_width"), 60, 600, 160)

        data = cfg.setdefault("data", {})
        if data.get("provider") not in _PROVIDERS:
            data["provider"] = "auto"
        data["interval_seconds"] = _clamp(data.get("interval_seconds"), 2, 60, 3)
        data["timeout_seconds"] = _clamp(data.get("timeout_seconds"), 2, 30, 6)
        data["stale_after_seconds"] = _clamp(data.get("stale_after_seconds"), 5, 600, 30)
        data["intraday_refresh_seconds"] = _clamp(
            data.get("intraday_refresh_seconds"), 10, 900, 60
        )

        win = cfg.setdefault("window", {})
        # 第一版用绝对坐标 x/y，这里迁移为相对托盘的偏移，避免旧值干扰
        win.pop("x", None)
        win.pop("y", None)
        win["offset_x"] = _clamp(win.get("offset_x"), -4000, 4000, 0)
        win["offset_y"] = _clamp(win.get("offset_y"), -4000, 4000, 0)
        win["monitor"] = str(win.get("monitor") or "").strip()
        hint = win.get("monitor_rect")
        if isinstance(hint, (list, tuple)) and len(hint) == 4:
            try:
                win["monitor_rect"] = [int(v) for v in hint]
            except (TypeError, ValueError):
                win["monitor_rect"] = []
        else:
            win["monitor_rect"] = []
        win["max_width"] = _clamp(win.get("max_width"), 0, 2000, 0)
        if win.get("text_theme") not in ("auto", "dark", "light"):
            win["text_theme"] = "auto"
        win["always_on_top"] = bool(win.get("always_on_top", True))
        # 第三版默认 1.0，第四版默认缩小到 80%；用版本号区分“旧默认值”和用户手选
        if int(win.get("scale_version") or 0) < 2:
            win["scale"] = 0.8
            win["scale_version"] = 2
        # 缩放是小数，不能用 _clamp（会把 0.8 取整成 0）
        win["scale"] = round(_clamp_float(win.get("scale"), 0.6, 1.4, 0.8), 2)

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # --------------------------------------------------------------- helpers
    def symbols(self) -> list[str]:
        return [item["symbol"] for item in self.cfg["watchlist"]]

    def alias_of(self, symbol: str) -> str:
        for item in self.cfg["watchlist"]:
            if item["symbol"] == symbol:
                return item.get("alias") or ""
        return ""

    def item_of(self, symbol: str) -> dict:
        for item in self.cfg["watchlist"]:
            if item["symbol"] == symbol:
                return item
        return {}

    def kind_of(self, symbol: str) -> str:
        return str(self.item_of(symbol).get("kind") or "stock")


def _clamp(value: Any, low, high, default):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))
