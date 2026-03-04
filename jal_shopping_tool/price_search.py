"""価格検索モジュール - 楽天市場API・Yahoo!ショッピングAPI連携"""

import time
from dataclasses import dataclass

import requests

from .config import Config


@dataclass
class SearchResult:
    """商品検索結果"""

    product_name: str
    price: int
    shop_name: str
    shop_url: str
    image_url: str
    source: str  # "rakuten" | "yahoo"

    @property
    def price_display(self) -> str:
        return f"¥{self.price:,}"


def search_rakuten(
    query: str, config: Config, max_results: int = 10
) -> list[SearchResult]:
    """楽天市場商品検索API で商品を検索する

    API: https://webservice.rakuten.co.jp/documentation/ichiba-item-search
    """
    if not config.rakuten_app_id:
        return []

    url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    params = {
        "applicationId": config.rakuten_app_id,
        "keyword": query,
        "hits": min(max_results, 30),
        "sort": "+itemPrice",  # 価格昇順
        "availability": 1,  # 購入可能な商品のみ
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [楽天API エラー] {e}")
        return []

    results = []
    for item_wrapper in data.get("Items", []):
        item = item_wrapper.get("Item", {})
        results.append(
            SearchResult(
                product_name=item.get("itemName", ""),
                price=item.get("itemPrice", 0),
                shop_name=item.get("shopName", ""),
                shop_url=item.get("itemUrl", ""),
                image_url=(item.get("mediumImageUrls", [{}])[0].get("imageUrl", "")
                           if item.get("mediumImageUrls") else ""),
                source="rakuten",
            )
        )
    return results


def search_yahoo(
    query: str, config: Config, max_results: int = 10
) -> list[SearchResult]:
    """Yahoo!ショッピング商品検索API v3 で商品を検索する

    API: https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html
    """
    if not config.yahoo_app_id:
        return []

    url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    params = {
        "appid": config.yahoo_app_id,
        "query": query,
        "results": min(max_results, 50),
        "sort": "+price",  # 価格昇順
        "in_stock": "true",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [Yahoo!ショッピングAPI エラー] {e}")
        return []

    results = []
    for hit in data.get("hits", []):
        results.append(
            SearchResult(
                product_name=hit.get("name", ""),
                price=int(hit.get("price", 0)),
                shop_name=hit.get("seller", {}).get("name", "")
                if isinstance(hit.get("seller"), dict)
                else "",
                shop_url=hit.get("url", ""),
                image_url=(hit.get("image", {}).get("medium", "")
                           if isinstance(hit.get("image"), dict) else ""),
                source="yahoo",
            )
        )
    return results


def search_all(
    query: str, config: Config, max_results: int = 10
) -> list[SearchResult]:
    """全ソースから商品を検索し、価格順にソートして返す"""
    all_results = []

    # 楽天
    rakuten_results = search_rakuten(query, config, max_results)
    all_results.extend(rakuten_results)

    # Yahoo!ショッピング（楽天APIのレート制限対応で1秒待つ）
    if config.rakuten_app_id and config.yahoo_app_id:
        time.sleep(1)
    yahoo_results = search_yahoo(query, config, max_results)
    all_results.extend(yahoo_results)

    # 価格順にソート
    all_results.sort(key=lambda r: r.price)
    return all_results
