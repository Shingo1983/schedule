"""Flask Webアプリケーション"""

import os

from flask import Flask, render_template, request, redirect, url_for, flash, make_response

from .analyzer import analyze
from .config import Config
from .jal_shops import get_shops, SHOP_CACHE_FILE
from .price_search import search_all_sites
from .site_scrapers import get_manual_search_shops, SCRAPER_SHOP_NAMES, _HAS_CLOUDSCRAPER, _HAS_PLAYWRIGHT


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

    @app.route("/_debug_scrape")
    def debug_scrape():
        """1ショップだけ実際にスクレイプして詳細をJSONで返す診断エンドポイント。
        使い方: /_debug_scrape?shop=Amazon.co.jp&q=airpods
        """
        import time as _t, traceback as _tb
        from .site_scrapers import SCRAPERS

        shop_arg = request.args.get("shop", "").strip()
        query = request.args.get("q", "airpods pro").strip()
        config = Config.load()

        # ショップ検索: 完全一致 or 部分一致
        matched = None
        for fn, name in SCRAPERS:
            if name == shop_arg or shop_arg.lower() in name.lower():
                matched = (fn, name)
                break

        result = {
            "query": query,
            "requested_shop": shop_arg,
            "available_shops": sorted(n for _, n in SCRAPERS)[:50],
        }

        if not matched:
            result["error"] = "shop not matched; use ?shop=<exact_name>&q=<query>"
            return result, 400

        fn, name = matched
        result["resolved_shop"] = name
        t0 = _t.time()
        try:
            sp = fn(query, config)
            result["phase1_elapsed_s"] = round(_t.time() - t0, 2)
            result["phase1"] = {
                "price": sp.price,
                "product_name": sp.product_name[:120] if sp.product_name else "",
                "product_url": sp.product_url[:200] if sp.product_url else "",
                "search_url": sp.search_url[:200] if sp.search_url else "",
                "error": sp.error,
            }
        except Exception as e:
            result["phase1_elapsed_s"] = round(_t.time() - t0, 2)
            result["phase1"] = {"exception": str(e), "trace": _tb.format_exc()[-800:]}

        # Phase 1 と同じ HTTP 取得を独立に行い、HTMLのサイズ・先頭テキスト・
        # 主要コンテナの出現数を返す（cloudscraper が bot 検知ページを返していないか等を検証）
        diag: dict = {}
        try:
            from .site_scrapers import _fetch as _sfetch, _soup as _ssoup
            search_url_for_diag = (result.get("phase1") or {}).get("search_url") or ""
            if search_url_for_diag:
                rd = _sfetch(search_url_for_diag)
                diag["status_code"] = rd.status_code
                diag["html_length"] = len(rd.text)
                diag["content_type"] = rd.headers.get("Content-Type", "")[:100]
                sp2 = _ssoup(rd)
                title_el = sp2.find("title")
                diag["title"] = (title_el.get_text(strip=True)[:120]
                                 if title_el else "")
                # 代表的な Amazon コンテナセレクタのマッチ数を記録
                for sel in [
                    '[data-component-type="s-search-result"]',
                    'div[data-asin]:not([data-asin=""])',
                    '.s-result-item[data-asin]',
                    '[role="listitem"][data-asin]',
                    '.a-price .a-offscreen',
                    '.a-price-whole',
                ]:
                    diag[f"count::{sel}"] = len(sp2.select(sel))
                # HTML 先頭 400 文字（bot チャレンジページ判定用）
                diag["html_head"] = rd.text[:400]
                # Amazon 専用: コンテナ単位の抽出結果をサンプリング（最初の 5 件）
                if "amazon" in search_url_for_diag.lower():
                    from .site_scrapers import (
                        _extract_amazon_price as _aep,
                        _extract_amazon_name as _aen,
                    )
                    samples = []
                    for c in sp2.select('[data-component-type="s-search-result"]')[:5]:
                        offscreen = c.select_one('.a-price .a-offscreen')
                        price_whole = c.select_one('.a-price-whole')
                        h2 = c.select_one('h2')
                        a = c.select_one('h2 a') or c.select_one('a.a-link-normal')
                        samples.append({
                            "extracted_price": _aep(c),
                            "extracted_name": _aen(c)[:80],
                            "offscreen_text": (offscreen.get_text(strip=True)[:40]
                                               if offscreen else None),
                            "price_whole_text": (price_whole.get_text(strip=True)[:40]
                                                 if price_whole else None),
                            "h2_text": h2.get_text(strip=True)[:80] if h2 else None,
                            "has_a_price": bool(c.select_one('.a-price')),
                            "data_asin": c.get("data-asin", ""),
                            "container_html_head": str(c)[:300],
                        })
                    diag["amazon_samples"] = samples
        except Exception as e:
            diag["error"] = str(e)
            diag["trace"] = _tb.format_exc()[-500:]
        result["phase1_diag"] = diag

        # Playwright Phase 2 を単発で叩く
        pw_info: dict = {}
        try:
            from playwright.sync_api import sync_playwright
            t1 = _t.time()
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True, args=[
                        "--no-sandbox", "--disable-dev-shm-usage",
                    ])
                    pw_info["launch_elapsed_s"] = round(_t.time() - t1, 2)
                    context = browser.new_context(locale="ja-JP")
                    page = context.new_page()
                    t2 = _t.time()
                    search_url = (result.get("phase1") or {}).get("search_url") or ""
                    if search_url:
                        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
                        pw_info["goto_elapsed_s"] = round(_t.time() - t2, 2)
                        pw_info["page_status"] = "loaded"
                        pw_info["page_title"] = (page.title() or "")[:120]
                        pw_info["html_length"] = len(page.content())
                    else:
                        pw_info["skipped"] = "no search_url"
                    browser.close()
                except Exception as e:
                    pw_info["browser_error"] = str(e)
                    pw_info["trace"] = _tb.format_exc()[-600:]
        except Exception as e:
            pw_info["playwright_error"] = str(e)

        result["playwright"] = pw_info
        return result, 200

    @app.route("/healthz")
    def healthz():
        """Railway等のヘルスチェック用軽量エンドポイント"""
        return "ok", 200

    @app.route("/_debug")
    def debug_info():
        """環境診断: ライブラリ可用性・Chromium存在・APIキー設定状況を返す。
        本番で「ほとんどのショップで価格が取れない」原因を切り分けるために使う。
        """
        import sys, shutil, glob as _glob, os as _os, subprocess as _sp
        config = Config.load()
        # Chromium バイナリの所在を探索
        chromium_paths = []
        # 新旧両パターン: Playwright 1.49+ は chrome-linux64、それ以前は chrome-linux
        for pat in (
            "/ms-playwright/chromium-*/chrome-linux64/chrome",
            "/ms-playwright/chromium-*/chrome-linux/chrome",
            "/ms-playwright/chromium_headless_shell-*/chrome-linux64/headless_shell",
            "/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
            "/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
            "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        ):
            chromium_paths.extend(_glob.glob(pat))
        # /ms-playwright 直下をスキャン（どのバージョンの Chromium が入ったか確認用）
        ls_ms = []
        if _os.path.isdir("/ms-playwright"):
            try:
                ls_ms = sorted(_os.listdir("/ms-playwright"))[:20]
            except OSError:
                pass
        # Playwright が認識しているブラウザパス
        pw_exec = None
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                pw_exec = p.chromium.executable_path
        except Exception as e:
            pw_exec = f"error: {e}"
        # find でフルスキャン（最大 30 件）
        found_binaries = []
        try:
            r = _sp.run(
                ["find", "/ms-playwright", "-maxdepth", "5",
                 "-name", "chrome", "-o", "-name", "headless_shell"],
                capture_output=True, text=True, timeout=5,
            )
            found_binaries = [l for l in r.stdout.strip().split("\n") if l][:30]
        except Exception:
            pass
        info = {
            "python": sys.version.split()[0],
            "has_cloudscraper": _HAS_CLOUDSCRAPER,
            "has_playwright": _HAS_PLAYWRIGHT,
            "chromium_found": bool(chromium_paths) or bool(found_binaries),
            "chromium_paths": chromium_paths[:5],
            "found_binaries": found_binaries,
            "ms_playwright_dir": ls_ms,
            "playwright_executable_path": pw_exec,
            "PLAYWRIGHT_BROWSERS_PATH": _os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
            "HOME": _os.environ.get("HOME"),
            "system_chrome": shutil.which("google-chrome") or shutil.which("chromium"),
            "rakuten_api_key": bool(config.rakuten_app_id),
            "yahoo_api_key": bool(config.yahoo_app_id),
            "scraper_shops": len(SCRAPER_SHOP_NAMES),
        }
        return info, 200

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

    return app


def main():
    app = create_app()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "1") == "1")
