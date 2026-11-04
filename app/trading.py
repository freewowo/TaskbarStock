"""A 股交易时段判断与交易日历缓存。

交易日历来源：https://timor.tech/api/holiday/year/{year}（公开节假日日历，
含法定节假日与调休信息）。首次获取后缓存到配置目录 trading_calendar.json，
记录年份、来源与更新时间；缓存超过 30 天或缺少当年/下一年时后台刷新。
取不到时退回「周末休市 + 内置已知节假日表」，不猜测未公布的假期。
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Set

from .config import CONFIG_DIR

CALENDAR_PATH = CONFIG_DIR / "trading_calendar.json"
SOURCE_TEMPLATE = "https://timor.tech/api/holiday/year/{year}"
CACHE_TTL = 30 * 86400

# 开发时从上述接口取样（2025-11-04），仅作为离线兜底；接口恢复后会自动更新缓存。
KNOWN_HOLIDAYS: Dict[int, Set[str]] = {
    2025: {
        "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
        "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
        "2025-04-04", "2025-04-05", "2025-04-06",
        "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
        "2025-05-31", "2025-06-01", "2025-06-02",
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
        "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    },
    2026: {
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
        "2026-10-05", "2026-10-06", "2026-10-07",
    },
}

OPEN_TIME = _dt.time(9, 30)
MORNING_END = _dt.time(11, 30)
AFTERNOON_START = _dt.time(13, 0)
CLOSE_TIME = _dt.time(15, 0)


class TradingCalendar:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holidays: Dict[int, Set[str]] = {}
        self._source = ""
        self._fetched_at = 0.0
        self._thread: threading.Thread | None = None
        self._load()

    # ------------------------------------------------------------------ cache
    def _load(self) -> None:
        try:
            raw = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        years = raw.get("years") if isinstance(raw, dict) else None
        if not isinstance(years, dict):
            return
        with self._lock:
            for key, dates in years.items():
                try:
                    year = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(dates, list):
                    self._holidays[year] = {str(d) for d in dates if isinstance(d, str)}
            self._source = str(raw.get("source") or "")
            try:
                self._fetched_at = float(raw.get("fetched_at") or 0)
            except (TypeError, ValueError):
                self._fetched_at = 0.0

    def _save(self) -> None:
        with self._lock:
            payload = {
                "source": self._source,
                "fetched_at": int(self._fetched_at),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._fetched_at)),
                "years": {str(year): sorted(dates)
                          for year, dates in self._holidays.items()},
            }
        try:
            CALENDAR_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        except Exception:
            pass

    def _years(self) -> list:
        today = _dt.date.today()
        return [today.year, today.year + 1]

    def stale(self) -> bool:
        if not self._fetched_at or time.time() - self._fetched_at > CACHE_TTL:
            return True
        with self._lock:
            return any(year not in self._holidays for year in self._years())

    def ensure_refresh(self) -> None:
        """后台刷新：不阻塞启动；失败时保留缓存/内置兜底。"""
        if not self.stale():
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._refresh, name="trading-calendar",
                                        daemon=True)
        self._thread.start()

    def _refresh(self) -> None:
        changed = False
        for year in self._years():
            dates = self._fetch(year)
            if dates is None:
                continue
            with self._lock:
                self._holidays[year] = dates
            changed = True
        if changed:
            with self._lock:
                self._source = "timor.tech:" + ",".join(str(y) for y in self._years())
                self._fetched_at = time.time()
            self._save()

    @staticmethod
    def _fetch(year: int) -> Set[str] | None:
        url = SOURCE_TEMPLATE.format(year=year)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 TaskbarStock/0.2",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.load(resp)
            entries = payload.get("holiday")
            if not isinstance(entries, dict):
                return None
            dates = set()
            for item in entries.values():
                if not isinstance(item, dict) or not item.get("holiday"):
                    continue
                text = str(item.get("date") or "")
                if len(text) == 10:
                    dates.add(text)
            return dates or None
        except Exception:
            return None

    # ------------------------------------------------------------------ query
    def holidays_for(self, year: int) -> Set[str]:
        with self._lock:
            dates = set(self._holidays.get(year) or ())
        return dates or set(KNOWN_HOLIDAYS.get(year) or ())

    def is_trading_day(self, day: _dt.date) -> bool:
        if day.weekday() >= 5:
            return False
        return day.isoformat() not in self.holidays_for(day.year)

    def next_open(self, now: _dt.datetime) -> _dt.datetime:
        day = now.date()
        for offset in range(0, 40):
            cand = day + _dt.timedelta(days=offset)
            if not self.is_trading_day(cand):
                continue
            moment = _dt.datetime.combine(cand, OPEN_TIME)
            if moment > now:
                return moment
        return _dt.datetime.combine(day + _dt.timedelta(days=1), OPEN_TIME)

    def status(self, kind: str = "stock", now: _dt.datetime | None = None) -> dict:
        """返回 {phase, label, countdown}；积存金不套用 A 股时段。"""
        if kind == "gold":
            return {"phase": "gold", "label": "", "countdown": ""}
        now = now or _dt.datetime.now()
        day = now.date()
        if not self.is_trading_day(day):
            target = self.next_open(now)
            return {"phase": "holiday", "label": "休市",
                    "countdown": _countdown(now, target)}
        clock = now.time()
        if clock < OPEN_TIME:
            return {"phase": "preopen", "label": "未开盘",
                    "countdown": _countdown(now, self.next_open(now))}
        if clock < MORNING_END:
            return {"phase": "trading", "label": "", "countdown": ""}
        if clock < AFTERNOON_START:
            return {"phase": "lunch", "label": "午间休市", "countdown": ""}
        if clock < CLOSE_TIME:
            return {"phase": "trading", "label": "", "countdown": ""}
        return {"phase": "closed", "label": "已收盘", "countdown": ""}


def next_boundary(now: _dt.datetime | None = None) -> _dt.datetime:
    """下一次交易状态切换的时刻（09:30 / 11:30 / 13:00 / 15:00 / 下一交易日 09:30）。"""
    now = now or _dt.datetime.now()
    day = now.date()
    if CALENDAR.is_trading_day(day):
        for moment in (OPEN_TIME, MORNING_END, AFTERNOON_START, CLOSE_TIME):
            candidate = _dt.datetime.combine(day, moment)
            if candidate > now:
                return candidate
    return CALENDAR.next_open(now)


def gold_session(now: _dt.datetime | None = None) -> str:
    """上金所 Au99.99 参考价的交易时段（日盘 + 夜盘），仅用于决定轮询频率。"""
    now = now or _dt.datetime.now()
    if not CALENDAR.is_trading_day(now.date()):
        return "closed"
    clock = now.time()
    if (_dt.time(9, 0) <= clock < MORNING_END
            or _dt.time(13, 30) <= clock < _dt.time(15, 30)
            or clock >= _dt.time(20, 0) or clock < _dt.time(2, 30)):
        return "open"
    return "closed"


def gold_interval(now: _dt.datetime | None = None) -> float:
    """积存金参考价的轮询间隔：上金所日盘/夜盘 30 秒，休市 5 分钟，非交易日 30 分钟。"""
    now = now or _dt.datetime.now()
    if not CALENDAR.is_trading_day(now.date()):
        return 1800.0
    return 30.0 if gold_session(now) == "open" else 300.0


def poll_interval(base: float, has_stock: bool = True, has_gold: bool = False,
                  now: _dt.datetime | None = None) -> dict:
    """按交易状态给出轮询间隔与下一次唤醒时刻。

    基准是用户设置的盘中间隔（默认 3 秒）；午休/盘前 5 分钟，收盘后 30 分钟，
    非交易日 60 分钟。积存金参考价按上金所日盘/夜盘 15 秒、其余时段 5 分钟。
    实际睡眠还会被“下一次状态切换时刻”截断，保证 09:30 / 13:00 秒级恢复。
    """
    now = now or _dt.datetime.now()
    base = max(1.0, float(base))
    stock = CALENDAR.status("stock", now)
    phase = stock["phase"]
    if phase == "trading":
        stock_interval = base
    elif phase == "preopen":
        remain = max(0.0, (CALENDAR.next_open(now) - now).total_seconds())
        stock_interval = base if remain <= 300 else 300.0
    elif phase == "lunch":
        stock_interval = 300.0
    elif phase == "closed":
        stock_interval = 1800.0
    else:
        stock_interval = 3600.0

    gold_every = gold_interval(now)
    intervals = []
    if has_stock:
        intervals.append(stock_interval)
    if has_gold:
        intervals.append(gold_every)
    interval = min(intervals) if intervals else 3600.0
    boundary = next_boundary(now)
    until = max(0.0, (boundary - now).total_seconds())
    sleep = min(interval, until)
    return {
        "phase": phase,
        "gold_session": gold_session(now),
        "stock_interval": round(stock_interval, 3) if has_stock else 0,
        "gold_interval": round(gold_every, 3) if has_gold else 0,
        "interval": round(interval, 3),
        "sleep": round(sleep, 3),
        "next_at": int(time.time() + sleep),
        "boundary": boundary.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _countdown(now: _dt.datetime, target: _dt.datetime) -> str:
    total = max(0, int((target - now).total_seconds()))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}天{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


CALENDAR = TradingCalendar()


def status_for(kind: str = "stock", now: _dt.datetime | None = None) -> dict:
    return CALENDAR.status(kind, now)


def calendar_info() -> dict:
    """给 README/诊断用：来源、缓存时间与已缓存年份。"""
    with CALENDAR._lock:
        return {
            "source": CALENDAR._source,
            "fetched_at": int(CALENDAR._fetched_at),
            "years": sorted(CALENDAR._holidays.keys()),
        }
