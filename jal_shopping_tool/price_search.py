"""価格検索モジュール - 楽天市場API・Yahoo!ショッピングAPI連携"""

import time
from dataclasses import dataclass, field

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


@dataclass
class SearchResponse:
    """検索結果とエラー情報をまとめて返す"""

    results: list[SearchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def search_rakuten(
    query: str, config: Config, max_results: int = 10
) -> SearchResponse:
    """楽天市場商品検索API で商品を検索する

    API: https://webservice.rakuten.co.jp/documentation/ichiba-item-search
    """
    if not config.rakuten_app_id:
        return SearchResponse()

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
        if resp.status_code != 200:
            try:
                err_data = resp.json()
                err_msg = err_data.get("error_description", err_data.get("error", resp.text[:200]))
            except ValueError:
                err_msg = resp.text[:200]
            msg = f"楽天API エラー (HTTP {resp.status_code}): {err_msg}"
            print(f"  [{msg}]")
            return SearchResponse(errors=[msg])
        data = resp.json()
    except requests.ConnectionError:
        msg = "楽天API: インターネットに接続できません"
        print(f"  [{msg}]")
        return SearchResponse(errors=[msg])
    except requests.Timeout:
        msg = "楽天API: 応答がありません（タイムアウト）"
        print(f"  [{msg}]")
        return SearchResponse(errors=[msg])
    except (requests.RequestException, ValueError) as e:
        msg = f"楽天API エラー: {e}"
        print(f"  [{msg}]")
        return SearchResponse(errors=[msg])

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
    return SearchResponse(results=results)


def search_yahoo(
    query: str, config: Config, max_results: int = 10
) -> SearchResponse:
    """Yahoo!ショッピング商品検索API v3 で商品を検索する

    API: https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html
    """
    if not config.yahoo_app_id:
        return SearchResponse()

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
        if resp.status_code != 200:
            try:
                err_data = resp.json()
                err_msg = err_data.get("Message", err_data.get("error", resp.text[:200]))
            except ValueError:
                err_msg = resp.text[:200]
            msg = f"Yahoo!ショッピングAPI エラー (HTTP {resp.status_code}): {err_msg}"
            print(f"  [{msg}]")
            return SearchResponse(errors=[msg])
        data = resp.json()
    except requests.ConnectionError:
        msg = "Yahoo!ショッピングAPI: インターネットに接続できません"
        print(f"  [{msg}]")
        return SearchResponse(errors=[msg])
    except requests.Timeout:
        msg = "Yahoo!ショッピングAPI: 応答がありません（タイムアウト）"
        print(f"  [{msg}]")
        return SearchResponse(errors=[msg])
    except (requests.RequestException, ValueError) as e:
        msg = f"Yahoo!ショッピングAPI エラー: {e}"
        print(f"  [{msg}]")
        return SearchResponse(errors=[msg])

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
    return SearchResponse(results=results)


def search_all(
    query: str, config: Config, max_results: int = 10
) -> SearchResponse:
    """全ソースから商品を検索し、価格順にソートして返す"""
    all_results = []
    all_errors = []

    # 楽天
    rakuten_resp = search_rakuten(query, config, max_results)
    all_results.extend(rakuten_resp.results)
    all_errors.extend(rakuten_resp.errors)

    # Yahoo!ショッピング（楽天APIのレート制限対応で1秒待つ）
    if config.rakuten_app_id and config.yahoo_app_id:
        time.sleep(1)
    yahoo_resp = search_yahoo(query, config, max_results)
    all_results.extend(yahoo_resp.results)
    all_errors.extend(yahoo_resp.errors)

    # 価格順にソート
    all_results.sort(key=lambda r: r.price)
    return SearchResponse(results=all_results, errors=all_errors)
