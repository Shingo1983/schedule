"""JALマイレージパーク LSP獲得を考慮したネットショッピング価格比較CLIツール

使い方:
  python -m jal_shopping_tool search "商品名"
  python -m jal_shopping_tool setup
  python -m jal_shopping_tool shops
"""

import argparse
import sys

from .analyzer import AnalysisResult, JalShopOffer, analyze
from .config import Config
from .jal_shops import get_shops
from .price_search import search_all


def cmd_setup(args: argparse.Namespace) -> None:
    """初期セットアップ（APIキーの設定）"""
    config = Config.load()

    print("=" * 60)
    print(" JALマイレージパーク LSP ショッピング比較ツール - セットアップ")
    print("=" * 60)
    print()
    print("各ショッピングサイトのAPIキーを設定します。")
    print("（未設定のまま Enter で既存の設定を維持します）")
    print()

    # 楽天
    print("【楽天市場API】")
    print("  取得先: https://webservice.rakuten.co.jp/")
    print(f"  現在の設定: {'設定済み' if config.rakuten_app_id else '未設定'}")
    val = input("  アプリID: ").strip()
    if val:
        config.rakuten_app_id = val

    print()

    # Yahoo!ショッピング
    print("【Yahoo!ショッピングAPI】")
    print("  取得先: https://developer.yahoo.co.jp/webapi/shopping/")
    print(f"  現在の設定: {'設定済み' if config.yahoo_app_id else '未設定'}")
    val = input("  Client ID (アプリケーションID): ").strip()
    if val:
        config.yahoo_app_id = val

    print()

    # LSP価値設定
    print("【LSP価値設定】")
    print("  1 LSPあたり何円の価値があると考えますか？")
    print(f"  現在の設定: {config.lsp_value_yen}円/LSP")
    val = input(f"  1 LSPの価値（円）[デフォルト: {config.lsp_value_yen}]: ").strip()
    if val:
        try:
            config.lsp_value_yen = float(val)
        except ValueError:
            print("  無効な値です。デフォルトを維持します。")

    config.save()
    print()
    print("設定を保存しました。")
    print()

    # 設定状況サマリ
    print("設定状況:")
    print(f"  楽天市場API:          {'OK' if config.rakuten_app_id else 'NG（未設定）'}")
    print(f"  Yahoo!ショッピングAPI: {'OK' if config.yahoo_app_id else 'NG（未設定）'}")
    print(f"  1 LSPの価値:          {config.lsp_value_yen}円")


def cmd_shops(args: argparse.Namespace) -> None:
    """JALマイレージパーク掲載ショップ一覧を表示"""
    shops = get_shops()

    print("=" * 70)
    print(" JALマイレージパーク LSP対象ショップ一覧")
    print("=" * 70)
    print()
    print(f"{'ショップ名':<25} {'マイルレート':<20} {'カテゴリ':<15}")
    print("-" * 70)
    for shop in sorted(shops, key=lambda s: s.yen_per_mile):
        print(f"{shop.name:<25} {shop.mile_rate_desc:<20} {shop.category:<15}")

    print()
    print(f"合計: {len(shops)} ショップ")
    print()
    print("※ LSPレート: 獲得マイル100マイルにつき1 LSP")
    print("※ レートは変更される場合があります。最新情報はJAL公式サイトでご確認ください。")


def cmd_search(args: argparse.Namespace) -> None:
    """商品を検索して価格比較・LSP分析を行う"""
    config = Config.load()
    query = args.query

    if not config.rakuten_app_id and not config.yahoo_app_id:
        print("エラー: APIキーが設定されていません。")
        print("まず 'python -m jal_shopping_tool setup' を実行してください。")
        sys.exit(1)

    print(f"「{query}」を検索中...")
    print()

    # 商品検索
    results = search_all(query, config, max_results=args.max_results)

    if not results:
        print("商品が見つかりませんでした。")
        print("検索キーワードを変えて再度お試しください。")
        sys.exit(0)

    # 分析
    analysis = analyze(query, results, config)

    # 結果表示
    _print_analysis(analysis, config)


def _print_analysis(analysis: AnalysisResult, config: Config) -> None:
    """分析結果を見やすく表示する"""
    print("=" * 70)
    print(f" 検索結果: 「{analysis.query}」")
    print("=" * 70)
    print()

    # --- 全ネット最安値 ---
    print("■ 全検索結果の最安値")
    print("-" * 70)
    if analysis.lowest_price_result:
        r = analysis.lowest_price_result
        print(f"  価格:    ¥{r.price:,}")
        print(f"  商品名:  {r.product_name[:60]}")
        print(f"  ショップ: {r.shop_name} ({_source_label(r.source)})")
        print(f"  URL:     {r.shop_url}")
    print()

    # --- 検索結果上位（価格順） ---
    print(f"■ 価格順 上位{min(5, len(analysis.all_results))}件")
    print("-" * 70)
    for i, r in enumerate(analysis.all_results[:5], 1):
        diff = r.price - (analysis.lowest_price or 0)
        diff_str = f" (+¥{diff:,})" if diff > 0 else ""
        print(f"  {i}. ¥{r.price:,}{diff_str}")
        print(f"     {r.product_name[:55]}")
        print(f"     [{_source_label(r.source)}] {r.shop_name}")
    print()

    # --- JALマイレージパーク経由のオファー ---
    print("■ JALマイレージパーク経由で購入した場合")
    print("-" * 70)
    if not analysis.jal_offers:
        print("  JALマイレージパーク掲載ショップでの該当商品は見つかりませんでした。")
    else:
        print(f"  (1 LSP = {config.lsp_value_yen}円として計算)")
        print()

        for i, offer in enumerate(analysis.jal_offers, 1):
            _print_jal_offer(i, offer, analysis.lowest_price or 0)

    print()

    # --- 推奨判定 ---
    _print_recommendation(analysis, config)


def _print_jal_offer(index: int, offer: JalShopOffer, lowest_price: int) -> None:
    """JALショップオファーの詳細を表示"""
    diff = offer.price - lowest_price
    diff_str = f" (+¥{diff:,})" if diff > 0 else " (最安!)"

    print(f"  {index}. {offer.shop_name} ({offer.jal_shop.mile_rate_desc})")
    print(f"     商品:    {offer.product_name[:50]}")
    print(f"     価格:    ¥{offer.price:,}{diff_str}")
    print(f"     獲得マイル: {offer.miles_earned} マイル")
    print(f"     獲得LSP:   {offer.lsp_earned:.1f} LSP (≒ ¥{offer.lsp_value_yen:,.0f}相当)")
    print(f"     実質価格:  ¥{offer.effective_price:,.0f} (LSP価値を差し引いた場合)")
    print(f"     JAL経由URL: {offer.jal_shop.url}")
    print()


def _print_recommendation(analysis: AnalysisResult, config: Config) -> None:
    """推奨判定を表示"""
    print("■ 推奨判定")
    print("=" * 70)

    if not analysis.jal_offers:
        print("  JALマイレージパーク掲載ショップでの該当商品がないため、")
        print("  最安ショップでの購入をおすすめします。")
        return

    lowest = analysis.lowest_price or 0
    best_jal = analysis.best_jal_offer
    best_lsp = analysis.best_lsp_offer

    if best_jal and best_jal.effective_price <= lowest:
        print("  >>> LSP獲得を考慮すると、JALマイレージパーク経由がお得です! <<<")
        print()
        print(f"  推奨ショップ: {best_jal.shop_name}")
        print(f"  商品価格:    ¥{best_jal.price:,}")
        print(f"  最安との差:  ¥{best_jal.price_diff_from_lowest:,}")
        print(f"  獲得LSP:    {best_jal.lsp_earned:.1f} LSP (≒ ¥{best_jal.lsp_value_yen:,.0f})")
        print(f"  実質価格:   ¥{best_jal.effective_price:,.0f}")
        savings = lowest - best_jal.effective_price
        print(f"  実質節約額:  ¥{savings:,.0f} お得!")
    elif best_jal:
        extra_cost = best_jal.price_diff_from_lowest
        print(f"  最安値（¥{lowest:,}）に対してJALマイレージパーク経由は")
        print(f"  ¥{extra_cost:,} 高くなりますが、{best_jal.lsp_earned:.1f} LSPを獲得できます。")
        print()
        if best_jal.lsp_value_yen >= extra_cost:
            print(f"  → LSP価値（¥{best_jal.lsp_value_yen:,.0f}）が差額（¥{extra_cost:,}）以上なので、")
            print(f"    JALマイレージパーク経由（{best_jal.shop_name}）がお得です!")
        else:
            print(f"  → 差額（¥{extra_cost:,}）がLSP価値（¥{best_jal.lsp_value_yen:,.0f}）を上回るため、")
            print(f"    最安ショップでの購入が経済的です。")
            print()
            print(f"    ただし、LSPの価値を1 LSP = ¥{extra_cost / best_jal.lsp_earned:.0f} 以上と")
            print(f"    考えるなら、JALマイレージパーク経由も検討に値します。")

    # LSP最大のオファー
    if best_lsp and best_jal and best_lsp.shop_name != best_jal.shop_name:
        print()
        print(f"  【LSP最大】 {best_lsp.shop_name}: {best_lsp.lsp_earned:.1f} LSP")
        print(f"              (価格 ¥{best_lsp.price:,}, 実質 ¥{best_lsp.effective_price:,.0f})")


def _source_label(source: str) -> str:
    """検索ソースの表示ラベル"""
    labels = {
        "rakuten": "楽天市場",
        "yahoo": "Yahoo!ショッピング",
    }
    return labels.get(source, source)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JALマイレージパーク LSP獲得を考慮したネットショッピング価格比較ツール",
    )
    subparsers = parser.add_subparsers(dest="command", help="コマンド")

    # search
    search_parser = subparsers.add_parser("search", help="商品を検索して価格比較・LSP分析")
    search_parser.add_argument("query", help="検索キーワード")
    search_parser.add_argument(
        "-n", "--max-results", type=int, default=10, help="各ソースの最大取得件数 (デフォルト: 10)"
    )

    # setup
    subparsers.add_parser("setup", help="初期セットアップ（APIキーの設定）")

    # shops
    subparsers.add_parser("shops", help="JALマイレージパーク掲載ショップ一覧")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "shops":
        cmd_shops(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
