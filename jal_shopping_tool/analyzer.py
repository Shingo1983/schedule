"""価格比較・LSP分析モジュール

検索結果を分析し、最安値・JALマイレージパーク経由の価格・LSP獲得量を
比較してユーザーの意思決定を支援する。
"""

from dataclasses import dataclass, field

from .config import Config, MILES_PER_LSP
from .jal_shops import JalShop, get_shops, get_shop_for_store
from .price_search import SearchResult


@dataclass
class JalShopOffer:
    """JALマイレージパーク経由で購入した場合の情報"""

    jal_shop: JalShop
    search_result: SearchResult
    miles_earned: int
    lsp_earned: float
    lsp_value_yen: float  # LSP獲得分の金銭的価値（ユーザー設定のレート）
    effective_price: float  # 実質価格 = 商品価格 - LSP価値
    price_diff_from_lowest: int  # 全ネット最安値との差額

    @property
    def price(self) -> int:
        return self.search_result.price

    @property
    def shop_name(self) -> str:
        return self.jal_shop.name

    @property
    def product_name(self) -> str:
        return self.search_result.product_name


@dataclass
class AnalysisResult:
    """分析結果"""

    query: str
    lowest_price_result: SearchResult | None
    all_results: list[SearchResult]
    jal_offers: list[JalShopOffer]
    jal_shops_without_product: list[JalShop]  # 商品が見つからなかったJALショップ

    @property
    def lowest_price(self) -> int | None:
        if self.lowest_price_result:
            return self.lowest_price_result.price
        return None

    @property
    def best_jal_offer(self) -> JalShopOffer | None:
        """実質価格が最も安いJALショップオファー"""
        if not self.jal_offers:
            return None
        return min(self.jal_offers, key=lambda o: o.effective_price)

    @property
    def best_lsp_offer(self) -> JalShopOffer | None:
        """LSP獲得量が最も多いJALショップオファー"""
        if not self.jal_offers:
            return None
        return max(self.jal_offers, key=lambda o: o.lsp_earned)


def analyze(
    query: str,
    search_results: list[SearchResult],
    config: Config,
) -> AnalysisResult:
    """検索結果を分析してLSP獲得込みの比較を行う"""
    jal_shops = get_shops()

    # 全ネット最安値
    lowest = min(search_results, key=lambda r: r.price) if search_results else None
    lowest_price = lowest.price if lowest else 0

    # JALマイレージパーク掲載ショップに該当する検索結果を探す
    jal_offers: list[JalShopOffer] = []
    matched_shop_names: set[str] = set()

    for result in search_results:
        # 検索結果のショップ名からJALマイレージパーク掲載ショップを特定
        jal_shop = _match_result_to_jal_shop(result, jal_shops)
        if jal_shop and jal_shop.name not in matched_shop_names:
            matched_shop_names.add(jal_shop.name)
            miles = jal_shop.calc_miles(result.price)
            lsp = jal_shop.calc_lsp(result.price)
            lsp_value = lsp * config.lsp_value_yen
            effective_price = result.price - lsp_value

            jal_offers.append(
                JalShopOffer(
                    jal_shop=jal_shop,
                    search_result=result,
                    miles_earned=miles,
                    lsp_earned=lsp,
                    lsp_value_yen=lsp_value,
                    effective_price=effective_price,
                    price_diff_from_lowest=result.price - lowest_price,
                )
            )

    # JALマイレージパーク掲載だが商品が見つからなかったショップ
    # （主要ショップのみ表示）
    major_sources = {"rakuten": "楽天市場", "yahoo": "Yahoo!ショッピング"}
    jal_shops_without = []
    for shop in jal_shops:
        if shop.name not in matched_shop_names:
            # 楽天・Yahoo!以外で、掲載があるが検索結果に出ない場合
            jal_shops_without.append(shop)

    # JALオファーを実質価格順にソート
    jal_offers.sort(key=lambda o: o.effective_price)

    return AnalysisResult(
        query=query,
        lowest_price_result=lowest,
        all_results=search_results,
        jal_offers=jal_offers,
        jal_shops_without_product=jal_shops_without,
    )


def _match_result_to_jal_shop(
    result: SearchResult, jal_shops: list[JalShop]
) -> JalShop | None:
    """検索結果をJALマイレージパーク掲載ショップとマッチさせる

    楽天市場・Yahoo!ショッピングの検索結果はそれぞれのモール全体に対応する。
    """
    if result.source == "rakuten":
        # 楽天市場の検索結果 → JALマイレージパークの「楽天市場」に紐づけ
        for shop in jal_shops:
            if shop.name == "楽天市場":
                return shop
    elif result.source == "yahoo":
        # Yahoo!ショッピングの検索結果 → JALマイレージパークの「Yahoo!ショッピング」
        for shop in jal_shops:
            if shop.name == "Yahoo!ショッピング":
                return shop

    # 個別ショップのマッチング（ストア名ベース）
    return get_shop_for_store(result.shop_name, jal_shops)
