"""各ショップサイトのスクレイパー

JALマイレージパーク提携ショップの検索ページをスクレイピングし、
商品の最安値を取得する。

戦略:
Phase 1: cloudscraper + BeautifulSoup（高速、bot検出回避）
  1. CSSセレクタでHTML要素から価格を取得
  2. JSON-LD構造化データから価格を取得
  3. ページ内のJSON（__NEXT_DATA__等）から価格を取得
Phase 2: Playwright ブラウザレンダリング（Phase 1で失敗したショップのみ）
  JavaScript描画のSPAサイトでも価格を取得可能
"""

import json
import re
import logging
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from .config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# cloudscraper（Cloudflare/bot検出回避）: オプショナル
# ---------------------------------------------------------------------------
try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
    logger.info("cloudscraper available - anti-bot bypass enabled")
except ImportError:
    _HAS_CLOUDSCRAPER = False
    logger.info("cloudscraper not installed - using standard requests")

# ---------------------------------------------------------------------------
# Playwright（ブラウザレンダリング）: オプショナル
# ---------------------------------------------------------------------------
_HAS_PLAYWRIGHT = False
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
    logger.info("playwright available - browser rendering enabled")
except ImportError:
    logger.info("playwright not installed - browser rendering disabled")

# 共通ヘッダー（最新Chromeを完全模倣 - bot検出回避）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
    "DNT": "1",
}

# JSON API用ヘッダー
_JSON_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

# リクエストタイムアウト（秒）
_TIMEOUT = 25

# 並列実行のワーカー数（多すぎるとbot検出される → 5に制限）
_MAX_WORKERS = 5


@dataclass
class ShopPrice:
    """ショップでの検索結果（最安値1件）"""

    shop_name: str
    price: int | None  # None = 取得失敗 or 商品なし
    product_name: str
    product_url: str
    search_url: str  # ユーザーが手動で確認できるURL
    error: str | None = None  # エラーメッセージ


def _new_session() -> requests.Session:
    """毎回新しいHTTPセッションを作成
    cloudscraper利用可能時はCloudflare/bot検出を自動回避
    """
    if _HAS_CLOUDSCRAPER:
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
        )
        # cloudscraper のUser-Agentを維持（TLSフィンガープリントと一致させるため）
        # UA以外のヘッダーのみ追加
        extra = {k: v for k, v in _HEADERS.items() if k.lower() != "user-agent"}
        session.headers.update(extra)
    else:
        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(_HEADERS)
    return session


def _fetch(url: str, headers: dict | None = None, **kwargs) -> requests.Response:
    """HTTP GETリクエスト（cloudscraper対応）"""
    session = _new_session()
    if headers:
        session.headers.update(headers)
    # Refererを自動設定（bot検出対策）
    if "Referer" not in (headers or {}):
        parsed = urlparse(url)
        session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return session.get(url, timeout=_TIMEOUT, **kwargs)


def _fetch_json(url: str, params: dict | None = None, **kwargs) -> requests.Response:
    """JSON API用のGETリクエスト"""
    session = _new_session()
    session.headers.update(_JSON_HEADERS)
    return session.get(url, params=params, timeout=_TIMEOUT, **kwargs)


def _parse_price(text: str) -> int | None:
    """価格テキストから最初の価格数値を抽出: '¥12,345' → 12345

    改善点:
    - テキスト全体の数字を結合するのではなく、最初の価格パターンのみ抽出
    - 「ポイント」「件」「%」等の非価格数値を除外
    - 「送料」の数値を除外
    """
    if not text:
        return None

    # ポイント・件数・パーセントなどの非価格テキストを除外
    # 「1,234ポイント」「100件」「50%OFF」等のパターンを先に除去
    cleaned = re.sub(r'[\d,]+\s*(?:ポイント|ﾎﾟｲﾝﾄ|point|pts?|件|%|％|倍)', '', text, flags=re.IGNORECASE)
    # 送料関連を除去: 「送料XXX円」「送料無料」
    cleaned = re.sub(r'送料\s*[\d,]*\s*円?', '', cleaned)
    # 「〜」以降を除去（価格範囲の上限を拾わないため）
    cleaned = re.sub(r'[〜~～].+', '', cleaned)

    # 価格パターン: ¥マーク付き or 数字+円 or カンマ区切り数字
    # 最初にマッチしたものだけを使う
    price_patterns = [
        r'[¥￥]\s*([\d,]+)',           # ¥12,345
        r'([\d,]+)\s*円',              # 12,345円
        r'(?:税込|税抜|価格|特価|販売価格|通常価格)[^\d]*([\d,]+)',  # 価格: 12,345
        r'(?:^|[^\d])([\d]{1,3}(?:,\d{3})+)(?:[^\d]|$)',  # カンマ区切り: 12,345 or 1,234,567
        r'(?:^|[^\d])([\d]{3,7})(?:[^\d]|$)',  # 3〜7桁の数字（100〜9,999,999）
    ]

    for pattern in price_patterns:
        m = re.search(pattern, cleaned)
        if m:
            digits = re.sub(r'[^\d]', '', m.group(1))
            if digits:
                val = int(digits)
                if 100 <= val <= 99_999_999:  # 100円〜約1億円の範囲
                    return val

    return None


def _soup(resp: requests.Response) -> BeautifulSoup:
    """レスポンスからBeautifulSoupオブジェクトを作成"""
    return BeautifulSoup(resp.text, "lxml")


def _is_relevant_product(query: str, product_name: str) -> bool:
    """商品名が検索クエリと関連しているかチェック。

    「airpods pro 3」で検索 → 「AirPods Pro 第3世代」はOK、「化粧水」はNG
    検索語のうち少なくとも1つの重要語が商品名に含まれていること。
    """
    if not product_name:
        return False  # 商品名なし = 関連性判定不能 → 不採用

    query_lower = query.lower()
    name_lower = product_name.lower()

    # 1文字の語やストップワードを除外
    stop_words = {"the", "a", "an", "and", "or", "in", "on", "at", "to", "for",
                  "no", "の", "に", "を", "は", "が", "と", "で", "も", "から", "まで",
                  "pro", "max", "plus", "mini", "lite"}
    # クエリを単語分割
    words = re.split(r'[\s　/／\-]+', query_lower)
    # 重要な語（2文字以上でストップワードでないもの）
    keywords = [w for w in words if len(w) >= 2 and w not in stop_words]

    if not keywords:
        # すべてストップワード → 元の語で判定
        keywords = [w for w in words if len(w) >= 1]

    # キーワードのうち少なくとも1つが商品名に含まれること
    for kw in keywords:
        if kw in name_lower:
            return True

    return False


def _is_bot_blocked_page(soup: BeautifulSoup) -> bool:
    """bot検出/アクセス制限ページかどうかを判定"""
    text = soup.get_text()
    block_patterns = [
        r'一時的なアクセス増加',
        r'一時的に.*アクセス.*制限',
        r'アクセスが集中',
        r'しばらく.*お待ち',
        r'混雑.*しております',
        r'アクセス制限',
        r'ただいまメンテナンス',
        r'メンテナンス中',
        r'Access\s+Denied',
        r'Bot\s+Protection',
        r'Please\s+verify.*human',
        r'captcha',
        r'Checking your browser',
        r'Just a moment',
        r'Enable JavaScript and cookies',
    ]
    for pattern in block_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# 共通抽出関数
# ---------------------------------------------------------------------------

def _extract_jsonld_prices(soup: BeautifulSoup) -> list[dict]:
    """JSON-LD構造化データから商品情報を抽出する。
    SPAサイトでもSEO用にJSON-LDが埋め込まれていることが多い。

    改善: Product/Offer型のみ対象（BreadcrumbList、Organization等を除外）
    Returns: [{"name": str, "price": int, "url": str}, ...]
    """
    results = []
    # 商品に関連するJSON-LDの@type
    _PRODUCT_TYPES = {"Product", "Offer", "AggregateOffer", "IndividualProduct"}

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            dtype = data.get("@type", "")
            if dtype == "ItemList":
                items = data.get("itemListElement", [])
            elif dtype in _PRODUCT_TYPES:
                items = [data]
            elif "mainEntity" in data:
                me = data["mainEntity"]
                items = me if isinstance(me, list) else [me]
            elif dtype in ("WebPage", "SearchResultsPage"):
                # WebPageの場合、mainEntityや関連プロパティを探す
                for key in ("mainEntity", "about", "mentions"):
                    val = data.get(key)
                    if val:
                        items = val if isinstance(val, list) else [val]
                        break
            # BreadcrumbList, Organization, WebSite 等は無視

        for item in items:
            if isinstance(item, dict) and item.get("@type") == "ListItem":
                item = item.get("item", item)

            if not isinstance(item, dict):
                continue

            # Product/Offer型でない場合はスキップ（商品以外のJSON-LDを除外）
            item_type = item.get("@type", "")
            if item_type and item_type not in _PRODUCT_TYPES and item_type != "ListItem":
                # ただしoffersを持っていれば商品の可能性がある
                if "offers" not in item:
                    continue

            name = item.get("name", "")
            url = item.get("url", "")
            price = None

            # Product > offers > price
            offers = item.get("offers", {})
            if isinstance(offers, list):
                prices = []
                for o in offers:
                    p = o.get("price") or o.get("lowPrice")
                    if p is not None:
                        parsed = _parse_price(str(p))
                        if parsed:
                            prices.append(parsed)
                if prices:
                    price = min(prices)  # 最安値を採用
            elif isinstance(offers, dict):
                p = offers.get("price") or offers.get("lowPrice")
                if p is not None:
                    price = _parse_price(str(p))

            # 直接priceがある場合
            if not price and item.get("price") is not None:
                price = _parse_price(str(item["price"]))

            if price and name:  # 名前がないものは除外
                results.append({"name": name, "price": price, "url": url})

    return results


def _extract_embedded_json(html: str) -> list[dict]:
    """ページ内の埋め込みJSON（__NEXT_DATA__, __INITIAL_STATE__等）から商品価格を抽出"""
    results = []

    patterns = [
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.+?})\s*</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.+?});\s*</script>',
        r'window\.__PRELOADED_STATE__\s*=\s*({.+?});\s*</script>',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            _find_prices_in_dict(data, results, depth=0)
        except (json.JSONDecodeError, RecursionError):
            continue

    return results


def _find_prices_in_dict(obj, results: list, depth: int = 0):
    """再帰的にJSONオブジェクト内の商品（price + name）を探す

    改善:
    - priceが数値型であることを確認（文字列のIDなどを除外）
    - nameが十分な長さであることを確認
    - 最大結果数を制限
    """
    if depth > 8 or len(results) >= 20:
        return
    if isinstance(obj, dict):
        has_price = "price" in obj or "salePrice" in obj or "itemPrice" in obj
        has_name = "name" in obj or "itemName" in obj or "title" in obj or "productName" in obj
        if has_price and has_name:
            raw_price = obj.get("salePrice") or obj.get("price") or obj.get("itemPrice")
            if raw_price is not None:
                # 数値型チェック: 文字列の場合は数字のみの文字列であること
                if isinstance(raw_price, (int, float)):
                    price = int(raw_price) if raw_price >= 100 else None
                elif isinstance(raw_price, str):
                    price = _parse_price(raw_price)
                else:
                    price = None

                if price and 100 <= price <= 99_999_999:
                    name = obj.get("name") or obj.get("itemName") or obj.get("title") or obj.get("productName", "")
                    name = str(name).strip()
                    if len(name) >= 2:  # 名前が短すぎるものは除外
                        url = obj.get("url") or obj.get("itemUrl") or obj.get("productUrl", "")
                        results.append({"name": name, "price": price, "url": str(url)})
        for v in obj.values():
            _find_prices_in_dict(v, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:30]:
            _find_prices_in_dict(item, results, depth + 1)


def _is_no_results_page(soup: BeautifulSoup) -> bool:
    """検索結果が0件のページかどうかを判定"""
    text = soup.get_text()
    no_results_patterns = [
        r'(?<!\d)0\s*件中\s*0',
        r'件中\s*0\s*～\s*0\s*件',
        r'(?<!\d)0\s*件\s*～\s*0\s*件.*?表示',
        r'該当する商品.*?(?:ございません|みつかりません|見つかりません|ありません)',
        r'みつかりませんでした',
        r'見つかりませんでした',
        r'一致する.*?(?:ございません|ありません|見つかりません)',
        r'商品が見つかりません',
        r'検索結果.*?ありません',
        r'お探しの.*?見つかりません',
        r'該当.*?(?<!\d)0\s*件',
        r'検索結果\s*[:：]?\s*(?<!\d)0\s*件',
        r'(?<!\d)0\s*件の商品',
        r'(?<!\d)0\s*件の検索結果',
        r'no\s+results?\s+found',
        r'(?<!\d)0\s+items?\s+found',
        r'ヒットしませんでした',
        r'条件に合う商品.*?(?:ございません|ありません)',
    ]
    for pattern in no_results_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _extract_price_from_element(el) -> int | None:
    """価格要素からできるだけ正確に価格を抽出する。

    戦略:
    1. 価格専用の子要素（.a-price-whole等）があればそれだけ使う
    2. なければ要素の直接テキストのみ使う（子要素のポイント等を除外）
    3. 最終的にget_text()でフォールバック
    """
    # 価格専用の子要素を試す
    price_children = el.select('.a-price-whole, [class*="price-value"], [class*="priceValue"]')
    for child in price_children:
        price = _parse_price(child.get_text())
        if price:
            return price

    # 要素の直接テキストのみ使う（子要素のテキストを含めない）
    # これにより「¥12,345 (1,234ポイント)」のような場合にポイント部分を含めない
    direct_text = ""
    for child in el.children:
        if isinstance(child, str):
            direct_text += child
    if direct_text.strip():
        price = _parse_price(direct_text)
        if price:
            return price

    # フォールバック: 全テキスト（改善した_parse_priceが非価格を除外する）
    return _parse_price(el.get_text())


def _find_price_in_soup(soup: BeautifulSoup, selectors: list[tuple[str, str, str]],
                        base_url: str = "", query: str = "") -> tuple[int | None, str, str]:
    """複数のCSSセレクタパターンで商品を探す。(price, name, url)を返す

    戦略:
    1. CSSセレクタでHTML要素から抽出
    2. JSON-LD構造化データ
    3. 埋め込みJSON（__NEXT_DATA__等）
    ※正規表現フォールバックは廃止（無関係な価格を誤検出する原因のため）

    queryが指定されている場合、各段階で関連性チェックを行い、
    無関係な商品（「airpods pro 3」検索で化粧水等）を除外する。
    """
    # 0. 検索結果なしページを先にチェック
    if _is_no_results_page(soup):
        return None, "", ""

    def _extract_url(name_el, item) -> str:
        """商品URLを抽出するヘルパー"""
        url = ""
        if name_el and name_el.get("href"):
            href = name_el["href"]
            if href.startswith("http"):
                url = href
            elif href.startswith("/") and base_url:
                url = f"{base_url}{href}"
            elif base_url:
                url = f"{base_url}/{href}"
        if not url:
            link = item.select_one("a[href]")
            if link:
                href = link["href"]
                if href.startswith("http"):
                    url = href
                elif href.startswith("/") and base_url:
                    url = f"{base_url}{href}"
        return url

    # 1. CSSセレクタで探す
    for item_sel, price_sel, name_sel in selectors:
        items = soup.select(item_sel)
        if not items:
            continue
        for item in items[:10]:  # 最初の10件だけチェック
            price_el = item.select_one(price_sel)
            if not price_el:
                continue
            price = _extract_price_from_element(price_el)
            if not price:
                continue

            name_el = item.select_one(name_sel)
            name = name_el.get_text(strip=True) if name_el else ""

            # 関連性チェック（queryが指定されている場合）
            if query and name and not _is_relevant_product(query, name):
                continue  # 次の商品を試す

            url = _extract_url(name_el, item)
            return price, name, url

    # 2. JSON-LD構造化データから探す
    jsonld_items = _extract_jsonld_prices(soup)
    if jsonld_items:
        # 関連性でフィルタしてから最安値
        if query:
            jsonld_items = [i for i in jsonld_items
                           if not i["name"] or _is_relevant_product(query, i["name"])]
        if jsonld_items:
            cheapest = min(jsonld_items, key=lambda x: x["price"])
            return cheapest["price"], cheapest["name"], cheapest["url"]

    # 3. 埋め込みJSONから探す
    embedded = _extract_embedded_json(str(soup))
    if embedded:
        if query:
            embedded = [i for i in embedded
                        if not i["name"] or _is_relevant_product(query, i["name"])]
        if embedded:
            cheapest = min(embedded, key=lambda x: x["price"])
            return cheapest["price"], cheapest["name"], cheapest["url"]

    return None, "", ""


def _make_error_result(shop_name: str, search_url: str, error: str) -> ShopPrice:
    """エラー結果を生成（共通ヘルパー）"""
    return ShopPrice(shop_name, None, "", "", search_url, error=error)


def _scrape_generic(shop_name: str, search_url: str,
                    selectors: list[tuple[str, str, str]],
                    base_url: str,
                    headers: dict | None = None,
                    query: str = "") -> ShopPrice:
    """汎用スクレイパー: HTML取得→セレクタ→JSON-LD→埋め込みJSONの順で試行

    改善:
    - bot検出ページを判定して適切なエラーメッセージ
    - 検索クエリとの関連性チェック（無関係な商品を除外）
    - bot検出時は1回リトライ（3秒待機）
    """
    max_attempts = 2

    for attempt in range(max_attempts):
        try:
            # リトライ時は待機
            if attempt > 0:
                time.sleep(3 + random.uniform(0, 2))

            resp = _fetch(search_url, headers=headers)
            if resp.status_code == 403:
                if attempt < max_attempts - 1:
                    continue  # リトライ
                return _make_error_result(shop_name, search_url, "アクセス制限（手動で検索してください）")
            if resp.status_code == 503:
                if attempt < max_attempts - 1:
                    continue  # リトライ
                return _make_error_result(shop_name, search_url, "サーバー混雑（手動で検索してください）")
            if resp.status_code != 200:
                return _make_error_result(shop_name, search_url, f"HTTP {resp.status_code}")

            soup = _soup(resp)

            # bot検出ページの判定
            if _is_bot_blocked_page(soup):
                if attempt < max_attempts - 1:
                    logger.info("Bot blocked for %s, retrying...", shop_name)
                    continue  # リトライ
                return _make_error_result(shop_name, search_url, "アクセス制限（手動で検索してください）")

            price, name, url = _find_price_in_soup(soup, selectors, base_url, query=query)
            if price:
                return ShopPrice(shop_name, price, name, url, search_url)

            return ShopPrice(shop_name, None, "", "", search_url)

        except requests.exceptions.SSLError:
            return _make_error_result(shop_name, search_url, "SSL接続エラー（手動で検索してください）")
        except requests.exceptions.ConnectionError:
            if attempt < max_attempts - 1:
                continue  # リトライ
            return _make_error_result(shop_name, search_url, "接続エラー（手動で検索してください）")
        except requests.exceptions.Timeout:
            if attempt < max_attempts - 1:
                continue  # リトライ
            return _make_error_result(shop_name, search_url, "タイムアウト（手動で検索してください）")
        except Exception as e:
            logger.warning("scrape error for %s: %s", shop_name, e)
            return _make_error_result(shop_name, search_url, "取得失敗（手動で検索してください）")

    return _make_error_result(shop_name, search_url, "取得失敗（手動で検索してください）")


# ============================================================
# 楽天市場 (API + Webスクレイピングフォールバック)
# ============================================================
def _search_rakuten_api(query: str, config: Config, search_url: str) -> ShopPrice | None:
    """楽天API検索（APIキー設定済みの場合のみ）。成功時はShopPrice、失敗時はNone"""
    if not config.rakuten_app_id:
        return None

    api_url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    params = {
        "applicationId": config.rakuten_app_id,
        "keyword": query,
        "hits": 5,
        "sort": "+itemPrice",
        "availability": 1,
    }

    try:
        resp = requests.get(api_url, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None  # APIエラー → Webスクレイピングにフォールバック

        data = resp.json()
        items = data.get("Items", [])
        if not items:
            return ShopPrice("楽天市場", None, "", "", search_url)

        item = items[0].get("Item", {})
        return ShopPrice(
            shop_name="楽天市場",
            price=item.get("itemPrice", 0),
            product_name=item.get("itemName", ""),
            product_url=item.get("itemUrl", ""),
            search_url=search_url,
        )
    except Exception:
        return None  # フォールバック


def search_rakuten(query: str, config: Config) -> ShopPrice:
    search_url = f"https://search.rakuten.co.jp/search/mall/{quote(query)}/"

    # 1. APIキーがあればAPIを試行
    api_result = _search_rakuten_api(query, config, search_url)
    if api_result is not None:
        return api_result

    # 2. Webスクレイピングフォールバック（APIキー不要）
    selectors = [
        (".searchresultitem", ".important", ".title a"),
        ('[class*="dui-card"]', '[class*="price"]', '[class*="title"] a'),
        ('[class*="product"]', '[class*="price"]', '[class*="title"] a, [class*="name"] a'),
        (".item", ".price", "a.title, .name a"),
    ]
    return _scrape_generic("楽天市場", search_url, selectors,
                           "https://search.rakuten.co.jp", query=query)


# ============================================================
# Yahoo!ショッピング (API)
# ============================================================
def _search_yahoo_api(query: str, config: Config, search_url: str) -> ShopPrice | None:
    """Yahoo API検索（APIキー設定済みの場合のみ）。成功時はShopPrice、失敗時はNone"""
    if not config.yahoo_app_id:
        return None

    api_url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    params = {
        "appid": config.yahoo_app_id,
        "query": query,
        "results": 5,
        "sort": "+price",
        "in_stock": "true",
    }

    try:
        resp = requests.get(api_url, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None  # APIエラー → Webスクレイピングにフォールバック

        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return ShopPrice("Yahoo!ショッピング", None, "", "", search_url)

        hit = hits[0]
        return ShopPrice(
            shop_name="Yahoo!ショッピング",
            price=int(hit.get("price", 0)),
            product_name=hit.get("name", ""),
            product_url=hit.get("url", ""),
            search_url=search_url,
        )
    except Exception:
        return None  # フォールバック


def search_yahoo(query: str, config: Config) -> ShopPrice:
    search_url = f"https://shopping.yahoo.co.jp/search?p={quote(query)}&ss_first=1&ts=1&mcr=on&used=0&stipIcon=1&elc=1&cid=&brandId=&oid=&ran_cr=&ran_sh=&X=2&di=&sc_i=shp_pc_search_searchBox_2"

    # 1. APIキーがあればAPIを試行
    api_result = _search_yahoo_api(query, config, search_url)
    if api_result is not None:
        return api_result

    # 2. Webスクレイピングフォールバック（APIキー不要）
    selectors = [
        # Yahoo!ショッピング検索結果ページのセレクタ
        ('[class*="SearchResult"] [class*="Product"]',
         '[class*="Product__price"], [class*="Price__value"]',
         '[class*="Product__title"] a, [class*="Product__name"] a'),
        (".mdSearchProduct", ".elPriceValue, .mdSearchProduct__price",
         ".mdSearchProduct__title a, .elProductName a"),
        ('[data-cl-params*="product"]',
         '[class*="price"], [class*="Price"]',
         'a[class*="title"], a[class*="Title"], a[class*="name"]'),
        ('[class*="ProductItem"], [class*="productItem"]',
         '[class*="price"], [class*="Price"]',
         '[class*="title"] a, [class*="name"] a'),
        (".product", ".price", ".product-name a, .title a"),
    ]
    return _scrape_generic("Yahoo!ショッピング", search_url, selectors,
                           "https://shopping.yahoo.co.jp", query=query)


# ============================================================
# Amazon.co.jp (スクレイピング)
# ============================================================
def search_amazon(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.amazon.co.jp/s?k={quote(query)}&s=price-asc-rank"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return _make_error_result("Amazon.co.jp", search_url, f"HTTP {resp.status_code}")

        soup = _soup(resp)

        for result in soup.select('[data-component-type="s-search-result"]'):
            sponsored = result.select_one('.s-label-popover-default')
            if sponsored and 'スポンサー' in sponsored.get_text():
                continue

            price_whole = result.select_one(".a-price .a-price-whole")
            if not price_whole:
                continue
            price = _parse_price(price_whole.get_text())
            if not price:
                continue

            title_el = result.select_one("h2 a span")
            name = title_el.get_text(strip=True) if title_el else ""

            link_el = result.select_one("h2 a")
            url = ""
            if link_el and link_el.get("href"):
                href = link_el["href"]
                url = f"https://www.amazon.co.jp{href}" if href.startswith("/") else href

            return ShopPrice("Amazon.co.jp", price, name, url, search_url)

        return ShopPrice("Amazon.co.jp", None, "", "", search_url)

    except Exception as e:
        return _make_error_result("Amazon.co.jp", search_url, "取得失敗（手動で検索してください）")


# ============================================================
# ビックカメラ.com (スクレイピング)
# ============================================================
def search_biccamera(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.biccamera.com/bc/category/?q={quote(query)}&rowPerPage=25&sort=PRICE_ASC"
    selectors = [
        (".bcs_listItem", ".bcs_price", ".bcs_title a"),
        (".prod_box", ".val", ".prod_name a"),
        (".bcs_item", ".bcs_price .val", ".bcs_title a"),
        (".product_list_item", ".price", ".product_name a"),
        ("li.prod_item", ".prod_price", ".prod_name a"),
    ]
    return _scrape_generic("ビックカメラ.com", search_url, selectors,
                           "https://www.biccamera.com",
                           headers={
                               "Referer": "https://www.biccamera.com/",
                               "Sec-Fetch-Site": "same-origin",
                           }, query=query)


# ============================================================
# コジマネット (スクレイピング)
# ============================================================
def search_kojima(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.kojima.net/ec/prod_list.html?keyword={quote(query)}&sort=price&order=asc"
    selectors = [
        (".product-list-item", ".price, .itemPrice, .product-price", ".product-name a, .itemName a"),
        (".itemBox", ".itemPrice", ".itemName a"),
        (".product_item", ".product-price", ".product_name a"),
        ("li.item", ".price", "a.item-name, .name a"),
        (".goods_list li", ".price", ".goods_name a"),
    ]
    return _scrape_generic("コジマネット", search_url, selectors,
                           "https://www.kojima.net", query=query)


# ============================================================
# ヤマダウェブコム (スクレイピング)
# ============================================================
def search_yamada(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.yamada-denkiweb.com/search?keyword={quote(query)}&searchtarget=1&sorttype=price_asc"
    selectors = [
        (".searchResult__item", ".searchResult__price, .pPrice", ".searchResult__name a, .pName a"),
        (".product", ".price, .product-price", ".product-name a"),
        (".item", ".pPrice", ".pName a"),
        ('[class*="c-product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ("li.product-item", ".price-box .price", ".product-item-link"),
    ]
    return _scrape_generic("ヤマダウェブコム", search_url, selectors,
                           "https://www.yamada-denkiweb.com",
                           headers={"Referer": "https://www.yamada-denkiweb.com/"},
                           query=query)


# ============================================================
# Joshin webショップ (スクレイピング)
# ============================================================
def search_joshin(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://joshinweb.jp/servlet/emall.odr_wp?SHP=0&KW={quote(query)}&SORT=PRICE_LO"
    selectors = [
        (".productList__item", ".productList__price", ".productList__name a"),
        (".lineup_box", ".lineup_price", ".lineup_name a"),
        (".item", ".price", ".item-name a, .name a"),
        ("li.product-item", ".price-box .price", ".product-item-link"),
    ]
    return _scrape_generic("Joshin webショップ", search_url, selectors,
                           "https://joshinweb.jp", query=query)


# ============================================================
# au PAY マーケット (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_aupay(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://wowma.jp/itemlist?e_scope=O&at=FP&non_gr=ex&keyword={quote(query)}&categ_id=0&sort_type=priceasc"
    selectors = [
        (".itemList__item", ".itemList__price, .price", ".itemList__name a, .product-name a"),
        (".product-item", ".product-price, .price", ".product-name a"),
        ('[class*="ItemCard"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        (".item", ".price", "a.item-name"),
    ]
    return _scrape_generic("au PAY マーケット", search_url, selectors,
                           "https://wowma.jp", query=query)


# ============================================================
# セブンネットショッピング (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_seven(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://7net.omni7.jp/search/?keyword={quote(query)}&searchKeywordFlg=1"
    selectors = [
        (".productItem", ".productPrice, .price", ".productName a, .product-name a"),
        (".product", ".price, .productPrice", ".productName a"),
        (".item", ".price, .item-price", ".item-name a, .productName a"),
        ('[class*="product"]', '[class*="price"]', '[class*="name"] a'),
    ]
    return _scrape_generic("セブンネットショッピング", search_url, selectors,
                           "https://7net.omni7.jp", query=query)


# ============================================================
# Qoo10 (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_qoo10(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.qoo10.jp/s/{quote(query)}?sort=prc"
    selectors = [
        (".sc-prd", ".prc .prc-dc, .prc", ".tit a, .sbj a"),
        (".item_g", ".price, .prc", ".sbj a"),
        ('[class*="product"]', '[class*="price"]', '[class*="title"] a, [class*="name"] a'),
        (".goods_item", ".price", ".goods_name a, .title a"),
    ]
    return _scrape_generic("Qoo10", search_url, selectors,
                           "https://www.qoo10.jp", query=query)


# ============================================================
# エディオンネットショップ (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_edion(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.edion.com/detail_search.html?q={quote(query)}&sort=price_asc"
    selectors = [
        (".product-item, .item-list__item", ".price, .item-price", ".product-name a, .item-name a"),
        ('[class*="product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ('[class*="item"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        (".searchResultItem", ".resultPrice", ".resultName a"),
    ]
    return _scrape_generic("エディオンネットショップ", search_url, selectors,
                           "https://www.edion.com", query=query)


# ============================================================
# 汎用スクレイパー生成ファクトリ
# ============================================================

# 多くのECサイトで使える汎用CSSセレクタ
_GENERIC_SELECTORS: list[tuple[str, str, str]] = [
    ('[class*="product-item"], [class*="productItem"], [class*="ProductItem"]',
     '[class*="price"], [class*="Price"]',
     '[class*="name"] a, [class*="Name"] a, [class*="title"] a, [class*="Title"] a'),
    ('[class*="item-card"], [class*="itemCard"], [class*="ItemCard"]',
     '[class*="price"], [class*="Price"]',
     '[class*="name"] a, [class*="Name"] a, [class*="title"] a'),
    (".product", ".price", ".product-name a, .name a"),
    (".item", ".price", ".item-name a, .name a"),
    ("li.product-item", ".price-box .price", ".product-item-link"),
]


def _make_generic_scraper(
    shop_name: str,
    url_template: str,
    base_url: str,
    selectors: list[tuple[str, str, str]] | None = None,
    headers: dict | None = None,
):
    """汎用スクレイパー関数を生成するファクトリ"""

    def scraper(query: str, _config: Config) -> ShopPrice:
        search_url = url_template.replace("{query}", quote(query))
        sels = selectors or _GENERIC_SELECTORS
        return _scrape_generic(shop_name, search_url, sels, base_url,
                               headers=headers, query=query)

    scraper.__name__ = f"search_{shop_name}"
    return scraper


# ============================================================
# 追加ショップ（汎用スクレイパーで自動生成）
# ============================================================
search_uniqlo = _make_generic_scraper(
    "ユニクロオンラインストア",
    "https://www.uniqlo.com/jp/ja/search?q={query}",
    "https://www.uniqlo.com",
)

search_muji = _make_generic_scraper(
    "無印良品ネットストア",
    "https://www.muji.com/jp/ja/search?q={query}",
    "https://www.muji.com",
)

search_jalmall = _make_generic_scraper(
    "JAL Mall",
    "https://shop.jal.co.jp/products/list?keyword={query}",
    "https://shop.jal.co.jp",
    headers={"Sec-Fetch-Site": "same-origin", "Referer": "https://shop.jal.co.jp/"},
)

search_bellemaison = _make_generic_scraper(
    "ベルメゾンネット",
    "https://www.bellemaison.jp/search/?keyword={query}",
    "https://www.bellemaison.jp",
)

search_lohaco = _make_generic_scraper(
    "LOHACO",
    "https://lohaco.jp/search/?keyword={query}",
    "https://lohaco.jp",
)

search_nitori = _make_generic_scraper(
    "ニトリネット",
    "https://www.nitori-net.jp/ec/search/?q={query}",
    "https://www.nitori-net.jp",
)

search_zozo = _make_generic_scraper(
    "ZOZOTOWN",
    "https://zozo.jp/search/?p_keyv={query}",
    "https://zozo.jp",
)

search_dhc = _make_generic_scraper(
    "DHCオンラインショップ",
    "https://www.dhc.co.jp/goods/search.jsp?keyword={query}",
    "https://www.dhc.co.jp",
)

search_fancl = _make_generic_scraper(
    "ファンケルオンライン",
    "https://www.fancl.co.jp/search/?q={query}",
    "https://www.fancl.co.jp",
)

search_sony = _make_generic_scraper(
    "ソニーストア",
    "https://www.sony.jp/search/?q={query}",
    "https://www.sony.jp",
)

search_ksdenki = _make_generic_scraper(
    "ケーズデンキオンラインショップ",
    "https://www.ksdenki.com/shop/e/esearch/?keyword={query}",
    "https://www.ksdenki.com",
    headers={"Referer": "https://www.ksdenki.com/", "Sec-Fetch-Site": "same-origin"},
)

search_nojima = _make_generic_scraper(
    "ノジマオンライン",
    "https://online.nojima.co.jp/app/catalog/list/init?searchWord={query}",
    "https://online.nojima.co.jp",
)

search_matsukiyo = _make_generic_scraper(
    "マツモトキヨシオンラインストア",
    "https://www.matsukiyo.co.jp/store/online/search?text={query}",
    "https://www.matsukiyo.co.jp",
)

search_dshopping = _make_generic_scraper(
    "dショッピング",
    "https://shopping.dmkt-sp.jp/search/?keyword={query}",
    "https://shopping.dmkt-sp.jp",
)

search_buyma = _make_generic_scraper(
    "BUYMA",
    "https://www.buyma.com/r/-{query}/?sort=2",
    "https://www.buyma.com",
    selectors=[
        (".product_body", ".product_price .price", ".product_name a"),
        (".item_card", ".price", ".item_name a, .product_name a"),
        ('[class*="ProductCard"]', '[class*="price"], [class*="Price"]',
         '[class*="name"] a, [class*="title"] a'),
    ] + _GENERIC_SELECTORS,
)

search_abcmart = _make_generic_scraper(
    "ABC-MARTオンラインストア",
    "https://www.abc-mart.net/shop/goods/search.aspx?keyword={query}",
    "https://www.abc-mart.net",
)

search_gu = _make_generic_scraper(
    "GU オンラインストア",
    "https://www.gu-global.com/jp/ja/search?q={query}",
    "https://www.gu-global.com",
)

search_shopjapan = _make_generic_scraper(
    "ショップジャパン",
    "https://www.shopjapan.co.jp/search/?q={query}",
    "https://www.shopjapan.co.jp",
)

search_iherb = _make_generic_scraper(
    "iHerb",
    "https://jp.iherb.com/search?kw={query}",
    "https://jp.iherb.com",
)

search_cosme = _make_generic_scraper(
    "@cosme SHOPPING",
    "https://www.cosme.net/shopping/search/product/?keyword={query}",
    "https://www.cosme.net",
)


# ============================================================
# 全ショップ検索定義
# ============================================================

# (検索関数, JALショップ名) のリスト
# JALショップ名は jal_shops.py の KNOWN_SHOPS のキーと一致させること
SCRAPERS = [
    (search_rakuten, "楽天市場"),
    (search_yahoo, "Yahoo!ショッピング"),
    (search_amazon, "Amazon.co.jp"),
    (search_biccamera, "ビックカメラ.com"),
    (search_kojima, "コジマネット"),
    (search_yamada, "ヤマダウェブコム"),
    (search_joshin, "Joshin webショップ"),
    (search_aupay, "au PAY マーケット"),
    (search_seven, "セブンネットショッピング"),
    (search_qoo10, "Qoo10"),
    (search_edion, "エディオンネットショップ"),
    # 追加ショップ（汎用スクレイパー）
    (search_uniqlo, "ユニクロオンラインストア"),
    (search_muji, "無印良品ネットストア"),
    (search_jalmall, "JAL Mall"),
    (search_bellemaison, "ベルメゾンネット"),
    (search_lohaco, "LOHACO"),
    (search_nitori, "ニトリネット"),
    (search_zozo, "ZOZOTOWN"),
    (search_dhc, "DHCオンラインショップ"),
    (search_fancl, "ファンケルオンライン"),
    (search_sony, "ソニーストア"),
    (search_ksdenki, "ケーズデンキオンラインショップ"),
    (search_nojima, "ノジマオンライン"),
    (search_matsukiyo, "マツモトキヨシオンラインストア"),
    (search_dshopping, "dショッピング"),
    (search_buyma, "BUYMA"),
    (search_abcmart, "ABC-MARTオンラインストア"),
    (search_gu, "GU オンラインストア"),
    (search_shopjapan, "ショップジャパン"),
    (search_iherb, "iHerb"),
    (search_cosme, "@cosme SHOPPING"),
]

# スクレイパー対応済みショップ名のセット（自動生成）
SCRAPER_SHOP_NAMES = {name for _, name in SCRAPERS}


def get_manual_search_shops() -> list[str]:
    """スクレイパー未対応のKNOWN_SHOPSを自動的に返す（常にKNOWN_SHOPSと同期）"""
    from .jal_shops import KNOWN_SHOPS
    return [name for name in KNOWN_SHOPS if name not in SCRAPER_SHOP_NAMES]


def _get_selectors_for_shop(shop_name: str) -> list[tuple[str, str, str]]:
    """ショップ名から対応するCSSセレクタを取得"""
    for scraper_fn, name in SCRAPERS:
        if name == shop_name:
            # 各スクレイパーのselectorsを取得するため、ダミー呼び出しはせず
            # 汎用セレクタ＋拡張セレクタを返す
            break
    return _GENERIC_SELECTORS + [
        ('[class*="product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ('[class*="item"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ("li", '[class*="price"]', 'a'),
    ]


def _retry_with_browser(results: list[ShopPrice], query: str) -> None:
    """Phase 2: Playwright ブラウザレンダリングで失敗したショップを再試行

    Phase 1 (cloudscraper) で価格取得できなかったショップのみ対象。
    ブラウザでJavaScript描画を実行し、レンダリング後のHTMLから価格を抽出する。
    """
    if not _HAS_PLAYWRIGHT:
        return

    # 再試行対象: 価格なし & エラーなし（= HTMLは取れたがJSレンダリングが必要）
    # + エラーありでも接続エラー以外（403等はブラウザで回避できる可能性あり）
    retry_indices = []
    for i, r in enumerate(results):
        if r.price is not None:
            continue  # 既に価格取得済み
        if r.error and ("APIキー" in r.error or "タイムアウト" in r.error):
            continue  # API問題・タイムアウトはブラウザでも解決しない
        if r.search_url:
            retry_indices.append(i)

    if not retry_indices:
        return

    logger.info("Phase 2: Playwright browser retry for %d shops", len(retry_indices))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="ja-JP",
                viewport={"width": 1920, "height": 1080},
            )

            for idx in retry_indices:
                r = results[idx]
                try:
                    page = context.new_page()
                    page.goto(r.search_url, timeout=20000, wait_until="domcontentloaded")
                    # JS描画を待つ（networkidleは遅すぎるのでdomcontentloaded + 固定待機）
                    page.wait_for_timeout(3000)
                    html = page.content()
                    page.close()

                    soup = BeautifulSoup(html, "lxml")

                    # bot検出ページチェック
                    if _is_bot_blocked_page(soup):
                        logger.info("Browser retry: bot blocked for %s", r.shop_name)
                        results[idx] = _make_error_result(r.shop_name, r.search_url,
                                                          "アクセス制限（手動で検索してください）")
                        continue

                    selectors = _get_selectors_for_shop(r.shop_name)
                    base = f"{urlparse(r.search_url).scheme}://{urlparse(r.search_url).netloc}"
                    price, name, url = _find_price_in_soup(soup, selectors, base, query=query)
                    if price:
                        results[idx] = ShopPrice(r.shop_name, price, name, url, r.search_url)
                        logger.info("Browser retry success: %s = %d", r.shop_name, price)
                    else:
                        logger.info("Browser retry: no price found for %s", r.shop_name)

                except Exception as e:
                    logger.warning("Browser retry error for %s: %s", r.shop_name, e)
                    # 元の結果をそのまま保持

            context.close()
            browser.close()

    except Exception as e:
        err_msg = str(e)
        if "Executable doesn't exist" in err_msg or "browserType.launch" in err_msg:
            logger.warning(
                "Playwright browser not installed. Run: python -m playwright install chromium"
            )
        else:
            logger.error("Playwright browser failed: %s", e)


def _delayed_scrape(scraper_fn, query: str, config: Config, delay: float):
    """遅延付きスクレイパー実行（bot検出回避のためリクエストを分散）"""
    if delay > 0:
        time.sleep(delay)
    return scraper_fn(query, config)


def search_all_shops(query: str, config: Config) -> list[ShopPrice]:
    """全ショップを検索して結果を返す（2段階方式）

    Phase 1: cloudscraper + BeautifulSoup（並列、分散遅延付き）
    Phase 2: Playwright ブラウザ（Phase 1失敗分のみ、逐次）

    改善:
    - 同時接続数を5に制限（15→5、bot検出回避）
    - リクエスト間に分散遅延を追加（0〜3秒のランダム遅延）
    - bot検出ページの判定とリトライ
    """
    results: list[ShopPrice] = []

    # === Phase 1: cloudscraper（並列実行・分散遅延付き） ===
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        future_to_name: dict = {}
        for i, (scraper_fn, name) in enumerate(SCRAPERS):
            # 各リクエストに0〜3秒のランダム遅延を追加
            # 同じドメインに同時にアクセスしないようにする
            delay = i * 0.3 + random.uniform(0, 0.5)
            future = executor.submit(_delayed_scrape, scraper_fn, query, config, delay)
            future_to_name[future] = name

        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result(timeout=_TIMEOUT + 10)
                results.append(result)
            except Exception as e:
                results.append(ShopPrice(name, None, "", "", "", error=str(e)))

    phase1_found = sum(1 for r in results if r.price is not None)
    logger.info("Phase 1 complete: %d/%d shops found prices", phase1_found, len(results))

    # === Phase 2: Playwright ブラウザレンダリング（失敗分のみ） ===
    _retry_with_browser(results, query)

    phase2_found = sum(1 for r in results if r.price is not None)
    if phase2_found > phase1_found:
        logger.info("Phase 2 recovered %d additional shops", phase2_found - phase1_found)

    return results
