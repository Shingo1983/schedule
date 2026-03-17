"""JALマイレージパーク LSP獲得を考慮したネットショッピング価格比較CLIツール

使い方:
  python -m jal_shopping_tool search "商品名"
  python -m jal_shopping_tool setup
  python -m jal_shopping_tool shops
"""

import argparse
import sys

from .analyzer import AnalysisResult, analyze
from .config import Config
from .jal_shops import get_shops
from .price_search import search_all_sites


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
    """商品を全ショップで検索して価格比較・LSP分析を行う"""
    config = Config.load()
    query = args.query

    print(f"「{query}」を全ショップで検索中...")
    print()

    # 全ショップ検索
    shop_prices = search_all_sites(query, config)

    # 分析
    analysis = analyze(query, shop_prices, config)

    if analysis.found_count == 0:
        print("どのショップでも商品が見つかりませんでした。")
        print("検索キーワードを変えて再度お試しください。")
        # エラーがあれば表示
        for c in analysis.comparisons:
            if c.error:
                print(f"  {c.shop_name}: {c.error}")
        sys.exit(0)

    # 結果表示
    _print_analysis(analysis, config)


def _print_analysis(analysis: AnalysisResult, config: Config) -> None:
    """分析結果を見やすく表示する"""
    print("=" * 80)
    print(f" 検索結果: 「{analysis.query}」")
    print(f" {analysis.found_count}/{analysis.total_count} ショップで商品が見つかりました")
    print("=" * 80)
    print()

    # --- 比較テーブル ---
    print(f"  (1 LSP = {config.lsp_value_yen}円として計算)")
    print()

    print(f"{'#':>3} {'ショップ':<20} {'最安値':>10} {'1位との差':>10} {'マイルレート':<16} {'獲得LSP':>8} {'LSP差':>8} {'実質価格':>10}")
    print("-" * 95)

    rank = 0
    for c in analysis.comparisons:
        if c.price is None or c.price == 0:
            continue
        rank += 1

        diff_str = "最安" if c.is_cheapest else f"+¥{c.price_diff:,}"
        lsp_diff_str = "-" if c.lsp_diff == 0 else f"{c.lsp_diff:+.2f}"

        print(
            f"{rank:>3} {c.shop_name:<20} ¥{c.price:>9,} {diff_str:>10} "
            f"{c.mile_rate_desc:<16} {c.lsp_earned:>7.2f} {lsp_diff_str:>8} ¥{c.effective_price:>9,.0f}"
        )

    print()

    # --- 取得失敗ショップ ---
    not_found = [c for c in analysis.comparisons if c.price is None or c.price == 0]
    if not_found:
        print("■ 取得できなかったショップ")
        for c in not_found:
            reason = c.error if c.error else "商品なし"
            print(f"  - {c.shop_name}: {reason}")
            if c.search_url:
                print(f"    手動確認: {c.search_url}")
        print()

    # --- 推奨 ---
    if analysis.best_effective and analysis.cheapest:
        print("■ 推奨")
        print("-" * 80)
        be = analysis.best_effective
        ch = analysis.cheapest
        if be.effective_price < ch.price:
            savings = ch.price - be.effective_price
            print(f"  LSP込みでは「{be.shop_name}」（実質 ¥{be.effective_price:,.0f}）が最もお得!")
            print(f"  最安値「{ch.shop_name}」（¥{ch.price:,}）より実質 ¥{savings:,.0f} お得です。")
        else:
            print(f"  最安値は「{ch.shop_name}」（¥{ch.price:,}）です。")
            if ch.lsp_earned > 0:
                print(f"  JALマイレージパーク経由で {ch.lsp_earned:.2f} LSP も獲得できます。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JALマイレージパーク LSP獲得を考慮したネットショッピング価格比較ツール",
    )
    subparsers = parser.add_subparsers(dest="command", help="コマンド")

    # search
    search_parser = subparsers.add_parser("search", help="商品を全ショップで検索して価格比較・LSP分析")
    search_parser.add_argument("query", help="検索キーワード")

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
