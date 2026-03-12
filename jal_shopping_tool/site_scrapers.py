"""各ショップサイトのスクレイパー

JALマイレージパーク提携ショップの検索ページをスクレイピングし、
商品の最安値を取得する。
"""

import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from .config import Config

logger = logging.getLogger(__name__)

# 共通ヘッダー
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# リクエストタイムアウト（秒）
_TIMEOUT = 20

# 並列実行のワーカー数
_MAX_WORKERS = 6


def _create_session() -> requests.Session:
    """リトライ付きのHTTPセッションを作成"""
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=_MAX_WORKERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_HEADERS)
    return session


# グローバルセッション（コネクションプーリング）
_session = _create_session()


@dataclass
class ShopPrice:
    """ショップでの検索結果（最安値1件）"""

    shop_name: str
    price: int | None  # None = 取得失敗 or 商品なし
    product_name: str
    product_url: str
    search_url: str  # ユーザーが手動で確認できるURL
    error: str | None = None  # エラーメッセージ


def _fetch(url: str, params: dict | None = None, **kwargs) -> requests.Response:
    """共通のHTTP GETリクエスト（セッション使用）"""
    return _session.get(url, params=params, timeout=_TIMEOUT, **kwargs)


def _parse_price(text: str) -> int | None:
    """価格テキストから数値を抽出: '¥12,345' → 12345"""
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        val = int(digits)
        # 明らかに小さすぎる値は除外（送料等のノイズ）
        if val >= 10:
            return val
    return None


def _soup(resp: requests.Response) -> BeautifulSoup:
    """レスポンスからBeautifulSoupオブジェクトを作成"""
    return BeautifulSoup(resp.text, "lxml")


# ============================================================
# 楽天市場 (API)
# ============================================================
def search_rakuten(query: str, config: Config) -> ShopPrice:
    search_url = f"https://search.rakuten.co.jp/search/mall/{quote(query)}/"

    if not config.rakuten_app_id:
        return ShopPrice(
            shop_name="楽天市場",
            price=None,
            product_name="",
            product_url="",
            search_url=search_url,
            error="APIキー未設定（設定画面で登録してください）",
        )

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
            try:
                err = resp.json()
                msg = err.get("error_description", err.get("error", f"HTTP {resp.status_code}"))
            except ValueError:
                msg = f"HTTP {resp.status_code}"
            return ShopPrice("楽天市場", None, "", "", search_url, error=f"API: {msg}")

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
    except Exception as e:
        return ShopPrice("楽天市場", None, "", "", search_url, error=str(e))


# ============================================================
# Yahoo!ショッピング (API)
# ============================================================
def search_yahoo(query: str, config: Config) -> ShopPrice:
    search_url = f"https://shopping.yahoo.co.jp/search?p={quote(query)}"

    if not config.yahoo_app_id:
        return ShopPrice(
            shop_name="Yahoo!ショッピング",
            price=None,
            product_name="",
            product_url="",
            search_url=search_url,
            error="APIキー未設定（設定画面で登録してください）",
        )

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
            try:
                err = resp.json()
                msg = err.get("Message", err.get("error", f"HTTP {resp.status_code}"))
            except ValueError:
                msg = f"HTTP {resp.status_code}"
            return ShopPrice("Yahoo!ショッピング", None, "", "", search_url, error=f"API: {msg}")

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
    except Exception as e:
        return ShopPrice("Yahoo!ショッピング", None, "", "", search_url, error=str(e))


# ============================================================
# Amazon.co.jp (スクレイピング)
# ============================================================
def search_amazon(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.amazon.co.jp/s?k={quote(query)}&s=price-asc-rank"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("Amazon.co.jp", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        for result in soup.select('[data-component-type="s-search-result"]'):
            # スポンサー商品をスキップ
            if result.select_one('.s-label-popover-default'):
                sponsored_text = result.select_one('.s-label-popover-default')
                if sponsored_text and 'スポンサー' in sponsored_text.get_text():
                    continue

            price_whole = result.select_one(".a-price .a-price-whole")
            if not price_whole:
                continue
            price = _parse_price(price_whole.get_text())
            if not price or price == 0:
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
        return ShopPrice("Amazon.co.jp", None, "", "", search_url, error=str(e))


# ============================================================
# ビックカメラ.com (スクレイピング)
# ============================================================
def search_biccamera(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.biccamera.com/bc/category/?q={quote(query)}&rowPerPage=25&sort=PRICE_ASC"

    try:
        # ビックカメラはRefererが必要な場合がある
        headers = {**_HEADERS, "Referer": "https://www.biccamera.com/"}
        resp = _session.get(search_url, timeout=_TIMEOUT, headers=headers)
        if resp.status_code != 200:
            return ShopPrice("ビックカメラ.com", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        # 複数のセレクタパターンで試行
        selectors = [
            # 新レイアウト
            (".bcs_listItem", ".bcs_price", ".bcs_title a"),
            # 旧レイアウト
            (".prod_box", ".val", ".prod_name a"),
            # カテゴリ検索結果
            (".bcs_item", ".bcs_price .val", ".bcs_title a"),
            # 汎用
            ('[class*="product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    url = f"https://www.biccamera.com{href}" if href.startswith("/") else href

                return ShopPrice("ビックカメラ.com", price, name, url, search_url)

        return ShopPrice("ビックカメラ.com", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("ビックカメラ.com", None, "", "", search_url, error=str(e))


# ============================================================
# コジマネット (スクレイピング)
# ============================================================
def search_kojima(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.kojima.net/ec/disp/CSfDispListPage_001.jsp?dispNo=&q={quote(query)}&sort=price&order=asc"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("コジマネット", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        # コジマネットの商品リスト
        selectors = [
            (".product-list-item", ".price, .itemPrice, .product-price", ".product-name a, .itemName a"),
            (".itemBox", ".itemPrice", ".itemName a"),
            (".product_item", ".product-price", ".product_name a"),
            ("li.item", ".price", "a.item-name, .name a"),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    url = f"https://www.kojima.net{href}" if href.startswith("/") else href

                return ShopPrice("コジマネット", price, name, url, search_url)

        return ShopPrice("コジマネット", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("コジマネット", None, "", "", search_url, error=str(e))


# ============================================================
# ヤマダウェブコム (スクレイピング)
# ============================================================
def search_yamada(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.yamada-denkiweb.com/search?q={quote(query)}&sort=price_asc"

    try:
        headers = {
            **_HEADERS,
            "Referer": "https://www.yamada-denkiweb.com/",
        }
        resp = _session.get(search_url, timeout=_TIMEOUT, headers=headers)
        if resp.status_code == 403:
            return ShopPrice("ヤマダウェブコム", None, "", "", search_url,
                             error="アクセス制限（手動で検索してください）")
        if resp.status_code != 200:
            return ShopPrice("ヤマダウェブコム", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        selectors = [
            (".searchResult__item", ".searchResult__price, .pPrice", ".searchResult__name a, .pName a"),
            (".product", ".price, .product-price", ".product-name a"),
            (".item", ".pPrice", ".pName a"),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    url = f"https://www.yamada-denkiweb.com{href}" if href.startswith("/") else href

                return ShopPrice("ヤマダウェブコム", price, name, url, search_url)

        return ShopPrice("ヤマダウェブコム", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("ヤマダウェブコム", None, "", "", search_url, error=str(e))


# ============================================================
# Joshin webショップ (スクレイピング)
# ============================================================
def search_joshin(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://joshinweb.jp/servlet/emall.odr_wp?SHP=0&KW={quote(query)}&SORT=PRICE_LO"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("Joshin webショップ", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        selectors = [
            (".productList__item", ".productList__price", ".productList__name a"),
            (".lineup_box", ".lineup_price", ".lineup_name a"),
            (".item", ".price", ".item-name a, .name a"),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    url = f"https://joshinweb.jp{href}" if href.startswith("/") else href

                return ShopPrice("Joshin webショップ", price, name, url, search_url)

        return ShopPrice("Joshin webショップ", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("Joshin webショップ", None, "", "", search_url, error=str(e))


# ============================================================
# au PAY マーケット (スクレイピング)
# ============================================================
def search_aupay(query: str, _config: Config) -> ShopPrice:
    # au PAY マーケット（旧Wowma!）
    search_url = f"https://wowma.jp/itemlist?e_scope=O&at=FP&non_gr=ex&e_desc=Y&spe=Y&keyword={quote(query)}&categ_id=0&sort_type=priceasc"

    try:
        resp = _fetch(search_url, allow_redirects=True)
        if resp.status_code != 200:
            return ShopPrice("au PAY マーケット", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        selectors = [
            (".itemList__item", ".itemList__price, .price", ".itemList__name a, .product-name a"),
            (".product-item", ".product-price, .price", ".product-name a"),
            ('[class*="ItemCard"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
            (".item", ".price", "a.item-name"),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    if href.startswith("/"):
                        url = f"https://wowma.jp{href}"
                    elif href.startswith("http"):
                        url = href
                    else:
                        url = f"https://wowma.jp/{href}"

                return ShopPrice("au PAY マーケット", price, name, url, search_url)

        return ShopPrice("au PAY マーケット", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("au PAY マーケット", None, "", "", search_url, error=str(e))


# ============================================================
# セブンネットショッピング (スクレイピング)
# ============================================================
def search_seven(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://7net.omni7.jp/search/?keyword={quote(query)}&searchKeywordFlg=1"

    try:
        resp = _fetch(search_url, allow_redirects=True)
        if resp.status_code != 200:
            return ShopPrice("セブンネットショッピング", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        selectors = [
            (".productItem", ".productPrice, .price", ".productName a, .product-name a"),
            (".product", ".price, .productPrice", ".productName a"),
            (".item", ".price, .item-price", ".item-name a, .productName a"),
            ('[class*="product"]', '[class*="price"]', '[class*="name"] a'),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    url = f"https://7net.omni7.jp{href}" if href.startswith("/") else href

                return ShopPrice("セブンネットショッピング", price, name, url, search_url)

        return ShopPrice("セブンネットショッピング", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("セブンネットショッピング", None, "", "", search_url, error=str(e))


# ============================================================
# Qoo10 (スクレイピング)
# ============================================================
def search_qoo10(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.qoo10.jp/s/{quote(query)}?sort=prc"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("Qoo10", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        selectors = [
            (".sc-prd", ".prc .prc-dc, .prc", ".tit a, .sbj a"),
            (".item_g", ".price, .prc", ".sbj a"),
            ('[class*="product"]', '[class*="price"]', '[class*="title"] a, [class*="name"] a'),
            (".goods_item", ".price", ".goods_name a, .title a"),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    if href.startswith("/"):
                        url = f"https://www.qoo10.jp{href}"
                    elif href.startswith("http"):
                        url = href
                    else:
                        url = f"https://www.qoo10.jp/{href}"

                return ShopPrice("Qoo10", price, name, url, search_url)

        return ShopPrice("Qoo10", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("Qoo10", None, "", "", search_url, error=str(e))


# ============================================================
# エディオンネットショップ (スクレイピング)
# ============================================================
def search_edion(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.edion.com/search?keyword={quote(query)}&sort=price_asc"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("エディオンネットショップ", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)

        selectors = [
            (".product-item, .item-list__item", ".price, .item-price", ".product-name a, .item-name a"),
            ('[class*="product"]', '[class*="price"]', '[class*="name"] a'),
            (".searchResultItem", ".resultPrice", ".resultName a"),
        ]

        for item_sel, price_sel, name_sel in selectors:
            items = soup.select(item_sel)
            if not items:
                continue
            for item in items:
                price_el = item.select_one(price_sel)
                if not price_el:
                    continue
                price = _parse_price(price_el.get_text())
                if not price or price == 0:
                    continue

                name_el = item.select_one(name_sel)
                name = name_el.get_text(strip=True) if name_el else ""
                url = ""
                if name_el and name_el.get("href"):
                    href = name_el["href"]
                    url = f"https://www.edion.com{href}" if href.startswith("/") else href

                return ShopPrice("エディオンネットショップ", price, name, url, search_url)

        return ShopPrice("エディオンネットショップ", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("エディオンネットショップ", None, "", "", search_url, error=str(e))


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
]

# スクレイパー対応済みショップ名のセット（自動生成）
SCRAPER_SHOP_NAMES = {name for _, name in SCRAPERS}


def get_manual_search_shops() -> list[str]:
    """スクレイパー未対応のKNOWN_SHOPSを自動的に返す（常にKNOWN_SHOPSと同期）"""
    from .jal_shops import KNOWN_SHOPS
    return [name for name in KNOWN_SHOPS if name not in SCRAPER_SHOP_NAMES]


def search_all_shops(query: str, config: Config) -> list[ShopPrice]:
    """全ショップを並列に検索して結果を返す"""
    results: list[ShopPrice] = []

    # 並列実行
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        future_to_name: dict = {}
        for scraper_fn, name in SCRAPERS:
            future = executor.submit(scraper_fn, query, config)
            future_to_name[future] = name

        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result(timeout=_TIMEOUT + 5)
                results.append(result)
            except Exception as e:
                results.append(ShopPrice(name, None, "", "", "", error=str(e)))

    return results
