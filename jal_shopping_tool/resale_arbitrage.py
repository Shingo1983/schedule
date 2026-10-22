"""転売アービトラージ検索モジュール

楽天市場・Yahoo!ショッピング・コジマネットで仕入れ、
Amazon または メルカリで販売した場合の損益を分析する。

想定条件:
- 物理サイズが小さい商品（宅急便コンパクト〜80サイズ程度）
- 仕入れ価格 30,000円以上
- 出品手数料・配送料を差し引いても買値とほぼ同額が返ってくる商品

設計上の注意:
- 仕入れ価格は各ショップの「最安値（新品）」。ポイント還元を差し引いた
  実質仕入れ価格も併せて算出する（判定基準は FeeSettings.use_point_rebate で切替）。
- Amazon の販売価格は検索結果の最安値（新品）＝自分が合わせなければならない価格。
- メルカリの販売価格は「売り切れ」かつ「新品、未使用」の直近出品の中央値を優先し、
  取得できなければ出品中の最安値にフォールバックする。
- 手数料率・配送料は 2025〜2026 年時点の公開情報に基づく目安。FeeSettings で変更可能。
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterator
from urllib.parse import quote, urlencode

from .config import Config
from .site_scrapers import (
    ShopPrice,
    _HAS_PLAYWRIGHT,
    _HEADERS,
    _fetch,
    _is_bot_blocked_page,
    _is_relevant_product,
    _parse_price,
    _soup,
    search_amazon,
    search_kojima,
    search_rakuten,
    search_yahoo,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
BUY_SHOPS: tuple[str, ...] = ("楽天市場", "Yahoo!ショッピング", "コジマネット")
SELL_PLATFORMS: tuple[str, ...] = ("Amazon", "メルカリ")

# 梱包サイズ区分 → 配送料（円）
#   mercari: らくらくメルカリ便（2025年時点: ネコポス210 / コンパクト450+箱70 / 60:750 / 80:850 / 100:1,050）
#   amazon : 自己発送（ヤマト/ゆうパック持込等の一般料金の目安。メルカリ便より高い）
SIZE_TIERS: dict[str, dict] = {
    "ネコポス":   {"label": "ネコポス（A4・厚さ3cm以内）", "mercari": 210,  "amazon": 250},
    "コンパクト": {"label": "宅急便コンパクト（25×20×5cm）", "mercari": 520,  "amazon": 700},
    "60":        {"label": "60サイズ（3辺合計60cm以内）",   "mercari": 750,  "amazon": 950},
    "80":        {"label": "80サイズ（3辺合計80cm以内）",   "mercari": 850,  "amazon": 1150},
    "100":       {"label": "100サイズ（3辺合計100cm以内）", "mercari": 1050, "amazon": 1400},
}
DEFAULT_SIZE_TIER = "60"

# 旧表記との互換（候補リストや CLI 引数で使う短い呼び名）
SIZE_ALIASES: dict[str, str] = {
    "超小型": "コンパクト", "小型": "60", "中型": "80", "大型": "100",
    "compact": "コンパクト", "nekopos": "ネコポス",
}

# Amazon.co.jp カテゴリー別販売手数料（税抜の目安）
#   エレクトロニクス/カメラ/PC/ゲーム機本体: 8%、ビューティー/ドラッグストア/おもちゃ/生活家電: 10%、
#   TVゲーム（ソフト・アクセサリ）/腕時計/ホーム&キッチン: 15%
AMAZON_REFERRAL_RATES: dict[str, float] = {
    "electronics": 0.08,    # 家電&カメラ（オーディオ・イヤホン等）
    "camera": 0.08,         # カメラ・レンズ
    "pc": 0.08,             # パソコン・周辺機器・タブレット
    "game_console": 0.08,   # ゲーム機本体
    "game": 0.15,           # TVゲーム（ソフト・アクセサリ）
    "appliance": 0.10,      # 生活家電（シェーバー等）
    "beauty": 0.10,         # ビューティー（ドライヤー・美顔器・化粧品）
    "health": 0.10,         # ドラッグストア
    "toy": 0.10,            # おもちゃ&ホビー
    "watch": 0.15,          # 腕時計
    "kitchen": 0.15,        # ホーム&キッチン
    "general": 0.10,        # その他
}
AMAZON_CATEGORY_LABELS: dict[str, str] = {
    "electronics": "家電&カメラ", "camera": "カメラ", "pc": "PC・タブレット",
    "game_console": "ゲーム機本体", "game": "TVゲーム", "appliance": "生活家電",
    "beauty": "ビューティー", "health": "ドラッグストア", "toy": "おもちゃ",
    "watch": "腕時計", "kitchen": "ホーム&キッチン", "general": "その他",
}


def normalize_size(size: str | None) -> str:
    """サイズ表記を SIZE_TIERS のキーに正規化"""
    if not size:
        return DEFAULT_SIZE_TIER
    s = str(size).strip()
    s = SIZE_ALIASES.get(s, s)
    return s if s in SIZE_TIERS else DEFAULT_SIZE_TIER


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass
class FeeSettings:
    """手数料・判定条件の設定（全て上書き可能）"""

    # --- 条件 ---
    min_buy_price: int = 30000          # この金額未満の商品は対象外
    breakeven_tolerance: float = 0.05   # 仕入れ額に対する許容損失率（5% = ほぼ同額）
    use_point_rebate: bool = True       # 判定にポイント還元を含めるか

    # --- 仕入れ側ポイント還元率（ショップ名 → 率） ---
    point_rates: dict[str, float] = field(default_factory=dict)

    # --- Amazon ---
    amazon_plan: str = "small"          # "small"=小口（1点100円） / "pro"=大口（月額、成約料なし）
    amazon_closing_fee: int = 100       # 小口出品 基本成約料（税抜）
    amazon_fee_tax_rate: float = 0.10   # 手数料に加算される消費税
    amazon_min_referral_fee: int = 30   # 最低販売手数料
    amazon_referral_rates: dict[str, float] = field(
        default_factory=lambda: dict(AMAZON_REFERRAL_RATES))

    # --- メルカリ ---
    mercari_rate: float = 0.10          # 販売手数料
    mercari_payout_fee: int = 0         # 振込手数料（200円/回。1点ごとに乗せるなら200）

    # --- 共通 ---
    packaging_cost: int = 80            # 梱包材（箱・緩衝材）の目安
    inter_item_delay: float = 1.0       # スキャン時の商品間待機秒数（bot検出対策）

    def __post_init__(self):
        # 外部入力（フォーム/CLI）をそのまま受けるため、ここで必ず妥当な範囲に丸める
        try:
            tol = float(self.breakeven_tolerance)
        except (TypeError, ValueError):
            tol = 0.05
        if not math.isfinite(tol):
            tol = 0.05
        self.breakeven_tolerance = min(max(tol, 0.0), 1.0)
        try:
            self.min_buy_price = max(int(self.min_buy_price), 0)
        except (TypeError, ValueError, OverflowError):
            self.min_buy_price = 30000
        if self.amazon_plan not in ("small", "pro"):
            self.amazon_plan = "small"
        self.use_point_rebate = bool(self.use_point_rebate)
        cleaned: dict[str, float] = {}
        for k, v in (self.point_rates or {}).items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(fv):
                cleaned[k] = min(max(fv, 0.0), 1.0)
        self.point_rates = cleaned

    @classmethod
    def from_config(cls, config: Config, **overrides) -> "FeeSettings":
        settings = cls(**overrides)
        if not settings.point_rates:
            settings.point_rates = {
                "楽天市場": float(config.rakuten_point_rate or 0.0),
                "Yahoo!ショッピング": float(config.yahoo_paypay_rate or 0.0),
                "コジマネット": 0.01,
            }
            settings.__post_init__()
        return settings

    def effective_price(self, shop_name: str, price: int) -> int:
        """ポイント還元を差し引いた実質価格"""
        return int(round(price * (1.0 - self.point_rates.get(shop_name, 0.0))))

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 手数料計算
# ---------------------------------------------------------------------------
@dataclass
class SellingFees:
    """販売プラットフォームの手数料内訳"""
    platform: str
    sell_price: int
    commission: int           # 販売手数料（税込）
    closing_fee: int = 0      # 成約料・振込手数料など固定費（税込）
    shipping_cost: int = 0    # 配送料
    packaging_cost: int = 0   # 梱包材
    rate_label: str = ""      # 表示用（例: "8% + 消費税"）
    shipping_label: str = ""  # 表示用（例: "60サイズ"）

    @property
    def total_fees(self) -> int:
        return self.commission + self.closing_fee + self.shipping_cost + self.packaging_cost

    @property
    def net_proceeds(self) -> int:
        return self.sell_price - self.total_fees

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_fees"] = self.total_fees
        d["net_proceeds"] = self.net_proceeds
        return d


def calc_amazon_fees(sell_price: int, category: str = "general",
                     size: str = DEFAULT_SIZE_TIER,
                     settings: FeeSettings | None = None) -> SellingFees:
    """Amazon（自己発送）の手数料を計算

    - 販売手数料: カテゴリー別 8〜15%（税抜）＋消費税
    - 基本成約料: 小口出品のみ 1点 100円（税抜）＋消費税
    - 配送料: 自己発送の目安（サイズ区分別）
    """
    s = settings or FeeSettings()
    rate = s.amazon_referral_rates.get(category, s.amazon_referral_rates.get("general", 0.10))
    tax = 1.0 + s.amazon_fee_tax_rate
    commission = max(int(round(sell_price * rate)), s.amazon_min_referral_fee)
    commission = int(round(commission * tax))
    closing = int(round(s.amazon_closing_fee * tax)) if s.amazon_plan == "small" else 0
    tier = SIZE_TIERS[normalize_size(size)]
    return SellingFees(
        platform="Amazon",
        sell_price=sell_price,
        commission=commission,
        closing_fee=closing,
        shipping_cost=tier["amazon"],
        packaging_cost=s.packaging_cost,
        rate_label=f"{rate * 100:.0f}%＋消費税" if s.amazon_fee_tax_rate else f"{rate * 100:.0f}%",
        shipping_label=tier["label"],
    )


def calc_mercari_fees(sell_price: int, size: str = DEFAULT_SIZE_TIER,
                      settings: FeeSettings | None = None) -> SellingFees:
    """メルカリの手数料を計算

    - 販売手数料: 10%
    - 配送料: らくらくメルカリ便（サイズ区分別）
    - 振込手数料: 任意（デフォルト 0。まとめて振り込むなら無視できる）
    """
    s = settings or FeeSettings()
    tier = SIZE_TIERS[normalize_size(size)]
    return SellingFees(
        platform="メルカリ",
        sell_price=sell_price,
        commission=int(round(sell_price * s.mercari_rate)),
        closing_fee=s.mercari_payout_fee,
        shipping_cost=tier["mercari"],
        packaging_cost=s.packaging_cost,
        rate_label=f"{s.mercari_rate * 100:.0f}%",
        shipping_label=f"らくらくメルカリ便 {tier['label']}",
    )


# ---------------------------------------------------------------------------
# 販売先の相場
# ---------------------------------------------------------------------------
@dataclass
class SellQuote:
    """販売先での相場（1プラットフォーム分）"""
    platform: str
    price: int | None                 # 判定に使う価格
    product_name: str = ""
    product_url: str = ""
    search_url: str = ""
    error: str | None = None
    basis: str = ""                   # "最安値" / "売却済み中央値" など
    sample_count: int = 0
    sample_prices: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _shopprice_to_quote(sp: ShopPrice, platform: str, basis: str) -> SellQuote:
    return SellQuote(
        platform=platform, price=sp.price, product_name=sp.product_name,
        product_url=sp.product_url, search_url=sp.search_url, error=sp.error,
        basis=basis if sp.price else "", sample_count=1 if sp.price else 0,
        sample_prices=[sp.price] if sp.price else [],
    )


def fetch_amazon_quote(query: str, config: Config) -> SellQuote:
    """Amazon の相場（新品最安値）を取得"""
    try:
        sp = search_amazon(query, config)
    except Exception as e:  # noqa: BLE001
        logger.warning("Amazon quote error: %s", e)
        sp = ShopPrice("Amazon.co.jp", None, "", "",
                       f"https://www.amazon.co.jp/s?k={quote(query)}",
                       error="取得失敗（手動で検索してください）")
    return _shopprice_to_quote(sp, "Amazon", "新品最安値")


# --- メルカリ ---------------------------------------------------------------
def mercari_search_url(query: str, sold: bool = True, new_only: bool = True) -> str:
    params = {"keyword": query}
    if sold:
        params["status"] = "sold_out"
        params["sort"] = "created_time"
        params["order"] = "desc"
    else:
        params["status"] = "on_sale"
        params["sort"] = "price"
        params["order"] = "asc"
    if new_only:
        params["item_condition_id"] = "1"   # 新品、未使用
    return "https://jp.mercari.com/search?" + urlencode(params)


def _price_from_node(node: dict):
    """商品らしき dict から価格を取り出す（直下の price、または schema.org の offers）"""
    price = node.get("price")
    if price is None:
        offers = node.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")
    if price is None:
        return 0
    try:
        return int(float(str(price).replace(",", "").replace("¥", "")))
    except ValueError:
        return 0


def _walk_json_for_items(node, out: list[dict], depth: int = 0) -> None:
    """ネストした JSON から {name, price, id} を持つ商品らしき dict を収集"""
    if depth > 12:
        return
    if isinstance(node, dict):
        name = node.get("name") or node.get("itemName")
        if isinstance(name, str):
            p = _price_from_node(node)
            if p > 0:
                out.append({
                    "name": name,
                    "price": p,
                    "id": str(node.get("id") or node.get("itemId") or ""),
                    "status": str(node.get("status") or node.get("itemStatus") or ""),
                })
        for v in node.values():
            _walk_json_for_items(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_json_for_items(v, out, depth + 1)


_QUERY_SPLIT_RE = re.compile(r'[\s　/／\-]+')          # _is_relevant_product と同じ分割
_MODEL_TOKEN_RE = re.compile(r'^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]+$')  # 1000XM5, NA0J, 42mm


def _mercari_relevant(query: str, name: str) -> bool:
    """メルカリ出品タイトル向けの関連性判定

    - 個人出品はクエリと一致しやすい短いタイトルが多い → エコーバック検出は無効化
    - ブランド名を省いたタイトル（例: 「WF-1000XM5 ブラック 新品」）も多いので、
      先頭語がブランドらしく（英字/かな）、残りに型番らしい語（英数字混在）がある場合は
      先頭語を除いた条件でも再判定する。「AirPods Pro 3」→「Pro 3」のような
      緩すぎる再判定は型番条件で防ぐ。
    """
    if _is_relevant_product(query, name, allow_echo=True):
        return True
    tokens = [t for t in _QUERY_SPLIT_RE.split(query) if t]
    if (len(tokens) >= 3 and tokens[0].isalpha()
            and any(_MODEL_TOKEN_RE.match(t) for t in tokens[1:])):
        return _is_relevant_product(" ".join(tokens[1:]), name, allow_echo=True)
    return False


def _parse_mercari_html(html_or_soup, query: str) -> list[tuple[int, str, str]]:
    """メルカリ検索ページから (価格, 商品名, URL) をページ掲載順で抽出。複数の構造に対応。"""
    from bs4 import BeautifulSoup
    soup = html_or_soup if isinstance(html_or_soup, BeautifulSoup) \
        else BeautifulSoup(html_or_soup, "lxml")
    found: list[tuple[int, str, str]] = []

    def _add(price, name, url):
        # 名前が取れない要素は相場サンプルとして信用できないので捨てる
        if not price or price < 1000 or not name:
            return
        if not _mercari_relevant(query, name):
            return
        found.append((int(price), name, url or ""))

    # 1) Web Components 版: <mer-item-thumbnail item-name="..." price="..." sticker="sold">
    for el in soup.select("mer-item-thumbnail[price]"):
        price = _parse_price(el.get("price", ""))
        name = el.get("item-name", "") or el.get("alt", "")
        link = el.find_parent("a")
        href = link.get("href", "") if link else ""
        _add(price, name, _abs_mercari(href))

    # 2) data-testid 版
    if not found:
        for cell in soup.select('li[data-testid="item-cell"], [data-testid="item-cell"]'):
            name_el = cell.select_one('[data-testid*="item-name"], [data-testid*="itemName"], '
                                      '[class*="itemName"], [class*="ItemName"]')
            price_el = cell.select_one('[data-testid*="price"], [class*="price"], '
                                       '[class*="Price"], span[class*="number"]')
            name = name_el.get_text(" ", strip=True) if name_el else ""
            if not name:
                img = cell.select_one("img[alt]")
                name = img["alt"] if img else ""
            price = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None
            if price is None:
                price = _parse_price(cell.get_text(" ", strip=True))
            link = cell.select_one("a[href]")
            _add(price, name, _abs_mercari(link.get("href", "") if link else ""))

    # 3) __NEXT_DATA__ / 埋め込み JSON
    if not found:
        for script in soup.select('script#__NEXT_DATA__, script[type="application/json"]'):
            try:
                data = json.loads(script.string or script.get_text() or "")
            except Exception:  # noqa: BLE001
                continue
            items: list[dict] = []
            _walk_json_for_items(data, items)
            for it in items:
                url = f"https://jp.mercari.com/item/{it['id']}" if it["id"].startswith("m") else ""
                _add(it["price"], it["name"], url)

    # 4) JSON-LD（ItemList → Product{name, offers{price}}）
    if not found:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except Exception:  # noqa: BLE001
                continue
            items: list[dict] = []
            _walk_json_for_items(data, items)
            for it in items:
                _add(it["price"], it["name"], "")

    # 重複除去（URL or 名前+価格）。掲載順は保持
    uniq: dict[tuple, tuple[int, str, str]] = {}
    for price, name, url in found:
        key = (url or name, price)
        uniq.setdefault(key, (price, name, url))
    return list(uniq.values())


def _abs_mercari(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://jp.mercari.com" + href
    return ""


RenderFn = Callable[[str, str], "str | None"]


@contextmanager
def _browser_session() -> Iterator[tuple[RenderFn | None, str | None]]:
    """Playwright ブラウザを1回だけ起動し、render(url, wait_selector) -> html|None を提供する。

    yield (None, 理由) の場合はブラウザが使えない（未導入 / 起動失敗）。
    """
    if not _HAS_PLAYWRIGHT:
        yield None, "Playwright未導入"
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        yield None, f"Playwright読込失敗: {e}"
        return
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception as e:  # noqa: BLE001
                logger.warning("Playwright launch failed: %s", e)
                yield None, "ブラウザ起動失敗（playwright install chromium が必要）"
                return
            try:
                context = browser.new_context(
                    user_agent=_HEADERS["User-Agent"], locale="ja-JP",
                    viewport={"width": 1366, "height": 900},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

                def render(url: str, wait_selector: str, timeout_ms: int = 25000) -> str | None:
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                        try:
                            page.wait_for_selector(wait_selector, timeout=8000)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            page.wait_for_load_state("networkidle", timeout=4000)
                        except Exception:  # noqa: BLE001
                            pass
                        page.wait_for_timeout(500)
                        return page.content()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Playwright render error (%s): %s", url, e)
                        return None
                    finally:
                        page.close()

                yield render, None
            finally:
                browser.close()
    except GeneratorExit:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("Playwright session error: %s", e)
        yield None, f"ブラウザエラー: {e}"


_MERCARI_WAIT_SELECTOR = 'mer-item-thumbnail, [data-testid="item-cell"]'


def _quote_from_sold(items: list[tuple[int, str, str]], url: str, max_samples: int) -> SellQuote:
    """売り切れ一覧（新しい順）から相場を決める: 直近 N 件 → 外れ値除去 → 中央値"""
    recent = [p for p, _, _ in items][:max_samples]
    med0 = statistics.median(recent)
    kept = [p for p in recent if p >= med0 * 0.3] or recent
    median = int(statistics.median(kept))
    rep = min(items, key=lambda t: abs(t[0] - median))
    return SellQuote(
        platform="メルカリ", price=median, product_name=rep[1], product_url=rep[2],
        search_url=url, basis="売却済み(新品)中央値",
        sample_count=len(kept), sample_prices=sorted(kept),
    )


def _quote_from_onsale(items: list[tuple[int, str, str]], url: str, max_samples: int) -> SellQuote:
    """出品中一覧から相場を決める: 外れ値除去 → 最安値（まだ売れていない価格の下限）"""
    prices = sorted(p for p, _, _ in items)
    if len(prices) >= 3:
        med = prices[len(prices) // 2]
        prices = [p for p in prices if p >= med * 0.3] or prices
    low = prices[0]
    rep = min(items, key=lambda t: abs(t[0] - low))
    return SellQuote(
        platform="メルカリ", price=low, product_name=rep[1], product_url=rep[2],
        search_url=url, basis="出品中(新品)最安値",
        sample_count=len(prices), sample_prices=prices[:max_samples],
    )


def fetch_mercari_quote(query: str, max_samples: int = 12) -> SellQuote:
    """メルカリの相場を取得

    優先順位:
      1. 売り切れ × 新品未使用 の直近出品 → 中央値（実際に売れた価格）
      2. 出品中 × 新品未使用 の最安値（まだ売れていないが相場の下限）
    メルカリは JavaScript 描画の SPA なので、通常の HTTP 取得で商品が得られない場合は
    Playwright（インストール時のみ、ブラウザは1回だけ起動）で描画する。
    どちらも不可なら手動確認用 URL とエラー理由を返す。
    """
    sold_url = mercari_search_url(query, sold=True)
    onsale_url = mercari_search_url(query, sold=False)
    errors: list[str] = []

    def _http_items(url: str) -> list[tuple[int, str, str]]:
        try:
            resp = _fetch(url)
        except Exception as e:  # noqa: BLE001
            logger.info("Mercari fetch error: %s", e)
            errors.append("接続エラー")
            return []
        if resp.status_code != 200:
            errors.append(f"HTTP {resp.status_code}")
            return []
        soup = _soup(resp)
        if _is_bot_blocked_page(soup):
            errors.append("アクセス制限")
            return []
        return _parse_mercari_html(soup, query)

    # --- 1. 通常の HTTP 取得 ---
    items = _http_items(sold_url)
    if items:
        return _quote_from_sold(items, sold_url, max_samples)
    items = _http_items(onsale_url)
    if items:
        return _quote_from_onsale(items, onsale_url, max_samples)

    # --- 2. ブラウザ描画（SPA シェルしか返らない場合）---
    with _browser_session() as (render, unavailable):
        if render is None:
            reason = errors[0] if errors else f"要ブラウザ描画（{unavailable}）"
            return SellQuote(platform="メルカリ", price=None, search_url=sold_url, error=reason)
        for url, make_quote in ((sold_url, _quote_from_sold), (onsale_url, _quote_from_onsale)):
            html = render(url, _MERCARI_WAIT_SELECTOR)
            if not html:
                errors.append("描画失敗")
                continue
            soup = _soup_from_html(html)
            if _is_bot_blocked_page(soup):
                errors.append("アクセス制限（ブラウザ）")
                continue
            items = _parse_mercari_html(soup, query)
            if items:
                return make_quote(items, url, max_samples)

    return SellQuote(platform="メルカリ", price=None, search_url=sold_url,
                     error=errors[0] if errors else "該当商品なし")


def _soup_from_html(html: str):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# 候補商品
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    query: str
    category: str = "general"       # AMAZON_REFERRAL_RATES のキー
    size: str = DEFAULT_SIZE_TIER   # SIZE_TIERS のキー（別名可）
    note: str = ""
    amazon_restricted: bool = False  # Amazon で新品出品にブランド承認が必要（要申請）

    def __post_init__(self):
        self.size = normalize_size(self.size)
        if self.category not in AMAZON_REFERRAL_RATES:
            self.category = "general"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["size_label"] = SIZE_TIERS[self.size]["label"]
        d["category_label"] = AMAZON_CATEGORY_LABELS.get(self.category, self.category)
        return d


# 小型・3万円以上・需要が安定している商品。型番/世代を明記して各サイトの検索結果を揃える。
# ※ 価格はあくまで目安。Apple 製品は Amazon で新品出品にブランド承認が必要。
ARBITRAGE_CANDIDATES: list[Candidate] = [
    # --- Apple ---
    Candidate("AirPods Pro 3", "electronics", "コンパクト",
              "Apple定価39,800円。回転率が高く相場が安定", amazon_restricted=True),
    Candidate("AirPods Max USB-C", "electronics", "60",
              "Apple定価84,800円。値引きは少ないがポイント還元で差が出やすい", amazon_restricted=True),
    Candidate("Apple Watch Series 11 GPS 42mm", "electronics", "コンパクト",
              "6万円台。サイズ/ケース素材で価格が分かれるため型番確認必須", amazon_restricted=True),
    Candidate("Apple Watch Ultra 3", "electronics", "コンパクト",
              "約13万円。単価が高い分、手数料額も大きい", amazon_restricted=True),
    Candidate("iPad mini A17 Pro Wi-Fi 128GB", "pc", "60",
              "定価78,800円。容量違いに注意", amazon_restricted=True),
    # --- ゲーム ---
    Candidate("Nintendo Switch 2 本体 日本語版", "game_console", "80",
              "定価49,980円。品薄時はメルカリで定価超え。購入制限・転売規制あり"),
    Candidate("Nintendo Switch 2 マリオカート ワールド セット", "game_console", "80",
              "定価53,980円。同梱版はプレミアが付きやすい"),
    Candidate("Meta Quest 3S 128GB", "electronics", "80",
              "定価48,400円。256GB版は64,900円"),
    # --- オーディオ ---
    Candidate("SONY WF-1000XM5", "electronics", "コンパクト",
              "定価41,800円。楽天/Yahooのセールで3万円台前半になりやすい"),
    Candidate("SONY WH-1000XM6", "electronics", "60",
              "2025年発売。定価59,400円"),
    Candidate("SONY WH-1000XM5", "electronics", "60",
              "型落ちで値下がり傾向。4万円前後"),
    Candidate("Bose QuietComfort Ultra Headphones", "electronics", "60",
              "5〜6万円。世代（第1/第2）で相場が異なる"),
    # --- カメラ・映像 ---
    Candidate("DJI Osmo Pocket 3", "camera", "コンパクト",
              "定価74,800円。品薄が続きメルカリ相場が強い"),
    Candidate("Insta360 X5", "camera", "コンパクト",
              "2025年発売。約8.5万円"),
    Candidate("GoPro HERO13 Black", "camera", "コンパクト",
              "定価62,800円"),
    Candidate("SONY SEL50F14GM", "camera", "60",
              "FE 50mm F1.4 GM。約20万円。小型だが単価が高く手数料額も大きい"),
    # --- 美容家電・理美容 ---
    Candidate("Dyson Airwrap i.d. Complete Long", "beauty", "60",
              "約7万円。型番（HS08系）と付属アタッチメントを確認"),
    Candidate("ReFa BEAUTECH DRYER PRO", "beauty", "60",
              "4万円前後。正規品/並行品の表記に注意"),
    Candidate("パナソニック ナノケア EH-NA0J", "beauty", "60",
              "3万円台。カラー違いで相場差あり"),
    Candidate("パナソニック ラムダッシュ パームイン ES-PV6A", "appliance", "コンパクト",
              "約3.3万円。手のひらサイズのシェーバー"),
    Candidate("ブラウン シリーズ9 Pro+", "appliance", "60",
              "5万円前後。型番末尾（cc等）で付属品が異なる"),
    Candidate("ヤーマン フォトプラス プレステージ S", "beauty", "コンパクト",
              "約6万円の美顔器。公式限定モデルに注意"),
    # --- 高級コスメ ---
    Candidate("SK-II フェイシャルトリートメントエッセンス 230ml", "beauty", "コンパクト",
              "3万円前後。並行輸入品はメルカリで敬遠されるため正規品表記を確認"),
    Candidate("ドゥ・ラ・メール クレーム ドゥ・ラ・メール 60ml", "beauty", "コンパクト",
              "約6万円。Amazonはビューティー要承認の場合あり"),
    # --- 腕時計 ---
    Candidate("GARMIN Venu 3", "watch", "コンパクト",
              "定価60,800円。腕時計カテゴリは手数料15%で不利"),
    Candidate("G-SHOCK GMW-B5000D-1JF", "watch", "コンパクト",
              "フルメタル。7万円前後で相場が安定"),
]


def find_candidate(query: str) -> Candidate | None:
    q = query.strip().lower()
    for c in ARBITRAGE_CANDIDATES:
        if c.query.lower() == q:
            return c
    return None


def resolve_candidate_meta(query: str, category: str | None = None,
                           size: str | None = None) -> tuple[str, str, str, bool]:
    """呼び出し側の指定を優先し、未指定（None/空）の項目だけ候補リストから補完する

    Returns: (category, size, note, amazon_restricted)
    """
    c = find_candidate(query)
    if category in AMAZON_REFERRAL_RATES:
        cat = category
    else:
        cat = c.category if c else "general"
    if size:
        sz = normalize_size(size)
    else:
        sz = c.size if c else DEFAULT_SIZE_TIER
    return cat, sz, (c.note if c else ""), (c.amazon_restricted if c else False)


# ---------------------------------------------------------------------------
# 分析結果
# ---------------------------------------------------------------------------
VERDICT_LABELS: dict[str, str] = {
    "profit": "利益あり",
    "breakeven": "ほぼ同額",
    "near": "やや損",
    "loss": "損失",
    "no_sell_data": "販売相場 取得不可",
    "no_buy_data": "仕入れ先 取得不可",
    "below_min": "対象外（仕入れ額 条件未満）",
}


@dataclass
class PlatformResult:
    """販売先1つ分の損益"""
    platform: str
    quote: SellQuote
    fees: SellingFees | None = None
    profit_nominal: int | None = None     # 手取り − 仕入れ額（ポイント考慮なし）
    profit_effective: int | None = None   # 手取り − 実質仕入れ額（ポイント考慮あり）

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "quote": self.quote.to_dict(),
            "fees": self.fees.to_dict() if self.fees else None,
            "profit_nominal": self.profit_nominal,
            "profit_effective": self.profit_effective,
        }


@dataclass
class ArbitrageResult:
    """1商品のアービトラージ分析結果"""
    query: str
    category: str = "general"
    size: str = DEFAULT_SIZE_TIER
    note: str = ""
    amazon_restricted: bool = False

    buy_quotes: dict[str, ShopPrice] = field(default_factory=dict)
    best_buy: ShopPrice | None = None
    point_rate: float = 0.0
    meets_min_price: bool = False

    platforms: dict[str, PlatformResult] = field(default_factory=dict)
    judged_on_effective: bool = True

    best_platform: str = ""
    best_profit: int | None = None
    verdict: str = "no_buy_data"

    # --- 派生値 ---
    @property
    def buy_price(self) -> int:
        return int(self.best_buy.price) if self.best_buy and self.best_buy.price else 0

    @property
    def point_rebate(self) -> int:
        return int(round(self.buy_price * self.point_rate))

    @property
    def effective_buy_price(self) -> int:
        return self.buy_price - self.point_rebate

    @property
    def judged_buy_price(self) -> int:
        """判定基準の仕入れ額（ポイント込み or 名目）"""
        return self.effective_buy_price if self.judged_on_effective else self.buy_price

    @property
    def best_buy_shop(self) -> str:
        return self.best_buy.shop_name if self.best_buy else ""

    @property
    def best_net_proceeds(self) -> int | None:
        pr = self.platforms.get(self.best_platform)
        return pr.fees.net_proceeds if pr and pr.fees else None

    @property
    def profit_rate(self) -> float:
        """判定基準の仕入れ額に対する損益率（%）"""
        base = self.judged_buy_price
        if not base or self.best_profit is None:
            return 0.0
        return self.best_profit / base * 100.0

    @property
    def has_sell_data(self) -> bool:
        return any(p.fees is not None for p in self.platforms.values())

    @property
    def sell_error_summary(self) -> str:
        """相場が取れなかった販売先と理由（表示用）"""
        parts = []
        for name, pr in self.platforms.items():
            if not pr.quote.price:
                parts.append(f"{name}: {pr.quote.error or '該当商品なし'}")
        return " / ".join(parts)

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)

    @property
    def size_label(self) -> str:
        return SIZE_TIERS[normalize_size(self.size)]["label"]

    @property
    def category_label(self) -> str:
        return AMAZON_CATEGORY_LABELS.get(self.category, self.category)

    @property
    def buy_rows(self) -> list[ShopPrice]:
        """テンプレート用: 価格昇順（取得失敗は末尾）"""
        return sorted(self.buy_quotes.values(),
                      key=lambda sp: (sp.price is None, sp.price or 0))

    @property
    def amazon(self) -> PlatformResult | None:
        return self.platforms.get("Amazon")

    @property
    def mercari(self) -> PlatformResult | None:
        return self.platforms.get("メルカリ")

    @property
    def is_viable(self) -> bool:
        return self.verdict in ("profit", "breakeven")

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "category": self.category,
            "category_label": self.category_label,
            "size": self.size,
            "size_label": self.size_label,
            "note": self.note,
            "amazon_restricted": self.amazon_restricted,
            "buy_quotes": {k: asdict(v) for k, v in self.buy_quotes.items()},
            "best_buy_shop": self.best_buy_shop,
            "buy_price": self.buy_price,
            "point_rate": self.point_rate,
            "point_rebate": self.point_rebate,
            "effective_buy_price": self.effective_buy_price,
            "meets_min_price": self.meets_min_price,
            "platforms": {k: v.to_dict() for k, v in self.platforms.items()},
            "judged_on_effective": self.judged_on_effective,
            "best_platform": self.best_platform,
            "best_profit": self.best_profit,
            "best_net_proceeds": self.best_net_proceeds,
            "profit_rate": round(self.profit_rate, 2),
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "sell_error_summary": self.sell_error_summary,
        }


@dataclass
class ArbitrageReport:
    """スキャン全体のレポート"""
    results: list[ArbitrageResult] = field(default_factory=list)
    total_searched: int = 0
    settings: FeeSettings | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def viable_results(self) -> list[ArbitrageResult]:
        """利益あり or ほぼ同額（許容損失内）"""
        return [r for r in self.results if r.is_viable]

    @property
    def viable_count(self) -> int:
        return len(self.viable_results)

    @property
    def analyzed_results(self) -> list[ArbitrageResult]:
        """仕入れ価格が取れた商品（条件未満も含む）"""
        return [r for r in self.results if r.buy_price > 0]

    @property
    def ranked_results(self) -> list[ArbitrageResult]:
        """損益の良い順（損益不明は末尾）"""
        return sorted(self.results, key=_rank_key)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0

    def to_dict(self) -> dict:
        return {
            "total_searched": self.total_searched,
            "viable_count": self.viable_count,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "settings": self.settings.to_dict() if self.settings else None,
            "results": [r.to_dict() for r in self.ranked_results],
        }


def _rank_key(r: ArbitrageResult):
    order = {"profit": 0, "breakeven": 1, "near": 2, "loss": 3,
             "no_sell_data": 4, "below_min": 5, "no_buy_data": 6}
    return (order.get(r.verdict, 9), -(r.best_profit if r.best_profit is not None else -10**9))


# ---------------------------------------------------------------------------
# 分析ロジック
# ---------------------------------------------------------------------------
BUY_SCRAPERS: dict[str, Callable[[str, Config], ShopPrice]] = {
    "楽天市場": search_rakuten,
    "Yahoo!ショッピング": search_yahoo,
    "コジマネット": search_kojima,
}


def fetch_buy_quotes(query: str, config: Config,
                     shops: tuple[str, ...] = BUY_SHOPS) -> dict[str, ShopPrice]:
    """仕入れ先の最安値を並列取得"""
    quotes: dict[str, ShopPrice] = {}
    with ThreadPoolExecutor(max_workers=len(shops)) as ex:
        futures = {ex.submit(BUY_SCRAPERS[name], query, config): name for name in shops}
        for fut, name in futures.items():
            try:
                quotes[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("Buy quote error %s: %s", name, e)
                quotes[name] = ShopPrice(name, None, "", "", "", error=f"取得失敗: {e}")
    return quotes


def fetch_sell_quotes(query: str, config: Config) -> dict[str, SellQuote]:
    """販売先の相場を並列取得"""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_amz = ex.submit(fetch_amazon_quote, query, config)
        f_mer = ex.submit(fetch_mercari_quote, query)
        quotes = {}
        for name, fut in (("Amazon", f_amz), ("メルカリ", f_mer)):
            try:
                quotes[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("Sell quote error %s: %s", name, e)
                quotes[name] = SellQuote(platform=name, price=None, error=f"取得失敗: {e}")
    return quotes


def evaluate(result: ArbitrageResult, settings: FeeSettings) -> ArbitrageResult:
    """取得済みの相場から損益・判定を計算する（純粋関数。テスト容易）

    - 最安仕入れ先は判定基準と同じ物差し（ポイント込み or 名目）で選ぶ
    - 30,000円条件は名目価格で判定する
    """
    result.judged_on_effective = settings.use_point_rebate

    # --- 仕入れ ---
    priced = [sp for sp in result.buy_quotes.values() if sp.price]

    def _buy_key(sp: ShopPrice):
        judged = settings.effective_price(sp.shop_name, sp.price) \
            if settings.use_point_rebate else sp.price
        return (judged, sp.price)

    result.best_buy = min(priced, key=_buy_key) if priced else None
    if not result.best_buy:
        result.verdict = "no_buy_data"
        result.best_platform, result.best_profit = "", None
        return result
    result.point_rate = float(settings.point_rates.get(result.best_buy.shop_name, 0.0))
    result.meets_min_price = result.buy_price >= settings.min_buy_price

    # --- 販売先ごとの損益 ---
    best: tuple[str, int] | None = None
    for name, pr in result.platforms.items():
        q = pr.quote
        if not q.price:
            pr.fees, pr.profit_nominal, pr.profit_effective = None, None, None
            continue
        if name == "Amazon":
            pr.fees = calc_amazon_fees(q.price, result.category, result.size, settings)
        else:
            pr.fees = calc_mercari_fees(q.price, result.size, settings)
        pr.profit_nominal = pr.fees.net_proceeds - result.buy_price
        pr.profit_effective = pr.fees.net_proceeds - result.effective_buy_price
        judged = pr.profit_effective if settings.use_point_rebate else pr.profit_nominal
        if best is None or judged > best[1]:
            best = (name, judged)

    result.best_platform, result.best_profit = best if best else ("", None)

    if not result.meets_min_price:
        result.verdict = "below_min"
        return result
    if best is None:
        result.verdict = "no_sell_data"
        return result

    base = result.judged_buy_price
    tol = int(base * settings.breakeven_tolerance)
    if result.best_profit >= 0:
        result.verdict = "profit"
    elif result.best_profit >= -tol:
        result.verdict = "breakeven"
    elif result.best_profit >= -tol * 2:
        result.verdict = "near"
    else:
        result.verdict = "loss"
    return result


def analyze_single(query: str, config: Config, *,
                   category: str = "general", size: str = DEFAULT_SIZE_TIER,
                   note: str = "", amazon_restricted: bool = False,
                   settings: FeeSettings | None = None,
                   skip_sell_if_below_min: bool = False) -> ArbitrageResult:
    """1商品について仕入れ相場・販売相場を取得し、損益を評価する"""
    settings = settings or FeeSettings.from_config(config)
    result = ArbitrageResult(
        query=query, category=category if category in AMAZON_REFERRAL_RATES else "general",
        size=normalize_size(size), note=note, amazon_restricted=amazon_restricted,
    )
    result.buy_quotes = fetch_buy_quotes(query, config)
    evaluate(result, settings)  # best_buy / meets_min_price を確定

    if result.best_buy is None:
        return result
    if skip_sell_if_below_min and not result.meets_min_price:
        return result

    sell_quotes = fetch_sell_quotes(query, config)
    result.platforms = {name: PlatformResult(platform=name, quote=q)
                        for name, q in sell_quotes.items()}
    return evaluate(result, settings)


ProgressCallback = Callable[[int, int, str, "ArbitrageResult | None"], None]


def run_arbitrage_scan(config: Config,
                       candidates: list[Candidate] | None = None,
                       max_items: int = 0,
                       settings: FeeSettings | None = None,
                       progress_cb: ProgressCallback | None = None) -> ArbitrageReport:
    """候補商品を順にスキャンしてレポートを生成

    Args:
        config: アプリ設定（ポイント還元率・APIキー）
        candidates: 候補リスト。None ならデフォルト
        max_items: 最大件数（0 = 全件）
        settings: 手数料・判定設定
        progress_cb: 進捗通知 (index, total, query, result)。開始時は result=None
    """
    settings = settings or FeeSettings.from_config(config)
    cands = list(candidates if candidates is not None else ARBITRAGE_CANDIDATES)
    if max_items > 0:
        cands = cands[:max_items]

    report = ArbitrageReport(total_searched=len(cands), settings=settings,
                             started_at=time.time())
    total = len(cands)
    for i, c in enumerate(cands, 1):
        if progress_cb:
            progress_cb(i, total, c.query, None)
        logger.info("Arbitrage scan [%d/%d]: %s", i, total, c.query)
        try:
            r = analyze_single(
                c.query, config, category=c.category, size=c.size, note=c.note,
                amazon_restricted=c.amazon_restricted, settings=settings,
                skip_sell_if_below_min=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Arbitrage scan error for %s: %s", c.query, e)
            r = ArbitrageResult(query=c.query, category=c.category, size=c.size,
                                note=c.note, amazon_restricted=c.amazon_restricted)
            r.verdict = "no_buy_data"
        report.results.append(r)
        if progress_cb:
            progress_cb(i, total, c.query, r)
        logger.info("  → %s: buy ¥%s / %s %s",
                    r.verdict, f"{r.buy_price:,}", r.best_platform,
                    f"¥{r.best_profit:+,}" if r.best_profit is not None else "-")
        if i < total and settings.inter_item_delay > 0:
            time.sleep(settings.inter_item_delay)

    report.finished_at = time.time()
    report.results.sort(key=_rank_key)
    return report


def run_single_arbitrage(query: str, config: Config, category: str | None = None,
                         size: str | None = None,
                         settings: FeeSettings | None = None) -> ArbitrageResult:
    """単一商品の分析

    category / size が未指定（None・空）なら候補リストに同名があればその設定を使う。
    指定があれば常にそれを優先する（表示と入力が食い違わないように）。
    """
    cat, sz, note, restricted = resolve_candidate_meta(query, category, size)
    return analyze_single(query, config, category=cat, size=sz, note=note,
                          amazon_restricted=restricted, settings=settings)
