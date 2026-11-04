"""数据层：行情抓取 + 后台轮询。

设计要点：
- UI 只通过 `MarketService.snapshot()` 读取数据，两者之间没有其他耦合；
- 数据源抽象成 Provider，新增数据源时只需要实现 fetch_quotes / fetch_intraday；
- 抓取失败不清空缓存，只把最后一次成功的数据标记为 stale（异常态），
  这样界面不会闪、不会崩，用户仍能看到上一次的价格。
"""
from __future__ import annotations

import json as _json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, List, Optional

try:
    import requests  # 本机已安装时优先使用（连接复用更稳）
except ImportError:  # pragma: no cover - 纯标准库兜底
    requests = None

from .config import CONFIG_DIR, clean_code, normalize_symbol
from .trading import gold_interval as current_gold_interval
from .trading import poll_interval
from .util import log

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class ProviderError(RuntimeError):
    pass


def _num(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


class _HttpResponse:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise ProviderError(f"HTTP {self.status_code}")

    def json(self):
        return _json.loads(self.content.decode("utf-8", errors="ignore"))


class _UrllibSession:
    """requests 不可用时的最小替代，保持相同调用方式。"""

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}

    def get(self, url: str, params: Optional[dict] = None,
            timeout: float = 6.0) -> _HttpResponse:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return _HttpResponse(getattr(resp, "status", 200), resp.read())
        except urllib.error.URLError as exc:
            raise ProviderError(str(exc)) from exc


def _new_session():
    return requests.Session() if requests else _UrllibSession()


@dataclass
class Quote:
    symbol: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    quote_time: str = ""
    updated_at: float = 0.0
    stale: bool = False
    source: str = ""

    @property
    def direction(self) -> int:
        if self.change_pct > 0:
            return 1
        if self.change_pct < 0:
            return -1
        return 0


@dataclass
class Spark:
    symbol: str
    points: List[float]
    times: List[str]
    prev_close: float = 0.0
    source: str = ""


# ---------------------------------------------------------------- providers
class TencentProvider:
    name = "tencent"
    label = "腾讯行情"

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout
        self.session = _new_session()
        self.session.headers.update(
            {"User-Agent": UA, "Referer": "https://finance.qq.com"}
        )

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")

        quotes: Dict[str, Quote] = {}
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            key, _, payload = chunk.partition("=")
            key = key.strip()
            if not key.startswith("v_"):
                continue
            symbol = key[2:]
            payload = payload.strip().strip('"')
            fields = payload.split("~")
            if len(fields) < 35:
                continue
            try:
                price = float(fields[3])
                prev = float(fields[4] or 0)
            except ValueError:
                continue
            change = _num(fields[31], price - prev)
            pct = _num(fields[32], (change / prev * 100) if prev else 0.0)
            high = _num(fields[33])
            low = _num(fields[34])
            open_price = _num(fields[5])
            quotes[symbol] = Quote(
                symbol=symbol,
                name=fields[1],
                price=price,
                prev_close=prev,
                open=open_price,
                high=high,
                low=low,
                change=change,
                change_pct=pct,
                quote_time=fields[30],
                updated_at=time.time(),
                source=self.name,
            )
        if not quotes:
            raise ProviderError("腾讯行情返回空数据")
        return quotes

    def fetch_intraday(self, symbol: str) -> Spark:
        try:
            spark = self._intraday(symbol)
            if spark and len(spark.points) >= 2:
                return spark
        except Exception:
            pass
        return self._daily(symbol)

    def _intraday(self, symbol: str) -> Optional[Spark]:
        url = "http://web.ifzq.gtimg.cn/appstock/app/minute/query"
        resp = self.session.get(url, params={"code": symbol}, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        node = (payload.get("data") or {}).get(symbol) or {}
        inner = node.get("data") or {}
        rows = inner.get("data") or []
        points: List[float] = []
        times: List[str] = []
        for row in rows:
            parts = str(row).split()
            if len(parts) < 2:
                continue
            try:
                points.append(float(parts[1]))
            except ValueError:
                continue
            times.append(parts[0])
        if len(points) < 2:
            return None
        prev = 0.0
        qt = (node.get("qt") or {}).get(symbol)
        if isinstance(qt, list) and len(qt) > 4:
            try:
                prev = float(qt[4])
            except ValueError:
                prev = 0.0
        return Spark(symbol, points, times, prev, "intraday")

    def _daily(self, symbol: str) -> Spark:
        url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        resp = self.session.get(
            url, params={"param": f"{symbol},day,,,30,qfq"}, timeout=self.timeout
        )
        resp.raise_for_status()
        payload = resp.json()
        node = (payload.get("data") or {}).get(symbol) or {}
        rows = node.get("qfqday") or node.get("day") or []
        points: List[float] = []
        times: List[str] = []
        for row in rows:
            if len(row) < 3:
                continue
            try:
                points.append(float(row[2]))
            except ValueError:
                continue
            times.append(str(row[0]))
        if len(points) < 2:
            raise ProviderError(f"{symbol} 无分时/日线数据")
        prev = 0.0
        try:
            prev = float(node.get("prec") or 0)
        except ValueError:
            prev = 0.0
        if not prev and len(points) > 1:
            prev = points[-2]
        return Spark(symbol, points, times, prev, "daily")


class SinaProvider:
    name = "sina"
    label = "新浪财经"

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout
        self.session = _new_session()
        self.session.headers.update(
            {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}
        )

    @staticmethod
    def _to_sina(symbol: str) -> str:
        if symbol.lower().startswith("us"):
            return "gb_" + symbol[2:].lower()
        return symbol

    @staticmethod
    def _from_sina(symbol: str) -> str:
        if symbol.startswith("gb_"):
            return "us" + symbol[3:].upper()
        return symbol

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        sina_symbols = [self._to_sina(s) for s in symbols]
        url = "https://hq.sinajs.cn/list=" + ",".join(sina_symbols)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")

        quotes: Dict[str, Quote] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("var hq_str_"):
                continue
            key, _, payload = line.partition("=")
            sina_symbol = key[len("var hq_str_"):].strip()
            payload = payload.strip().rstrip(";").strip('"')
            fields = payload.split(",")
            if len(fields) < 32:
                continue
            try:
                open_price = float(fields[1])
                prev = float(fields[2])
                price = float(fields[3])
            except ValueError:
                continue
            if price <= 0 or prev <= 0:
                continue
            change = price - prev
            symbol = self._from_sina(sina_symbol)
            quotes[symbol] = Quote(
                symbol=symbol,
                name=fields[0],
                price=price,
                prev_close=prev,
                open=open_price,
                high=float(fields[4] or 0),
                low=float(fields[5] or 0),
                change=change,
                change_pct=change / prev * 100,
                quote_time=f"{fields[30]} {fields[31]}",
                updated_at=time.time(),
                source=self.name,
            )
        if not quotes:
            raise ProviderError("新浪行情返回空数据")
        return quotes

    def fetch_intraday(self, symbol: str) -> Spark:
        url = (
            "https://quotes.sina.cn/cn/api/json_v2.php/"
            "CN_MarketDataService.getKLineData"
        )
        resp = self.session.get(
            url,
            params={
                "symbol": self._to_sina(symbol),
                "scale": 5,
                "ma": "no",
                "datalen": 60,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or len(rows) < 2:
            raise ProviderError(f"{symbol} 新浪分时数据为空")
        points = [float(row["close"]) for row in rows if "close" in row]
        times = [str(row.get("day", ""))[-5:] for row in rows]
        if len(points) < 2:
            raise ProviderError(f"{symbol} 新浪分时数据为空")
        return Spark(symbol, points, times, 0.0, "sina-5m")


PROVIDERS = {"tencent": TencentProvider, "sina": SinaProvider}

UA_EASTMONEY = {
    "User-Agent": UA,
    "Referer": "https://quote.eastmoney.com/",
    # 东财 WAF 对“类脚本”请求头很敏感：补全浏览器常用头并禁用 zstd，
    # 否则打包后的 exe 会被直接断开连接（Python 直跑时不一定复现）。
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}
BREADTH_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
BREADTH_SECIDS = "1.000001,0.399001"   # 上证指数 + 深证成指，合计 = 沪深两市
NAMES_PATH = CONFIG_DIR / "names.json"
RESOLVED_PATH = CONFIG_DIR / "resolved.json"

SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

# ---------------------------------------------------------------- gold / 积存金
GOLD_SGE_SYMBOL = "gold:AU9999"        # 上海黄金交易所 Au99.99（参考金价）
GOLD_MANUAL_PREFIX = "gold:manual:"
SGE_SECIDS = {GOLD_SGE_SYMBOL: "118.AU9999"}
GOLD_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SINA_GOLD_URL = "https://hq.sinajs.cn/list=SGE_AU9999"
GOLD_NOTE = "参考金价（上金所 Au99.99），非银行积存金报价"


class GoldProvider:
    """上海黄金交易所 Au99.99 参考价（单位 元/克）。

    主源：新浪财经 SGE_AU9999（返回现价、昨收、高低、更新时间）；
    备源：东方财富 118.AU9999。银行积存金（工行/建行/招行/农行）没有
    稳定的公开报价接口，界面统一标注“参考”，不冒充银行报价。
    贵金属站点不走系统代理，避免代理抽风时行情完全不可用。
    """

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout
        self.session = _new_session()
        try:
            self.session.trust_env = False
        except Exception:
            pass
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://finance.sina.com.cn/",
        })

    def fetch(self, symbols: List[str]) -> Dict[str, Quote]:
        wanted = [s for s in symbols if s in SGE_SECIDS]
        if not wanted:
            return {}
        try:
            return self._fetch_sina(wanted)
        except Exception as sina_exc:
            try:
                return self._fetch_eastmoney(wanted)
            except Exception as em_exc:
                raise ProviderError(f"新浪: {sina_exc}; 东财: {em_exc}")

    def _fetch_sina(self, symbols: List[str]) -> Dict[str, Quote]:
        resp = self.session.get(SINA_GOLD_URL, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        out: Dict[str, Quote] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("var hq_str_SGE_AU9999="):
                continue
            payload = line.partition("=")[2].strip().strip(";").strip('"')
            fields = payload.split(",")
            if len(fields) < 18:
                continue
            price = _num(fields[3])
            prev = _num(fields[5])
            if price <= 0 or prev <= 0:
                continue
            change = price - prev
            pct = _num(fields[17].replace("%", ""), change / prev * 100)
            stamp = fields[16].strip()
            quote_time = stamp.split(" ")[-1] if stamp else ""
            out[GOLD_SGE_SYMBOL] = Quote(
                symbol=GOLD_SGE_SYMBOL,
                name="黄金9999",
                price=price,
                prev_close=prev,
                open=_num(fields[9]),
                high=_num(fields[7]),
                low=_num(fields[8]),
                change=change,
                change_pct=pct,
                quote_time=quote_time,
                updated_at=time.time(),
                source="sge-sina",
            )
        if not out:
            raise ProviderError("新浪 SGE_AU9999 返回空数据")
        return out

    def _fetch_eastmoney(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for symbol in symbols:
            secid = SGE_SECIDS.get(symbol)
            if not secid:
                continue
            data = self._one(secid)
            if not data:
                continue
            price = _num(data.get("f43"))
            prev = _num(data.get("f60"))
            if price <= 0:
                continue
            pct = _num(data.get("f170"))
            stamp = data.get("f86")
            quote_time = ""
            try:
                if stamp:
                    quote_time = datetime.fromtimestamp(int(stamp)).strftime("%H:%M:%S")
            except (TypeError, ValueError, OSError):
                quote_time = ""
            out[symbol] = Quote(
                symbol=symbol,
                name=str(data.get("f58") or "黄金9999"),
                price=price,
                prev_close=prev,
                change=_num(data.get("f169"), price - prev),
                change_pct=pct if pct else ((price - prev) / prev * 100 if prev else 0),
                quote_time=quote_time,
                updated_at=time.time(),
                source="sge-em",
            )
        if not out:
            raise ProviderError("上金所 Au99.99 报价为空")
        return out

    def _one(self, secid: str) -> Optional[dict]:
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = self.session.get(
                    GOLD_QUOTE_URL,
                    params={
                        "secid": secid,
                        "fltt": "2",
                        "invt": "2",
                        "fields": "f43,f57,f58,f60,f86,f169,f170",
                    },
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                data = (payload or {}).get("data")
                if isinstance(data, dict) and _num(data.get("f43")) > 0:
                    return data
                raise ProviderError("Au99.99 返回空数据")
            except Exception as exc:
                last = exc
                if attempt == 0:
                    time.sleep(0.5)
        if last:
            raise ProviderError(str(last))
        return None

_resolved: Optional[Dict[str, str]] = None


def _load_resolved() -> Dict[str, str]:
    global _resolved
    if _resolved is None:
        _resolved = {}
        try:
            raw = _json.loads(RESOLVED_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _resolved = {str(k): str(v) for k, v in raw.items()}
        except Exception:
            pass
    return _resolved


def _remember_resolved(raw: str, symbol: str) -> None:
    cache = _load_resolved()
    if raw and cache.get(raw) != symbol:
        cache[raw] = symbol
        try:
            tmp = RESOLVED_PATH.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(cache, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(RESOLVED_PATH)
        except Exception:
            pass


MARKET_LABELS = {
    "sh": "沪市", "sz": "深市", "bj": "北交所", "hk": "港股", "us": "美股",
}


def market_label(symbol: str) -> str:
    return MARKET_LABELS.get(str(symbol or "")[:2].lower(), "")


def _secid_to_symbol(quote_id: str, classify: str = "",
                     type_name: str = "") -> str:
    """东财 QuoteID → 项目 symbol。

    1.000905 / 0.399905 是沪深两市的常规映射；北交所同样用 0 前缀，
    只能靠 SecurityTypeName（京A）或 Classify（NEEQ）识别，指数 899xxx 单独判断。
    """
    match = re.match(r"^([01])\.(\d{6})$", str(quote_id or "").strip())
    if not match:
        return ""
    code = match.group(2)
    if type_name.startswith("京") or classify == "NEEQ" or code.startswith(("92", "899")):
        return "bj" + code
    return ("sh" if match.group(1) == "1" else "sz") + code


def _suggest_rows(raw: str, timeout: float, count: int = 10) -> list:
    """东财 suggest 原始返回；网络/接口异常直接抛出，由调用方决定如何降级。"""
    query = _search_query(raw)
    if not query:
        return []
    session = _new_session()
    session.headers.update(UA_EASTMONEY)
    resp = session.get(
        SEARCH_URL,
        params={"input": query, "type": "14", "token": SEARCH_TOKEN,
                "count": str(count)},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = (((payload or {}).get("QuotationCodeTable") or {}).get("Data")) or []
    return rows if isinstance(rows, list) else []


def _candidate(row: dict) -> Optional[dict]:
    """一条 suggest 结果 → 候选；板块/场外基金/港美股等无法行情报价的跳过。"""
    symbol = _secid_to_symbol(
        str(row.get("QuoteID") or ""),
        str(row.get("Classify") or ""),
        str(row.get("SecurityTypeName") or ""),
    )
    if not symbol:
        return None
    return {
        "symbol": symbol,
        "code": str(row.get("Code") or symbol[2:]),
        "name": str(row.get("Name") or "").strip(),
        "market": market_label(symbol),
        "type": str(row.get("SecurityTypeName") or ""),
        "classify": str(row.get("Classify") or ""),
    }


def search_candidates(raw: str, timeout: float = 6.0,
                      limit: int = 12) -> List[dict]:
    """按名称/代码/拼音搜索候选。

    正常返回候选列表（可能为空）；网络或接口异常抛出 ProviderError，
    调用方据此区分“无结果”和“搜索失败”。
    """
    try:
        rows = _suggest_rows(raw, timeout, limit)
    except Exception as exc:
        raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
    out: List[dict] = []
    seen = set()
    for row in rows:
        item = _candidate(row)
        if not item or item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        out.append(item)
    return out


def local_candidates(raw: str, extra_names: Optional[Dict[str, str]] = None,
                     limit: int = 12) -> List[dict]:
    """接口不可用时的本地兜底：匹配已缓存英文名/别名与代码。"""
    text = clean_code(raw)
    if not text:
        return []

    table: Dict[str, str] = {}
    try:
        cached = _json.loads(NAMES_PATH.read_text(encoding="utf-8"))
        if isinstance(cached, dict):
            table.update({str(k): str(v) for k, v in cached.items()})
    except Exception:
        pass
    for symbol, name in (extra_names or {}).items():
        if name and symbol not in table:
            table[symbol] = str(name)

    out: List[dict] = []
    seen = set()

    def add(symbol: str, name: str) -> None:
        if not symbol or symbol in seen or len(out) >= limit:
            return
        seen.add(symbol)
        out.append({
            "symbol": symbol,
            "code": symbol[2:],
            "name": name,
            "market": market_label(symbol),
            "type": "",
            "classify": "",
            "local": True,
        })

    # 输入本身就是代码：规范化 / 历史解析结果优先
    guess = normalize_symbol(text) or _load_resolved().get(text, "")
    if guess:
        add(guess, table.get(guess, ""))

    for symbol, name in table.items():
        if len(out) >= limit:
            break
        if text in name.upper() or text in symbol.upper():
            add(symbol, name)
    return out


def friendly_error(exc: object) -> str:
    """把底层异常翻译成一句用户能看懂的提示。"""
    message = str(exc).lower()
    name = type(exc).__name__
    if "timeout" in message or "timed out" in message or "超时" in message:
        return "请求超时"
    if ("connect" in message or "network" in message or "unreachable" in message
            or "refused" in message or "dns" in message):
        return "网络连接失败"
    if "name" in message and "resolution" in message:
        return "域名解析失败"
    if "http" in message or name == "HTTPError":
        return "接口返回异常"
    return "接口不可用"


def search_with_fallback(raw: str, timeout: float = 5.0,
                         extra_names: Optional[Dict[str, str]] = None) -> tuple:
    """搜索并降级：返回 (候选列表, 错误提示)。

    成功时错误提示为空串；失败时退回本地缓存，并把失败原因交给 UI 展示。
    """
    try:
        return search_candidates(raw, timeout), ""
    except Exception as exc:
        log(f"名称搜索失败 {raw}: {type(exc).__name__}: {exc}")
        return local_candidates(raw, extra_names), friendly_error(exc)


def _search_query(raw: str) -> str:
    """搜索接口只认代码/名称，先把 sh/sz/1B 之类的标记去掉并补零。"""
    text = clean_code(raw)
    match = re.match(r"^(SH|SZ|BJ|HK|US)\.?([\w.]+)$", text)
    if match:
        text = match.group(2).replace(".", "")
    match = re.match(r"^([01])B(\d{1,6})$", text)
    if match:
        text = match.group(2).zfill(6)
    return text


def search_symbol(raw: str, timeout: float = 6.0) -> str:
    """用东方财富搜索接口把代码/中文名解析成标准 symbol（返回 '' 表示没找到）。"""
    query = _search_query(raw)
    if not query:
        return ""
    try:
        session = _new_session()
        session.headers.update(UA_EASTMONEY)
        resp = session.get(
            SEARCH_URL,
            params={"input": query, "type": "14", "token": SEARCH_TOKEN,
                    "count": "10"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log(f"代码搜索失败 {raw}: {type(exc).__name__}: {exc}")
        return ""
    rows = (((payload or {}).get("QuotationCodeTable") or {}).get("Data")) or []
    if not isinstance(rows, list):
        return ""
    candidates = []
    for row in rows:
        symbol = _secid_to_symbol(row.get("QuoteID"))
        if not symbol:
            continue
        classify = str(row.get("Classify") or "")
        candidates.append((symbol, classify, str(row.get("Name") or "")))
    if not candidates:
        return ""
    # 优先指数/个股，其次基金、债券等；同名时取第一条
    for symbol, classify, _name in candidates:
        if classify in ("Index", "AStock"):
            return symbol
    return candidates[0][0]


def probe_quote(symbol: str, timeout: float = 6.0) -> Optional[dict]:
    """实时校验一个 symbol 是否有行情，返回名称/价格/涨跌幅。"""
    for provider in (TencentProvider(timeout), SinaProvider(timeout)):
        try:
            quote = provider.fetch_quotes([symbol]).get(symbol)
        except Exception:
            continue
        if quote and quote.price > 0:
            return {"symbol": symbol, "name": quote.name or "",
                    "price": quote.price, "pct": quote.change_pct,
                    "source": provider.name}
    return None


def resolve_input(raw: str, timeout: float = 6.0) -> Optional[dict]:
    """把用户输入解析成可用的 symbol。

    顺序：本地规范化 → 实时行情校验；失败再走东财搜索；都失败返回 None。
    """
    text = clean_code(raw)
    if not text:
        return None
    guess = normalize_symbol(text) or _load_resolved().get(text, "")
    if guess:
        hit = probe_quote(guess, timeout)
        if hit:
            _remember_resolved(text, guess)
            return hit
    found = search_symbol(text, timeout)
    if found and found != guess:
        hit = probe_quote(found, timeout)
        if hit:
            _remember_resolved(text, found)
            return hit
    # 行情暂时连不上但写法本身合法：仍然允许添加，UI 会提示离线
    if guess:
        return {"symbol": guess, "name": "", "price": None, "pct": None,
                "source": ""}
    if found:
        return {"symbol": found, "name": "", "price": None, "pct": None,
                "source": ""}
    return None



def _is_chinese(text: str) -> bool:
    return bool(text) and any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _int_or_none(value) -> Optional[int]:
    try:
        if value in (None, "", "-", "--"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ service
class MarketService:
    """后台轮询线程；线程安全，UI 侧只需 snapshot()。"""

    def __init__(self, cfg: dict) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._symbols: List[str] = []
        self._stock_symbols: List[str] = []
        self._gold_items: List[dict] = []
        self._gold_hist: Dict[str, deque] = {}
        self._gold_at = 0.0
        self._gold_every = 30
        self._gold_failed = False
        self._stock_at = 0.0
        self._phase_prev: Optional[str] = None
        self._force = threading.Event()
        self._poll_log: deque = deque(maxlen=60)
        self._adaptive: dict = {}
        self._quotes: Dict[str, Quote] = {}
        self._sparks: Dict[str, Spark] = {}
        self._spark_at: Dict[str, float] = {}
        self._last_error = ""
        self._names: Dict[str, str] = self._load_names()
        self._names_dirty = False
        self._breadth: Optional[dict] = None
        self._breadth_error = ""
        self._breadth_at = 0.0
        self._breadth_session = _new_session()
        self._breadth_session.headers.update(UA_EASTMONEY)
        self._meta = {
            "version": 0,
            "last_ok": 0.0,
            "last_attempt": 0.0,
            "error": "",
            "provider": "",
            "polls": 0,
            "names": {},
            "breadth": None,
            "breadth_error": "",
            "gold_error": "",
        }
        self._timeout = 6.0
        self._interval = 3.0
        self._stale_after = 30.0
        self._intraday_every = 60.0
        self._provider = "auto"
        self.apply(cfg)
        self._thread = threading.Thread(
            target=self._run, name="market-poller", daemon=True
        )

    # ------------------------------------------------------------- config
    def apply(self, cfg: dict) -> None:
        with self._lock:
            watch = list(cfg["watchlist"])
            self._stock_symbols = [
                item["symbol"] for item in watch
                if str(item.get("kind") or "stock") == "stock"
            ]
            self._gold_items = [
                dict(item) for item in watch
                if str(item.get("kind") or "stock") == "gold"
            ]
            self._symbols = list(self._stock_symbols)
            self._gold_at = 0.0
            data = cfg["data"]
            self._timeout = float(data["timeout_seconds"])
            self._interval = float(data["interval_seconds"])
            self._stale_after = float(data["stale_after_seconds"])
            self._intraday_every = float(data["intraday_refresh_seconds"])
            self._provider = data.get("provider", "auto")
        # 配置变化（切换标的/保存设置）后强制请求一次，避免沿用旧缓存
        self._force.set()
        self._wake.set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def refresh_now(self) -> None:
        """用户手动刷新/切换标的/重新显示时强制请求一次（无视自适应间隔）。"""
        self._force.set()
        self._wake.set()

    def adaptive_plan(self) -> dict:
        with self._lock:
            return dict(self._adaptive)

    # ------------------------------------------------------------ snapshot
    def snapshot(self):
        with self._lock:
            quotes = {k: replace(v) for k, v in self._quotes.items()}
            sparks = dict(self._sparks)
            for symbol, points in self._gold_hist.items():
                if len(points) < 2:
                    continue
                quote = self._quotes.get(symbol)
                sparks[symbol] = Spark(
                    symbol, list(points), [], quote.prev_close if quote else 0.0,
                    "gold-history")
            meta = dict(self._meta)
            meta["names"] = dict(self._names)
            meta["breadth"] = dict(self._breadth) if self._breadth else None
            meta["adaptive"] = dict(self._adaptive)
            meta["poll_log"] = list(self._poll_log)
        return quotes, sparks, meta

    @staticmethod
    def _manual_quote(item: dict) -> Optional[Quote]:
        """手动维护的固定价格：涨跌幅按“上次更新/昨收”口径计算。"""
        symbol = str(item.get("symbol") or "")
        price = float(item.get("manual_price") or 0)
        if price <= 0:
            return None
        prev = float(item.get("manual_prev") or 0)
        change = price - prev if prev > 0 else 0.0
        pct = (change / prev * 100) if prev > 0 else 0.0
        stamp = float(item.get("manual_updated") or 0)
        try:
            quote_time = (datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M")
                          if stamp > 0 else "手动")
        except (ValueError, OSError):
            quote_time = "手动"
        return Quote(
            symbol=symbol,
            name=str(item.get("alias") or "自定义金价"),
            price=price,
            prev_close=prev,
            change=change,
            change_pct=pct,
            quote_time=quote_time,
            updated_at=stamp or time.time(),
            stale=False,
            source="manual",
        )

    # --------------------------------------------------------------- names
    @staticmethod
    def _load_names() -> Dict[str, str]:
        """本地名称缓存：接口给过中文名就记下来，下次接口缺名时补位。"""
        try:
            raw = _json.loads(NAMES_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): str(v) for k, v in raw.items() if _is_chinese(str(v))}
        except Exception:
            pass
        return {}

    def _remember_name(self, symbol: str, name: str) -> None:
        name = (name or "").strip()
        if not symbol or not _is_chinese(name):
            return
        if self._names.get(symbol) != name:
            self._names[symbol] = name
            self._names_dirty = True

    def remember_name(self, symbol: str, name: str) -> None:
        """添加代码时把搜索/行情得到的名称先写进缓存，列表立即可见。"""
        self._remember_name(symbol, name)

    def _save_names(self) -> None:
        if not self._names_dirty:
            return
        self._names_dirty = False
        try:
            tmp = NAMES_PATH.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(self._names, ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            tmp.replace(NAMES_PATH)
        except Exception:
            pass

    def _fill_names(self, quotes: Dict[str, Quote], symbols: List[str],
                    providers: List[object], used) -> None:
        """优先行情接口中文名；缺名时用本地缓存，再不行用另一个接口补一次。"""
        missing: List[str] = []
        for symbol in symbols:
            quote = quotes.get(symbol)
            name = (quote.name or "").strip() if quote else ""
            if _is_chinese(name):
                self._remember_name(symbol, name)
                continue
            cached = self._names.get(symbol)
            if cached:
                if quote and not _is_chinese(quote.name or ""):
                    quote.name = cached
                continue
            # 英文/拼音名也记为缺名，继续尝试另一个接口补中文
            missing.append(symbol)
        if not missing:
            return
        alt = next((p for p in providers if p is not used), None)
        if alt is None:
            alt = SinaProvider(self._timeout) if getattr(used, "name", "") == "tencent" \
                else TencentProvider(self._timeout)
        try:
            extra = alt.fetch_quotes(missing[:6])
        except Exception:
            return
        for symbol, quote in extra.items():
            name = (quote.name or "").strip()
            if not _is_chinese(name):
                continue
            self._remember_name(symbol, name)
            if symbol in quotes and not _is_chinese(quotes[symbol].name or ""):
                quotes[symbol].name = name

    # ------------------------------------------------------------- breadth
    def _fetch_breadth(self) -> Optional[dict]:
        """东方财富 ulist 接口：f104 上涨 / f105 下跌 / f106 平盘（按指数汇总）。"""
        last_exc = None
        for attempt in range(2):
            try:
                resp = self._breadth_session.get(
                    BREADTH_URL,
                    params={
                        "fltt": "2",
                        "secids": BREADTH_SECIDS,
                        "fields": "f12,f14,f104,f105,f106",
                    },
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                rows = ((payload or {}).get("data") or {}).get("diff") or []
                up = down = flat = 0
                markets = 0
                names = []
                for row in rows:
                    u, d, f = (_int_or_none(row.get("f104")),
                               _int_or_none(row.get("f105")),
                               _int_or_none(row.get("f106")))
                    if u is None or d is None:
                        continue
                    markets += 1
                    up += u
                    down += d
                    flat += f or 0
                    names.append(str(row.get("f14") or ""))
                if markets == 0 or up + down + flat <= 0:
                    raise ProviderError("涨跌家数字段为空")
                self._breadth_error = ""
                return {"up": up, "down": down, "flat": flat,
                        "markets": names, "partial": markets < 2,
                        "source": "eastmoney"}
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    # 启动首轮偶发被东财 WAF 直接断开，稍等重试一次即可恢复
                    time.sleep(0.4)
        message = f"{type(last_exc).__name__}: {last_exc}"
        if message != self._breadth_error:
            log("涨跌家数获取失败: " + message)
        self._breadth_error = message
        return None

    # ---------------------------------------------------------------- loop
    def _plan(self) -> dict:
        with self._lock:
            has_stock = bool(self._stock_symbols)
            has_gold = any(not str(item.get("symbol") or "").startswith(GOLD_MANUAL_PREFIX)
                           for item in self._gold_items)
            base = self._interval
        return poll_interval(base, has_stock, has_gold)

    def _run(self) -> None:
        log("行情轮询线程启动")
        last_phase = None
        while not self._stop.is_set():
            plan = self._plan()
            with self._lock:
                self._adaptive = plan
            if plan["phase"] != last_phase:
                log(f"自适应刷新: {plan['phase']}，唤醒 {plan['sleep']:.0f}s，"
                    f"下次约 {plan['next_at']}")
                last_phase = plan["phase"]
            try:
                self._poll()
            except Exception:
                log("轮询循环异常", exc=True)
            # 请求可能耗时，唤醒时刻按请求结束的当前时间重新计算，
            # 保证 09:30 / 13:00 的切换不会被粗粒度 sleep 拖后。
            plan = self._plan()
            if self._stop.is_set():
                break
            self._wake.wait(max(0.05, plan["sleep"]))
            self._wake.clear()
        log("行情轮询线程退出")

    def _poll(self) -> None:
        with self._lock:
            symbols = list(self._symbols)
            gold_items = [dict(item) for item in self._gold_items]
        now = time.time()
        force = self._force.is_set()
        if force:
            self._force.clear()
        stock_plan = poll_interval(self._interval, True, False)
        phase = stock_plan["phase"]
        # 进入新状态（盘前→开盘、午休→开盘、收盘等）补一次请求，
        # 拿到最新价后即可停摆，不再按分钟级轮询。
        transition = self._phase_prev != phase
        self._phase_prev = phase
        self._gold_every = current_gold_interval()

        # 涨跌家数与股票同属 A 股时段：只在盘中按 15s 刷新；
        # 其余时段仅在状态切换/手动刷新时请求一次。
        breadth_attempted = (
            force or transition
            or (phase == "trading" and now - self._breadth_at >= 15.0)
        )
        breadth = self._fetch_breadth() if breadth_attempted else None
        if breadth_attempted:
            self._breadth_at = now
        with self._lock:
            if breadth:
                breadth["updated_at"] = now
                breadth["stale"] = False
                self._breadth = breadth
                self._meta["breadth_error"] = ""
            elif breadth_attempted and self._breadth:
                # 失败不清空旧值：保留数字并标 stale，UI 显示琥珀点
                self._breadth["stale"] = True
                self._meta["breadth_error"] = self._breadth_error
            elif breadth_attempted:
                self._meta["breadth_error"] = self._breadth_error

        if not symbols and not gold_items:
            with self._lock:
                self._meta["error"] = "未配置自选股"
                self._meta["version"] += 1
                self._meta["names"] = dict(self._names)
            self._save_names()
            return

        # 股票：盘中按用户间隔；开盘前 5 分钟恢复高频；午休/收盘/非交易日
        # 只在状态切换时请求 1 次，之后完全停止（积存金仍按自身时段更新）。
        want_stock = bool(symbols) and (
            force or transition
            or (stock_plan["stock_active"]
                and now - self._stock_at >= stock_plan["interval"])
        )
        if want_stock:
            self._stock_at = now

        quotes: Optional[Dict[str, Quote]] = None
        error = ""
        used = None
        if symbols and want_stock:
            providers: List[object] = []
            if self._provider in PROVIDERS:
                providers = [PROVIDERS[self._provider](self._timeout)]
            else:
                providers = [TencentProvider(self._timeout), SinaProvider(self._timeout)]
            for provider in providers:
                try:
                    quotes = provider.fetch_quotes(symbols)
                    used = provider
                    break
                except Exception as exc:
                    error = (f"{provider.label if hasattr(provider, 'label') else provider.name}"
                             f": {exc}")
            if quotes:
                self._fill_names(quotes, symbols, providers, used)

        # ---------------------------------------------------------- gold
        gold_quotes: Dict[str, Quote] = {}
        gold_error = ""
        remote_gold = [item["symbol"] for item in gold_items
                       if not str(item["symbol"]).startswith(GOLD_MANUAL_PREFIX)]
        attempted_gold = False
        if remote_gold and (force or now - self._gold_at >= self._gold_every):
            attempted_gold = True
            self._gold_at = now
            try:
                gold_quotes = GoldProvider(self._timeout).fetch(remote_gold)
                self._gold_failed = False
            except Exception as exc:
                self._gold_failed = True
                gold_error = f"上金所 Au99.99: {exc}"

        manual_quotes: Dict[str, Quote] = {}
        for item in gold_items:
            symbol = str(item["symbol"])
            if not symbol.startswith(GOLD_MANUAL_PREFIX):
                continue
            quote = self._manual_quote(item)
            if quote:
                manual_quotes[symbol] = quote

        now = time.time()
        with self._lock:
            self._meta["polls"] += 1
            self._meta["last_attempt"] = now
            if quotes:
                for symbol in symbols:
                    quote = quotes.get(symbol)
                    if quote:
                        self._quotes[symbol] = quote
                self._meta["last_ok"] = now
                self._meta["error"] = ""
                self._meta["provider"] = used.name if used else ""
            elif symbols and want_stock:
                self._meta["error"] = error or "行情获取失败"

            if remote_gold and gold_quotes:
                for symbol, quote in gold_quotes.items():
                    prev = self._quotes.get(symbol)
                    self._quotes[symbol] = quote
                    self._gold_hist.setdefault(symbol, deque(maxlen=120)).append(quote.price)
                    if prev is None:
                        self._gold_hist[symbol].appendleft(quote.prev_close)
                self._meta["gold_error"] = ""
            elif remote_gold and attempted_gold:
                self._meta["gold_error"] = gold_error or "上金所 Au99.99 获取失败"

            for symbol, quote in manual_quotes.items():
                self._quotes[symbol] = quote
                self._gold_hist.setdefault(symbol, deque(maxlen=120)).append(quote.price)
            for item in gold_items:
                symbol = str(item["symbol"])
                if symbol.startswith(GOLD_MANUAL_PREFIX) and symbol not in manual_quotes:
                    self._quotes.pop(symbol, None)
                    self._meta["gold_error"] = "手动价格未设置"

            all_symbols = symbols + [str(item["symbol"]) for item in gold_items]
            for symbol in all_symbols:
                quote = self._quotes.get(symbol)
                if not quote:
                    continue
                if not quote.name:
                    item = next((i for i in gold_items if i["symbol"] == symbol), {})
                    quote.name = (item.get("alias") or self._names.get(symbol, ""))
                if str(symbol).startswith("gold:"):
                    if str(symbol).startswith(GOLD_MANUAL_PREFIX):
                        quote.stale = False
                    elif attempted_gold:
                        # 只有本轮真的请求过才判断异常；低频时段保留上次结论。
                        quote.stale = (self._gold_failed
                                       or (now - quote.updated_at) > self._stale_after)
                    continue
                if not want_stock:
                    # 低频时段没有发请求，旧价格就是最近收盘价，不算异常。
                    continue
                missing = bool(quotes) and symbol not in quotes
                quote.stale = (
                    quotes is None
                    or missing
                    or (now - quote.updated_at) > self._stale_after
                )
            self._meta["names"] = {
                symbol: (self._quotes.get(symbol).name if self._quotes.get(symbol) else "")
                          or self._names.get(symbol, "")
                for symbol in all_symbols
            }
            self._meta["version"] += 1
            current_error = self._meta["error"]

        self._save_names()

        if current_error and current_error != self._last_error:
            log("行情获取失败: " + current_error)
        self._last_error = current_error
        if gold_error:
            log("积存金参考价获取失败: " + gold_error)

        # 分时数据单独刷新（比报价慢），失败时保留旧曲线；只在真正请求
        # 股票行情的轮次里刷新，低频时段不额外发请求。
        if symbols and want_stock:
            provider = used or providers[0]
            for symbol in symbols:
                if now - self._spark_at.get(symbol, 0) < self._intraday_every:
                    continue
                try:
                    spark = provider.fetch_intraday(symbol)
                except Exception as exc:
                    log(f"分时数据获取失败 {symbol}: {exc}")
                    self._spark_at[symbol] = now
                    continue
                if spark and len(spark.points) >= 2:
                    with self._lock:
                        self._sparks[symbol] = spark
                        self._spark_at[symbol] = now

        with self._lock:
            self._poll_log.append({
                "time": time.strftime("%H:%M:%S"),
                "phase": phase,
                "stock": bool(symbols and want_stock),
                "breadth": bool(breadth_attempted),
                "gold": bool(attempted_gold),
                "transition": bool(transition),
                "force": bool(force),
            })


def status_text(meta: dict) -> str:
    """主窗口/状态提示共用的一段人类可读状态。"""
    if meta.get("error"):
        return f"离线 · {meta['error']}"
    if not meta.get("last_ok"):
        return "正在获取行情…"
    provider = {"tencent": "腾讯", "sina": "新浪"}.get(meta.get("provider", ""), "—")
    age = max(0, int(time.time() - meta["last_ok"]))
    return f"{provider} · {age}s 前更新"
