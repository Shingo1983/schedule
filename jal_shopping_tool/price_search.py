"""価格検索モジュール - 全ショップ横断検索

JALマイレージパーク提携の各ショップサイトで商品を検索し、
各サイトの最安値を取得する。
"""

from .config import Config
from .site_scrapers import ShopPrice, search_all_shops


def search_all_sites(query: str, config: Config) -> list[ShopPrice]:
    """全ショップで検索し、結果を返す"""
    return search_all_shops(query, config)
