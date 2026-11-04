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
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

try:
    import requests  # 本机已安装时优先使用（连接复用更稳）
except ImportError:  # pragma: no cover - 纯标准库兜底
    requests = None

from .config import CONFIG_DIR, clean_code, normalize_symbol
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
        self._quotes: Dict[str, Quote] = {}
        self._sparks: Dict[str, Spark] = {}
        self._spark_at: Dict[str, float] = {}
        self._last_error = ""
        self._names: Dict[str, str] = self._load_names()
        self._names_dirty = False
        self._breadth: Optional[dict] = None
        self._breadth_error = ""
        self._breadth_at = 0.0
        self._breadth_every = 15.0
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
            self._symbols = [item["symbol"] for item in cfg["watchlist"]]
            data = cfg["data"]
            self._timeout = float(data["timeout_seconds"])
            self._interval = float(data["interval_seconds"])
            self._stale_after = float(data["stale_after_seconds"])
            self._intraday_every = float(data["intraday_refresh_seconds"])
            self._provider = data.get("provider", "auto")
        self._wake.set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def refresh_now(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------ snapshot
    def snapshot(self):
        with self._lock:
            quotes = {k: replace(v) for k, v in self._quotes.items()}
            sparks = dict(self._sparks)
            meta = dict(self._meta)
            meta["names"] = dict(self._names)
            meta["breadth"] = dict(self._breadth) if self._breadth else None
        return quotes, sparks, meta

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
    def _run(self) -> None:
        log("行情轮询线程启动")
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:
                log("轮询循环异常", exc=True)
            self._wake.wait(self._interval)
            self._wake.clear()
        log("行情轮询线程退出")

    def _poll(self) -> None:
        with self._lock:
            symbols = list(self._symbols)
        now = time.time()

        # 涨跌家数不需要 3 秒一刷，降低频率可避免东财风控直接断开连接
        breadth = None
        if now - self._breadth_at >= self._breadth_every:
            self._breadth_at = now
            breadth = self._fetch_breadth()
        with self._lock:
            if breadth:
                breadth["updated_at"] = now
                breadth["stale"] = False
                self._breadth = breadth
                self._meta["breadth_error"] = ""
            elif self._breadth:
                # 失败不清空旧值：保留数字并标 stale，UI 显示琥珀点
                self._breadth["stale"] = True
                self._meta["breadth_error"] = self._breadth_error
            else:
                self._meta["breadth_error"] = self._breadth_error

        if not symbols:
            with self._lock:
                self._meta["error"] = "未配置自选股"
                self._meta["version"] += 1
                self._meta["names"] = dict(self._names)
            self._save_names()
            return

        providers: List[object] = []
        if self._provider in PROVIDERS:
            providers = [PROVIDERS[self._provider](self._timeout)]
        else:
            providers = [TencentProvider(self._timeout), SinaProvider(self._timeout)]

        quotes: Optional[Dict[str, Quote]] = None
        error = ""
        used = None
        for provider in providers:
            try:
                quotes = provider.fetch_quotes(symbols)
                used = provider
                break
            except Exception as exc:
                error = f"{provider.label if hasattr(provider, 'label') else provider.name}: {exc}"

        if quotes:
            self._fill_names(quotes, symbols, providers, used)

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
            else:
                self._meta["error"] = error or "行情获取失败"
            for symbol in symbols:
                quote = self._quotes.get(symbol)
                if not quote:
                    continue
                if not quote.name:
                    quote.name = self._names.get(symbol, "")
                missing = bool(quotes) and symbol not in quotes
                quote.stale = (
                    quotes is None
                    or missing
                    or (now - quote.updated_at) > self._stale_after
                )
            self._meta["names"] = {
                symbol: (self._quotes.get(symbol).name if self._quotes.get(symbol) else "")
                          or self._names.get(symbol, "")
                for symbol in symbols
            }
            self._meta["version"] += 1
            current_error = self._meta["error"]

        self._save_names()

        if current_error and current_error != self._last_error:
            log("行情获取失败: " + current_error)
        self._last_error = current_error

        # 分时数据单独刷新（比报价慢），失败时保留旧曲线
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


def status_text(meta: dict) -> str:
    """主窗口/状态提示共用的一段人类可读状态。"""
    if meta.get("error"):
        return f"离线 · {meta['error']}"
    if not meta.get("last_ok"):
        return "正在获取行情…"
    provider = {"tencent": "腾讯", "sina": "新浪"}.get(meta.get("provider", ""), "—")
    age = max(0, int(time.time() - meta["last_ok"]))
    return f"{provider} · {age}s 前更新"
