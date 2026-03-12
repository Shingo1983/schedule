"""Flask Webアプリケーション"""

import os

from flask import Flask, render_template, request, redirect, url_for, flash

from .analyzer import analyze
from .config import Config
from .jal_shops import get_shops, SHOP_CACHE_FILE
from .price_search import search_all_sites
from .site_scrapers import get_manual_search_shops, SCRAPER_SHOP_NAMES


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "jal-lsp-shopping-tool-dev")

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
                manual_shops.append({
                    "name": name,
                    "search_url": shop.get_search_url(query),
                    "jal_url": shop.url,
                    "mile_rate_desc": shop.mile_rate_desc,
                    "category": shop.category,
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

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        config = Config.load()

        if request.method == "POST":
            rakuten = request.form.get("rakuten_app_id", "").strip()
            yahoo = request.form.get("yahoo_app_id", "").strip()
            lsp_value = request.form.get("lsp_value_yen", "").strip()

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

    @app.template_filter("currency_float")
    def currency_float_filter(value):
        try:
            return f"¥{float(value):,.0f}"
        except (ValueError, TypeError):
            return str(value)

    return app


def main():
    app = create_app()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
