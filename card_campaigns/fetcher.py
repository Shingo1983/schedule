"""キャンペーン一覧ページの取得と解析

定期実行はせず、ページを開いたときにキャッシュが古ければ裏で取り直す（オンデマンド更新）。
"""

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from . import store
from .sources import NEW_MEMBER_KEYWORDS, SOURCES, Source

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
CACHE_TTL_SEC = float(os.environ.get("CARDS_CACHE_HOURS", "6")) * 3600

_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "ja,en;q=0.8"}

_refresh_lock = threading.Lock()
_refreshing = threading.Event()


def today_jst() -> date:
    return datetime.now(JST).date()


# ---------------------------------------------------------------- 日付の抽出

_DATE_RE = re.compile(
    r"(?<!\d)(?:(20\d{2})\s*[年/.\-]\s*)?(\d{1,2})\s*[月/.]\s*(\d{1,2})(?!\s*[\d%％倍円万])\s*日?"
)


def extract_dates(text: str, base: date) -> list[date]:
    """文中の日付を出現順に返す。年が省略された日付は直前の年、なければ base の年で補う。"""
    out: list[date] = []
    last_year: int | None = None
    for m in _DATE_RE.finditer(text):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        year = int(y) if y else (last_year or base.year)
        try:
            dt = date(year, mo, d)
        except ValueError:
            continue
        if not y and not last_year and dt < base - timedelta(days=180):
            dt = date(year + 1, mo, d)
        last_year = dt.year
        out.append(dt)
    return out


def guess_deadline(text: str, base: date) -> date | None:
    """期間表記の最後の日付を締切とみなす。"""
    dates = extract_dates(text, base)
    return max(dates) if dates else None


# ---------------------------------------------------------------- 解析

def _normalize_url(href: str, page_url: str) -> str:
    u = urlparse(urljoin(page_url, href))
    return urlunparse((u.scheme, u.netloc, u.path, "", u.query, ""))


def _context_text(a) -> str:
    """リンクの周辺テキスト（カード型レイアウトの親要素）を返す。"""
    own = a.get_text(" ", strip=True)
    node = a
    best = own
    for _ in range(4):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        txt = node.get_text(" ", strip=True)
        if len(txt) > 600 or len(node.find_all("a")) > 3:
            break
        best = txt
    return best


def _title_of(a) -> str:
    for attr in ("title", "aria-label"):
        if a.get(attr):
            return a[attr].strip()
    for tag in ("h2", "h3", "h4", "p", "span"):
        el = a.find(tag)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    txt = a.get_text(" ", strip=True)
    if txt:
        return txt
    img = a.find("img", alt=True)
    return img["alt"].strip() if img else ""


def is_new_member(text: str) -> bool:
    return any(k in text for k in NEW_MEMBER_KEYWORDS)


def parse_campaigns(html: str, page_url: str, src: Source, base: date) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    link_re = re.compile(src.link_pattern)
    excl_re = re.compile(src.exclude_href_pattern) if src.exclude_href_pattern else None
    page_norm = _normalize_url(page_url, page_url)
    host = urlparse(page_url).netloc.split(".")[-2:]

    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        url = _normalize_url(a["href"], page_url)
        if url == page_norm or not link_re.search(url):
            continue
        if urlparse(url).netloc.split(".")[-2:] != host:
            continue
        if excl_re and excl_re.search(url):
            continue
        title = re.sub(r"\s+", " ", _title_of(a))[:160]
        if len(title) < 4:
            continue
        ctx = _context_text(a)
        deadline = guess_deadline(ctx, base)
        item = found.get(url)
        if item and len(item["title"]) >= len(title):
            continue
        found[url] = {
            "title": title,
            "url": url,
            "summary": ctx[:240] if ctx != title else "",
            "deadline": deadline.isoformat() if deadline else None,
            "new_member": is_new_member(title + " " + ctx),
            "entry_required": ("エントリー" in ctx) or ("要登録" in ctx) or ("要エントリー" in ctx),
        }
    return list(found.values())


# ---------------------------------------------------------------- 取得

def _fetch_requests(url: str) -> str:
    r = requests.get(url, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
    return r.text


def _fetch_browser(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=_UA, locale="ja-JP",
                                    viewport={"width": 390, "height": 844})
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            # 遅延読み込み対策で最下部までスクロール
            for _ in range(6):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(400)
            return page.content()
        finally:
            browser.close()


def fetch_source(src: Source) -> dict:
    """1つの取得元を取得・解析する。失敗時も例外を投げず error を返す。"""
    base = today_jst()
    errors: list[str] = []
    for url in src.list_urls:
        methods = [("requests", _fetch_requests)]
        if src.needs_browser:
            methods.append(("browser", _fetch_browser))
        for method_name, fn in methods:
            try:
                html = fn(url)
            except Exception as e:  # noqa: BLE001 - 取得失敗は画面に出す
                errors.append(f"{url} [{method_name}] {type(e).__name__}: {e}"[:300])
                continue
            items = parse_campaigns(html, url, src, base)
            if items:
                return {"ok": True, "items": items, "used_url": url,
                        "method": method_name, "error": None}
            errors.append(f"{url} [{method_name}] 一覧からキャンペーンを検出できませんでした")
    return {"ok": False, "items": [], "used_url": None, "method": None,
            "error": " / ".join(errors) or "取得URL未設定"}


def _refresh_all() -> None:
    try:
        targets = [s for s in SOURCES if s.kind == "scrape"]
        # ブラウザ取得はメモリを使うので並列数を抑える
        with ThreadPoolExecutor(max_workers=2) as ex:
            results = dict(zip([s.id for s in targets], ex.map(fetch_source, targets)))
        now = time.time()
        old = store.load_cache()
        for sid, res in results.items():
            if not res["ok"] and old.get(sid, {}).get("items"):
                # 取得失敗時は前回の成功結果を残し、エラーだけ差し替える
                prev = old[sid]
                prev["error"] = res["error"]
                prev["stale"] = True
                store.save_cache(sid, prev, prev.get("fetched_at", now))
            else:
                store.save_cache(sid, res, now)
    except Exception:
        logger.exception("card campaign refresh failed")
    finally:
        _refreshing.clear()


def is_refreshing() -> bool:
    return _refreshing.is_set()


def ensure_fresh(force: bool = False) -> bool:
    """キャッシュが古ければバックグラウンドで更新を開始する。開始したら True。"""
    with _refresh_lock:
        if _refreshing.is_set():
            return True
        cache = store.load_cache()
        targets = [s.id for s in SOURCES if s.kind == "scrape"]
        oldest = min((cache.get(t, {}).get("fetched_at", 0) for t in targets), default=0)
        if not force and time.time() - oldest < CACHE_TTL_SEC:
            return False
        _refreshing.set()
        threading.Thread(target=_refresh_all, daemon=True).start()
        return True
