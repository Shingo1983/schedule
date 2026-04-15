"""JALマイレージパークのショップ・マイルレート情報管理モジュール

JAL公式PDFからLSP対象ショップ一覧を取得し、
partner.jal.co.jpからショップごとのマイル付与レートをスクレイピングする。
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CACHE_DIR, JAL_LSP_PDF_URL

SHOP_CACHE_FILE = CACHE_DIR / "jal_shops.json"
# キャッシュの有効期間（秒）: 7日
CACHE_TTL = 7 * 24 * 3600

# JALマイレージパーク ショップ一覧ページ（五十音・アルファベット順）
SHOPLIST_URL = "https://partner.jal.co.jp/shoplist/"

# ---------------------------------------------------------------------------
# 主要ショップのマイルレート（手動管理のフォールバック）
# 「Xマイル/Y円」の形式で記録
# JALマイレージパークのショップは頻繁にレートが変わるため、
# 最新情報はPDFまたはWebで確認を推奨
#
# search_url_template: 検索URL ({query} をキーワードに置換)
# note: 注記（カテゴリ制限等）
# ---------------------------------------------------------------------------
KNOWN_SHOPS = {
    "楽天市場": {
        "shop_id": "15268",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "総合通販",
        "search_url_template": "https://search.rakuten.co.jp/search/mall/{query}/",
    },
    "Yahoo!ショッピング": {
        "shop_id": "10122",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "総合通販",
        "search_url_template": "https://shopping.yahoo.co.jp/search?p={query}",
    },
    "Amazon.co.jp": {
        "shop_id": "200186",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "総合通販",
        "search_url_template": "https://www.amazon.co.jp/s?k={query}",
        "note": "特定カテゴリのみマイル対象（ファッション、食品・飲料、ドラッグストア、ビューティー等）。Amazonデバイス・デジタルコンテンツ・ギフト券等は対象外",
    },
    "ビックカメラ.com": {
        "shop_id": "10589",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://www.biccamera.com/bc/category/?q={query}",
    },
    "Apple公式サイト": {
        "shop_id": "10195",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://www.apple.com/jp/search/{query}",
    },
    "ユニクロオンラインストア": {
        "shop_id": "15087",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "ファッション",
        "search_url_template": "https://www.uniqlo.com/jp/ja/search?q={query}",
    },
    "無印良品ネットストア": {
        "shop_id": "15374",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "生活雑貨",
        "search_url_template": "https://www.muji.com/jp/ja/search?q={query}",
    },
    "JAL Mall": {
        "shop_id": "200432",
        "mile_rate_desc": "100円(税抜)につき1マイル",
        "yen_per_mile": 100,
        "category": "総合通販",
        "search_url_template": "https://ec.jal.co.jp/shop/goods/search.aspx?keyword={query}&search=x",
    },
    "ヤマダウェブコム": {
        "shop_id": "10159",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "家電",
        "search_url_template": "https://www.yamada-denkiweb.com/search/{query}/",
    },
    "コジマネット": {
        "shop_id": "10587",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://www.kojima.net/ec/prod_list.html?keyword={query}",
    },
    "Joshin webショップ": {
        "shop_id": "10091",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://joshinweb.jp/servlet/emall.odr_wp?QS=&REQUEST_CODE=1&category_id=&SHP=0&QK={query}&PID=srhzs",
    },
    "セブンネットショッピング": {
        "shop_id": "10585",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
        "search_url_template": "https://7net.omni7.jp/search/?keyword={query}&searchKeywordFlg=1",
    },
    "ベルメゾンネット": {
        "shop_id": "10045",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
        "search_url_template": "https://www.bellemaison.jp/search/{query}",
    },
    "LOHACO": {
        "shop_id": "10558",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "日用品",
        "search_url_template": "https://lohaco.yahoo.co.jp/search?p={query}",
    },
    "ニトリネット": {
        "shop_id": "15357",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家具・インテリア",
        "search_url_template": "https://www.nitori-net.jp/ec/search/?q={query}",
    },
    "ZOZOTOWN": {
        "shop_id": "15267",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "ファッション",
        "search_url_template": "https://zozo.jp/search/?p_keyv={query}",
    },
    "DHCオンラインショップ": {
        "shop_id": "10051",
        "mile_rate_desc": "100円につき1マイル",
        "yen_per_mile": 100,
        "category": "コスメ・健康",
        "search_url_template": "https://www.dhc.co.jp/goods/search.jsp?keyword={query}",
    },
    "ファンケルオンライン": {
        "shop_id": "10083",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "コスメ・健康",
        "search_url_template": "https://www.fancl.co.jp/search/?q={query}",
    },
    "ソニーストア": {
        "shop_id": "10098",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://search.sony.jp/ja_all/search.x?q={query}",
    },
    "au PAY マーケット": {
        "shop_id": "15091",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
        "search_url_template": "https://wowma.jp/itemlist?keyword={query}",
    },
    # ====================================================================
    # 追加ショップ（JALマイレージパーク提携）
    # ====================================================================
    "Qoo10": {
        "shop_id": "200206",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
        "search_url_template": "https://www.qoo10.jp/s/{query}?keyword={query}",
    },
    "エディオンネットショップ": {
        "shop_id": "10100",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://www.edion.com/detail_search.html?q={query}",
    },
    "ケーズデンキオンラインショップ": {
        "shop_id": "10588",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://www.ksdenki.com/shop/goods/search.aspx?keyword={query}",
    },
    "ノジマオンライン": {
        "shop_id": "10586",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "家電",
        "search_url_template": "https://online.nojima.co.jp/app/catalog/list/init?searchWord={query}",
    },
    "マツモトキヨシオンラインストア": {
        "shop_id": "10158",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "ドラッグストア",
        "search_url_template": "https://www.matsukiyo.co.jp/store/online/search?text={query}",
    },
    "dショッピング": {
        "shop_id": "200111",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
        "search_url_template": "https://dshopping.docomo.ne.jp/search?keyword={query}",
    },
    "BUYMA": {
        "shop_id": "10556",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "ファッション",
        "search_url_template": "https://www.buyma.com/r/-{query}/",
    },
    "ABC-MARTオンラインストア": {
        "shop_id": "10180",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "ファッション",
        "search_url_template": "https://www.abc-mart.net/shop/goods/search.aspx?keyword={query}",
    },
    "GU オンラインストア": {
        "shop_id": "200207",
        "mile_rate_desc": "300円につき1マイル",
        "yen_per_mile": 300,
        "category": "ファッション",
        "search_url_template": "https://www.gu-global.com/jp/ja/search?q={query}",
    },
    "ショップジャパン": {
        "shop_id": "10199",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "総合通販",
        "search_url_template": "https://www.shopjapan.co.jp/search/?q={query}",
    },
    "iHerb": {
        "shop_id": "200240",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "健康食品・サプリ",
        "search_url_template": "https://jp.iherb.com/search?kw={query}",
    },
    "@cosme SHOPPING": {
        "shop_id": "200250",
        "mile_rate_desc": "200円につき1マイル",
        "yen_per_mile": 200,
        "category": "コスメ・健康",
        "search_url_template": "https://www.cosme.com/products/list.php?name={query}",
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
    search_url_template: str = ""
    note: str = ""  # カテゴリ制限等の注記

    @property
    def url(self) -> str:
        return f"https://partner.jal.co.jp/shop/?tp={self.shop_id}"

    def get_search_url(self, query: str) -> str:
        """ショップの検索URLを生成"""
        if self.search_url_template:
            from urllib.parse import quote
            import re as _re
            # 全角スペース→半角、全角英数→半角
            q = query.replace('\u3000', ' ')
            normalized = []
            for ch in q:
                cp = ord(ch)
                if 0xFF01 <= cp <= 0xFF5E:
                    normalized.append(chr(cp - 0xFEE0))
                else:
                    normalized.append(ch)
            q = _re.sub(r'\s+', ' ', ''.join(normalized)).strip()
            return self.search_url_template.replace("{query}", quote(q))
        return ""

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
            "search_url_template": self.search_url_template,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JalShop":
        # 古いキャッシュとの互換性
        return cls(
            name=data["name"],
            shop_id=data["shop_id"],
            mile_rate_desc=data["mile_rate_desc"],
            yen_per_mile=data["yen_per_mile"],
            category=data.get("category", ""),
            search_url_template=data.get("search_url_template", ""),
            note=data.get("note", ""),
        )


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
                search_url_template=info.get("search_url_template", ""),
                note=info.get("note", ""),
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
    # キャッシュのショップ数がKNOWN_SHOPSと異なる場合は再生成
    if cached and len(cached) == len(KNOWN_SHOPS):
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
    """ストア名からJALマイレージパーク掲載ショップを特定する"""
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
