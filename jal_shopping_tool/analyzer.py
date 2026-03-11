"""価格比較・LSP分析モジュール

各ショップの最安値を基に、LSP獲得量を計算し、
全サイトの最安値からの差額を含めた比較一覧を生成する。
"""

from dataclasses import dataclass

from .config import Config, MILES_PER_LSP
from .jal_shops import JalShop, get_shops
from .site_scrapers import ShopPrice


@dataclass
class ShopComparison:
    """1ショップの比較データ"""

    shop_name: str
    jal_shop: JalShop | None  # JALマイレージパーク掲載情報
    price: int | None  # 最安値（Noneなら取得失敗）
    product_name: str
    product_url: str
    search_url: str
    error: str | None

    # 以下はprice != Noneの場合のみ有効
    miles_earned: int = 0
    lsp_earned: float = 0.0
    lsp_value_yen: float = 0.0
    effective_price: float = 0.0  # 実質価格 = price - LSP価値

    # 全サイト最安値との比較
    price_diff: int = 0  # 最安値との価格差
    lsp_diff: float = 0.0  # 最安値ショップとのLSP差
    is_cheapest: bool = False

    @property
    def jal_url(self) -> str:
        """JALマイレージパーク経由URL"""
        if self.jal_shop:
            return self.jal_shop.url
        return ""

    @property
    def mile_rate_desc(self) -> str:
        if self.jal_shop:
            return self.jal_shop.mile_rate_desc
        return "不明"

    @property
    def yen_per_mile(self) -> int:
        if self.jal_shop:
            return self.jal_shop.yen_per_mile
        return 0


@dataclass
class AnalysisResult:
    """全ショップ比較の分析結果"""

    query: str
    comparisons: list[ShopComparison]  # 価格順にソート済
    cheapest: ShopComparison | None
    best_effective: ShopComparison | None  # 実質価格が最安のショップ

    @property
    def found_count(self) -> int:
        """商品が見つかったショップ数"""
        return sum(1 for c in self.comparisons if c.price is not None)

    @property
    def total_count(self) -> int:
        return len(self.comparisons)


def analyze(
    query: str,
    shop_prices: list[ShopPrice],
    config: Config,
) -> AnalysisResult:
    """各ショップの検索結果を分析してLSP込みの比較を行う"""
    jal_shops = get_shops()
    jal_shop_map = {s.name: s for s in jal_shops}

    comparisons: list[ShopComparison] = []

    for sp in shop_prices:
        jal_shop = jal_shop_map.get(sp.shop_name)

        comp = ShopComparison(
            shop_name=sp.shop_name,
            jal_shop=jal_shop,
            price=sp.price,
            product_name=sp.product_name,
            product_url=sp.product_url,
            search_url=sp.search_url,
            error=sp.error,
        )

        if sp.price is not None and sp.price > 0 and jal_shop:
            comp.miles_earned = jal_shop.calc_miles(sp.price)
            comp.lsp_earned = jal_shop.calc_lsp(sp.price)
            comp.lsp_value_yen = comp.lsp_earned * config.lsp_value_yen
            comp.effective_price = sp.price - comp.lsp_value_yen

        comparisons.append(comp)

    # 価格が取得できたショップだけで最安値を特定
    priced = [c for c in comparisons if c.price is not None and c.price > 0]

    cheapest = None
    best_effective = None

    if priced:
        cheapest = min(priced, key=lambda c: c.price)
        cheapest.is_cheapest = True
        cheapest_price = cheapest.price

        # 実質価格が最安のショップ
        best_effective = min(priced, key=lambda c: c.effective_price)

        # 各ショップの差分を計算
        for c in priced:
            c.price_diff = c.price - cheapest_price
            c.lsp_diff = c.lsp_earned - cheapest.lsp_earned

    # ソート: 価格取得済みを価格順 → 取得失敗を末尾
    priced.sort(key=lambda c: c.price)
    no_price = [c for c in comparisons if c.price is None or c.price == 0]
    sorted_comparisons = priced + no_price

    return AnalysisResult(
        query=query,
        comparisons=sorted_comparisons,
        cheapest=cheapest,
        best_effective=best_effective,
    )
