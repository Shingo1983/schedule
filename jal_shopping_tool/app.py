"""Flask Webアプリケーション"""

import os

from flask import Flask, render_template, request, redirect, url_for, flash

from .analyzer import analyze
from .config import Config
from .jal_shops import get_shops
from .price_search import search_all


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

        if not config.rakuten_app_id and not config.yahoo_app_id:
            flash("APIキーが設定されていません。先に設定画面で登録してください。", "error")
            return redirect(url_for("settings"))

        max_results = int(request.args.get("n", 10))
        response = search_all(query, config, max_results=max_results)

        # APIエラーがあればブラウザに表示
        for err in response.errors:
            flash(err, "error")

        if not response.results:
            return render_template(
                "results.html",
                query=query,
                analysis=None,
                no_results=True,
                config=config,
            )

        analysis = analyze(query, response.results, config)
        return render_template(
            "results.html",
            query=query,
            analysis=analysis,
            no_results=False,
            config=config,
        )

    @app.route("/shops")
    def shops():
        shop_list = get_shops()
        shop_list.sort(key=lambda s: s.yen_per_mile)
        return render_template("shops.html", shops=shop_list)

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
