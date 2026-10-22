"""転売アービトラージ機能のユニットテスト（ネットワーク不要）"""

import json

import pytest

from jal_shopping_tool import resale_arbitrage as ra
from jal_shopping_tool.config import Config
from jal_shopping_tool.site_scrapers import ShopPrice


# ---------------------------------------------------------------------------
# 手数料
# ---------------------------------------------------------------------------
def test_amazon_fees_small_plan_electronics_60():
    f = ra.calc_amazon_fees(50000, "electronics", "60", ra.FeeSettings())
    # 8% × 1.1 = 4,400 / 成約料 110 / 配送 950 / 梱包 80
    assert f.commission == 4400
    assert f.closing_fee == 110
    assert f.shipping_cost == ra.SIZE_TIERS["60"]["amazon"]
    assert f.packaging_cost == 80
    assert f.net_proceeds == 50000 - 4400 - 110 - 950 - 80


def test_amazon_fees_pro_plan_has_no_closing_fee():
    f = ra.calc_amazon_fees(50000, "watch", "コンパクト", ra.FeeSettings(amazon_plan="pro"))
    assert f.closing_fee == 0
    assert f.commission == int(round(50000 * 0.15 * 1.1))


def test_amazon_fees_unknown_category_and_size_fall_back():
    f = ra.calc_amazon_fees(10000, "nope", "巨大")
    assert f.commission == int(round(10000 * 0.10 * 1.1))
    assert f.shipping_label == ra.SIZE_TIERS[ra.DEFAULT_SIZE_TIER]["label"]


def test_amazon_minimum_referral_fee():
    f = ra.calc_amazon_fees(100, "electronics", "ネコポス")
    assert f.commission == int(round(30 * 1.1))


def test_mercari_fees_by_size():
    f = ra.calc_mercari_fees(40000, "コンパクト")
    assert f.commission == 4000
    assert f.shipping_cost == 520
    assert f.net_proceeds == 40000 - 4000 - 520 - 80
    f80 = ra.calc_mercari_fees(40000, "中型")  # 別名 → 80
    assert f80.shipping_cost == 850


def test_normalize_size_aliases():
    assert ra.normalize_size("超小型") == "コンパクト"
    assert ra.normalize_size("小型") == "60"
    assert ra.normalize_size("80") == "80"
    assert ra.normalize_size(None) == ra.DEFAULT_SIZE_TIER
    assert ra.normalize_size("???") == ra.DEFAULT_SIZE_TIER


# ---------------------------------------------------------------------------
# 判定ロジック（evaluate は純粋関数）
# ---------------------------------------------------------------------------
def _sp(shop, price, name="商品"):
    return ShopPrice(shop, price, name if price else "", "", f"https://example/{shop}")


def _result(buy: dict, amazon=None, mercari=None, category="electronics", size="60"):
    r = ra.ArbitrageResult(query="X", category=category, size=size)
    r.buy_quotes = {k: _sp(k, v) for k, v in buy.items()}
    r.platforms = {}
    if amazon is not None:
        r.platforms["Amazon"] = ra.PlatformResult(
            "Amazon", ra.SellQuote("Amazon", amazon, basis="新品最安値"))
    if mercari is not None:
        r.platforms["メルカリ"] = ra.PlatformResult(
            "メルカリ", ra.SellQuote("メルカリ", mercari, basis="売却済み(新品)中央値"))
    return r


def _settings(**kw):
    base = dict(point_rates={"楽天市場": 0.10, "Yahoo!ショッピング": 0.05, "コジマネット": 0.01})
    base.update(kw)
    return ra.FeeSettings(**base)


def test_best_buy_is_global_minimum_even_if_below_threshold():
    # 以前のバグ: 3万円未満の最安を無視して高い方の仕入れ先を選んでいた
    r = _result({"楽天市場": 29800, "Yahoo!ショッピング": 31000}, amazon=36000)
    ra.evaluate(r, _settings())
    assert r.best_buy_shop == "楽天市場"
    assert r.buy_price == 29800
    assert r.meets_min_price is False
    assert r.verdict == "below_min"


def test_no_sell_data_is_not_viable():
    # 以前のバグ: 販売相場なしでも profit=0 扱いで「ほぼ同額」判定になっていた
    r = _result({"楽天市場": 40000}, amazon=None, mercari=None)
    ra.evaluate(r, _settings())
    assert r.verdict == "no_sell_data"
    assert r.best_profit is None
    assert r.is_viable is False
    rep = ra.ArbitrageReport(results=[r], total_searched=1, settings=_settings())
    assert rep.viable_results == []


def test_no_buy_data():
    r = _result({"楽天市場": None, "コジマネット": None}, amazon=50000)
    ra.evaluate(r, _settings())
    assert r.verdict == "no_buy_data"
    assert r.best_buy is None


def test_profit_verdict_and_platform_choice():
    r = _result({"楽天市場": 40000, "Yahoo!ショッピング": 41000}, amazon=46000, mercari=46000)
    ra.evaluate(r, _settings())
    # Amazon: 46000 - 4048 - 110 - 950 - 80 = 40812 → 名目 +812 / 実質(40000*0.9=36000) +4812
    # メルカリ: 46000 - 4600 - 750 - 80 = 40570 → 名目 +570 / 実質 +4570
    assert r.platforms["Amazon"].profit_nominal == 812
    assert r.platforms["Amazon"].profit_effective == 4812
    assert r.platforms["メルカリ"].profit_nominal == 570
    assert r.best_platform == "Amazon"
    assert r.best_profit == 4812
    assert r.verdict == "profit"
    assert r.is_viable
    assert r.best_net_proceeds == 40812
    assert round(r.profit_rate, 2) == round(4812 / 36000 * 100, 2)


def test_points_toggle_changes_verdict():
    r = _result({"楽天市場": 40000}, amazon=42000)
    # 名目: 42000 - 3696 - 110 - 950 - 80 = 37164 → -2836 (-7.1%) → near
    ra.evaluate(r, _settings(use_point_rebate=False))
    assert r.judged_on_effective is False
    assert r.best_profit == -2836
    assert r.verdict == "near"
    # ポイント込み: 実質 36000 → +1164 → profit
    ra.evaluate(r, _settings(use_point_rebate=True))
    assert r.best_profit == 1164
    assert r.verdict == "profit"


def test_breakeven_tolerance_boundaries():
    s = _settings(use_point_rebate=False, breakeven_tolerance=0.05)
    # 損益 -2,000 / 40,000 = -5.0% → breakeven（境界含む）
    r = _result({"コジマネット": 40000}, mercari=None, amazon=None)
    r.platforms["メルカリ"] = ra.PlatformResult("メルカリ", ra.SellQuote("メルカリ", 43144))
    # 43144 - 4314(10%) - 750 - 80 = 38000 → -2000
    ra.evaluate(r, s)
    assert r.platforms["メルカリ"].fees.net_proceeds == 38000
    assert r.verdict == "breakeven"
    # -4,000 → near（5%超・10%以内。境界含む）
    r2 = _result({"コジマネット": 40000})
    r2.platforms["メルカリ"] = ra.PlatformResult("メルカリ", ra.SellQuote("メルカリ", 40922))
    # 40922 - 4092 - 750 - 80 = 36000 → -4000
    ra.evaluate(r2, s)
    assert r2.platforms["メルカリ"].profit_nominal == -4000
    assert r2.verdict == "near"
    # -4,002 → loss（10%超）
    r2b = _result({"コジマネット": 40000})
    r2b.platforms["メルカリ"] = ra.PlatformResult("メルカリ", ra.SellQuote("メルカリ", 40920))
    ra.evaluate(r2b, s)
    assert r2b.verdict == "loss"
    # 大損 → loss
    r3 = _result({"コジマネット": 40000}, amazon=35000)
    ra.evaluate(r3, s)
    assert r3.verdict == "loss"


def test_report_ranking_and_dict():
    good = _result({"楽天市場": 40000}, amazon=50000)
    bad = _result({"楽天市場": 40000}, amazon=34000)   # 実質36,000に対し -6,132 → loss
    none = _result({"楽天市場": None})
    s = _settings()
    for r in (good, bad, none):
        ra.evaluate(r, s)
    rep = ra.ArbitrageReport(results=[none, bad, good], total_searched=3, settings=s)
    assert [r.verdict for r in rep.ranked_results] == ["profit", "loss", "no_buy_data"]
    d = rep.to_dict()
    assert d["viable_count"] == 1
    assert d["results"][0]["verdict"] == "profit"
    json.dumps(d, ensure_ascii=False)  # シリアライズ可能


# ---------------------------------------------------------------------------
# analyze_single / run_arbitrage_scan（スクレイパーをモック）
# ---------------------------------------------------------------------------
@pytest.fixture
def mocked_scrapers(monkeypatch):
    calls = {"buy": [], "sell": []}

    def _buy(name, price):
        def fn(query, config):
            calls["buy"].append((name, query))
            return _sp(name, price, f"{query} 本体")
        return fn

    monkeypatch.setattr(ra, "BUY_SCRAPERS", {
        "楽天市場": _buy("楽天市場", 40000),
        "Yahoo!ショッピング": _buy("Yahoo!ショッピング", 42000),
        "コジマネット": _buy("コジマネット", None),
    })

    def _sell(query, config):
        calls["sell"].append(query)
        return {
            "Amazon": ra.SellQuote("Amazon", 46000, basis="新品最安値"),
            "メルカリ": ra.SellQuote("メルカリ", None, error="要ブラウザ描画"),
        }

    monkeypatch.setattr(ra, "fetch_sell_quotes", _sell)
    return calls


def test_analyze_single_end_to_end(mocked_scrapers):
    cfg = Config(rakuten_point_rate=0.10, yahoo_paypay_rate=0.05)
    r = ra.analyze_single("TEST ITEM", cfg, category="electronics", size="コンパクト")
    assert r.best_buy_shop == "楽天市場"
    assert r.point_rate == 0.10
    assert r.platforms["Amazon"].fees is not None
    assert r.platforms["メルカリ"].fees is None
    assert r.best_platform == "Amazon"
    assert r.verdict == "profit"
    assert len(mocked_scrapers["sell"]) == 1


def test_scan_skips_sell_side_below_min_price(mocked_scrapers):
    cfg = Config()
    settings = ra.FeeSettings.from_config(cfg, min_buy_price=50000, inter_item_delay=0)
    cands = [ra.Candidate("A", "electronics", "60"), ra.Candidate("B", "beauty", "コンパクト")]
    progress = []
    rep = ra.run_arbitrage_scan(cfg, candidates=cands, settings=settings,
                                progress_cb=lambda i, n, q, r: progress.append((i, n, q, r is None)))
    assert rep.total_searched == 2
    assert all(r.verdict == "below_min" for r in rep.results)
    assert mocked_scrapers["sell"] == []          # 販売側は取得しない
    assert progress[0] == (1, 2, "A", True) and progress[-1] == (2, 2, "B", False)
    assert rep.elapsed_seconds >= 0


def test_scan_respects_limit_and_survives_scraper_exception(monkeypatch, mocked_scrapers):
    cfg = Config()

    def boom(query, config):
        raise RuntimeError("boom")
    monkeypatch.setitem(ra.BUY_SCRAPERS, "楽天市場", boom)
    settings = ra.FeeSettings.from_config(cfg, inter_item_delay=0)
    rep = ra.run_arbitrage_scan(cfg, max_items=1, settings=settings)
    assert rep.total_searched == 1
    r = rep.results[0]
    assert r.buy_quotes["楽天市場"].error.startswith("取得失敗")
    assert r.best_buy_shop == "Yahoo!ショッピング"   # 残りのショップで継続


def test_run_single_uses_candidate_metadata(mocked_scrapers):
    cfg = Config()
    c = ra.ARBITRAGE_CANDIDATES[0]
    r = ra.run_single_arbitrage(c.query, cfg)
    assert r.category == c.category and r.size == c.size
    assert r.amazon_restricted == c.amazon_restricted


def test_candidates_are_well_formed():
    seen = set()
    for c in ra.ARBITRAGE_CANDIDATES:
        assert c.query not in seen
        seen.add(c.query)
        assert c.category in ra.AMAZON_REFERRAL_RATES
        assert c.size in ra.SIZE_TIERS
        assert c.size in ("ネコポス", "コンパクト", "60", "80"), f"{c.query} は小型ではない"


# ---------------------------------------------------------------------------
# メルカリ HTML パース
# ---------------------------------------------------------------------------
def test_parse_mercari_web_components():
    html = """
    <ul>
      <li><a href="/item/m111"><mer-item-thumbnail item-name="SONY WF-1000XM5 新品未開封" price="35000" sticker="sold"></mer-item-thumbnail></a></li>
      <li><a href="/item/m222"><mer-item-thumbnail item-name="SONY WF-1000XM5 ブラック 新品" price="33500"></mer-item-thumbnail></a></li>
      <li><a href="/item/m333"><mer-item-thumbnail item-name="WF-1000XM5 用 ケース" price="1500"></mer-item-thumbnail></a></li>
    </ul>"""
    items = ra._parse_mercari_html(html, "SONY WF-1000XM5")
    prices = sorted(p for p, _, _ in items)
    assert prices == [33500, 35000]           # アクセサリ(ケース)は除外
    assert any(u == "https://jp.mercari.com/item/m111" for _, _, u in items)


def test_parse_mercari_data_testid_and_next_data():
    html = """
    <li data-testid="item-cell"><a href="/item/m1">
      <span data-testid="thumbnail-item-name">AirPods Pro 3 新品未開封</span>
      <span data-testid="thumbnail-item-price">¥36,500</span></a></li>
    """
    items = ra._parse_mercari_html(html, "AirPods Pro 3")
    assert items == [(36500, "AirPods Pro 3 新品未開封", "https://jp.mercari.com/item/m1")]

    nd = {"props": {"pageProps": {"searchResult": {"items": [
        {"id": "m9", "name": "AirPods Pro 3 未使用", "price": 37000, "status": "ITEM_STATUS_SOLD_OUT"},
        {"id": "m8", "name": "AirPods Pro 3", "price": "35,800"},
    ]}}}}
    html2 = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd, ensure_ascii=False)}</script>'
    items2 = ra._parse_mercari_html(html2, "AirPods Pro 3")
    assert sorted(p for p, _, _ in items2) == [35800, 37000]


def test_fetch_mercari_quote_prefers_sold_median(monkeypatch):
    class Resp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __init__(self, text):
            self.text = text
            self.content = text.encode("utf-8")

    def _html(prices):
        rows = "".join(
            f'<a href="/item/m{i}"><mer-item-thumbnail item-name="GoPro HERO13 Black 新品" price="{p}"></mer-item-thumbnail></a>'
            for i, p in enumerate(prices))
        return f"<html><body>{rows}</body></html>"

    def fake_fetch(url, headers=None, **kw):
        if "sold_out" in url:
            return Resp(_html([52000, 55000, 58000, 150000]))
        return Resp(_html([49000]))

    monkeypatch.setattr(ra, "_fetch", fake_fetch)
    q = ra.fetch_mercari_quote("GoPro HERO13 Black")
    assert q.basis.startswith("売却済み")
    assert q.price == 56500          # 中央値 (55000+58000)/2
    assert q.sample_count == 4
    assert "status=sold_out" in q.search_url


def test_fetch_mercari_quote_falls_back_to_on_sale_then_manual(monkeypatch):
    class Resp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        text = "<html><body><div id='root'></div></body></html>"
        content = text.encode("utf-8")

    monkeypatch.setattr(ra, "_fetch", lambda url, **kw: Resp())
    monkeypatch.setattr(ra, "_render_html_with_browser", lambda url, sels, **kw: None)
    q = ra.fetch_mercari_quote("X")
    assert q.price is None
    assert "ブラウザ" in (q.error or "")

    # ブラウザ描画で出品中が取れるケース
    rendered = ('<a href="/item/m5"><mer-item-thumbnail item-name="X 新品" price="30000">'
                '</mer-item-thumbnail></a>')
    monkeypatch.setattr(ra, "_render_html_with_browser",
                        lambda url, sels, **kw: rendered if "on_sale" in url else "<html></html>")
    q2 = ra.fetch_mercari_quote("X")
    assert q2.price == 30000 and q2.basis.startswith("出品中")


def test_mercari_search_url_params():
    url = ra.mercari_search_url("A B", sold=True)
    assert "keyword=A+B" in url and "status=sold_out" in url and "item_condition_id=1" in url
    url2 = ra.mercari_search_url("A", sold=False, new_only=False)
    assert "status=on_sale" in url2 and "item_condition_id" not in url2
