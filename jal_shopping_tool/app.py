"""Flask Webアプリケーション"""

import os

from flask import (Flask, abort, flash, jsonify, redirect,
                   render_template, request, url_for)

from .analyzer import analyze
from .arbitrage_jobs import CACHE, JOBS, ResultCache
from .config import Config
from .jal_shops import get_shops
from .price_search import search_all_sites
from .resale_arbitrage import (
    AMAZON_CATEGORY_LABELS,
    AMAZON_REFERRAL_RATES,
    ARBITRAGE_CANDIDATES,
    SIZE_TIERS,
    FeeSettings,
    normalize_size,
    resolve_candidate_meta,
    run_arbitrage_scan,
    run_single_arbitrage,
)
from .site_scrapers import get_manual_search_shops, SCRAPER_SHOP_NAMES, _HAS_CLOUDSCRAPER, _HAS_PLAYWRIGHT


def _arbitrage_settings(config: Config, args) -> tuple[FeeSettings, dict]:
    """クエリ/フォームの値から FeeSettings を構築。表示用のフォーム値も返す

    値の妥当性チェック（inf/nan/範囲外）は FeeSettings.__post_init__ に集約し、
    フォームには丸めた後の値を返す（入力と表示が食い違わないように）。
    """
    overrides = {}
    v = (args.get("min_price") or "").strip()
    if v.isdigit():
        overrides["min_buy_price"] = int(v[:9])
    v = (args.get("tolerance") or "").strip()
    if v:
        try:
            overrides["breakeven_tolerance"] = float(v) / 100.0
        except ValueError:
            pass
    # チェックボックスは「フォームが送信されたか」で判断（submitted=1 を hidden で付ける）
    if args.get("submitted"):
        overrides["use_point_rebate"] = bool(args.get("use_points"))
    plan = (args.get("amazon_plan") or "").strip()
    if plan in ("small", "pro"):
        overrides["amazon_plan"] = plan
    settings = FeeSettings.from_config(config, **overrides)
    tol = round(settings.breakeven_tolerance * 100, 2)
    form = {
        "min_price": settings.min_buy_price,
        "tolerance": int(tol) if tol == int(tol) else tol,
        "use_points": settings.use_point_rebate,
        "amazon_plan": settings.amazon_plan,
    }
    return settings, form


def _has_fetch_errors(result) -> bool:
    """一時的な取得失敗を含む結果か（キャッシュ期間を短くする判断用）"""
    if result.best_buy is None:
        return True
    if any(sp.error for sp in result.buy_quotes.values()):
        return True
    return any(pr.quote.error for pr in result.platforms.values())


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "jal-lsp-shopping-tool-dev")

    @app.after_request
    def add_no_cache_headers(response):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.route("/")
    def index():
        config = Config.load()
        has_api_keys = bool(config.rakuten_app_id or config.yahoo_app_id)
        return render_template("index.html", has_api_keys=has_api_keys)

    @app.route("/search")
    def search():
        config = Config.load()
        query = request.args.get("q", "").strip()
        if not query:
            return redirect(url_for("index"))

        # 全ショップを検索
        shop_prices = search_all_sites(query, config)

        # エラー一覧（画面表示用）
        errors = [sp.error for sp in shop_prices if sp.error]

        # 分析
        analysis = analyze(query, shop_prices, config)

        # 手動検索ショップのURL生成（スクレイパー未対応の全ショップ）
        jal_shops = get_shops()
        jal_shop_map = {s.name: s for s in jal_shops}
        manual_shop_names = get_manual_search_shops()
        manual_shops = []
        for name in manual_shop_names:
            shop = jal_shop_map.get(name)
            if shop:
                miles_10k = shop.calc_miles(10000)
                lsp_10k = shop.calc_lsp(10000)
                manual_shops.append({
                    "name": name,
                    "search_url": shop.get_search_url(query),
                    "jal_url": shop.url,
                    "mile_rate_desc": shop.mile_rate_desc,
                    "category": shop.category,
                    "yen_per_mile": shop.yen_per_mile,
                    "miles_10k": miles_10k,
                    "lsp_10k": f"{lsp_10k:.2f}",
                })

        # 全ショップ数（ショップ一覧ページと同じ数）
        total_shop_count = len(jal_shops)

        return render_template(
            "results.html",
            query=query,
            analysis=analysis,
            errors=errors,
            config=config,
            manual_shops=manual_shops,
            total_shop_count=total_shop_count,
            has_cloudscraper=_HAS_CLOUDSCRAPER,
            has_playwright=_HAS_PLAYWRIGHT,
        )

    @app.route("/shops")
    def shops():
        shop_list = get_shops()
        shop_list.sort(key=lambda s: s.yen_per_mile)
        auto_count = len(SCRAPER_SHOP_NAMES)
        manual_count = len(shop_list) - auto_count
        return render_template(
            "shops.html",
            shops=shop_list,
            scraper_names=SCRAPER_SHOP_NAMES,
            auto_count=auto_count,
            manual_count=manual_count,
        )

    # ------------------------------------------------------------------
    # 転売アービトラージ
    # ------------------------------------------------------------------
    def _arbitrage_context(**extra) -> dict:
        ctx = {
            "candidates": ARBITRAGE_CANDIDATES,
            "candidate_count": len(ARBITRAGE_CANDIDATES),
            "size_tiers": SIZE_TIERS,
            "categories": [(k, AMAZON_CATEGORY_LABELS[k], int(round(v * 100)))
                           for k, v in AMAZON_REFERRAL_RATES.items()],
            "has_playwright": _HAS_PLAYWRIGHT,
            "scan_running": JOBS.running_count() > 0,
        }
        ctx.update(extra)
        return ctx

    @app.route("/arbitrage")
    def arbitrage():
        config = Config.load()
        settings, form = _arbitrage_settings(config, request.args)
        query = request.args.get("q", "").strip()
        # 空/不正 = 「自動」（候補リストに同名があればその設定、なければ汎用）
        category_in = request.args.get("category", "").strip()
        category = category_in if category_in in AMAZON_REFERRAL_RATES else ""
        size_in = request.args.get("size", "").strip()
        size = normalize_size(size_in) if size_in else ""

        if not query:
            return render_template("arbitrage.html", mode="index", form=form,
                                   settings=settings, query="", category=category,
                                   size=size, **_arbitrage_context())

        cat, sz, _note, _restricted = resolve_candidate_meta(query, category or None, size or None)
        key = ResultCache.make_key(query, cat, sz, settings)
        result = CACHE.get(key)
        cached = result is not None
        if result is None:
            result = run_single_arbitrage(query, config, category=cat, size=sz,
                                          settings=settings)
            # 取得失敗を含む結果は短時間だけキャッシュ（連打防止）し、すぐ再試行できるようにする
            CACHE.put(key, result, ttl_seconds=60 if _has_fetch_errors(result) else None)

        if request.args.get("format") == "json":
            return jsonify(result.to_dict())

        return render_template("arbitrage.html", mode="single", form=form,
                               settings=settings, query=query, category=category,
                               size=size, result=result, cached=cached,
                               **_arbitrage_context())

    @app.route("/arbitrage/scan", methods=["POST"])
    def arbitrage_scan_start():
        config = Config.load()
        settings, _ = _arbitrage_settings(config, request.form)
        try:
            limit = max(0, int(request.form.get("limit", "0")))
        except ValueError:
            limit = 0
        try:
            job = JOBS.start(lambda cb: run_arbitrage_scan(
                config, max_items=limit, settings=settings, progress_cb=cb),
                settings=settings)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("arbitrage"))
        return redirect(url_for("arbitrage_scan_status", job_id=job.id))

    @app.route("/arbitrage/scan/<job_id>")
    def arbitrage_scan_status(job_id):
        job = JOBS.get(job_id)
        if job is None:
            abort(404)
        if request.args.get("format") == "json":
            # light=1 は進捗バー更新用（結果本体を含めない）
            return jsonify(job.to_dict(include_results=request.args.get("light") != "1"))
        settings = job.report.settings if job.report else job.settings
        return render_template("arbitrage.html", mode="scan", job=job,
                               report=job.report, settings=settings,
                               form=None, query="", category="", size="",
                               **_arbitrage_context())

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        config = Config.load()

        if request.method == "POST":
            rakuten = request.form.get("rakuten_app_id", "").strip()
            yahoo = request.form.get("yahoo_app_id", "").strip()
            lsp_value = request.form.get("lsp_value_yen", "").strip()
            yahoo_paypay = request.form.get("yahoo_paypay_rate", "").strip()
            rakuten_point = request.form.get("rakuten_point_rate", "").strip()

            if rakuten:
                config.rakuten_app_id = rakuten
            if yahoo:
                config.yahoo_app_id = yahoo
            if lsp_value:
                try:
                    config.lsp_value_yen = float(lsp_value)
                except ValueError:
                    flash("LSP価値には数値を入力してください。", "error")
                    return render_template("settings.html", config=config)
            if yahoo_paypay:
                try:
                    config.yahoo_paypay_rate = float(yahoo_paypay) / 100.0
                except ValueError:
                    pass
            if rakuten_point:
                try:
                    config.rakuten_point_rate = float(rakuten_point) / 100.0
                except ValueError:
                    pass

            config.save()
            flash("設定を保存しました。", "success")
            return redirect(url_for("settings"))

        return render_template("settings.html", config=config)

    @app.template_filter("currency")
    def currency_filter(value):
        try:
            return f"¥{int(value):,}"
        except (ValueError, TypeError):
            return str(value)

    @app.template_filter("signed_currency")
    def signed_currency_filter(value):
        try:
            return f"¥{int(value):+,}"
        except (ValueError, TypeError):
            return "---"

    @app.template_filter("currency_float")
    def currency_float_filter(value):
        try:
            return f"¥{float(value):,.0f}"
        except (ValueError, TypeError):
            return str(value)

    @app.template_filter("format_lsp")
    def format_lsp_filter(value):
        """LSP値を小数点以下2桁で統一表示（0.8 → 0.80）"""
        try:
            return f"{float(value):.2f}"
        except (ValueError, TypeError):
            return str(value)

    @app.template_filter("pct")
    def pct_filter(value):
        try:
            return f"{float(value):+.1f}%"
        except (ValueError, TypeError):
            return "---"

    return app


def main():
    app = create_app()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "1") == "1")
