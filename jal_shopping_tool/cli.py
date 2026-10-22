"""JALマイレージパーク LSP獲得を考慮したネットショッピング価格比較CLIツール

使い方:
  python -m jal_shopping_tool search "商品名"
  python -m jal_shopping_tool setup
  python -m jal_shopping_tool shops
  python -m jal_shopping_tool arbitrage [商品名] [--limit N] [--json]
"""

import argparse
import json
import sys

from .analyzer import AnalysisResult, analyze
from .config import Config
from .jal_shops import get_shops
from .price_search import search_all_sites
from .resale_arbitrage import (
    AMAZON_CATEGORY_LABELS,
    AMAZON_REFERRAL_RATES,
    ARBITRAGE_CANDIDATES,
    SIZE_TIERS,
    ArbitrageReport,
    ArbitrageResult,
    FeeSettings,
    normalize_size,
    run_arbitrage_scan,
    run_single_arbitrage,
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


# ===========================================================================
# 転売アービトラージ
# ===========================================================================
_VERDICT_MARK = {
    "profit": "◎", "breakeven": "○", "near": "△", "loss": "×",
    "no_sell_data": "?", "below_min": "-", "no_buy_data": "-",
}


def _yen(v) -> str:
    if v is None:
        return "---"
    return f"¥{int(v):,}"


def _signed_yen(v) -> str:
    if v is None:
        return "---"
    return f"¥{int(v):+,}"


def _build_settings(args: argparse.Namespace, config: Config) -> FeeSettings:
    overrides = {}
    if args.min_price is not None:
        overrides["min_buy_price"] = int(args.min_price)
    if args.tolerance is not None:
        overrides["breakeven_tolerance"] = float(args.tolerance) / 100.0
    if args.no_points:
        overrides["use_point_rebate"] = False
    if args.amazon_plan:
        overrides["amazon_plan"] = args.amazon_plan
    if args.no_delay:
        overrides["inter_item_delay"] = 0.0
    return FeeSettings.from_config(config, **overrides)


def cmd_arbitrage(args: argparse.Namespace) -> None:
    """転売アービトラージ検索"""
    config = Config.load()
    settings = _build_settings(args, config)

    if args.list:
        _print_candidates()
        return

    if args.query:
        if not args.json:
            print(f"「{args.query}」のアービトラージ分析中...")
            print("  仕入先: 楽天市場 / Yahoo!ショッピング / コジマネット")
            print("  販売先: Amazon / メルカリ")
            print()
        # --category/--size 未指定なら候補リストの設定（なければ汎用）を使う
        result = run_single_arbitrage(args.query, config, category=args.category,
                                      size=normalize_size(args.size) if args.size else None,
                                      settings=settings)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_arbitrage_single(result, settings)
        return

    # 候補リスト全体スキャン
    total = len(ARBITRAGE_CANDIDATES) if args.limit <= 0 else min(args.limit, len(ARBITRAGE_CANDIDATES))
    if not args.json:
        print(f"転売アービトラージスキャン開始（{total}商品）")
        print("  仕入先: 楽天市場 / Yahoo!ショッピング / コジマネット")
        print("  販売先: Amazon / メルカリ")
        print(f"  条件: 仕入れ {settings.min_buy_price:,}円以上、"
              f"許容損失 {settings.breakeven_tolerance * 100:.0f}%、"
              f"ポイント還元{'込み' if settings.use_point_rebate else '除外'}、"
              f"Amazon {'小口' if settings.amazon_plan == 'small' else '大口'}出品")
        print()

    def _progress(i, n, q, r):
        if args.json:
            return
        if r is None:
            print(f"  [{i}/{n}] {q} ...", flush=True)
        else:
            mark = _VERDICT_MARK.get(r.verdict, "?")
            if r.buy_price:
                print(f"        {mark} 仕入 {_yen(r.buy_price)} ({r.best_buy_shop}) "
                      f"→ {r.best_platform or '-'} 損益 {_signed_yen(r.best_profit)}  [{r.verdict_label}]")
            else:
                print(f"        {mark} {r.verdict_label}")

    report = run_arbitrage_scan(config, max_items=args.limit, settings=settings,
                                progress_cb=_progress)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print()
        _print_arbitrage_report(report)


def _print_candidates() -> None:
    print("=" * 100)
    print(f" 候補商品リスト（{len(ARBITRAGE_CANDIDATES)}件）")
    print("=" * 100)
    print(f"{'#':>3} {'商品':<44} {'カテゴリ':<12} {'サイズ':<8} 備考")
    print("-" * 100)
    for i, c in enumerate(ARBITRAGE_CANDIDATES, 1):
        flag = " [Amazon要承認]" if c.amazon_restricted else ""
        print(f"{i:>3} {c.query:<44} {AMAZON_CATEGORY_LABELS.get(c.category, c.category):<12} "
              f"{c.size:<8} {c.note}{flag}")
    print()
    print("サイズ区分と配送料目安:")
    for key, tier in SIZE_TIERS.items():
        print(f"  {key:<8} {tier['label']:<28} メルカリ便 ¥{tier['mercari']:,} / Amazon自己発送 ¥{tier['amazon']:,}")


def _print_fee_block(pr, result: ArbitrageResult) -> None:
    q = pr.quote
    print(f"  【{pr.platform}】")
    if not q.price:
        print(f"    相場取得不可: {q.error or '該当商品なし'}")
        if q.search_url:
            print(f"    手動確認: {q.search_url}")
        return
    print(f"    相場:          {_yen(q.price):>10}  ({q.basis}"
          f"{f', {q.sample_count}件' if q.sample_count > 1 else ''})")
    if q.product_name:
        print(f"    参考商品:      {q.product_name[:60]}")
    f = pr.fees
    print(f"    販売手数料:    {('-' + _yen(f.commission)):>10}  ({f.rate_label})")
    if f.closing_fee:
        label = "基本成約料" if pr.platform == "Amazon" else "振込手数料"
        print(f"    {label}:    {('-' + _yen(f.closing_fee)):>10}")
    print(f"    配送料:        {('-' + _yen(f.shipping_cost)):>10}  ({f.shipping_label})")
    if f.packaging_cost:
        print(f"    梱包材:        {('-' + _yen(f.packaging_cost)):>10}")
    print(f"    手取り:        {_yen(f.net_proceeds):>10}")
    print(f"    損益:          名目 {_signed_yen(pr.profit_nominal)} / "
          f"ポイント込み {_signed_yen(pr.profit_effective)}")
    if pr.platform == "Amazon" and result.amazon_restricted:
        print("    ⚠ Amazon で新品出品するにはブランド承認（出品許可申請）が必要")


def _print_arbitrage_single(r: ArbitrageResult, settings: FeeSettings) -> None:
    """単一商品のアービトラージ結果を表示"""
    print("=" * 80)
    print(f" アービトラージ分析: 「{r.query}」")
    print(f"   カテゴリ: {r.category_label}（Amazon手数料 "
          f"{AMAZON_REFERRAL_RATES.get(r.category, 0.1) * 100:.0f}%） / サイズ: {r.size_label}")
    if r.note:
        print(f"   備考: {r.note}")
    print("=" * 80)
    print()

    print("■ 仕入れ先（新品最安値）")
    for sp in r.buy_rows:
        if sp.price:
            rate = settings.point_rates.get(sp.shop_name, 0.0)
            eff = int(round(sp.price * (1 - rate)))
            mark = "★" if r.best_buy and sp.shop_name == r.best_buy.shop_name else " "
            print(f"  {mark} {sp.shop_name:<18} {_yen(sp.price):>10}  "
                  f"(還元{rate * 100:.1f}% → 実質 {_yen(eff)})  {sp.product_name[:40]}")
        else:
            print(f"    {sp.shop_name:<18} {'---':>10}  ({sp.error or '商品なし'})")
            if sp.search_url:
                print(f"      手動確認: {sp.search_url}")

    if not r.best_buy:
        print()
        print("  → 仕入れ価格を取得できませんでした。上記URLで手動確認してください。")
        return

    print()
    print(f"  → 最安仕入れ: {r.best_buy_shop} {_yen(r.buy_price)}"
          f"（ポイント {r.point_rate * 100:.1f}% = {_yen(r.point_rebate)} → 実質 {_yen(r.effective_buy_price)}）")
    if not r.meets_min_price:
        print(f"  ※ 条件（{settings.min_buy_price:,}円以上）を満たしていません")
    print()

    print("■ 販売先")
    for name in ("Amazon", "メルカリ"):
        pr = r.platforms.get(name)
        if pr:
            _print_fee_block(pr, r)
        else:
            print(f"  【{name}】 未取得")
        print()

    print("■ 判定")
    print("-" * 80)
    basis = "ポイント還元込み" if settings.use_point_rebate else "名目"
    mark = _VERDICT_MARK.get(r.verdict, "?")
    if r.best_profit is not None:
        print(f"  {mark} {r.verdict_label}  最良販売先: {r.best_platform}  "
              f"損益({basis}): {_signed_yen(r.best_profit)}（{r.profit_rate:+.1f}%）")
        print(f"     許容損失: 仕入れ額の {settings.breakeven_tolerance * 100:.0f}% 以内を「ほぼ同額」と判定")
    else:
        print(f"  {mark} {r.verdict_label}")


def _print_arbitrage_report(report: ArbitrageReport) -> None:
    """全体レポートを表示"""
    s = report.settings or FeeSettings()
    print("=" * 110)
    print(" 転売アービトラージ スキャン結果")
    print(f" 検索数: {report.total_searched}商品 / 成立（利益あり+ほぼ同額）: {report.viable_count}商品"
          f" / 所要 {report.elapsed_seconds:.0f}秒")
    print("=" * 110)
    print()

    viable = report.viable_results
    if viable:
        print("■ ほぼ同額以上で売れる商品")
        print()
        _print_report_table(viable, s)
        print()

    analyzed = [r for r in report.ranked_results if r.buy_price > 0 and r not in viable]
    if analyzed:
        print("■ その他（仕入れ価格は取得できたが条件を満たさない）")
        print()
        _print_report_table(analyzed, s)
        print()

    missing = [r for r in report.results if r.buy_price == 0]
    if missing:
        print("■ 仕入れ価格を取得できなかった商品")
        for r in missing:
            errs = ", ".join(f"{n}: {sp.error or '商品なし'}" for n, sp in r.buy_quotes.items())
            print(f"  - {r.query}  ({errs})")
        print()

    print("判定: ◎=利益あり ○=ほぼ同額（許容損失内） △=やや損（許容の2倍以内） ×=損失 ?=販売相場なし -=対象外")
    print(f"判定基準: 仕入れ{'（ポイント還元込み）' if s.use_point_rebate else '（名目）'}に対する損益、"
          f"許容損失 {s.breakeven_tolerance * 100:.0f}%")
    print()

    if viable:
        print("■ 手動確認用URL（価格は変動するため必ず最新価格をご確認ください）")
        for r in viable[:5]:
            print(f"\n  【{r.query}】")
            for sp in r.buy_rows:
                if sp.search_url:
                    print(f"    {sp.shop_name}: {sp.search_url}")
            for name, pr in r.platforms.items():
                if pr.quote.search_url:
                    print(f"    {name}: {pr.quote.search_url}")


def _print_report_table(rows: list[ArbitrageResult], s: FeeSettings) -> None:
    print(f"  {'#':>2} {'商品':<40} {'サイズ':<8} {'仕入':>9} {'実質':>9} {'仕入先':<12} "
          f"{'最良':<7} {'手取り':>9} {'損益':>9} {'率':>7} 判定")
    print("  " + "-" * 125)
    for i, r in enumerate(rows, 1):
        mark = _VERDICT_MARK.get(r.verdict, "?")
        flag = "†" if r.amazon_restricted and r.best_platform == "Amazon" else ""
        print(f"  {i:>2} {r.query[:40]:<40} {r.size:<8} {_yen(r.buy_price):>9} "
              f"{_yen(r.effective_buy_price):>9} {r.best_buy_shop[:12]:<12} "
              f"{(r.best_platform or '-')[:7]:<7} {_yen(r.best_net_proceeds):>9} "
              f"{_signed_yen(r.best_profit):>9} {r.profit_rate:>+6.1f}% {mark}{flag}")
        if r.verdict == "no_sell_data" and r.sell_error_summary:
            print(f"     └ {r.sell_error_summary}")
    if any(r.amazon_restricted for r in rows):
        print("  † Amazon で新品出品するにはブランド承認が必要")


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
    arb = subparsers.add_parser(
        "arbitrage",
        help="転売アービトラージ検索（楽天/Yahoo/コジマで仕入れ → Amazon/メルカリで販売）")
    arb.add_argument("query", nargs="?", default="",
                     help="検索キーワード（省略時は候補リスト全体をスキャン）")
    arb.add_argument("--category", default=None,
                     choices=sorted(AMAZON_REFERRAL_RATES.keys()),
                     help="Amazon手数料カテゴリ（単一商品検索時。省略時は候補リストの設定か general）")
    arb.add_argument("--size", default=None,
                     help="梱包サイズ区分: ネコポス/コンパクト/60/80/100（省略時は候補リストの設定か 60）")
    arb.add_argument("--limit", type=int, default=0, help="スキャン最大件数（0=全件）")
    arb.add_argument("--min-price", type=int, default=None,
                     help="対象とする最低仕入れ価格（デフォルト 30000）")
    arb.add_argument("--tolerance", type=float, default=None,
                     help="「ほぼ同額」とみなす許容損失率%%（デフォルト 5）")
    arb.add_argument("--no-points", action="store_true",
                     help="判定にポイント還元を含めない")
    arb.add_argument("--amazon-plan", choices=["small", "pro"], default=None,
                     help="Amazon出品プラン（small=小口 1点100円 / pro=大口 成約料なし）")
    arb.add_argument("--no-delay", action="store_true", help="商品間の待機を省略")
    arb.add_argument("--json", action="store_true", help="JSONで出力")
    arb.add_argument("--list", action="store_true", help="候補商品リストを表示して終了")

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
