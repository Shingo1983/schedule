"""JALマイレージパークのショップ・マイルレート情報管理モジュール

JAL公式PDFからLSP対象ショップ一覧を取得し、
partner.jal.co.jpからショップごとのマイル付与レートをスクレイピングする。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR, JAL_LSP_PDF_URL

SHOP_CACHE_FILE = CACHE_DIR / "jal_shops.json"
# キャッシュの有効期間（秒）: 7日
CACHE_TTL = 7 * 24 * 3600

# JALマイレージパーク ショップ一覧ページ（五十音・アルファベット順）
SHOPLIST_URL = "https://partner.jal.co.jp/shoplist/"

# 主要ショップのマイルレート（手動管理のフォールバック）
# 「Xマイル/Y円」の形式で記録
# JALマイレージパークのショップは頻繁にレートが変わるため、
# 最新情報はPDFまたはWebで確認を推奨
KNOWN_SHOPS = {
    "楽天市場": {
        "shop_id": "15268",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "総合通販",
    },
    "Yahoo!ショッピング": {
        "shop_id": "10122",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "総合通販",
    },
    "Amazon.co.jp": {
        "shop_id": "200186",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "総合通販",
    },
    "ビックカメラ.com": {
        "shop_id": "10589",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
    },
    "Apple公式サイト": {
        "shop_id": "10195",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
    },
    "ユニクロオンラインストア": {
        "shop_id": "15087",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "ファッション",
    },
    "無印良品ネットストア": {
        "shop_id": "15374",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "生活雑貨",
    },
    "JAL Mall": {
        "shop_id": "200432",
        "mile_rate_desc": "100円(税抜)につき1マイル",
        "yen_per_mile": 100,
        "category": "総合通販",
    },
    "ヤマダウェブコム": {
        "shop_id": "10159",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "家電",
    },
    "コジマネット": {
        "shop_id": "10587",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
    },
    "Joshin webショップ": {
        "shop_id": "10091",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
    },
    "セブンネットショッピング": {
        "shop_id": "10585",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
    },
    "ベルメゾンネット": {
        "shop_id": "10045",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
    },
    "LOHACO": {
        "shop_id": "10558",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "日用品",
    },
    "ニトリネット": {
        "shop_id": "15357",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家具・インテリア",
    },
    "ZOZOTOWN": {
        "shop_id": "15267",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "ファッション",
    },
    "DHCオンラインショップ": {
        "shop_id": "10051",
        "mile_rate_desc": "100円につき1マイル",
        "yen_per_mile": 100,
        "category": "コスメ・健康",
    },
    "ファンケルオンライン": {
        "shop_id": "10083",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "コスメ・健康",
    },
    "ソニーストア": {
        "shop_id": "10098",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
    },
    "au PAY マーケット": {
        "shop_id": "15091",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
    },
}


@dataclass
class JalShop:
    """JALマイレージパーク掲載ショップの情報"""

    name: str
    shop_id: str
    mile_rate_desc: str
    yen_per_mile: int  # 1マイル獲得に必要な金額（円）
    category: str = ""

    @property
    def url(self) -> str:
        return f"https://partner.jal.co.jp/shop/?tp={self.shop_id}"

    def calc_miles(self, price: int) -> int:
        """指定金額で獲得できるマイル数を計算"""
        if self.yen_per_mile <= 0:
            return 0
        return price // self.yen_per_mile

    def calc_lsp(self, price: int) -> float:
        """指定金額で獲得できるLSPを計算（小数点以下も表示）"""
        return self.calc_miles(price) / 100.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "shop_id": self.shop_id,
            "mile_rate_desc": self.mile_rate_desc,
            "yen_per_mile": self.yen_per_mile,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JalShop":
        return cls(**data)


def load_known_shops() -> list[JalShop]:
    """組み込みのショップデータを返す"""
    shops = []
    for name, info in KNOWN_SHOPS.items():
        shops.append(
            JalShop(
                name=name,
                shop_id=info["shop_id"],
                mile_rate_desc=info["mile_rate_desc"],
                yen_per_mile=info["yen_per_mile"],
                category=info.get("category", ""),
            )
        )
    return shops


def load_cached_shops() -> list[JalShop] | None:
    """キャッシュからショップ一覧を読み込む"""
    if not SHOP_CACHE_FILE.exists():
        return None
    try:
        stat = SHOP_CACHE_FILE.stat()
        if time.time() - stat.st_mtime > CACHE_TTL:
            return None
        with open(SHOP_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [JalShop.from_dict(d) for d in data]
    except (json.JSONDecodeError, KeyError):
        return None


def save_shops_cache(shops: list[JalShop]) -> None:
    """ショップ一覧をキャッシュに保存"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SHOP_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump([s.to_dict() for s in shops], f, indent=2, ensure_ascii=False)


def get_shops() -> list[JalShop]:
    """ショップ一覧を取得（キャッシュ→組み込みデータの順で検索）"""
    cached = load_cached_shops()
    if cached:
        return cached
    shops = load_known_shops()
    save_shops_cache(shops)
    return shops


def find_matching_shops(shop_name_query: str, shops: list[JalShop] | None = None) -> list[JalShop]:
    """ショップ名で部分一致検索"""
    if shops is None:
        shops = get_shops()
    query_lower = shop_name_query.lower()
    return [s for s in shops if query_lower in s.name.lower()]


def get_shop_for_store(store_name: str, shops: list[JalShop] | None = None) -> JalShop | None:
    """ストア名からJALマイレージパーク掲載ショップを特定する

    楽天やYahoo!の商品検索結果に含まれるストア名から、
    JALマイレージパーク上のショップを探す。
    """
    if shops is None:
        shops = get_shops()

    store_lower = store_name.lower()

    # 完全一致
    for s in shops:
        if s.name.lower() == store_lower:
            return s

    # 部分一致（ショップ名がストア名に含まれる、またはその逆）
    for s in shops:
        shop_lower = s.name.lower()
        if shop_lower in store_lower or store_lower in shop_lower:
            return s

    return None
