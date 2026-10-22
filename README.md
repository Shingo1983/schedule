# JAL マイレージパーク LSP ショッピング比較ツール

JALマイレージパーク経由のネットショッピングで獲得できるLSP（Life Status Points）を考慮した価格比較ツールです。

## 機能

- 楽天市場・Yahoo!ショッピングの商品価格を一括検索
- 全検索結果の最安値を表示
- JALマイレージパーク経由で購入した場合の獲得マイル・LSPを自動計算
- LSPの金銭的価値を考慮した「実質価格」で比較
- 最安値とJALマイレージパーク経由の差額を分析し、購入判断を支援
- 転売アービトラージ検索: 楽天/Yahoo!/コジマで仕入れ → Amazon/メルカリで販売した場合の損益を手数料込みで試算

## セットアップ

### 1. 依存関係のインストール

```bash
cd jal_shopping_tool
pip install -e .
```

### 2. APIキーの取得

以下のAPIキーを取得してください（両方またはいずれか一方）:

| API | 取得先 | 費用 |
|-----|--------|------|
| 楽天市場 | https://webservice.rakuten.co.jp/ | 無料 |
| Yahoo!ショッピング | https://developer.yahoo.co.jp/webapi/shopping/ | 無料 |

### 3. 初期設定

```bash
python -m jal_shopping_tool setup
```

対話形式でAPIキーとLSP価値（1 LSPあたり何円の価値があるか）を設定します。

## 使い方

### Webアプリとして起動（推奨）

```bash
python -m jal_shopping_tool web
```

ブラウザで http://localhost:5000 を開いて利用できます。
設定画面からAPIキーの登録、商品検索・比較、ショップ一覧の確認がすべてブラウザ上で完結します。

### CLIで商品を検索して価格比較・LSP分析

```bash
python -m jal_shopping_tool search "AirPods Pro"
python -m jal_shopping_tool search "ダイソン 掃除機" -n 20
```

### 出力例

```
======================================================================
 検索結果: 「AirPods Pro」
======================================================================

■ 全検索結果の最安値
----------------------------------------------------------------------
  価格:    ¥35,800
  商品名:  Apple AirPods Pro (第2世代)
  ショップ: ABC電気 (楽天市場)

■ JALマイレージパーク経由で購入した場合
----------------------------------------------------------------------
  (1 LSP = 15円として計算)

  1. 楽天市場 (300円につき1マイル)
     価格:    ¥35,800
     獲得マイル: 119 マイル
     獲得LSP:   1.2 LSP (≒ ¥18相当)
     実質価格:  ¥35,782

■ 推奨判定
======================================================================
  >>> LSP獲得を考慮すると、JALマイレージパーク経由がお得です! <<<
```

### JALマイレージパーク掲載ショップ一覧

```bash
python -m jal_shopping_tool shops
```

## 転売アービトラージ検索

楽天市場・Yahoo!ショッピング・コジマネットで仕入れ、Amazon またはメルカリで売った場合に
「出品手数料・配送料を引いても買値とほぼ同額が返ってくるか」を試算します。
対象は小型（宅急便コンパクト〜80サイズ）・仕入れ 30,000 円以上の商品です。

```bash
# 候補リスト（26件）を一括スキャン
python -m jal_shopping_tool arbitrage
python -m jal_shopping_tool arbitrage --limit 5            # 先頭5件だけ
python -m jal_shopping_tool arbitrage --json > result.json # JSON出力

# 単一商品を分析（カテゴリ=Amazon手数料区分、サイズ=梱包区分）
python -m jal_shopping_tool arbitrage "SONY WF-1000XM5" --category electronics --size コンパクト

# 判定条件の変更
python -m jal_shopping_tool arbitrage --min-price 50000 --tolerance 3 --no-points --amazon-plan pro

# 候補リストと配送料区分を表示
python -m jal_shopping_tool arbitrage --list
```

Web 版は `http://localhost:5000/arbitrage` から。一括スキャンはバックグラウンドで実行され、
進捗ページが 5 秒ごとに更新されます（`?format=json` で JSON 取得可）。

### 試算モデル

| 項目 | 内容 |
|------|------|
| 仕入れ価格 | 各ショップ検索結果の新品最安値。ポイント還元（設定画面の楽天/Yahoo!率、コジマ 1%）を引いた「実質仕入れ」も算出 |
| Amazon 相場 | 検索結果の新品最安値（＝自分が合わせる必要のある価格） |
| Amazon 手数料 | カテゴリ別 8〜15%（税抜）＋消費税 10%、小口出品は基本成約料 100 円/点、自己発送の配送料目安、梱包材 80 円 |
| メルカリ相場 | 「売り切れ × 新品、未使用」の直近出品の中央値を優先。取れなければ出品中の新品最安値 |
| メルカリ手数料 | 10%、らくらくメルカリ便（ネコポス 210 / コンパクト 520 / 60サイズ 750 / 80サイズ 850 / 100サイズ 1,050 円）、梱包材 80 円 |
| 判定 | 利益あり ◎ / ほぼ同額 ○（損失が仕入れ額の 5% 以内） / やや損 △（10% 以内） / 損失 × 。販売相場が取れない商品は「成立」に含めない |

- メルカリは JavaScript 描画のため、通常の HTTP 取得で相場が取れない場合は Playwright（`pip install playwright && playwright install chromium`）で描画します。未導入時は手動確認用 URL を表示します。
- 手数料率・配送料は 2025〜2026 年時点の公開情報に基づく目安です。`FeeSettings` または CLI オプションで変更できます。
- Apple 製品など一部ブランドは Amazon で新品出品にブランド承認が必要です。営利目的で継続的に転売する場合は古物商許可が必要になることがあります。酒類・医薬品・チケット等は別途規制があります。

### テスト

```bash
pip install pytest
python -m pytest tests
```

## LSP（Life Status Points）について

- JALマイレージパーク経由のショッピングで獲得したマイルは、100マイルにつき1 LSPとして積算されます
- LSPはJAL Life Statusプログラムのステータス維持・昇格に利用できます
- 詳細: https://www.jal.co.jp/jp/ja/jmb/lsp/

## 設定ファイル

設定は `~/.config/jal-shopping-tool/config.json` に保存されます。

```json
{
  "rakuten_app_id": "your_rakuten_app_id",
  "yahoo_app_id": "your_yahoo_client_id",
  "lsp_value_yen": 15.0
}
```

## 対応ショップ

主要なJALマイレージパーク掲載ショップの情報を内蔵しています:

- JAL Mall (100円/1マイル)
- DHCオンラインショップ (100円/1マイル)
- ビックカメラ.com (200円/1マイル)
- Apple公式サイト (200円/1マイル)
- ZOZOTOWN (200円/1マイル)
- 楽天市場 (300円/1マイル)
- Yahoo!ショッピング (300円/1マイル)
- Amazon.co.jp (300円/1マイル)
- その他 20+ ショップ

## 制限事項

- 楽天市場APIとYahoo!ショッピングAPIのレート制限（各1リクエスト/秒）
- Amazon PA-APIは売上実績が必要なため未対応（将来対応予定）
- 価格.comは公式APIが終了しているため未対応
- JALマイレージパークのマイルレートは変更される場合があります
