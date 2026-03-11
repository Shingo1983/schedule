"""各ショップサイトのスクレイパー

JALマイレージパーク提携ショップの検索ページをスクレイピングし、
商品の最安値を取得する。
"""

import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup

from .config import Config

# 共通ヘッダー
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# リクエストタイムアウト（秒）
_TIMEOUT = 15


@dataclass
class ShopPrice:
    """ショップでの検索結果（最安値1件）"""

    shop_name: str
    price: int | None  # None = 取得失敗 or 商品なし
    product_name: str
    product_url: str
    search_url: str  # ユーザーが手動で確認できるURL
    error: str | None = None  # エラーメッセージ


def _fetch(url: str, params: dict | None = None) -> requests.Response:
    """共通のHTTP GETリクエスト"""
    return requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)


def _parse_price(text: str) -> int | None:
    """価格テキストから数値を抽出: '¥12,345' → 12345"""
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        return int(digits)
    return None


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

        soup = BeautifulSoup(resp.text, "lxml")

        # 価格と商品情報を抽出
        for result in soup.select('[data-component-type="s-search-result"]'):
            # 価格を探す
            price_whole = result.select_one(".a-price .a-price-whole")
            if not price_whole:
                continue
            price = _parse_price(price_whole.get_text())
            if not price or price == 0:
                continue

            # 商品名
            title_el = result.select_one("h2 a span")
            name = title_el.get_text(strip=True) if title_el else ""

            # URL
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
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("ビックカメラ.com", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "lxml")

        # 商品一覧から最安を探す
        for item in soup.select(".bcs_item, .prod_box, .itemSearchList li, .product"):
            price_el = item.select_one(".bcs_price, .val, .price")
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price == 0:
                continue

            name_el = item.select_one(".bcs_title a, .prod_name a, a.product-name")
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

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".product-list-item, .itemBox, .product_item"):
            price_el = item.select_one(".price, .itemPrice, .product-price")
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price == 0:
                continue

            name_el = item.select_one(".product-name a, .itemName a, a.product_name")
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
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("ヤマダウェブコム", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".product, .item, .searchResult__item"):
            price_el = item.select_one(".price, .pPrice, .product-price")
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price == 0:
                continue

            name_el = item.select_one(".pName a, .product-name a, a.item-name")
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

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".productList__item, .item, .lineup_box"):
            price_el = item.select_one(".productList__price, .price, .lineup_price")
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price == 0:
                continue

            name_el = item.select_one(".productList__name a, .item-name a, .lineup_name a")
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
    search_url = f"https://wowma.jp/itemlist?e_scope=O&at=FP&non_gr=ex&e_desc=Y&spe=Y&keyword={quote(query)}&categ_id=0&price_type=&price_from=&price_to=&sort_type=priceasc"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("au PAY マーケット", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".itemList__item, .product-item, .item"):
            price_el = item.select_one(".price, .itemList__price, .product-price")
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price == 0:
                continue

            name_el = item.select_one(".itemList__name a, .product-name a, a.item-name")
            name = name_el.get_text(strip=True) if name_el else ""
            url = ""
            if name_el and name_el.get("href"):
                href = name_el["href"]
                url = f"https://wowma.jp{href}" if href.startswith("/") else href

            return ShopPrice("au PAY マーケット", price, name, url, search_url)

        return ShopPrice("au PAY マーケット", None, "", "", search_url)

    except Exception as e:
        return ShopPrice("au PAY マーケット", None, "", "", search_url, error=str(e))


# ============================================================
# セブンネットショッピング (スクレイピング)
# ============================================================
def search_seven(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://7net.omni7.jp/search/?keyword={quote(query)}&sortColumn=PRICE_LOW"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("セブンネットショッピング", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".product, .item, .productItem"):
            price_el = item.select_one(".price, .productPrice, .item-price")
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price == 0:
                continue

            name_el = item.select_one(".productName a, .item-name a, a.product-name")
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
]


def search_all_shops(query: str, config: Config) -> list[ShopPrice]:
    """全ショップを検索して結果を返す"""
    results = []
    for scraper_fn, _name in SCRAPERS:
        try:
            result = scraper_fn(query, config)
            results.append(result)
        except Exception as e:
            results.append(ShopPrice(_name, None, "", "", "", error=str(e)))
    return results
