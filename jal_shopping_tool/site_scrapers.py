"""各ショップサイトのスクレイパー

JALマイレージパーク提携ショップの検索ページをスクレイピングし、
商品の最安値を取得する。

戦略:
1. CSSセレクタでHTML要素から価格を取得（サーバーサイドレンダリングのサイト）
2. JSON-LD構造化データから価格を取得（SEO用にSPAでも埋め込まれていることが多い）
3. ページ内のJSON（__NEXT_DATA__等）から価格を取得
4. 正規表現でページ全体から価格パターンを検出（最終手段）
"""

import json
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from .config import Config

logger = logging.getLogger(__name__)

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

# 並列実行のワーカー数
_MAX_WORKERS = 15


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
    """毎回新しいHTTPセッションを作成（接続プール問題を回避）"""
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
    """HTTP GETリクエスト（毎回新しいセッションを使用）"""
    session = _new_session()
    if headers:
        session.headers.update(headers)
    # Refererを自動設定（bot検出対策）
    if "Referer" not in (headers or {}):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return session.get(url, timeout=_TIMEOUT, **kwargs)


def _fetch_json(url: str, params: dict | None = None, **kwargs) -> requests.Response:
    """JSON API用のGETリクエスト"""
    session = _new_session()
    session.headers.update(_JSON_HEADERS)
    return session.get(url, params=params, timeout=_TIMEOUT, **kwargs)


def _parse_price(text: str) -> int | None:
    """価格テキストから数値を抽出: '¥12,345' → 12345"""
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        val = int(digits)
        if val >= 100:  # 100円未満は送料等のノイズ
            return val
    return None


def _soup(resp: requests.Response) -> BeautifulSoup:
    """レスポンスからBeautifulSoupオブジェクトを作成"""
    return BeautifulSoup(resp.text, "lxml")


# ---------------------------------------------------------------------------
# 共通抽出関数
# ---------------------------------------------------------------------------

def _extract_jsonld_prices(soup: BeautifulSoup) -> list[dict]:
    """JSON-LD構造化データから商品情報を抽出する。
    SPAサイトでもSEO用にJSON-LDが埋め込まれていることが多い。
    Returns: [{"name": str, "price": int, "url": str}, ...]
    """
    results = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # ItemListなどの場合
            if data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            elif data.get("@type") in ("Product", "Offer"):
                items = [data]
            elif "mainEntity" in data:
                me = data["mainEntity"]
                items = me if isinstance(me, list) else [me]
            else:
                items = [data]

        for item in items:
            if isinstance(item, dict) and item.get("@type") == "ListItem":
                item = item.get("item", item)

            if not isinstance(item, dict):
                continue

            name = item.get("name", "")
            url = item.get("url", "")
            price = None

            # Product > offers > price
            offers = item.get("offers", {})
            if isinstance(offers, list):
                for o in offers:
                    p = o.get("price") or o.get("lowPrice")
                    if p:
                        price = _parse_price(str(p))
                        if price:
                            break
            elif isinstance(offers, dict):
                p = offers.get("price") or offers.get("lowPrice")
                if p:
                    price = _parse_price(str(p))

            # 直接priceがある場合
            if not price and item.get("price"):
                price = _parse_price(str(item["price"]))

            if price:
                results.append({"name": name, "price": price, "url": url})

    return results


def _extract_embedded_json(html: str) -> list[dict]:
    """ページ内の埋め込みJSON（__NEXT_DATA__, __INITIAL_STATE__等）から商品価格を抽出"""
    results = []

    # __NEXT_DATA__ (Next.js)
    patterns = [
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.*?})\s*</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>',
        r'window\.__PRELOADED_STATE__\s*=\s*({.*?});\s*</script>',
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
    """再帰的にJSONオブジェクト内の商品（price + name）を探す"""
    if depth > 8:  # 深さ制限
        return
    if isinstance(obj, dict):
        # "price" と "name" が同じレベルにあれば商品の可能性
        has_price = "price" in obj or "salePrice" in obj or "itemPrice" in obj
        has_name = "name" in obj or "itemName" in obj or "title" in obj or "productName" in obj
        if has_price and has_name:
            raw_price = obj.get("price") or obj.get("salePrice") or obj.get("itemPrice")
            if raw_price is not None:
                price = _parse_price(str(raw_price))
                if price:
                    name = obj.get("name") or obj.get("itemName") or obj.get("title") or obj.get("productName", "")
                    url = obj.get("url") or obj.get("itemUrl") or obj.get("productUrl", "")
                    results.append({"name": str(name), "price": price, "url": str(url)})
        for v in obj.values():
            _find_prices_in_dict(v, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:50]:  # リスト要素数制限
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


def _find_price_in_soup(soup: BeautifulSoup, selectors: list[tuple[str, str, str]],
                        base_url: str = "") -> tuple[int | None, str, str]:
    """複数のCSSセレクタパターンで商品を探す。(price, name, url)を返す

    戦略:
    1. CSSセレクタでHTML要素から抽出
    2. JSON-LD構造化データ
    3. 埋め込みJSON（__NEXT_DATA__等）
    ※正規表現フォールバックは廃止（無関係な価格を誤検出する原因のため）
    """
    # 1. CSSセレクタで探す
    for item_sel, price_sel, name_sel in selectors:
        items = soup.select(item_sel)
        if not items:
            continue
        for item in items:
            price_el = item.select_one(price_sel)
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price:
                continue

            name_el = item.select_one(name_sel)
            name = name_el.get_text(strip=True) if name_el else ""
            url = ""
            if name_el and name_el.get("href"):
                href = name_el["href"]
                if href.startswith("http"):
                    url = href
                elif href.startswith("/") and base_url:
                    url = f"{base_url}{href}"
                elif base_url:
                    url = f"{base_url}/{href}"

            return price, name, url

    # 2. JSON-LD構造化データから探す
    jsonld_items = _extract_jsonld_prices(soup)
    if jsonld_items:
        cheapest = min(jsonld_items, key=lambda x: x["price"])
        return cheapest["price"], cheapest["name"], cheapest["url"]

    # 3. 埋め込みJSONから探す
    embedded = _extract_embedded_json(str(soup))
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
                    headers: dict | None = None) -> ShopPrice:
    """汎用スクレイパー: HTML取得→セレクタ→JSON-LD→埋め込みJSONの順で試行"""
    try:
        resp = _fetch(search_url, headers=headers)
        if resp.status_code == 403:
            return _make_error_result(shop_name, search_url, "アクセス制限（手動で検索してください）")
        if resp.status_code != 200:
            return _make_error_result(shop_name, search_url, f"HTTP {resp.status_code}")

        soup = _soup(resp)
        price, name, url = _find_price_in_soup(soup, selectors, base_url)
        if price:
            return ShopPrice(shop_name, price, name, url, search_url)

        return ShopPrice(shop_name, None, "", "", search_url)

    except requests.exceptions.SSLError:
        return _make_error_result(shop_name, search_url, "SSL接続エラー（手動で検索してください）")
    except requests.exceptions.ConnectionError:
        return _make_error_result(shop_name, search_url, "接続エラー（手動で検索してください）")
    except requests.exceptions.Timeout:
        return _make_error_result(shop_name, search_url, "タイムアウト（手動で検索してください）")
    except Exception as e:
        logger.warning("scrape error for %s: %s", shop_name, e)
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
                           "https://search.rakuten.co.jp")


# ============================================================
# Yahoo!ショッピング (API)
# ============================================================
def search_yahoo(query: str, config: Config) -> ShopPrice:
    search_url = f"https://shopping.yahoo.co.jp/search?p={quote(query)}"

    if not config.yahoo_app_id:
        return ShopPrice("Yahoo!ショッピング", None, "", "", search_url,
                         error="APIキー未設定（設定画面で登録してください）")

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
                           })


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
                           "https://www.kojima.net")


# ============================================================
# ヤマダウェブコム (スクレイピング)
# ============================================================
def search_yamada(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.yamada-denkiweb.com/search?q={quote(query)}&sort=price_asc"
    selectors = [
        (".searchResult__item", ".searchResult__price, .pPrice", ".searchResult__name a, .pName a"),
        (".product", ".price, .product-price", ".product-name a"),
        (".item", ".pPrice", ".pName a"),
        ("li.product-item", ".price-box .price", ".product-item-link"),
    ]
    return _scrape_generic("ヤマダウェブコム", search_url, selectors,
                           "https://www.yamada-denkiweb.com",
                           headers={"Referer": "https://www.yamada-denkiweb.com/"})


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
                           "https://joshinweb.jp")


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
                           "https://wowma.jp")


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
                           "https://7net.omni7.jp")


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
                           "https://www.qoo10.jp")


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
                           "https://www.edion.com")


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
        return _scrape_generic(shop_name, search_url, sels, base_url, headers=headers)

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
    "https://mall.jal.co.jp/search/?q={query}",
    "https://mall.jal.co.jp",
    headers={"Sec-Fetch-Site": "same-origin", "Referer": "https://mall.jal.co.jp/"},
)

search_bellemaison = _make_generic_scraper(
    "ベルメゾンネット",
    "https://www.bellemaison.jp/search/{query}",
    "https://www.bellemaison.jp",
)

search_lohaco = _make_generic_scraper(
    "LOHACO",
    "https://lohaco.yahoo.co.jp/search?p={query}",
    "https://lohaco.yahoo.co.jp",
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
    "https://store.sony.jp/search/?q={query}",
    "https://store.sony.jp",
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
    "https://dshopping.docomo.ne.jp/search?keyword={query}",
    "https://dshopping.docomo.ne.jp",
)

search_buyma = _make_generic_scraper(
    "BUYMA",
    "https://www.buyma.com/r/-{query}/",
    "https://www.buyma.com",
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
    "https://www.cosme.com/products/search?keyword={query}",
    "https://www.cosme.com",
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


def search_all_shops(query: str, config: Config) -> list[ShopPrice]:
    """全ショップを並列に検索して結果を返す"""
    results: list[ShopPrice] = []

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
