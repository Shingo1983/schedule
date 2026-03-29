"""転売アービトラージ検索モジュール

楽天・Yahoo!ショッピング・コジマネットで仕入れ、
Amazon or メルカリで転売した場合の損益を分析する。

条件:
- 物理サイズが小さい商品
- 仕入れ価格 30,000円以上
- 出品・配送コストを考慮しても買値とほぼ同額が返ってくる商品
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import quote

from .config import Config
from .site_scrapers import (
    ShopPrice, _fetch, _soup, _parse_price, _is_relevant_product,
    _is_bot_blocked_page, search_rakuten, search_yahoo, search_kojima,
    search_amazon,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 手数料モデル
# ---------------------------------------------------------------------------
@dataclass
class SellingFees:
    """販売プラットフォームの手数料計算結果"""
    platform: str
    sell_price: int
    commission: int          # 販売手数料
    shipping_cost: int       # 配送料（小型商品前提）
    other_fees: int = 0      # その他手数料
    net_proceeds: int = 0    # 手取り額（売値 - 手数料合計）

    def __post_init__(self):
        self.net_proceeds = self.sell_price - self.commission - self.shipping_cost - self.other_fees


def calc_amazon_fees(sell_price: int, category: str = "general") -> SellingFees:
    """Amazon出品手数料を計算

    Amazon手数料体系:
    - 販売手数料: カテゴリにより8-15%（最低手数料30円）
    - 基本成約料: 大口出品者は月額4,900円（1商品あたり換算せず）
                  小口出品者は1商品あたり100円
    - 配送料: 自己発送の場合、小型商品で約600-800円
              FBA利用の場合、小型で約400-600円
    """
    # カテゴリ別販売手数料率
    CATEGORY_RATES = {
        "electronics": 0.08,       # 家電・カメラ
        "pc": 0.08,                # パソコン・周辺機器
        "game": 0.10,              # TVゲーム
        "beauty": 0.10,            # ビューティー
        "health": 0.10,            # ドラッグストア
        "kitchen": 0.15,           # ホーム＆キッチン
        "toy": 0.10,               # おもちゃ
        "watch": 0.15,             # 腕時計
        "general": 0.10,           # その他
    }
    rate = CATEGORY_RATES.get(category, 0.10)
    commission = max(int(sell_price * rate), 30)

    # 小口出品者前提: 1商品あたり100円の成約料
    other_fees = 100

    # 自己発送: 小型商品（60サイズ以下）の配送料目安
    shipping_cost = 700

    return SellingFees(
        platform="Amazon",
        sell_price=sell_price,
        commission=commission,
        shipping_cost=shipping_cost,
        other_fees=other_fees,
    )


def calc_mercari_fees(sell_price: int) -> SellingFees:
    """メルカリ出品手数料を計算

    メルカリ手数料体系:
    - 販売手数料: 10%
    - 配送料（らくらくメルカリ便）:
      ネコポス: 210円（A4薄型、厚さ3cm以内）
      宅急便コンパクト: 450円（専用BOX70円含まず）
      宅急便60サイズ: 750円
    - 小型商品前提: 宅急便コンパクト（520円 = 450円+BOX70円）を想定
    """
    commission = int(sell_price * 0.10)
    # 宅急便コンパクト + 専用BOX代
    shipping_cost = 520

    return SellingFees(
        platform="メルカリ",
        sell_price=sell_price,
        commission=commission,
        shipping_cost=shipping_cost,
    )


# ---------------------------------------------------------------------------
# メルカリスクレイパー
# ---------------------------------------------------------------------------
def search_mercari(query: str) -> ShopPrice:
    """メルカリで商品の売買価格を検索（sold + on_sale）"""
    search_url = f"https://jp.mercari.com/search?keyword={quote(query)}&status=on_sale&order=desc&sort=price"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return ShopPrice("メルカリ", None, "", "", search_url,
                             error=f"HTTP {resp.status_code}")

        soup = _soup(resp)
        if _is_bot_blocked_page(soup):
            return ShopPrice("メルカリ", None, "", "", search_url,
                             error="アクセス制限")

        # メルカリ検索結果からの価格抽出
        # 複数のセレクタパターンを試行
        candidates = []

        # パターン1: 現行のメルカリHTML構造
        items = soup.select('[data-testid="item-cell"], [class*="ItemCell"], '
                            '[class*="merItemThumbnail"], li[class*="item"]')
        if not items:
            # パターン2: 汎用セレクタ
            items = soup.select('.items-box, .search-result-item, '
                                '[class*="SearchResultItem"]')

        for item in items:
            # 価格抽出
            price_el = item.select_one(
                '[class*="price"], [class*="Price"], '
                '[data-testid*="price"], .items-box-price, '
                'span[class*="amount"]'
            )
            if not price_el:
                continue
            price = _parse_price(price_el.get_text())
            if not price or price < 1000:
                continue

            # 商品名抽出
            name_el = item.select_one(
                '[class*="itemName"], [class*="ItemName"], '
                '[data-testid*="name"], .items-box-name, '
                '[class*="title"], [aria-label]'
            )
            name = ""
            if name_el:
                name = name_el.get_text(strip=True)
            if not name:
                # aria-label fallback
                for el in item.select("[aria-label]"):
                    label = el.get("aria-label", "")
                    if len(label) > 5:
                        name = label
                        break
            if not name:
                img = item.select_one("img[alt]")
                if img and img.get("alt"):
                    name = img["alt"]

            if name and not _is_relevant_product(query, name):
                continue

            # URL抽出
            url = ""
            link = item.select_one("a[href]")
            if link:
                href = link.get("href", "")
                if href.startswith("/"):
                    url = f"https://jp.mercari.com{href}"
                elif href.startswith("http"):
                    url = href

            candidates.append((price, name, url))

        # JSON-LD / Next.js データからの抽出フォールバック
        if not candidates:
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    import json
                    data = json.loads(script.string or "")
                    if isinstance(data, list):
                        for item in data:
                            p = item.get("offers", {}).get("price")
                            n = item.get("name", "")
                            if p and n:
                                candidates.append((int(float(p)), n, ""))
                    elif isinstance(data, dict):
                        p = data.get("offers", {}).get("price")
                        n = data.get("name", "")
                        if p and n:
                            candidates.append((int(float(p)), n, ""))
                except Exception:
                    pass

        # __NEXT_DATA__ からの抽出
        if not candidates:
            for script in soup.select("script#__NEXT_DATA__"):
                try:
                    import json
                    data = json.loads(script.string or "")
                    items_data = (data.get("props", {})
                                  .get("pageProps", {})
                                  .get("searchResult", {})
                                  .get("items", []))
                    for item in items_data:
                        p = item.get("price")
                        n = item.get("name", "")
                        if p and n and _is_relevant_product(query, n):
                            candidates.append((int(p), n, f"https://jp.mercari.com/item/{item.get('id', '')}"))
                except Exception:
                    pass

        if candidates:
            # 外れ値除去
            if len(candidates) >= 3:
                prices = sorted(c[0] for c in candidates)
                median = prices[len(prices) // 2]
                candidates = [c for c in candidates if c[0] >= median * 0.3]
            if candidates:
                best = min(candidates, key=lambda c: c[0])
                return ShopPrice("メルカリ", best[0], best[1], best[2], search_url)

        return ShopPrice("メルカリ", None, "", "", search_url)

    except Exception as e:
        logger.warning("Mercari scrape error: %s", e)
        return ShopPrice("メルカリ", None, "", "", search_url,
                         error="取得失敗（手動で検索してください）")


# ---------------------------------------------------------------------------
# アービトラージ候補商品リスト
# ---------------------------------------------------------------------------
# 条件: 小型、30,000円以上、流動性が高い（需要がある）商品カテゴリ
ARBITRAGE_CANDIDATES: list[dict] = [
    # --- Apple製品 ---
    {
        "query": "AirPods Pro 2",
        "category": "electronics",
        "note": "Apple純正イヤホン。定価安定、回転率高い",
        "size": "超小型",
    },
    {
        "query": "AirPods Max",
        "category": "electronics",
        "note": "Apple高級ヘッドホン。定価約85,000円",
        "size": "小型",
    },
    {
        "query": "Apple Watch Series 10",
        "category": "electronics",
        "note": "スマートウォッチ。モデルにより59,800円〜",
        "size": "超小型",
    },
    {
        "query": "Apple Watch Ultra 2",
        "category": "electronics",
        "note": "高価格帯Watch。定価128,800円",
        "size": "超小型",
    },
    {
        "query": "iPad mini 第7世代",
        "category": "electronics",
        "note": "コンパクトタブレット。定価78,800円〜",
        "size": "小型",
    },
    # --- ゲーム ---
    {
        "query": "Nintendo Switch 2",
        "category": "game",
        "note": "2025年発売の新型Switch。入手困難時にプレミア化",
        "size": "小型",
    },
    {
        "query": "PlayStation 5 Pro",
        "category": "game",
        "note": "PS5上位モデル。定価約80,000円",
        "size": "中型",
    },
    {
        "query": "Meta Quest 3S 256GB",
        "category": "game",
        "note": "VRヘッドセット。定価約48,400円",
        "size": "小型",
    },
    # --- 美容家電 ---
    {
        "query": "ReFa BEAUTECH ドライヤー",
        "category": "beauty",
        "note": "高級ドライヤー。定価約36,000円",
        "size": "小型",
    },
    {
        "query": "YA-MAN 美顔器",
        "category": "beauty",
        "note": "高級美顔器。3万円〜10万円帯",
        "size": "超小型",
    },
    {
        "query": "Dyson Airwrap",
        "category": "beauty",
        "note": "Dysonスタイラー。定価約60,000円〜",
        "size": "小型",
    },
    {
        "query": "パナソニック ナノケア ドライヤー EH-NA0J",
        "category": "beauty",
        "note": "人気ドライヤー。定価約38,000円",
        "size": "小型",
    },
    # --- カメラ・レンズ ---
    {
        "query": "SONY SEL50F14GM FE 50mm F1.4 GM",
        "category": "electronics",
        "note": "SONYの人気単焦点レンズ",
        "size": "小型",
    },
    {
        "query": "GoPro HERO13 Black",
        "category": "electronics",
        "note": "アクションカメラ。定価約62,800円",
        "size": "超小型",
    },
    # --- オーディオ ---
    {
        "query": "SONY WH-1000XM5",
        "category": "electronics",
        "note": "SONYノイキャンヘッドホン。定価約50,000円",
        "size": "小型",
    },
    {
        "query": "SONY WF-1000XM5",
        "category": "electronics",
        "note": "SONY完全ワイヤレスイヤホン。定価約42,000円",
        "size": "超小型",
    },
    {
        "query": "Bose QuietComfort Ultra Headphones",
        "category": "electronics",
        "note": "Bose最上位ノイキャン。定価約50,000円",
        "size": "小型",
    },
    # --- 高級コスメ ---
    {
        "query": "SK-II フェイシャルトリートメントエッセンス 230ml",
        "category": "beauty",
        "note": "人気化粧水。定価約30,000円",
        "size": "超小型",
    },
    {
        "query": "ポーラ BA クリーム",
        "category": "beauty",
        "note": "高級クリーム。定価約38,500円",
        "size": "超小型",
    },
    # --- スマートホーム ---
    {
        "query": "SwitchBot ロック Pro",
        "category": "electronics",
        "note": "スマートロック。キット合計3万円台",
        "size": "超小型",
    },
    # --- 腕時計 ---
    {
        "query": "GARMIN Venu 3",
        "category": "watch",
        "note": "GPSスマートウォッチ。定価約60,000円",
        "size": "超小型",
    },
    {
        "query": "G-SHOCK MRG",
        "category": "watch",
        "note": "カシオ高級G-SHOCK。3万円以上",
        "size": "超小型",
    },
]


# ---------------------------------------------------------------------------
# アービトラージ分析結果
# ---------------------------------------------------------------------------
@dataclass
class ArbitrageResult:
    """1商品のアービトラージ分析結果"""
    query: str
    category: str
    note: str
    size: str
    # 仕入れ先情報
    source_prices: dict[str, ShopPrice] = field(default_factory=dict)  # shop_name -> ShopPrice
    best_source: ShopPrice | None = None
    # 販売先情報
    amazon_price: ShopPrice | None = None
    mercari_price: ShopPrice | None = None
    # 損益分析
    amazon_fees: SellingFees | None = None
    mercari_fees: SellingFees | None = None
    amazon_profit: int = 0       # Amazon転売時の損益
    mercari_profit: int = 0      # メルカリ転売時の損益
    best_platform: str = ""      # 最も有利な販売先
    best_profit: int = 0         # 最大利益（マイナス=損失）
    is_viable: bool = False      # アービトラージ成立するか

    @property
    def best_source_price(self) -> int:
        """最安仕入れ価格"""
        return self.best_source.price if self.best_source and self.best_source.price else 0

    @property
    def profit_rate(self) -> float:
        """利益率（%）"""
        if self.best_source_price == 0:
            return 0.0
        return (self.best_profit / self.best_source_price) * 100


@dataclass
class ArbitrageReport:
    """アービトラージ全体レポート"""
    results: list[ArbitrageResult] = field(default_factory=list)
    viable_count: int = 0
    total_searched: int = 0

    @property
    def viable_results(self) -> list[ArbitrageResult]:
        return [r for r in self.results if r.is_viable]

    @property
    def near_breakeven_results(self) -> list[ArbitrageResult]:
        """損失が仕入れ額の5%以内の商品（ほぼ同額で売れる）"""
        return [r for r in self.results
                if r.best_source_price > 0
                and r.best_profit >= -int(r.best_source_price * 0.05)]


# ---------------------------------------------------------------------------
# メイン分析ロジック
# ---------------------------------------------------------------------------
def analyze_single(query: str, category: str, config: Config,
                   note: str = "", size: str = "") -> ArbitrageResult:
    """1商品についてアービトラージ分析を行う"""
    result = ArbitrageResult(
        query=query, category=category, note=note, size=size,
    )

    # --- 仕入れ先価格取得（並列） ---
    source_scrapers = [
        ("楽天市場", search_rakuten),
        ("Yahoo!ショッピング", search_yahoo),
        ("コジマネット", search_kojima),
    ]

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        for name, fn in source_scrapers:
            futures[executor.submit(fn, query, config)] = name
        for future in as_completed(futures):
            name = futures[future]
            try:
                sp = future.result(timeout=30)
                result.source_prices[name] = sp
            except Exception as e:
                logger.warning("Source scrape error %s: %s", name, e)

    # 最安仕入れ先を決定
    valid_sources = [(name, sp) for name, sp in result.source_prices.items()
                     if sp.price is not None and sp.price >= 30000]
    if valid_sources:
        best_name, best_sp = min(valid_sources, key=lambda x: x[1].price)
        result.best_source = best_sp

    if not result.best_source:
        return result  # 3万円以上の仕入れ先なし

    buy_price = result.best_source.price

    # --- 販売先価格取得（並列） ---
    with ThreadPoolExecutor(max_workers=2) as executor:
        amazon_future = executor.submit(search_amazon, query, config)
        mercari_future = executor.submit(search_mercari, query)

        try:
            result.amazon_price = amazon_future.result(timeout=30)
        except Exception as e:
            logger.warning("Amazon scrape error: %s", e)

        try:
            result.mercari_price = mercari_future.result(timeout=30)
        except Exception as e:
            logger.warning("Mercari scrape error: %s", e)

    # --- Amazon損益計算 ---
    if result.amazon_price and result.amazon_price.price:
        result.amazon_fees = calc_amazon_fees(result.amazon_price.price, category)
        result.amazon_profit = result.amazon_fees.net_proceeds - buy_price

    # --- メルカリ損益計算 ---
    if result.mercari_price and result.mercari_price.price:
        result.mercari_fees = calc_mercari_fees(result.mercari_price.price)
        result.mercari_profit = result.mercari_fees.net_proceeds - buy_price

    # --- 最良販売先の決定 ---
    profits = []
    if result.amazon_fees:
        profits.append(("Amazon", result.amazon_profit))
    if result.mercari_fees:
        profits.append(("メルカリ", result.mercari_profit))

    if profits:
        best = max(profits, key=lambda x: x[1])
        result.best_platform = best[0]
        result.best_profit = best[1]
        # 損失が仕入れ額の5%以内なら「ほぼ同額で売れる」と判断
        result.is_viable = best[1] >= -int(buy_price * 0.05)

    return result


def run_arbitrage_scan(config: Config,
                       candidates: list[dict] | None = None,
                       max_items: int = 0) -> ArbitrageReport:
    """候補商品リスト全体をスキャンしてアービトラージレポートを生成

    Args:
        config: 設定
        candidates: 検索候補リスト。Noneの場合はデフォルトリストを使用
        max_items: 最大検索数。0=全件
    """
    if candidates is None:
        candidates = ARBITRAGE_CANDIDATES

    if max_items > 0:
        candidates = candidates[:max_items]

    report = ArbitrageReport(total_searched=len(candidates))

    for i, cand in enumerate(candidates):
        query = cand["query"]
        logger.info("Arbitrage scan [%d/%d]: %s", i + 1, len(candidates), query)

        result = analyze_single(
            query=query,
            category=cand.get("category", "general"),
            config=config,
            note=cand.get("note", ""),
            size=cand.get("size", ""),
        )
        report.results.append(result)

        if result.is_viable:
            report.viable_count += 1
            logger.info("  → VIABLE: buy ¥%s, best sell net ¥%s (%s, profit ¥%s)",
                        f"{result.best_source_price:,}",
                        f"{result.best_profit + result.best_source_price:,}",
                        result.best_platform,
                        f"{result.best_profit:,}")
        elif result.best_source_price > 0:
            logger.info("  → buy ¥%s, best profit ¥%s (%s)",
                        f"{result.best_source_price:,}",
                        f"{result.best_profit:,}",
                        result.best_platform or "販売先なし")

    # 結果を利益率順にソート
    report.results.sort(key=lambda r: r.best_profit, reverse=True)

    return report


def run_single_arbitrage(query: str, config: Config,
                         category: str = "general") -> ArbitrageResult:
    """単一商品のアービトラージ分析"""
    return analyze_single(query, category, config)
