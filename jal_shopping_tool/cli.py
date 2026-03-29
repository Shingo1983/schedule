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
from .resale_arbitrage import (
    run_arbitrage_scan, run_single_arbitrage, ARBITRAGE_CANDIDATES,
    ArbitrageResult, ArbitrageReport,
)


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


def cmd_arbitrage(args: argparse.Namespace) -> None:
    """転売アービトラージ検索"""
    config = Config.load()

    if args.query:
        # 単一商品検索
        print(f"「{args.query}」のアービトラージ分析中...")
        print()
        result = run_single_arbitrage(args.query, config, category=args.category)
        _print_arbitrage_single(result)
    else:
        # 候補リスト全体スキャン
        max_items = args.limit
        total = min(max_items, len(ARBITRAGE_CANDIDATES)) if max_items > 0 else len(ARBITRAGE_CANDIDATES)
        print(f"転売アービトラージスキャン開始（{total}商品）...")
        print("仕入先: 楽天・Yahoo!ショッピング・コジマネット")
        print("販売先: Amazon・メルカリ")
        print("条件: 小型商品、30,000円以上")
        print()

        report = run_arbitrage_scan(config, max_items=max_items)
        _print_arbitrage_report(report)


def _print_arbitrage_single(r: ArbitrageResult) -> None:
    """単一商品のアービトラージ結果を表示"""
    print("=" * 80)
    print(f" アービトラージ分析: 「{r.query}」")
    print("=" * 80)
    print()

    # 仕入れ先
    print("■ 仕入れ先価格")
    print(f"  {'ショップ':<25} {'価格':>10} {'商品名'}")
    print("  " + "-" * 75)
    for name, sp in sorted(r.source_prices.items(), key=lambda x: x[1].price or 999999999):
        if sp.price:
            print(f"  {name:<25} ¥{sp.price:>9,} {sp.product_name[:40]}")
        else:
            err = sp.error or "商品なし"
            print(f"  {name:<25} {'---':>10} ({err})")

    if r.best_source:
        print(f"\n  → 最安仕入れ: {r.best_source.shop_name} ¥{r.best_source.price:,}")
    else:
        print("\n  → 30,000円以上の仕入れ先なし")
        return

    print()

    # 販売先
    print("■ 販売先分析")

    if r.amazon_price and r.amazon_price.price:
        print(f"\n  【Amazon】")
        print(f"    販売価格:   ¥{r.amazon_price.price:,}")
        if r.amazon_fees:
            print(f"    販売手数料: ¥{r.amazon_fees.commission:,} ({r.category}カテゴリ)")
            print(f"    成約料:     ¥{r.amazon_fees.other_fees:,}")
            print(f"    配送料:     ¥{r.amazon_fees.shipping_cost:,}")
            print(f"    手取り:     ¥{r.amazon_fees.net_proceeds:,}")
            print(f"    損益:       ¥{r.amazon_profit:,}")
    else:
        print(f"\n  【Amazon】 価格取得不可")

    if r.mercari_price and r.mercari_price.price:
        print(f"\n  【メルカリ】")
        print(f"    販売価格:   ¥{r.mercari_price.price:,}")
        if r.mercari_fees:
            print(f"    販売手数料: ¥{r.mercari_fees.commission:,} (10%)")
            print(f"    配送料:     ¥{r.mercari_fees.shipping_cost:,} (宅急便コンパクト)")
            print(f"    手取り:     ¥{r.mercari_fees.net_proceeds:,}")
            print(f"    損益:       ¥{r.mercari_profit:,}")
    else:
        print(f"\n  【メルカリ】 価格取得不可")

    print()
    print("■ 判定")
    print("-" * 80)
    if r.is_viable:
        print(f"  ✓ アービトラージ成立！")
        print(f"    最良販売先: {r.best_platform}")
        print(f"    損益: ¥{r.best_profit:,}（利益率 {r.profit_rate:+.1f}%）")
    elif r.best_source_price > 0 and r.best_profit >= -int(r.best_source_price * 0.10):
        print(f"  △ ほぼ損益分岐（損失{abs(r.best_profit):,}円 = 仕入れ額の{abs(r.profit_rate):.1f}%）")
        print(f"    最良販売先: {r.best_platform}")
    else:
        loss = abs(r.best_profit) if r.best_profit < 0 else 0
        print(f"  × アービトラージ不成立（損失 ¥{loss:,}）")


def _print_arbitrage_report(report: ArbitrageReport) -> None:
    """全体レポートを表示"""
    print("=" * 100)
    print(f" 転売アービトラージ スキャン結果")
    print(f" 検索数: {report.total_searched}商品 / 成立: {report.viable_count}商品")
    print("=" * 100)
    print()

    near_breakeven = report.near_breakeven_results

    if near_breakeven:
        print("■ ほぼ同額で売れる商品（損失5%以内）")
        print()
        print(f"  {'#':>3} {'商品':<30} {'サイズ':<6} {'仕入価格':>10} {'仕入先':<15} "
              f"{'販売先':<8} {'手取り':>10} {'損益':>10} {'率':>7}")
        print("  " + "-" * 115)

        for i, r in enumerate(near_breakeven, 1):
            sell_net = r.best_profit + r.best_source_price
            source = r.best_source.shop_name if r.best_source else "---"
            print(f"  {i:>3} {r.query:<30} {r.size:<6} ¥{r.best_source_price:>9,} "
                  f"{source:<15} {r.best_platform:<8} ¥{sell_net:>9,} "
                  f"¥{r.best_profit:>9,} {r.profit_rate:>+6.1f}%")
        print()

    # 全結果サマリ
    print("■ 全商品サマリ")
    print()
    results_with_data = [r for r in report.results if r.best_source_price > 0]

    if not results_with_data:
        print("  仕入れ可能な商品が見つかりませんでした。")
        return

    print(f"  {'#':>3} {'商品':<30} {'仕入価格':>10} {'Amazon損益':>12} {'メルカリ損益':>12} {'判定':<6}")
    print("  " + "-" * 90)

    for i, r in enumerate(results_with_data, 1):
        amz = f"¥{r.amazon_profit:,}" if r.amazon_fees else "---"
        mer = f"¥{r.mercari_profit:,}" if r.mercari_fees else "---"
        status = "○" if r.is_viable else "△" if r.best_profit >= -int(r.best_source_price * 0.10) else "×"
        print(f"  {i:>3} {r.query:<30} ¥{r.best_source_price:>9,} {amz:>12} {mer:>12} {status:<6}")

    print()
    print("  判定: ○=損失5%以内  △=損失5-10%  ×=損失10%超")
    print()

    # 検索URLの表示
    print("■ 手動確認用URL（価格は変動するため最新価格をご確認ください）")
    for r in near_breakeven[:5]:
        print(f"\n  【{r.query}】")
        for name, sp in r.source_prices.items():
            if sp.search_url:
                print(f"    {name}: {sp.search_url}")
        if r.amazon_price and r.amazon_price.search_url:
            print(f"    Amazon: {r.amazon_price.search_url}")
        if r.mercari_price and r.mercari_price.search_url:
            print(f"    メルカリ: {r.mercari_price.search_url}")


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

    # arbitrage
    arb_parser = subparsers.add_parser("arbitrage", help="転売アービトラージ検索")
    arb_parser.add_argument("query", nargs="?", default="",
                            help="検索キーワード（省略時は候補リスト全体をスキャン）")
    arb_parser.add_argument("--category", default="general",
                            help="商品カテゴリ（electronics, beauty, game等）")
    arb_parser.add_argument("--limit", type=int, default=0,
                            help="最大検索数（0=全件）")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "shops":
        cmd_shops(args)
    elif args.command == "arbitrage":
        cmd_arbitrage(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
