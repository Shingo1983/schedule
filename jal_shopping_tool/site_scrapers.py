"""各ショップサイトのスクレイパー

JALマイレージパーク提携ショップの検索ページをスクレイピングし、
商品の最安値を取得する。

戦略:
Phase 1: cloudscraper + BeautifulSoup（高速、bot検出回避）
  1. CSSセレクタでHTML要素から価格を取得
  2. JSON-LD構造化データから価格を取得
  3. ページ内のJSON（__NEXT_DATA__等）から価格を取得
Phase 2: Playwright ブラウザレンダリング（Phase 1で失敗したショップのみ）
  JavaScript描画のSPAサイトでも価格を取得可能
"""

import itertools
import json
import re
import logging
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from .config import Config


def _normalize_query(query: str) -> str:
    """検索クエリの正規化（全角→半角スペース、連続スペース除去等）"""
    # 全角スペース → 半角スペース
    q = query.replace('\u3000', ' ')
    # 全角英数 → 半角英数
    normalized = []
    for ch in q:
        cp = ord(ch)
        # 全角英数字 (Ａ-Ｚ, ａ-ｚ, ０-９)
        if 0xFF01 <= cp <= 0xFF5E:
            normalized.append(chr(cp - 0xFEE0))
        else:
            normalized.append(ch)
    q = ''.join(normalized)
    # 連続スペースを1つにまとめ
    q = re.sub(r'\s+', ' ', q).strip()
    return q


# 検索クエリ簡略化用: サイズ/容量/一般製品タイプ語を除去してコアキーワードのみ残す
_GENERIC_PRODUCT_TERMS = {
    # 日本語: コスメ・美容の一般用語
    'セラム', '美容液', '化粧水', 'クリーム', 'ローション', '乳液',
    'エッセンス', 'トナー', 'ミスト', 'オイル', 'バーム', 'パック',
    'マスク', 'クレンジング', '洗顔', 'シャンプー', '美白',
    # 英語
    'serum', 'cream', 'lotion', 'toner', 'essence', 'moisturizer',
    'cleanser', 'mask', 'oil', 'mist', 'balm',
}
_SIZE_PATTERN = re.compile(
    r'^\d+\s*(?:ml|g|kg|l|oz|fl\.?\s*oz|mg|mcg|cc|本|個|枚|包|袋|箱)$',
    re.IGNORECASE)


def _simplify_query(query: str) -> str | None:
    """検索クエリを簡略化（サイズ・容量・一般的な製品タイプを除去）

    長いクエリで検索結果が得られない場合のリトライ用。
    ブランド名・製品固有名詞のみを残す。

    例: "スキンシューティカルズ CE フェルリック セラム 30ml"
      → "スキンシューティカルズ CE フェルリック"
    """
    words = re.split(r'[\s　]+', query)
    if len(words) <= 2:
        return None  # 既に十分短い

    core_words = []
    for w in words:
        if _SIZE_PATTERN.match(w):
            continue
        if w.lower() in _GENERIC_PRODUCT_TERMS or w in _GENERIC_PRODUCT_TERMS:
            continue
        core_words.append(w)

    if not core_words or len(core_words) >= len(words):
        return None  # 削減できなかった

    simplified = ' '.join(core_words)
    if len(simplified) < 3:
        return None
    return simplified

def _build_english_query(query: str) -> str | None:
    """カタカナ/日本語キーワードを英語に変換したクエリを構築。

    _KEYWORD_REVERSE_MAP を使い、カタカナ語を英語に逆引きする。
    変換できた語が1つ以上あり、元クエリと異なる場合にのみ返す。

    例: "スキンシューティカルズ CE フェルリック セラム 30ml"
      → "skinceuticals CE ferulic serum 30ml"
    """
    words = re.split(r'[\s　]+', query)
    if not words:
        return None

    converted = []
    any_converted = False
    for w in words:
        w_lower = w.lower()
        # _KEYWORD_REVERSE_MAP はインポート後に構築されるため遅延参照
        eng_variants = _KEYWORD_REVERSE_MAP.get(w_lower, [])
        if eng_variants:
            converted.append(eng_variants[0])  # 最初の英語候補を使う
            any_converted = True
        else:
            # サイズ表記等はそのまま保持
            converted.append(w)

    if not any_converted:
        return None

    result = ' '.join(converted)
    # 元クエリと同じなら None
    if result.lower() == query.lower():
        return None
    return result


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# cloudscraper（Cloudflare/bot検出回避）: オプショナル
# ---------------------------------------------------------------------------
try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
    logger.info("cloudscraper available - anti-bot bypass enabled")
except ImportError:
    _HAS_CLOUDSCRAPER = False
    logger.info("cloudscraper not installed - using standard requests")

# ---------------------------------------------------------------------------
# Playwright（ブラウザレンダリング）: オプショナル
# ---------------------------------------------------------------------------
_HAS_PLAYWRIGHT = False
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
    logger.info("playwright available - browser rendering enabled")
except ImportError:
    logger.info("playwright not installed - browser rendering disabled")

# 共通ヘッダー（最新Chromeを完全模倣 - bot検出回避）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
    "DNT": "1",
}

# JSON API用ヘッダー
_JSON_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

# リクエストタイムアウト（秒）
_TIMEOUT = 15
# 重いサイト用の延長タイムアウト（ビックカメラ、Joshin等）
_TIMEOUT_SLOW = 30
_SLOW_SHOPS: set[str] = {
    "ビックカメラ.com", "Joshin webショップ", "ケーズデンキオンラインショップ",
}

# 並列実行のワーカー数（各ショップは別ドメインなので並列OK）
_MAX_WORKERS = 10


@dataclass
class ShopPrice:
    """ショップでの検索結果（最安値1件）"""

    shop_name: str
    price: int | None  # None = 取得失敗 or 商品なし
    product_name: str
    product_url: str
    search_url: str  # ユーザーが手動で確認できるURL
    error: str | None = None  # エラーメッセージ


# ---------------------------------------------------------------------------
# ショップカテゴリ × 商品ジャンル マッチング
# ---------------------------------------------------------------------------
# 各ショップの取扱ジャンル定義
_SHOP_CATEGORIES: dict[str, set[str]] = {
    # 家電量販店
    "ビックカメラ.com": {"electronics", "appliances", "camera", "gaming", "daily"},
    "Joshin webショップ": {"electronics", "appliances", "gaming"},
    "ケーズデンキオンラインショップ": {"electronics", "appliances"},
    "コジマネット": {"electronics", "appliances"},
    "エディオンネットショップ": {"electronics", "appliances"},
    "ノジマオンライン": {"electronics", "appliances"},
    "ヤマダウェブコム": {"electronics", "appliances", "daily"},
    "ソニーストア": {"electronics", "audio", "gaming"},
    # 総合EC（何でも売っている）
    "Amazon.co.jp": {"all"},
    "Yahoo!ショッピング": {"all"},
    "楽天市場": {"all"},
    "au PAY マーケット": {"all"},
    "Qoo10": {"all"},
    "dショッピング": {"all"},
    "セブンネットショッピング": {"all"},
    "JAL Mall": {"all"},
    "LOHACO": {"daily", "home"},  # 日用品メイン（電子機器なし）
    # ファッション
    "ユニクロオンラインストア": {"clothing"},
    "GU オンラインストア": {"clothing"},
    "ZOZOTOWN": {"clothing", "fashion"},
    "BUYMA": {"fashion", "luxury", "clothing"},
    "ABC-MARTオンラインストア": {"shoes", "fashion"},
    "ベルメゾンネット": {"clothing", "home"},
    # 美容・健康
    "DHCオンラインショップ": {"beauty", "health"},
    "ファンケルオンライン": {"beauty", "health"},
    "マツモトキヨシオンラインストア": {"beauty", "health", "daily"},
    "@cosme SHOPPING": {"beauty", "cosmetics"},
    "iHerb": {"health", "supplements"},
    # インテリア・家具
    "ニトリネット": {"furniture", "home"},
    "無印良品ネットストア": {"home", "clothing", "daily"},
    # その他
    "ショップジャパン": {"home", "fitness"},
}


def _detect_product_genres(query: str) -> set[str]:
    """検索クエリから商品ジャンルを推定する

    Returns:
        ジャンルのset。推定できない場合は {"all"} を返す（全ショップ対象）。
    """
    q = query.lower()

    _GENRE_KEYWORDS: list[tuple[str, list[str]]] = [
        ("electronics", [
            "airpods", "iphone", "ipad", "macbook", "mac", "apple watch",
            "イヤホン", "ヘッドホン", "スピーカー", "テレビ", "パソコン", "ノートpc",
            "スマホ", "スマートフォン", "タブレット", "カメラ", "レンズ",
            "ps5", "switch", "xbox", "ゲーム機", "コントローラー",
            "ssd", "hdd", "メモリ", "usb", "充電器", "モニター", "ディスプレイ",
            "プリンター", "ルーター", "wifi", "ドライヤー", "掃除機",
            "冷蔵庫", "洗濯機", "エアコン", "電子レンジ", "炊飯器",
            "換気扇", "食洗機", "食器洗い", "浴室乾燥", "給湯器", "温水器",
            "ダクト", "レンジフード", "ih", "ガスコンロ",
            "galaxy", "pixel", "xperia", "aquos", "dyson", "sony", "bose",
            "bluetooth", "ワイヤレス",
            # 家電メーカー名
            "三菱", "パナソニック", "panasonic", "東芝", "toshiba", "日立", "hitachi",
            "シャープ", "sharp", "ダイキン", "daikin", "三菱電機", "富士通",
            "toto", "lixil", "リクシル", "inax", "ノーリツ", "noritz", "リンナイ", "rinnai",
        ]),
        ("clothing", [
            "シャツ", "tシャツ", "パンツ", "ジーンズ", "デニム", "ジャケット",
            "コート", "ワンピース", "スカート", "セーター", "ニット",
            "ダウン", "パーカー", "スウェット", "下着", "靴下", "ソックス",
        ]),
        ("fashion", [
            "バッグ", "財布", "アクセサリー", "ネックレス", "リング", "指輪",
            "ブランド", "サングラス", "帽子", "マフラー", "ストール",
        ]),
        ("shoes", [
            "スニーカー", "ブーツ", "サンダル", "パンプス", "ローファー",
            "nike", "adidas", "new balance", "converse",
        ]),
        ("beauty", [
            "化粧水", "乳液", "美容液", "ファンデーション", "口紅", "リップ",
            "マスカラ", "アイシャドウ", "コスメ", "化粧品", "スキンケア",
            "シャンプー", "トリートメント", "ボディソープ", "日焼け止め",
            "クレンジング", "洗顔",
            # コスメブランド・成分名
            "セラム", "serum", "クリーム", "ローション", "エッセンス",
            "レチノール", "ヒアルロン", "ナイアシンアミド", "ビタミンc",
            "スキンシューティカルズ", "skinceuticals", "ランコム", "クリニーク",
            "エスティ", "資生堂", "フェルリック", "ferulic",
        ]),
        ("health", [
            "サプリメント", "ビタミン", "プロテイン", "青汁", "乳酸菌",
            "マスク", "体温計", "血圧計",
        ]),
        ("furniture", [
            "ソファ", "テーブル", "デスク", "椅子", "チェア", "ベッド",
            "マットレス", "棚", "ラック", "カーテン", "カーペット", "ラグ",
        ]),
        ("home", [
            "収納", "キッチン", "フライパン", "鍋", "食器", "タオル",
            "寝具", "枕", "布団", "照明", "時計",
        ]),
        ("daily", [
            "洗剤", "柔軟剤", "ティッシュ", "トイレットペーパー",
            "歯ブラシ", "歯磨き粉", "石鹸",
        ]),
    ]

    genres = set()
    for genre, keywords in _GENRE_KEYWORDS:
        if any(kw in q for kw in keywords):
            genres.add(genre)

    # 型番パターン検出（英数字+ハイフンの型番 → 家電量販店向け商品）
    if not genres and re.search(r'[A-Za-z]{1,5}[\-]?\d{2,}[A-Za-z]*\d*', query):
        genres.add("electronics")

    return genres if genres else {"all"}


def _is_shop_relevant(shop_name: str, genres: set[str]) -> bool:
    """ショップが検索ジャンルに関連するか判定

    ジャンル不明（"all"）の場合は全ショップ対象。
    ショップが"all"カテゴリの場合は常に対象。
    """
    if "all" in genres:
        return True
    shop_cats = _SHOP_CATEGORIES.get(shop_name, {"all"})
    if "all" in shop_cats:
        return True
    return bool(genres & shop_cats)


def _new_session() -> requests.Session:
    """毎回新しいHTTPセッションを作成
    cloudscraper利用可能時はCloudflare/bot検出を自動回避
    """
    if _HAS_CLOUDSCRAPER:
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
        )
        # cloudscraper のUser-Agentを維持（TLSフィンガープリントと一致させるため）
        # UA以外のヘッダーのみ追加
        extra = {k: v for k, v in _HEADERS.items() if k.lower() != "user-agent"}
        session.headers.update(extra)
    else:
        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(_HEADERS)
    return session


def _fetch(url: str, headers: dict | None = None, **kwargs) -> requests.Response:
    """HTTP GETリクエスト（cloudscraper対応）"""
    session = _new_session()
    if headers:
        session.headers.update(headers)
    # Refererを自動設定（bot検出対策）
    if "Referer" not in (headers or {}):
        parsed = urlparse(url)
        session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return session.get(url, timeout=_TIMEOUT, **kwargs)


def _fetch_json(url: str, params: dict | None = None, **kwargs) -> requests.Response:
    """JSON API用のGETリクエスト"""
    session = _new_session()
    session.headers.update(_JSON_HEADERS)
    return session.get(url, params=params, timeout=_TIMEOUT, **kwargs)


def _parse_price(text: str) -> int | None:
    """価格テキストから最初の価格数値を抽出: '¥12,345' → 12345

    改善点:
    - テキスト全体の数字を結合するのではなく、最初の価格パターンのみ抽出
    - 「ポイント」「件」「%」等の非価格数値を除外
    - 「送料」の数値を除外
    """
    if not text:
        return None

    # ポイント・件数・パーセントなどの非価格テキストを除外
    # 「1,234ポイント」「100件」「50%OFF」等のパターンを先に除去
    cleaned = re.sub(r'[\d,]+\s*(?:ポイント|ﾎﾟｲﾝﾄ|point|pts?|件|%|％|倍)', '', text, flags=re.IGNORECASE)
    # 送料関連を除去: 「送料XXX円」「送料無料」
    cleaned = re.sub(r'送料\s*[\d,]*\s*円?', '', cleaned)
    # 「〜」以降を除去（価格範囲の上限を拾わないため）
    cleaned = re.sub(r'[〜~～].+', '', cleaned)

    # 価格パターン: ¥マーク付き or 数字+円 or カンマ区切り数字
    # 最初にマッチしたものだけを使う
    price_patterns = [
        r'[¥￥]\s*([\d,]+)',           # ¥12,345
        r'([\d,]+)\s*円',              # 12,345円
        r'(?:税込|税抜|価格|特価|販売価格|通常価格)[^\d]*([\d,]+)',  # 価格: 12,345
        r'(?:^|[^\d])([\d]{1,3}(?:,\d{3})+)(?:[^\d]|$)',  # カンマ区切り: 12,345 or 1,234,567
        r'(?:^|[^\d])([\d]{3,7})(?:[^\d]|$)',  # 3〜7桁の数字（100〜9,999,999）
    ]

    for pattern in price_patterns:
        m = re.search(pattern, cleaned)
        if m:
            digits = re.sub(r'[^\d]', '', m.group(1))
            if digits:
                val = int(digits)
                if 100 <= val <= 99_999_999:  # 100円〜約1億円の範囲
                    return val

    return None


def _soup(resp: requests.Response) -> BeautifulSoup:
    """レスポンスからBeautifulSoupオブジェクトを作成

    requestsのエンコーディング自動検出はcharset未指定時にISO-8859-1にフォールバックし、
    日本語サイト（ヤマダウェブコム等）で文字化けの原因になる。
    apparent_encoding（chardet検出）を優先し、BeautifulSoupにバイト列を渡して
    meta charset / BOMによる自動検出も有効にする。
    """
    # Content-Typeにcharsetが明示されている場合はそのまま使う
    content_type = resp.headers.get("Content-Type", "")
    has_explicit_charset = "charset=" in content_type.lower()

    if has_explicit_charset:
        html = resp.text
    else:
        # charset未指定 → requests は ISO-8859-1 にフォールバックするため、
        # バイト列を直接BeautifulSoupに渡して自動検出させる
        html = resp.content

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _is_relevant_product(query: str, product_name: str) -> bool:
    """商品名が検索クエリと関連しているかチェック。

    2段階チェック:
    1. キーワードマッチ（過半数一致が必要）
    2. アクセサリ除外（パターンベース — 「充電ケース付き」等の本体記述は除外しない）

    例: query="airpods pro 3"
      OK: 「Apple AirPods Pro 3 ワイヤレスイヤホン MagSafe充電ケース付き」
      NG: 「AirPods Pro 3 用 ケース TPU クリア」（用ケース → アクセサリ）
      NG: 「AirPods Pro 保護フィルム」（保護フィルム → アクセサリ）
      NG: 「化粧水」（キーワード不一致）
    """
    if not product_name:
        return False

    name_lower = product_name.lower()
    query_lower = query.lower()

    # === 非商品ページ除外（FAQ・ヘルプ・お問い合わせ等） ===
    # 検索結果がない場合にFAQやヘルプページを返すサイト対策
    _NON_PRODUCT_PATTERNS = [
        r'お問い合わせ',
        r'よくある質問',
        r'ヘルプ',
        r'(?:ご利用|使い方)\s*ガイド',
        r'カスタマーサービス',
        r'サポート(?:ページ|センター)',
        r'FAQ',
        r'\bhelp\b',
        r'\bcontact\b',
        r'\bsupport\b',
    ]
    for pattern in _NON_PRODUCT_PATTERNS:
        if re.search(pattern, name_lower, re.IGNORECASE):
            return False

    # === クエリエコーバック検出 ===
    # 検索結果0件時にページがクエリを見出しに表示し、無関係商品の価格が
    # 近くにあるケースを防止（例: ユニクロ/GUの「〇〇の検索結果」）
    # 商品名がクエリとほぼ同一（余分な文字が少ない）なら実商品ではない
    _ECHO_STRIP_RE = re.compile(r'[\s　\-/／「」『』【】()（）・、。,.]+')
    _q_normalized = _ECHO_STRIP_RE.sub('', query_lower)
    _n_normalized = _ECHO_STRIP_RE.sub('', name_lower)
    if _q_normalized and _n_normalized:
        # 商品名がクエリとほぼ同じ（前後の装飾文字程度の差）→ エコーバック
        if (_n_normalized == _q_normalized
                # 商品名がクエリの先頭部分+少しだけ余分
                or (_n_normalized.startswith(_q_normalized)
                    and len(_n_normalized) - len(_q_normalized) < 10)
                # クエリが商品名に含まれ、差分が小さい
                or (_q_normalized in _n_normalized
                    and len(_n_normalized) - len(_q_normalized) < 15)
                # 商品名がクエリの部分文字列（クエリそのものが短縮されてエコー）
                or (_n_normalized in _q_normalized)):
            return False

    # === キーワードマッチ（先にチェック — 無関係商品を先に弾く） ===
    stop_words = {"the", "a", "an", "and", "or", "in", "on", "at", "to", "for",
                  "no", "の", "に", "を", "は", "が", "と", "で", "も", "から", "まで"}
    words = re.split(r'[\s　/／\-]+', query_lower)
    # 1文字でも数字はバージョン/型番として保持（例: "3" in "airpods pro 3"）
    keywords = [w for w in words if (len(w) >= 2 or w.isdigit()) and w not in stop_words]

    if not keywords:
        keywords = [w for w in words if len(w) >= 1]

    if keywords:
        # カタカナ展開込みでキーワードマッチ（双方向: 英語→カタカナ、カタカナ→英語）
        # スペース除去版も用意（"C E Ferulic" → "ceferulic" で "ce" マッチ対応）
        name_lower_nospace = re.sub(r'[\s　\-]+', '', name_lower)
        match_count = 0
        for kw in keywords:
            variants = [kw] + _KEYWORD_KATAKANA_MAP.get(kw, []) + _KEYWORD_REVERSE_MAP.get(kw, [])
            # 1-2文字の数字キーワード（型番/バージョン）はワードバウンダリでマッチ
            # 例: "3" が "MTJV3" にマッチしないように（"Pro 3" にはマッチ）
            if kw.isdigit() and len(kw) <= 2:
                pattern = r'(?<![a-zA-Z0-9])' + re.escape(kw) + r'(?![0-9])'
                if re.search(pattern, product_name, re.IGNORECASE):
                    match_count += 1
            elif any(v.lower() in name_lower or v.lower() in name_lower_nospace
                     for v in variants):
                match_count += 1
        if len(keywords) == 1:
            if match_count < 1:
                return False
        elif len(keywords) == 2:
            if match_count < 2:
                return False
        else:
            required = (len(keywords) + 1) // 2 + 1
            required = min(required, len(keywords))
            if match_count < required:
                return False

    # === アクセサリ除外（パターンベース） ===
    # クエリ自体がアクセサリを指している場合はスキップ
    _ACCESSORY_QUERY_WORDS = [
        "ケース", "カバー", "フィルム", "ストラップ", "バンド", "充電器",
        "イヤーピース", "case", "cover", "film", "charger", "strap",
    ]
    if any(aw in query_lower for aw in _ACCESSORY_QUERY_WORDS):
        return True  # クエリがアクセサリを求めている → 通す

    # パターンベースのアクセサリ判定
    # 「用ケース」「専用カバー」等は除外するが「充電ケース付き」は除外しない
    _ACCESSORY_PATTERNS = [
        # 「〜用」パターン（アクセサリの最も確実な指標）
        r'用\s*(?:ケース|カバー|フィルム|スタンド|ホルダー|ポーチ|バンド|充電器)',
        r'専用\s*(?:ケース|カバー|フィルム|イヤーピース|イヤーチップ)',
        r'対応\s*(?:ケース|カバー|フィルム|充電器)',
        # 素材+ケース（ケース製品を示す）
        r'(?:シリコン|TPU|レザー|ハード|ソフト|クリア|透明)\s*ケース',
        # スタンドアロンのアクセサリワード（「付き」で終わらないもの）
        # 「ケース」「カバー」単独 → アクセサリ
        # ただし「充電ケース」は本体の同梱品説明の場合あり → 除外
        # 「充電ケース付き」「充電ケース（USB-C）」等は本体商品の記述
        r'(?<!充電)ケース(?!付)',
        r'カバー(?!付)',
        # 常にアクセサリ（単体で十分明確）
        r'イヤーフック', r'イヤーピース', r'イヤーチップ', r'イヤーパッド',
        r'保護フィルム', r'ガラスフィルム', r'液晶保護',
        r'保護ケース', r'保護カバー', r'保護ガラス',
        r'ストラップ',
        r'クリーナー', r'クリーニング',
        r'ステッカー', r'スキンシール', r'デコシール',
        r'交換用',
        r'互換', r'(?:類似|模倣|コピー)\s*品',
        r'(?:収納|持ち運び)\s*(?:ケース|ポーチ|バッグ)',
        r'ダストガード', r'ダスト\s*カバー',
        r'(?:充電|変換)\s*(?:ケーブル|アダプタ)',
        r'落下防止',
        r'ネックストラップ',
        r'キーホルダー', r'キーチェーン',
        # English
        r'\bprotective\s+case\b', r'\bsilicone\s+case\b', r'\btpu\s+case\b',
        r'\bscreen\s+protector\b', r'\bprotector\b',
        r'\bear\s*(?:tips?|hooks?)\b', r'\bsleeve\b',
        r'\bcase\b', r'\bcover\b',
    ]
    for pattern in _ACCESSORY_PATTERNS:
        if re.search(pattern, name_lower, re.IGNORECASE):
            return False

    # === 中古・整備済み品の除外 ===
    _USED_PATTERNS = [
        r'整備済み', r'renewed', r'refurbished', r'中古', r'再生品',
        r'\bused\b', r'pre[\-\s]?owned', r'訳あり', r'ジャンク',
    ]
    for pattern in _USED_PATTERNS:
        if re.search(pattern, name_lower, re.IGNORECASE):
            return False

    # === 数量・セットサイズ不一致の除外 ===
    # クエリと商品名の数量指標を比較し、不一致なら除外する
    # 例: クエリ「リーデル ボルドー グラン クリュ ソムリエ」(単品) に対し
    #     「2脚セット」「ペア」「6脚セット」等は除外
    def _extract_quantity(text: str) -> int | None:
        """テキストから数量指標を抽出する。見つからなければNoneを返す。"""
        t = text.lower()
        # 「ペア」「ペアセット」 → 2
        if re.search(r'ペア(?:セット)?', t):
            return 2
        # 英語 "pair" → 2
        if re.search(r'\bpair\b', t, re.IGNORECASE):
            return 2
        # 日本語: 数字+助数詞 (脚, 個, 本, 枚, 客, 点, 組, 足)
        # 例: "2脚セット", "4個入り", "6脚", "3本セット", "2客"
        m = re.search(r'(\d+)\s*(?:脚|個|本|枚|客|点|組|足)', t)
        if m:
            return int(m.group(1))
        # 漢数字+助数詞
        _KANJI_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                      '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        m = re.search(r'([一二三四五六七八九十])\s*(?:脚|個|本|枚|客|点|組|足)', t)
        if m and m.group(1) in _KANJI_NUM:
            return _KANJI_NUM[m.group(1)]
        # 数字+セット/入り (助数詞なし): "2セット", "3入り"
        m = re.search(r'(\d+)\s*(?:セット|入り?|入数)', t)
        if m:
            return int(m.group(1))
        # English: number + pack/set/pcs/piece(s)
        m = re.search(r'(\d+)\s*[-]?\s*(?:pack|set|pcs|pieces?)\b', t, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    query_qty = _extract_quantity(query)
    product_qty = _extract_quantity(product_name)

    # クエリに数量指定がなければ単品(1)と仮定
    if query_qty is None:
        query_qty = 1

    # 商品名に数量指標があり、クエリの数量と一致しない場合は除外
    if product_qty is not None and product_qty != query_qty:
        return False

    return True


def _is_bot_blocked_page(soup: BeautifulSoup) -> bool:
    """bot検出/アクセス制限ページかどうかを判定

    注意: テキストが十分にある大きなページ（商品一覧等）では
    block_patterns の誤検出を防ぐため、パターンを本文の主要コンテンツ
    と照合しない（ヘッダー/フッターの「しばらくお待ちください」等で
    誤判定されるのを防止）。
    """
    text = soup.get_text()

    # HTMLが極端に短い場合のチャレンジページ検出
    html_str = str(soup)
    if len(html_str) < 2500:
        # Cloudflare/Akamai/PerimeterXのJSチャレンジ
        challenge_markers = [
            'cf-browser-verification', 'cf_chl_opt', 'cf-challenge',
            '_Incapsula_', 'reese84', 'px-captcha',
            'akamai', 'ak_bmsc', 'bm_sz',
        ]
        for marker in challenge_markers:
            if marker in html_str:
                return True

    block_patterns = [
        r'一時的なアクセス増加',
        r'一時的に.*アクセス.*制限',
        r'アクセスが集中',
        r'しばらく.*お待ち',
        r'混雑.*しております',
        r'アクセス制限',
        r'ただいまメンテナンス',
        r'メンテナンス中',
        r'Access\s+Denied',
        r'Bot\s+Protection',
        r'Please\s+verify.*human',
        r'captcha',
        r'Checking your browser',
        r'Just a moment',
        r'Enable JavaScript and cookies',
        r'Pardon Our Interruption',
        r'Are you a robot',
        r'あなたがロボットでない',
        r'不正なアクセス',
        r'ブラウザの確認',
    ]

    # 大きなページ（実コンテンツあり）ではブロックパターンを適用しない。
    # au PAY マーケット等は正常なページでも「しばらくお待ちください」等の
    # テキストがヘッダー/フッターに含まれ、誤検出の原因になる。
    # 閾値: テキスト5000文字以上 = 実際の商品一覧がある可能性が高い
    text_len = len(text.strip())
    if text_len > 5000:
        return False

    for pattern in block_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# 共通抽出関数
# ---------------------------------------------------------------------------

def _extract_jsonld_prices(soup: BeautifulSoup) -> list[dict]:
    """JSON-LD構造化データから商品情報を抽出する。
    SPAサイトでもSEO用にJSON-LDが埋め込まれていることが多い。

    改善: Product/Offer型のみ対象（BreadcrumbList、Organization等を除外）
    Returns: [{"name": str, "price": int, "url": str}, ...]
    """
    results = []
    # 商品に関連するJSON-LDの@type
    _PRODUCT_TYPES = {"Product", "Offer", "AggregateOffer", "IndividualProduct"}

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            dtype = data.get("@type", "")
            if dtype == "ItemList":
                items = data.get("itemListElement", [])
            elif dtype in _PRODUCT_TYPES:
                items = [data]
            elif "mainEntity" in data:
                me = data["mainEntity"]
                items = me if isinstance(me, list) else [me]
            elif dtype in ("WebPage", "SearchResultsPage"):
                # WebPageの場合、mainEntityや関連プロパティを探す
                for key in ("mainEntity", "about", "mentions"):
                    val = data.get(key)
                    if val:
                        items = val if isinstance(val, list) else [val]
                        break
            # BreadcrumbList, Organization, WebSite 等は無視

        for item in items:
            if isinstance(item, dict) and item.get("@type") == "ListItem":
                item = item.get("item", item)

            if not isinstance(item, dict):
                continue

            # Product/Offer型でない場合はスキップ（商品以外のJSON-LDを除外）
            item_type = item.get("@type", "")
            if item_type and item_type not in _PRODUCT_TYPES and item_type != "ListItem":
                # ただしoffersを持っていれば商品の可能性がある
                if "offers" not in item:
                    continue

            name = item.get("name", "")
            url = item.get("url", "")
            price = None

            # Product > offers > price
            offers = item.get("offers", {})
            if isinstance(offers, list):
                prices = []
                for o in offers:
                    p = o.get("price") or o.get("lowPrice")
                    if p is not None:
                        parsed = _parse_price(str(p))
                        if parsed:
                            prices.append(parsed)
                if prices:
                    price = min(prices)  # 最安値を採用
            elif isinstance(offers, dict):
                p = offers.get("price") or offers.get("lowPrice")
                if p is not None:
                    price = _parse_price(str(p))

            # 直接priceがある場合
            if not price and item.get("price") is not None:
                price = _parse_price(str(item["price"]))

            if price and name and 100 <= price <= 99_999_999:  # 名前がないもの・異常値は除外
                results.append({"name": name, "price": price, "url": url})

    return results


def _extract_embedded_json(html: str) -> list[dict]:
    """ページ内の埋め込みJSON（__NEXT_DATA__, __INITIAL_STATE__等）から商品価格を抽出"""
    results = []

    patterns = [
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.+?})\s*</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.+?});\s*</script>',
        r'window\.__PRELOADED_STATE__\s*=\s*({.+?});\s*</script>',
        r'window\.__NUXT__\s*=\s*({.+?});\s*</script>',
        r'window\.__data\s*=\s*({.+?});\s*</script>',
        r'window\.searchResult\s*=\s*({.+?});\s*</script>',
        r'<script[^>]*>\s*var\s+(?:searchData|productData|itemData)\s*=\s*({.+?});\s*</script>',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            _find_prices_in_dict(data, results, depth=0)
        except (json.JSONDecodeError, RecursionError):
            continue

    return results


def _find_prices_in_dict(obj, results: list, depth: int = 0):
    """再帰的にJSONオブジェクト内の商品（price + name）を探す

    改善:
    - priceが数値型であることを確認（文字列のIDなどを除外）
    - nameが十分な長さであることを確認
    - 最大結果数を制限
    """
    if depth > 8 or len(results) >= 20:
        return
    if isinstance(obj, dict):
        has_price = "price" in obj or "salePrice" in obj or "itemPrice" in obj
        has_name = "name" in obj or "itemName" in obj or "title" in obj or "productName" in obj
        if has_price and has_name:
            raw_price = obj.get("salePrice") or obj.get("price") or obj.get("itemPrice")
            if raw_price is not None:
                # 数値型チェック: 文字列の場合は数字のみの文字列であること
                if isinstance(raw_price, (int, float)):
                    price = int(raw_price) if raw_price >= 100 else None
                elif isinstance(raw_price, str):
                    price = _parse_price(raw_price)
                else:
                    price = None

                if price and 100 <= price <= 99_999_999:
                    name = obj.get("name") or obj.get("itemName") or obj.get("title") or obj.get("productName", "")
                    name = str(name).strip()
                    if len(name) >= 2:  # 名前が短すぎるものは除外
                        url = obj.get("url") or obj.get("itemUrl") or obj.get("productUrl", "")
                        results.append({"name": name, "price": price, "url": str(url)})
        for v in obj.values():
            _find_prices_in_dict(v, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:30]:
            _find_prices_in_dict(item, results, depth + 1)


def _is_no_results_page(soup: BeautifulSoup) -> bool:
    """検索結果が0件のページかどうかを判定

    注意: 大きいページ（>5KB text）は結果がある可能性が高いので
    パターンマッチを厳格化する。誤検出は価格取得の失敗に直結する。
    """
    text = soup.get_text()
    text_len = len(text)

    # 大きいページは結果がある可能性が高い → 「0件」系の厳密パターンのみ
    if text_len > 5000:
        strict_patterns = [
            r'(?<!\d)0\s*件中\s*0',
            r'検索結果\s*[:：]?\s*(?<!\d)0\s*件',
            r'(?<!\d)0\s*件の検索結果',
        ]
        for pattern in strict_patterns:
            if re.search(pattern, text):
                logger.debug("No results detected (strict) for %d char page", text_len)
                return True
        return False

    # 小さいページは広めのパターンでチェック
    no_results_patterns = [
        r'(?<!\d)0\s*件中\s*0',
        r'件中\s*0\s*～\s*0\s*件',
        r'該当する商品[がは]?\s*(?:ございません|ありません|見つかりません)',
        r'見つかりませんでした',
        r'一致する商品[がは]?\s*(?:ございません|ありません)',
        r'商品が見つかりません',
        r'検索結果\s*[:：]?\s*(?<!\d)0\s*件',
        r'(?<!\d)0\s*件の商品',
        r'(?<!\d)0\s*件の検索結果',
        r'no\s+results?\s+found',
        r'ヒットしませんでした',
    ]
    for pattern in no_results_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            logger.debug("No results detected for %d char page: %s", text_len, pattern)
            return True
    return False


def _extract_price_from_element(el) -> int | None:
    """価格要素からできるだけ正確に価格を抽出する。

    戦略:
    1. 価格専用の子要素（.a-price-whole等）があればそれだけ使う
    2. なければ要素の直接テキストのみ使う（子要素のポイント等を除外）
    3. 最終的にget_text()でフォールバック
    """
    # 価格専用の子要素を試す
    price_children = el.select('.a-price-whole, [class*="price-value"], [class*="priceValue"]')
    for child in price_children:
        price = _parse_price(child.get_text())
        if price:
            return price

    # 要素の直接テキストのみ使う（子要素のテキストを含めない）
    # これにより「¥12,345 (1,234ポイント)」のような場合にポイント部分を含めない
    direct_text = ""
    for child in el.children:
        if isinstance(child, str):
            direct_text += child
    if direct_text.strip():
        price = _parse_price(direct_text)
        if price:
            return price

    # フォールバック: 全テキスト（改善した_parse_priceが非価格を除外する）
    return _parse_price(el.get_text())


def _find_price_in_soup(soup: BeautifulSoup, selectors: list[tuple[str, str, str]],
                        base_url: str = "", query: str = "") -> tuple[int | None, str, str]:
    """複数のCSSセレクタパターンで商品を探す。(price, name, url)を返す

    戦略:
    1. CSSセレクタでHTML要素から抽出
    2. JSON-LD構造化データ
    3. 埋め込みJSON（__NEXT_DATA__等）
    ※正規表現フォールバックは廃止（無関係な価格を誤検出する原因のため）

    queryが指定されている場合、各段階で関連性チェックを行い、
    無関係な商品（「airpods pro 3」検索で化粧水等）を除外する。
    """
    # 0. 検索結果なしページを先にチェック
    if _is_no_results_page(soup):
        return None, "", ""

    def _extract_url(name_el, item) -> str:
        """商品URLを抽出するヘルパー"""
        url = ""
        if name_el and name_el.get("href"):
            href = name_el["href"]
            if href.startswith("http"):
                url = href
            elif href.startswith("/") and base_url:
                url = f"{base_url}{href}"
            elif base_url:
                url = f"{base_url}/{href}"
        if not url:
            link = item.select_one("a[href]")
            if link:
                href = link["href"]
                if href.startswith("http"):
                    url = href
                elif href.startswith("/") and base_url:
                    url = f"{base_url}{href}"
        return url

    # 1. CSSセレクタで探す（複数候補を収集し外れ値除去）
    css_candidates = []
    for item_sel, price_sel, name_sel in selectors:
        items = soup.select(item_sel)
        if not items:
            continue
        for item in items[:10]:  # 最初の10件だけチェック
            price_el = item.select_one(price_sel)
            if not price_el:
                continue
            price = _extract_price_from_element(price_el)
            if not price or price > 99_999_999:
                continue

            name_el = item.select_one(name_sel)
            name = name_el.get_text(strip=True) if name_el else ""

            # 関連性チェック（queryが指定されている場合）
            if query and not _is_relevant_product(query, name):
                continue

            url = _extract_url(name_el, item)
            css_candidates.append((price, name, url))

        if css_candidates:
            break  # 最初にマッチしたセレクタパターンの結果を使う

    if css_candidates:
        # 外れ値除去後、最安値を返す
        if len(css_candidates) >= 3:
            prices = sorted(c[0] for c in css_candidates)
            median_price = prices[len(prices) // 2]
            # 中央値の30%-250%の範囲外を外れ値として除去
            css_candidates = [c for c in css_candidates
                              if median_price * 0.3 <= c[0] <= median_price * 2.5]
        if css_candidates:
            return min(css_candidates, key=lambda c: c[0])

    # 2. JSON-LD構造化データから探す
    jsonld_items = _extract_jsonld_prices(soup)
    if jsonld_items:
        # 関連性でフィルタしてから最安値（名前なし or 無関係は除外）
        if query:
            jsonld_items = [i for i in jsonld_items
                           if _is_relevant_product(query, i["name"])]
        if jsonld_items:
            cheapest = min(jsonld_items, key=lambda x: x["price"])
            return cheapest["price"], cheapest["name"], cheapest["url"]

    # 3. 埋め込みJSONから探す
    embedded = _extract_embedded_json(str(soup))
    if embedded:
        if query:
            embedded = [i for i in embedded
                        if _is_relevant_product(query, i["name"])]
        if embedded:
            cheapest = min(embedded, key=lambda x: x["price"])
            return cheapest["price"], cheapest["name"], cheapest["url"]

    # 4. data-price属性を持つ要素を探す
    for price_el in soup.select('[data-price], [itemprop="price"]'):
        raw = price_el.get("data-price") or price_el.get("content") or price_el.get_text()
        price = _parse_price(str(raw))
        if not price:
            continue
        parent = price_el
        for _ in range(5):
            parent = parent.parent
            if parent is None:
                break
            link = parent.select_one("a[href]")
            if link:
                name = link.get_text(strip=True)
                if query and not _is_relevant_product(query, name):
                    break
                href = link.get("href", "")
                url = href if href.startswith("http") else (f"{base_url}{href}" if href.startswith("/") and base_url else "")
                return price, name, url

    # --- エコーバックガード（ステップ5-7共通） ---
    # 抽出結果の商品名がクエリと実質同一ならエコーバックとして拒否
    def _is_echoback_name(name: str, q: str) -> bool:
        if not name or not q:
            return False
        _es = re.compile(r'[\s　\-/／「」『』【】()（）・、。,.]+')
        n_norm = _es.sub('', name.lower())
        q_norm = _es.sub('', q.lower())
        if not n_norm or not q_norm:
            return False
        # 完全一致
        if n_norm == q_norm:
            return True
        # 商品名がクエリの部分文字列（クエリの一部だけエコー）
        if n_norm in q_norm:
            return True
        # クエリが商品名に含まれ、差分が小さい（装飾文字程度）
        # 差分が大きい場合は実際の商品名（型番・説明等が付加）と判断
        if q_norm in n_norm and len(n_norm) - len(q_norm) < 15:
            return True
        return False

    # 5. テキストベース汎用抽出（DOM構造ベース）
    if query:
        result = _extract_price_by_text(soup, query, base_url)
        if result:
            price_r, name_r, url_r = result
            if name_r and _is_echoback_name(name_r, query):
                logger.info("Echo-back rejected (text extraction): '%s'", name_r[:80])
            else:
                return result

    # 6. フルテキスト近接検索（DOM構造に一切依存しない最終手段）
    # 800KB超のHTMLでもページのプレーンテキストから価格を見つける
    if query:
        result = _extract_price_by_fulltext(soup, query, base_url)
        if result:
            return result

    # 7. 生HTML内の価格パターン検索（テキスト抽出で消えた価格を拾う）
    # JSフレームワーク(React/Vue/Next.js等)がデータをscriptタグやdata属性に
    # 格納している場合、get_text()では取得できない
    if query:
        result = _extract_price_from_raw_html(str(soup), query, base_url)
        if result:
            return result

    # デバッグ: 全ステップ失敗時の情報
    raw_html = str(soup)
    html_len = len(raw_html)
    text = soup.get_text()
    text_len = len(text)
    price_in_text = len(re.findall(r'[¥￥]\s*[\d,]+|[\d,]+\s*円|\d{4,8}\s*円', text))
    # HTMLでは追加の価格パターンもチェック（JS変数、data属性、HTMLエンティティ）
    price_in_html = len(re.findall(
        r'[¥￥]\s*[\d,]+|[\d,]+\s*円|\d{4,8}\s*円'
        r'|"price"\s*[=:]\s*[\d",]+'
        r'|data-price\s*=\s*"?\d+'
        r'|&yen;\s*[\d,]+'
        r'|\\u00a5\s*[\d,]+',
        raw_html
    ))
    if html_len > 10000:
        logger.info("Price extraction failed: HTML %d chars, text %d chars, "
                     "prices in text=%d, in html=%d",
                     html_len, text_len, price_in_text, price_in_html)
        # 見つかった価格パターンをサンプル表示（何が見つかっているか確認）
        if price_in_text > 0 and price_in_text <= 5:
            samples = re.findall(r'.{0,20}(?:[¥￥]\s*[\d,]+|[\d,]+\s*円|\d{4,8}\s*円).{0,20}', text)
            logger.info("  Price samples in text: %s", [s.strip()[:60] for s in samples[:3]])
        if price_in_html > 0 and price_in_html <= 5 and price_in_html != price_in_text:
            html_samples = re.findall(
                r'.{0,30}(?:"price"\s*[=:]\s*[\d",]+|data-price\s*=\s*"?\d+|&yen;\s*[\d,]+).{0,30}',
                raw_html)
            if html_samples:
                logger.info("  Price samples in HTML: %s", [s.strip()[:80] for s in html_samples[:3]])
        # 大きいHTMLで価格が全くない場合はHTML冒頭をログ出力（デバッグ用）
        if price_in_html == 0 and html_len > 50000:
            # scriptタグ内のコンテンツサンプルを出力
            scripts = soup.find_all("script")
            script_with_data = [s for s in scripts if s.string and len(s.string) > 500]
            if script_with_data:
                sample = script_with_data[0].string[:300]
                logger.info("  Largest script sample: %s...", sample.replace('\n', ' ')[:200])

    return None, "", ""


# 英語→カタカナのキーワード展開マップ（日本のECサイトはカタカナ表記が多い）
_KEYWORD_KATAKANA_MAP: dict[str, list[str]] = {
    "airpods": ["エアーポッズ", "エアポッズ", "エアーポッド", "エアポッド"],
    "iphone": ["アイフォン", "アイフォーン", "アイホン"],
    "ipad": ["アイパッド"],
    "macbook": ["マックブック"],
    "apple": ["アップル"],
    "samsung": ["サムスン"],
    "galaxy": ["ギャラクシー"],
    "sony": ["ソニー"],
    "nintendo": ["ニンテンドー", "任天堂"],
    "switch": ["スイッチ"],
    "playstation": ["プレイステーション", "プレステ"],
    "xbox": ["エックスボックス"],
    "pro": ["プロ"],
    "max": ["マックス"],
    "mini": ["ミニ"],
    "plus": ["プラス"],
    "ultra": ["ウルトラ"],
    "pixel": ["ピクセル"],
    "google": ["グーグル"],
    "bluetooth": ["ブルートゥース"],
    "wireless": ["ワイヤレス"],
    "noise": ["ノイズ"],
    "cancelling": ["キャンセリング"],
    "headphones": ["ヘッドホン", "ヘッドフォン"],
    "earbuds": ["イヤーバッズ", "イヤバッズ"],
    "earphone": ["イヤホン", "イヤフォン"],
    "speaker": ["スピーカー"],
    "keyboard": ["キーボード"],
    "mouse": ["マウス"],
    "monitor": ["モニター", "モニタ"],
    "camera": ["カメラ"],
    "lens": ["レンズ"],
    "watch": ["ウォッチ"],
    "tablet": ["タブレット"],
    "laptop": ["ラップトップ", "ノートパソコン"],
    "desktop": ["デスクトップ"],
    "dyson": ["ダイソン"],
    "panasonic": ["パナソニック"],
    "sharp": ["シャープ"],
    "toshiba": ["東芝"],
    "hitachi": ["日立"],
    # コスメ・美容
    "serum": ["セラム", "美容液"],
    "セラム": ["美容液", "serum", "essence", "エッセンス"],
    "美容液": ["セラム", "serum", "エッセンス"],
    "cream": ["クリーム"],
    "lotion": ["ローション", "化粧水"],
    "化粧水": ["ローション", "lotion", "トナー"],
    "essence": ["エッセンス", "美容液", "セラム"],
    "エッセンス": ["美容液", "セラム", "essence"],
    "moisturizer": ["モイスチャライザー"],
    "cleanser": ["クレンザー"],
    "toner": ["トナー", "トーナー"],
    "sunscreen": ["サンスクリーン"],
    "foundation": ["ファンデーション"],
    "mascara": ["マスカラ"],
    "lipstick": ["リップスティック"],
    "concealer": ["コンシーラー"],
    "primer": ["プライマー"],
    "retinol": ["レチノール"],
    "collagen": ["コラーゲン"],
    "hyaluronic": ["ヒアルロニック"],
    "ceramide": ["セラミド"],
    "niacinamide": ["ナイアシンアミド"],
    "vitamin": ["ビタミン"],
    "peptide": ["ペプチド"],
    "ferulic": ["フェルリック"],
    "antioxidant": ["アンチオキシダント"],
    "skinceuticals": ["スキンシューティカルズ"],
    "lancome": ["ランコム"],
    "clinique": ["クリニーク"],
    "estee": ["エスティ"],
    "lauder": ["ローダー"],
    "shiseido": ["資生堂", "シセイドウ"],
    "sulwhasoo": ["ソルファス"],
    "laneige": ["ラネージュ"],
    "innisfree": ["イニスフリー"],
    # ファッション・雑貨
    "sneakers": ["スニーカー"],
    "boots": ["ブーツ"],
    "jacket": ["ジャケット"],
    "shirt": ["シャツ"],
    "dress": ["ドレス", "ワンピース"],
    "backpack": ["バックパック", "リュック"],
    "wallet": ["ウォレット"],
    # 食品・健康
    "supplement": ["サプリメント", "サプリ"],
    "protein": ["プロテイン"],
    "organic": ["オーガニック"],
}

# 逆引きマップ自動生成（カタカナ→英語）— 双方向マッチング用
_KEYWORD_REVERSE_MAP: dict[str, list[str]] = {}
for _eng, _kata_list in _KEYWORD_KATAKANA_MAP.items():
    for _kata in _kata_list:
        _KEYWORD_REVERSE_MAP.setdefault(_kata.lower(), []).append(_eng)


def _expand_keywords(keywords: list[str]) -> list[str]:
    """英語キーワードにカタカナ展開を追加"""
    expanded = list(keywords)
    for kw in keywords:
        katakana_variants = _KEYWORD_KATAKANA_MAP.get(kw, [])
        expanded.extend(katakana_variants)
    return expanded


def _extract_price_by_fulltext(soup: BeautifulSoup, query: str,
                                base_url: str) -> tuple[int, str, str] | None:
    """フルテキスト近接検索（DOM構造に完全に依存しない最終手段）

    ページの全テキストを抽出し、価格パターンの近くにクエリキーワードがあるかを
    テキスト位置ベースでチェックする。800KB超のHTMLでも動作する。

    CSSセレクタやDOM走査に依存しないため、あらゆるHTML構造に対応。
    """
    # キーワード準備（カタカナ展開込み）
    query_lower = query.lower()
    keywords = [w for w in re.split(r'[\s　/／\-]+', query_lower) if len(w) >= 2 or w.isdigit()]
    if not keywords:
        return None
    # ページ全テキスト抽出（改行区切り）
    full_text = soup.get_text(separator='\n')
    if len(full_text) < 100:
        return None

    full_text_lower = full_text.lower()

    # === 検索クエリエコーバック領域を特定 ===
    # 検索結果ページでは「〇〇の検索結果」等の見出しにクエリが表示される。
    # この領域のキーワードを商品名と誤認しないよう、エコー位置を記録する。
    _q_normalized_ft = re.sub(r'[\s　\-/／]+', '', query_lower)
    _echo_zones: list[tuple[int, int]] = []  # (start, end) of query echo regions
    if _q_normalized_ft and len(_q_normalized_ft) >= 6:
        # full_text_lower内で、クエリの正規化版が出現する位置を検出
        # スペースを許容するパターンを構築（各文字間にオプションのスペース）
        _echo_pat_chars = []
        for ch in _q_normalized_ft:
            _echo_pat_chars.append(re.escape(ch))
        _echo_pat = r'[\s　]*'.join(_echo_pat_chars)
        for _em in re.finditer(_echo_pat, full_text_lower):
            _echo_zones.append((_em.start(), _em.end()))

    # 価格パターンを全テキストから検索（カンマあり/なし両対応）
    price_pattern = re.compile(
        r'[¥￥]\s*([\d,]+)'           # ¥39,800 or ¥39800
        r'|([\d]{1,3}(?:,\d{3})+)\s*円'  # 39,800円
        r'|(\d{4,8})\s*円'            # 39800円（カンマなし）
    )
    candidates = []

    for m in price_pattern.finditer(full_text):
        raw = m.group(1) or m.group(2) or m.group(3)
        if not raw:
            continue
        digits = re.sub(r'[^\d]', '', raw)
        if not digits:
            continue
        price = int(digits)
        if not (1000 <= price <= 99_999_999):  # フルテキストでは最低¥1,000以上
            continue

        # 非商品価格の除外（送料閾値、ポイント、配送料など）
        narrow_price_ctx = full_text_lower[max(0, m.start() - 60):min(len(full_text_lower), m.end() + 60)]
        _NON_PRODUCT_PATTERNS_FULLTEXT = [
            r'配送料', r'送料', r'以上で.*無料', r'以上で.*負担',
            r'ポイント', r'point', r'還元', r'付与',
            r'off\b', r'割引', r'クーポン', r'coupon',
            r'お問い合わせ', r'よくある質問', r'ヘルプ', r'FAQ',
        ]
        if any(re.search(p, narrow_price_ctx) for p in _NON_PRODUCT_PATTERNS_FULLTEXT):
            continue

        # 価格の前後の文字数はページサイズに応じて調整
        # 大きいページはテキストが散在するため広い窓が必要
        window = 500 if len(full_text) < 50000 else 1000
        start = max(0, m.start() - window)
        end = min(len(full_text_lower), m.end() + window)
        context = full_text_lower[start:end]

        # カタカナ展開込みでキーワードマッチ（双方向: 英語→カタカナ、カタカナ→英語）
        # ただしエコーバック領域内のみでマッチするキーワードは除外
        match_count = 0
        for kw in keywords:
            # 1-2文字の数字キーワードはワードバウンダリでマッチ
            if kw.isdigit() and len(kw) <= 2:
                pattern = r'(?<![a-zA-Z0-9])' + re.escape(kw) + r'(?![0-9])'
                found_outside_echo = False
                for _dm in re.finditer(pattern, full_text[start:end], re.IGNORECASE):
                    abs_pos = start + _dm.start()
                    if not any(ez[0] <= abs_pos < ez[1] for ez in _echo_zones):
                        found_outside_echo = True
                        break
                if found_outside_echo:
                    match_count += 1
            else:
                # 元キーワード自体 or カタカナ変換 or 逆引き英語 のいずれかが存在すればOK
                variants = [kw] + _KEYWORD_KATAKANA_MAP.get(kw, []) + _KEYWORD_REVERSE_MAP.get(kw, [])
                # スペース除去版も用意（"C E Ferulic" → "ceferulic" で "ce" マッチ対応）
                context_nospace = re.sub(r'[\s　\-]+', '', context)
                found_outside_echo = False
                for v in variants:
                    v_lower = v.lower()
                    # context内の全出現位置をチェック
                    search_start = 0
                    while True:
                        pos = context.find(v_lower, search_start)
                        if pos < 0:
                            break
                        abs_pos = start + pos
                        if not any(ez[0] <= abs_pos < ez[1] for ez in _echo_zones):
                            found_outside_echo = True
                            break
                        search_start = pos + 1
                    if found_outside_echo:
                        break
                    # スペース除去版でも試行（"C E" → "ce" マッチ）
                    if v_lower in context_nospace:
                        found_outside_echo = True
                        break
                if found_outside_echo:
                    match_count += 1
        # キーワードの一致数を要求（キーワード数・ページサイズに応じて緩和）
        if len(full_text) > 100000:
            required = max(1, (len(keywords) + 1) // 2)  # 過半数
        elif len(keywords) >= 5:
            required = max(2, len(keywords) - 2)  # 5+キーワード: 2つまで欠落許容
        else:
            required = max(1, len(keywords) - 1)  # ほぼ全キーワード
        if match_count >= required:
            # アクセサリチェック: 価格の前の行（商品名が通常ある）で判定
            # 300文字の広いコンテキストではなく、直前の狭い範囲で判定
            narrow_start = max(0, m.start() - 120)
            narrow_context = full_text_lower[narrow_start:m.start()]
            # 「充電ケース付き」は除外しない
            is_accessory = False
            if not any(acc in query_lower for acc in ['ケース', 'カバー', 'case', 'cover']):
                # アクセサリパターン: 「ケース」が「充電ケース付」でない場合
                acc_patterns = [
                    r'(?<!充電)ケース(?!付)', r'カバー(?!付)', r'フィルム',
                    r'ストラップ', r'イヤーフック', r'イヤーピース', r'イヤーチップ', r'互換',
                    r'\bcase\b', r'\bcover\b', r'\bsleeve\b',
                ]
                for ap in acc_patterns:
                    if re.search(ap, narrow_context):
                        is_accessory = True
                        break
            if not is_accessory:
                candidates.append((match_count, price))

    if not candidates:
        # デバッグ: なぜ候補がないか
        total_matches = 0
        kw_fail = 0
        acc_fail = 0
        for m in price_pattern.finditer(full_text):
            raw = m.group(1) or m.group(2) or m.group(3)
            if not raw:
                continue
            digits = re.sub(r'[^\d]', '', raw)
            if not digits:
                continue
            p = int(digits)
            if not (1000 <= p <= 99_999_999):
                continue
            total_matches += 1
            window = 500 if len(full_text) < 50000 else 1000
            start = max(0, m.start() - window)
            end = min(len(full_text_lower), m.end() + window)
            ctx = full_text_lower[start:end]
            mc = 0
            for kw in keywords:
                variants = [kw] + _KEYWORD_KATAKANA_MAP.get(kw, [])
                if any(v.lower() in ctx for v in variants):
                    mc += 1
            if len(full_text) > 100000:
                req = max(1, (len(keywords) + 1) // 2)
            elif len(keywords) >= 5:
                req = max(2, len(keywords) - 2)
            else:
                req = max(1, len(keywords) - 1)
            if mc < req:
                kw_fail += 1
                # 先頭のみ詳細ログ
                if kw_fail == 1:
                    narrow = full_text[max(0, m.start()-80):m.end()+20].replace('\n', ' ')
                    logger.info("Fulltext kw-fail: price=¥%s, matched=%d/%d required=%d, context='%s'",
                                digits, mc, len(keywords), req, narrow[:120])
            else:
                acc_fail += 1
                if acc_fail == 1:
                    ns = max(0, m.start() - 120)
                    nc = full_text_lower[ns:m.start()]
                    logger.info("Fulltext acc-fail: price=¥%s, narrow='%s'",
                                digits, nc.replace('\n', ' ')[:120])
        logger.info("Fulltext: %d prices, %d kw-fail, %d acc-fail, keywords=%s",
                     total_matches, kw_fail, acc_fail, keywords[:5])
        return None

    # キーワード一致数が最も高い候補を優先
    best_match = max(c[0] for c in candidates)
    top_candidates = sorted([p for mc, p in candidates if mc >= best_match])

    # 外れ値除去（中央値ベース: アクセサリ/バンドル除去）
    if len(top_candidates) >= 3:
        median = top_candidates[len(top_candidates) // 2]
        top_candidates = [p for p in top_candidates if median * 0.3 <= p <= median * 2.5]
    if len(top_candidates) >= 2:
        if top_candidates[0] < top_candidates[1] * 0.5:
            top_candidates = top_candidates[1:]

    if not top_candidates:
        # フォールバック: 全候補から（match_count降順、price昇順）
        all_prices = sorted(candidates, key=lambda x: (-x[0], x[1]))
        top_candidates = [all_prices[0][1]]

    price = top_candidates[0]

    # 商品名を推定（価格の近くにあるクエリマッチ行）
    name = ""  # デフォルトは空（クエリをフォールバックにしない）
    for m in price_pattern.finditer(full_text):
        raw = m.group(1) or m.group(2) or m.group(3)
        if not raw:
            continue
        p = int(re.sub(r'[^\d]', '', raw) or '0')
        if p == price:
            # この価格の前後から商品名行を探す
            start = max(0, m.start() - 300)
            context_lines = full_text[start:m.start()].split('\n')
            for line in reversed(context_lines):
                line = line.strip()
                if len(line) >= 10 and _is_relevant_product(query, line):
                    name = line[:120]
                    break
            break

    # エコーバック最終防御: 抽出された商品名がクエリと実質同一なら拒否
    if name:
        _echo_strip = re.compile(r'[\s　\-/／「」『』【】()（）・、。,.]+')
        _n_norm = _echo_strip.sub('', name.lower())
        _q_norm = _echo_strip.sub('', query.lower())
        if _n_norm and _q_norm and (
                _n_norm == _q_norm
                or _n_norm in _q_norm
                or _q_norm in _n_norm):
            logger.info("Fulltext echo-back rejected: name='%s' ≈ query='%s'",
                        name[:80], query[:80])
            name = ""

    # 商品名が取得できなかった場合は結果を返さない（エコーバック防止）
    if not name:
        logger.info("Fulltext: price ¥%s found but no valid product name (echo-back guard)",
                    f"{price:,}")
        return None

    # URLは検索URL（フルテキストからは個別URL取得困難）
    return price, name, ""


def _extract_price_from_raw_html(html: str, query: str,
                                  base_url: str) -> tuple[int, str, str] | None:
    """生HTML内の価格パターン検索（テキスト抽出で消えた価格を拾う）

    JSフレームワーク(React/Vue/Next.js/Nuxt等)がデータをscriptタグや
    data属性、JSONオブジェクト内に格納している場合に有効。
    get_text()では取得できない価格を生HTMLから直接抽出する。
    """
    query_lower = query.lower()
    keywords = [w for w in re.split(r'[\s　/／\-]+', query_lower) if len(w) >= 2 or w.isdigit()]
    if not keywords:
        return None

    # 生HTML内の価格パターン（JS変数、JSON、data属性、HTMLエンティティ）
    price_pattern = re.compile(
        r'"price"\s*:\s*(\d{3,8})'             # "price": 38192 (JSON number)
        r'|"price"\s*:\s*"([\d,]{3,8})"'       # "price": "38,192" (JSON string with commas)
        r'|"salePrice"\s*:\s*"?([\d,]{3,8})"?' # "salePrice": 38192 or "38,192"
        r'|"itemPrice"\s*:\s*"?([\d,]{3,8})"?' # "itemPrice": 38192
        r'|"sellingPrice"\s*:\s*"?([\d,]{3,8})"?'  # "sellingPrice"
        r'|"displayPrice"\s*:\s*"?([\d,]{3,8})"?'  # "displayPrice"
        r'|"amount"\s*:\s*"?([\d,]{3,8})"?'    # "amount": 38192
        r'|data-price="([\d,]{3,8})"'          # data-price="38192"
        r'|data-item-price="([\d,]{3,8})"'     # data-item-price="38192"
        r'|"lowPrice"\s*:\s*"?([\d,]{3,8})"?'  # "lowPrice": 38192
        r'|[¥￥]\s*([\d,]{4,})'                # ¥38,192 in HTML
        r'|([\d,]{4,})\s*円'                   # 38,192円 in HTML
        r'|&yen;\s*([\d,]{4,})'                # &yen;38,192 (HTML entity)
        r'|&#165;\s*([\d,]{4,})'               # &#165;38,192 (numeric entity)
    )

    # グループ数を計算
    _NUM_GROUPS = 14

    html_lower = html.lower()
    candidates = []

    for m in price_pattern.finditer(html):
        raw = None
        for i in range(1, _NUM_GROUPS + 1):
            if m.group(i):
                raw = m.group(i)
                break
        if not raw:
            continue

        digits = re.sub(r'[^\d]', '', raw)
        if not digits:
            continue
        price = int(digits)
        if not (1000 <= price <= 99_999_999):
            continue

        # キーワード近接チェック（HTMLなのでタグを跨ぐ）
        window = 2000  # HTML内はタグが多いので広めの窓
        start = max(0, m.start() - window)
        end = min(len(html_lower), m.end() + window)
        context = html_lower[start:end]

        # 非商品価格の除外（送料閾値、ポイント、配送料など）
        narrow_ctx = html_lower[max(0, m.start() - 100):min(len(html_lower), m.end() + 100)]
        _NON_PRODUCT_PATTERNS_HTML = [
            r'配送料', r'送料', r'以上で.*無料', r'以上で.*負担',
            r'ポイント', r'point', r'還元', r'付与',
            r'off\b', r'割引', r'クーポン', r'coupon',
            r'お問い合わせ', r'よくある質問', r'ヘルプ', r'FAQ',
        ]
        is_non_product = any(re.search(p, narrow_ctx) for p in _NON_PRODUCT_PATTERNS_HTML)
        if is_non_product:
            continue

        # カタカナ展開込みでキーワードマッチ
        match_count = 0
        for kw in keywords:
            variants = [kw] + _KEYWORD_KATAKANA_MAP.get(kw, [])
            if any(v.lower() in context for v in variants):
                match_count += 1
        required = max(1, (len(keywords) + 1) // 2)
        if match_count >= required:
            candidates.append(price)

    if not candidates:
        return None

    # 外れ値除去
    candidates.sort()
    if len(candidates) >= 3:
        median = candidates[len(candidates) // 2]
        candidates = [p for p in candidates if median * 0.4 <= p <= median * 2.5]
    if len(candidates) >= 2:
        if candidates[0] < candidates[1] * 0.5:
            candidates = candidates[1:]

    if not candidates:
        return None

    # 候補が1件のみで低価格の場合、検索エコーバック（検索窓の表示）の可能性が高い
    # 複数候補があれば実際の商品リストと判断
    if len(candidates) == 1 and candidates[0] < 5000:
        logger.info("Raw HTML extraction: skipping single low price ¥%s (likely echo-back)",
                     f"{candidates[0]:,}")
        return None

    price = candidates[0]
    logger.info("Raw HTML extraction found price: ¥%s", f"{price:,}")
    # 商品名は不明（生HTMLからは商品名を確実に取得できない）
    # クエリをフォールバックにするとエコーバックの原因になるため空文字列を返す
    return price, "", ""


def _extract_price_by_text(soup: BeautifulSoup, query: str,
                           base_url: str) -> tuple[int, str, str] | None:
    """テキストベースの汎用価格抽出（CSSセレクタ不要）

    戦略:
    1. <a>タグ（リンク）からクエリに関連する商品を検索
    2. 見つからない場合、任意の要素からテキストベースで検索
    3. 親要素内の価格パターンを探す
    4. 統計的外れ値除去で最安値を決定
    """
    candidates = _find_product_candidates(soup, query, base_url)

    if not candidates:
        return None

    # === 外れ値除去（中央値ベース: DOM順維持） ===
    if len(candidates) >= 3:
        prices = sorted(c[0] for c in candidates)
        median_price = prices[len(prices) // 2]
        candidates = [c for c in candidates
                      if median_price * 0.3 <= c[0] <= median_price * 2.5]

    # 最安値を返す
    if candidates:
        return min(candidates, key=lambda c: c[0])
    return None


# 価格テキストパターン（共通定義 - カンマあり/なし両対応）
_PRICE_TEXT_PATTERNS = [
    r'[¥￥]\s*([\d,]+)',
    r'([\d]{1,3}(?:,\d{3})+)\s*円',
    r'(\d{4,8})\s*円',  # カンマなし: 39800円
    r'(?:税込|価格|特価|販売価格)\s*[^\d]*([\d,]{4,})',
]


def _extract_prices_from_text(text: str) -> list[int]:
    """テキストから価格パターンを全て抽出"""
    found = []
    for pattern in _PRICE_TEXT_PATTERNS:
        for m in re.finditer(pattern, text):
            digits = re.sub(r'[^\d]', '', m.group(1))
            if digits:
                val = int(digits)
                if 100 <= val <= 99_999_999:
                    found.append(val)
    return found


def _find_price_near_element(el, max_levels: int = 8) -> int | None:
    """要素の親を遡って最初に見つかった価格を返す"""
    for level in range(1, max_levels + 1):
        parent = el
        for _ in range(level):
            if parent.parent is None:
                return None
            parent = parent.parent

        parent_text = parent.get_text(separator=" ", strip=True)
        # テキストが長すぎる場合は終了（上位レベルはさらに大きいため）
        if len(parent_text) > 5000:
            break
        prices = _extract_prices_from_text(parent_text)
        if prices:
            return min(prices)
    return None


def _find_product_candidates(soup: BeautifulSoup, query: str,
                             base_url: str) -> list[tuple[int, str, str]]:
    """商品候補をテキストベースで収集"""
    candidates = []
    seen_names = set()

    # 戦略1: <a>タグのテキストから商品を探す（最も信頼性が高い）
    for link in soup.find_all("a", href=True):
        name = link.get_text(strip=True)
        if not name or len(name) < 5 or name in seen_names:
            continue
        if not _is_relevant_product(query, name):
            continue
        seen_names.add(name)

        price = _find_price_near_element(link)
        if not price:
            continue

        href = link.get("href", "")
        if href.startswith("http"):
            url = href
        elif href.startswith("/") and base_url:
            url = f"{base_url}{href}"
        else:
            url = ""
        candidates.append((price, name, url))

        if len(candidates) >= 20:
            break

    if candidates:
        return candidates

    # 戦略2: リンクがなくても価格付きの商品テキストを探す
    # 一部サイトは<div>や<span>に商品名を入れ、<a>には入れない
    query_lower = query.lower()
    keywords = [w for w in re.split(r'[\s　/／\-]+', query_lower)
                if len(w) >= 2]
    if not keywords:
        return []

    # 価格要素を探してその近くに商品名があるか確認
    # テキスト内に価格パターンがある要素をregexで先にフィルタ
    price_elements = []
    for tag in itertools.islice(
        soup.find_all(string=re.compile(r'[¥￥][\d,]+|[\d,]+円')), 100
    ):
        if tag.parent:
            price_elements.append(tag.parent)
    for price_el in price_elements[:50]:
        price_text = price_el.get_text(strip=True)
        prices = _extract_prices_from_text(price_text)
        if not prices:
            continue
        price = min(prices)

        # 親要素からテキストを取得し、商品名にクエリキーワードが含まれるか確認
        for level in range(1, 6):
            parent = price_el
            for _ in range(level):
                if parent.parent is None:
                    break
                parent = parent.parent
            parent_text = parent.get_text(separator=" ", strip=True)
            if len(parent_text) > 5000:
                break
            parent_lower = parent_text.lower()
            match_count = sum(1 for kw in keywords if kw in parent_lower)
            required = max(1, (len(keywords) + 1) // 2)
            if match_count >= required:
                # 商品名としてリンクテキストか親テキストの先頭を使う
                link = parent.find("a")
                name = link.get_text(strip=True) if link else parent_text[:100]
                if _is_relevant_product(query, name):
                    url = ""
                    if link and link.get("href", "").startswith("http"):
                        url = link["href"]
                    elif link and link.get("href", "").startswith("/") and base_url:
                        url = f"{base_url}{link['href']}"
                    candidates.append((price, name, url))
                break

        if len(candidates) >= 20:
            break

    return candidates


def _make_error_result(shop_name: str, search_url: str, error: str) -> ShopPrice:
    """エラー結果を生成（共通ヘルパー）"""
    return ShopPrice(shop_name, None, "", "", search_url, error=error)


# bot対策が厳しいショップ → Phase1でセッション共有（ホームページ→検索）
_SESSION_FIRST_SHOPS: set[str] = {
    "ビックカメラ.com", "コジマネット", "ヤマダウェブコム",
    "au PAY マーケット", "ノジマオンライン", "エディオンネットショップ",
    "ケーズデンキオンラインショップ", "Joshin webショップ",
}


def _scrape_generic(shop_name: str, search_url: str,
                    selectors: list[tuple[str, str, str]],
                    base_url: str,
                    headers: dict | None = None,
                    query: str = "") -> ShopPrice:
    """汎用スクレイパー: HTML取得→セレクタ→JSON-LD→埋め込みJSONの順で試行

    改善:
    - bot検出ページを判定して適切なエラーメッセージ
    - 検索クエリとの関連性チェック（無関係な商品を除外）
    - bot検出時はリトライ（セッション共有 or 直接）
    - 厳しいbot対策ショップではホームページ訪問でcookie取得後に検索
    """
    max_attempts = 2
    use_session_first = shop_name in _SESSION_FIRST_SHOPS
    req_timeout = _TIMEOUT_SLOW if shop_name in _SLOW_SHOPS else _TIMEOUT

    for attempt in range(max_attempts):
        try:
            # リトライ時は待機
            if attempt > 0:
                time.sleep(3 + random.uniform(0, 2))

            session = _new_session()
            if headers:
                session.headers.update(headers)
            # Refererを自動設定
            if not (headers and "Referer" in headers):
                session.headers["Referer"] = base_url + "/"

            # bot対策が厳しいショップ: ホームページを先に訪問してcookie取得
            if use_session_first:
                try:
                    home_resp = session.get(base_url + "/", timeout=15)
                    logger.debug("Session-first for %s: home status=%d, cookies=%d",
                                 shop_name, home_resp.status_code, len(session.cookies))
                    # セッションcookieが取れたらRefererを更新
                    session.headers["Referer"] = base_url + "/"
                    session.headers["Sec-Fetch-Site"] = "same-origin"
                    time.sleep(1 + random.uniform(0, 1))
                except Exception as e:
                    logger.debug("Session-first homepage failed for %s: %s", shop_name, e)

            resp = session.get(search_url, timeout=req_timeout)
            if resp.status_code == 403:
                if attempt < max_attempts - 1:
                    continue  # リトライ
                return _make_error_result(shop_name, search_url, "アクセス制限（手動で検索してください）")
            if resp.status_code == 503:
                if attempt < max_attempts - 1:
                    continue  # リトライ
                return _make_error_result(shop_name, search_url, "サーバー混雑（手動で検索してください）")
            if resp.status_code == 202:
                # HTTP 202 = サーバー処理中（Akamai等）→ 少し待ってリトライ
                if attempt < max_attempts - 1:
                    time.sleep(2)
                    continue
            if resp.status_code not in (200, 202):
                return _make_error_result(shop_name, search_url, f"HTTP {resp.status_code}")

            soup = _soup(resp)

            # bot検出ページの判定
            if _is_bot_blocked_page(soup):
                if attempt < max_attempts - 1:
                    logger.info("Bot blocked for %s, retrying...", shop_name)
                    continue  # リトライ
                return _make_error_result(shop_name, search_url, "アクセス制限（手動で検索してください）")

            price, name, url = _find_price_in_soup(soup, selectors, base_url, query=query)
            if price:
                return ShopPrice(shop_name, price, name, url, search_url)

            # 価格が見つからなかった理由をログ出力
            text_len = len(soup.get_text())
            logger.info("Phase1 no price for %s (HTML %d chars, status %d)",
                        shop_name, text_len, resp.status_code)
            return ShopPrice(shop_name, None, "", "", search_url)

        except requests.exceptions.SSLError:
            return _make_error_result(shop_name, search_url, "SSL接続エラー（手動で検索してください）")
        except requests.exceptions.ConnectionError:
            if attempt < max_attempts - 1:
                continue  # リトライ
            return _make_error_result(shop_name, search_url, "接続エラー（手動で検索してください）")
        except requests.exceptions.Timeout:
            if attempt < max_attempts - 1:
                continue  # リトライ
            return _make_error_result(shop_name, search_url, "タイムアウト（手動で検索してください）")
        except Exception as e:
            logger.warning("scrape error for %s: %s", shop_name, e)
            return _make_error_result(shop_name, search_url, "取得失敗（手動で検索してください）")

    return _make_error_result(shop_name, search_url, "取得失敗（手動で検索してください）")


# ============================================================
# 楽天市場 (API + Webスクレイピングフォールバック)
# ============================================================
def _search_rakuten_api(query: str, config: Config, search_url: str) -> ShopPrice | None:
    """楽天API検索（APIキー設定済みの場合のみ）。成功時はShopPrice、失敗時はNone"""
    if not config.rakuten_app_id:
        return None

    api_url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    params = {
        "applicationId": config.rakuten_app_id,
        "keyword": query,
        "hits": 20,
        "sort": "standard",  # 関連性順（価格順だとアクセサリが先に来る）
        "availability": 1,
    }

    try:
        resp = requests.get(api_url, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None  # APIエラー → Webスクレイピングにフォールバック

        data = resp.json()
        items = data.get("Items", [])
        if not items:
            return ShopPrice("楽天市場", None, "", "", search_url)

        # 関連性チェック→外れ値除去→最安値選択
        candidates = []
        for item_wrapper in items:
            item = item_wrapper.get("Item", {})
            name = item.get("itemName", "")
            if not _is_relevant_product(query, name):
                continue
            price = item.get("itemPrice", 0)
            if price < 100:
                continue
            candidates.append((price, name, item.get("itemUrl", "")))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            # 外れ値除去
            if len(candidates) >= 3:
                median_price = candidates[len(candidates) // 2][0]
                candidates = [c for c in candidates if c[0] >= median_price * 0.3]
            if candidates:
                price, name, url = candidates[0]
                return ShopPrice(
                    shop_name="楽天市場",
                    price=price,
                    product_name=name,
                    product_url=url,
                    search_url=search_url,
                )
        # 全て無関係だった場合
        return ShopPrice("楽天市場", None, "", "", search_url)
    except Exception:
        return None  # フォールバック


def search_rakuten(query: str, config: Config) -> ShopPrice:
    search_url = f"https://search.rakuten.co.jp/search/mall/{quote(query)}/"

    # 1. APIキーがあればAPIを試行
    api_result = _search_rakuten_api(query, config, search_url)
    if api_result is not None:
        return api_result

    # 2. Webスクレイピングフォールバック（APIキー不要）
    selectors = [
        # 2024-2026年の楽天検索結果ページ
        (".searchresultitem", ".important", ".title a"),
        ('[class*="dui-card"]', '[class*="price"]', '[class*="title"] a'),
        ('[class*="product"]', '[class*="price"]', '[class*="title"] a, [class*="name"] a'),
        # React版の楽天検索ページ
        ('[data-testid*="item"], [data-testid*="product"]',
         '[data-testid*="price"], [class*="price"]',
         'a[data-testid*="title"], a[data-testid*="name"]'),
        # 旧版
        (".item", ".price", "a.title, .name a"),
        # 汎用フォールバック
        ("div.content--2nRdK, div[class*='content']",
         "span.price--3kDkc, span[class*='price']",
         "a[class*='title'], a[class*='name']"),
    ]
    return _scrape_generic("楽天市場", search_url, selectors,
                           "https://search.rakuten.co.jp", query=query)


# ============================================================
# Yahoo!ショッピング (API)
# ============================================================
def _search_yahoo_api(query: str, config: Config, search_url: str) -> ShopPrice | None:
    """Yahoo API検索（APIキー設定済みの場合のみ）。成功時はShopPrice、失敗時はNone"""
    if not config.yahoo_app_id:
        return None

    api_url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    params = {
        "appid": config.yahoo_app_id,
        "query": query,
        "results": 30,
        "sort": "-score",  # 関連性順（価格順だとアクセサリ/ゴミが上位に来る）
        "in_stock": "true",
    }

    try:
        resp = requests.get(api_url, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None  # APIエラー → Webスクレイピングにフォールバック

        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return ShopPrice("Yahoo!ショッピング", None, "", "", search_url)

        # 関連性チェック→外れ値除去→最安値選択
        candidates = []
        for hit in hits:
            name = hit.get("name", "")
            if not _is_relevant_product(query, name):
                continue
            price = int(hit.get("price", 0))
            if price < 100:
                continue
            candidates.append((price, name, hit.get("url", "")))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            # 外れ値除去: 3件以上あれば中央値の30%未満の価格は除外
            # （アクセサリがフィルタをすり抜けた場合の安全策）
            if len(candidates) >= 3:
                median_price = candidates[len(candidates) // 2][0]
                candidates = [c for c in candidates if c[0] >= median_price * 0.3]
            if candidates:
                price, name, url = candidates[0]
                return ShopPrice(
                    shop_name="Yahoo!ショッピング",
                    price=price,
                    product_name=name,
                    product_url=url,
                    search_url=search_url,
                )
        return ShopPrice("Yahoo!ショッピング", None, "", "", search_url)
    except Exception:
        return None  # フォールバック


def search_yahoo(query: str, config: Config) -> ShopPrice:
    search_url = f"https://shopping.yahoo.co.jp/search?p={quote(query)}&ss_first=1&ts=1&mcr=on&used=0&stipIcon=1&elc=1&cid=&brandId=&oid=&ran_cr=&ran_sh=&X=2&di=&sc_i=shp_pc_search_searchBox_2"

    # 1. APIキーがあればAPIを試行
    api_result = _search_yahoo_api(query, config, search_url)
    if api_result is not None:
        return api_result

    # 2. Webスクレイピングフォールバック（APIキー不要）
    selectors = [
        # Yahoo!ショッピング検索結果ページのセレクタ
        ('[class*="SearchResult"] [class*="Product"]',
         '[class*="Product__price"], [class*="Price__value"]',
         '[class*="Product__title"] a, [class*="Product__name"] a'),
        (".mdSearchProduct", ".elPriceValue, .mdSearchProduct__price",
         ".mdSearchProduct__title a, .elProductName a"),
        ('[data-cl-params*="product"]',
         '[class*="price"], [class*="Price"]',
         'a[class*="title"], a[class*="Title"], a[class*="name"]'),
        ('[class*="ProductItem"], [class*="productItem"]',
         '[class*="price"], [class*="Price"]',
         '[class*="title"] a, [class*="name"] a'),
        (".product", ".price", ".product-name a, .title a"),
    ]
    return _scrape_generic("Yahoo!ショッピング", search_url, selectors,
                           "https://shopping.yahoo.co.jp", query=query)


# ============================================================
# Amazon.co.jp (スクレイピング)
# ============================================================
def search_amazon(query: str, _config: Config) -> ShopPrice:
    # 関連性順でソート（価格順だとアクセサリが先に来る）
    search_url = f"https://www.amazon.co.jp/s?k={quote(query)}"

    try:
        resp = _fetch(search_url)
        if resp.status_code != 200:
            return _make_error_result("Amazon.co.jp", search_url, f"HTTP {resp.status_code}")

        soup = _soup(resp)

        # bot検出チェック
        if _is_bot_blocked_page(soup):
            logger.info("Amazon: bot blocked page detected")
            return _make_error_result("Amazon.co.jp", search_url, "アクセス制限（手動で検索してください）")

        results_found = soup.select('[data-component-type="s-search-result"]')
        logger.info("Amazon: found %d search results, HTML %d chars",
                     len(results_found), len(resp.text))

        # 全候補を収集して最安値を返す
        all_candidates = []
        first_valid_price_result = None

        for result in results_found:
            sponsored = result.select_one('.s-label-popover-default')
            if sponsored and 'スポンサー' in sponsored.get_text():
                continue

            price_whole = result.select_one(".a-price .a-price-whole")
            if not price_whole:
                continue
            price = _parse_price(price_whole.get_text())
            if not price:
                continue

            # 商品名: 複数の方法で抽出（Amazon HTML頻繁変更に対応）
            name = ""

            # 方法1: 各種CSSセレクタ
            for name_sel in ["h2 a span", "h2 span", "h2 a",
                             '[data-cy="title-recipe"] a span',
                             ".a-text-normal", ".a-link-normal .a-text-normal",
                             'span[class*="a-size-medium"]',
                             'span[class*="a-size-base-plus"]']:
                title_el = result.select_one(name_sel)
                if title_el:
                    name = title_el.get_text(strip=True)
                    if name:
                        break

            # 方法2: h2全体のテキスト
            if not name:
                h2 = result.select_one("h2")
                if h2:
                    name = h2.get_text(strip=True)

            # 方法3: aria-label属性（アクセシビリティ用に商品名が入っている）
            if not name:
                for el in result.select("[aria-label]"):
                    label = el.get("aria-label", "")
                    if len(label) > 10:  # 短すぎるのは無視
                        name = label
                        break

            # 方法4: 画像のalt属性
            if not name:
                img = result.select_one("img.s-image, img[data-image-latency]")
                if img and img.get("alt"):
                    name = img["alt"]

            # 関連性チェック（中古・整備済み品も除外される）
            if name and not _is_relevant_product(query, name):
                if len(all_candidates) == 0 and first_valid_price_result is None:
                    logger.info("Amazon: rejected '%s' (¥%s) by relevance filter",
                                name[:80], f"{price:,}")
                continue

            link_el = result.select_one("h2 a")
            url = ""
            if link_el and link_el.get("href"):
                href = link_el["href"]
                url = f"https://www.amazon.co.jp{href}" if href.startswith("/") else href

            if name:
                all_candidates.append((price, name, url))
            elif first_valid_price_result is None:
                first_valid_price_result = ShopPrice("Amazon.co.jp", price, "(商品名取得不可)", url, search_url)

        # 最安値を返す（候補がある場合）
        if all_candidates:
            # 外れ値除去（中央値の30%未満を除外 — アクセサリ等）
            prices = sorted(c[0] for c in all_candidates)
            if len(prices) >= 3:
                median = prices[len(prices) // 2]
                all_candidates = [c for c in all_candidates if c[0] >= median * 0.3]
            if all_candidates:
                best = min(all_candidates, key=lambda c: c[0])
                return ShopPrice("Amazon.co.jp", best[0], best[1], best[2], search_url)

        # 名前付き商品が見つからなかった場合、名前なしでも価格があれば返す
        if first_valid_price_result:
            logger.info("Amazon: using nameless result with price %d", first_valid_price_result.price)
            return first_valid_price_result

        # JSON-LD/embedded JSONもフォールバックとして試す
        price, name, url = _find_price_in_soup(soup, [], "https://www.amazon.co.jp", query=query)
        if price:
            return ShopPrice("Amazon.co.jp", price, name, url, search_url)

        # === 簡略クエリでリトライ ===
        # 長いクエリで関連商品が見つからない場合、サイズ/一般語を除いて再検索
        simplified = _simplify_query(query)
        if simplified:
            logger.info("Amazon: retrying with simplified query '%s'", simplified)
            retry_url = f"https://www.amazon.co.jp/s?k={quote(simplified)}"
            try:
                resp2 = _fetch(retry_url)
                if resp2.status_code == 200:
                    soup2 = _soup(resp2)
                    if not _is_bot_blocked_page(soup2):
                        retry_results = soup2.select('[data-component-type="s-search-result"]')
                        logger.info("Amazon retry: found %d search results", len(retry_results))
                        retry_candidates = []
                        for result in retry_results:
                            sponsored = result.select_one('.s-label-popover-default')
                            if sponsored and 'スポンサー' in sponsored.get_text():
                                continue
                            price_whole = result.select_one(".a-price .a-price-whole")
                            if not price_whole:
                                continue
                            p = _parse_price(price_whole.get_text())
                            if not p:
                                continue
                            n = ""
                            for name_sel in ["h2 a span", "h2 span", "h2 a",
                                             ".a-text-normal",
                                             'span[class*="a-size-medium"]',
                                             'span[class*="a-size-base-plus"]']:
                                title_el = result.select_one(name_sel)
                                if title_el:
                                    n = title_el.get_text(strip=True)
                                    if n:
                                        break
                            if not n:
                                h2 = result.select_one("h2")
                                if h2:
                                    n = h2.get_text(strip=True)
                            if not n:
                                img = result.select_one("img.s-image")
                                if img and img.get("alt"):
                                    n = img["alt"]
                            # 簡略クエリに対する関連性チェック（元クエリでは厳しすぎる）
                            if n and not _is_relevant_product(simplified, n):
                                logger.info("Amazon retry rejected: '%s' (¥%s)",
                                            n[:80], f"{p:,}")
                                continue
                            link_el = result.select_one("h2 a")
                            u = ""
                            if link_el and link_el.get("href"):
                                href = link_el["href"]
                                u = f"https://www.amazon.co.jp{href}" if href.startswith("/") else href
                            if n:
                                retry_candidates.append((p, n, u))

                        if retry_candidates:
                            prices = sorted(c[0] for c in retry_candidates)
                            if len(prices) >= 3:
                                median = prices[len(prices) // 2]
                                retry_candidates = [c for c in retry_candidates if c[0] >= median * 0.3]
                            if retry_candidates:
                                best = min(retry_candidates, key=lambda c: c[0])
                                logger.info("Amazon retry OK: ¥%s (%s)", f"{best[0]:,}", best[1][:50])
                                return ShopPrice("Amazon.co.jp", best[0], best[1], best[2], retry_url)
            except Exception as retry_err:
                logger.debug("Amazon retry error: %s", retry_err)

        # === 英語クエリでリトライ ===
        # カタカナブランド名を英語に変換して再検索（Amazon は英語名でインデックスしていることが多い）
        # 例: "スキンシューティカルズ CE フェルリック" → "skinceuticals CE ferulic"
        english_query = _build_english_query(query)
        if english_query:
            logger.info("Amazon: retrying with English query '%s'", english_query)
            eng_url = f"https://www.amazon.co.jp/s?k={quote(english_query)}"
            try:
                resp_eng = _fetch(eng_url)
                if resp_eng.status_code == 200:
                    soup_eng = _soup(resp_eng)
                    if not _is_bot_blocked_page(soup_eng):
                        eng_results = soup_eng.select('[data-component-type="s-search-result"]')
                        logger.info("Amazon English retry: found %d search results", len(eng_results))
                        eng_candidates = []
                        for result in eng_results:
                            sponsored = result.select_one('.s-label-popover-default')
                            if sponsored and 'スポンサー' in sponsored.get_text():
                                continue
                            price_whole = result.select_one(".a-price .a-price-whole")
                            if not price_whole:
                                continue
                            p = _parse_price(price_whole.get_text())
                            if not p:
                                continue
                            n = ""
                            for name_sel in ["h2 a span", "h2 span", "h2 a",
                                             ".a-text-normal",
                                             'span[class*="a-size-medium"]',
                                             'span[class*="a-size-base-plus"]']:
                                title_el = result.select_one(name_sel)
                                if title_el:
                                    n = title_el.get_text(strip=True)
                                    if n:
                                        break
                            if not n:
                                h2 = result.select_one("h2")
                                if h2:
                                    n = h2.get_text(strip=True)
                            if not n:
                                img = result.select_one("img.s-image")
                                if img and img.get("alt"):
                                    n = img["alt"]
                            # 英語クエリに対する関連性チェック
                            if n and not _is_relevant_product(english_query, n):
                                logger.info("Amazon English retry rejected: '%s' (¥%s)",
                                            n[:80], f"{p:,}")
                                continue
                            link_el = result.select_one("h2 a")
                            u = ""
                            if link_el and link_el.get("href"):
                                href = link_el["href"]
                                u = f"https://www.amazon.co.jp{href}" if href.startswith("/") else href
                            if n:
                                eng_candidates.append((p, n, u))

                        if eng_candidates:
                            prices = sorted(c[0] for c in eng_candidates)
                            if len(prices) >= 3:
                                median = prices[len(prices) // 2]
                                eng_candidates = [c for c in eng_candidates if c[0] >= median * 0.3]
                            if eng_candidates:
                                best = min(eng_candidates, key=lambda c: c[0])
                                logger.info("Amazon English retry OK: ¥%s (%s)", f"{best[0]:,}", best[1][:50])
                                return ShopPrice("Amazon.co.jp", best[0], best[1], best[2], eng_url)
            except Exception as eng_err:
                logger.debug("Amazon English retry error: %s", eng_err)

        return ShopPrice("Amazon.co.jp", None, "", "", search_url)

    except Exception as e:
        logger.warning("Amazon scrape error: %s", e)
        return _make_error_result("Amazon.co.jp", search_url, "取得失敗（手動で検索してください）")


# ============================================================
# ビックカメラ.com (スクレイピング)
# ============================================================
def search_biccamera(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.biccamera.com/bc/category/?q={quote(query)}&rowPerPage=25"
    selectors = [
        (".bcs_listItem", ".bcs_price", ".bcs_title a"),
        (".prod_box", ".val", ".prod_name a"),
        (".bcs_item", ".bcs_price .val", ".bcs_title a"),
        (".product_list_item", ".price", ".product_name a"),
        ("li.prod_item", ".prod_price", ".prod_name a"),
    ]
    return _scrape_generic("ビックカメラ.com", search_url, selectors,
                           "https://www.biccamera.com",
                           headers={
                               "Referer": "https://www.biccamera.com/",
                               "Sec-Fetch-Site": "same-origin",
                           }, query=query)


# ============================================================
# コジマネット (スクレイピング)
# ============================================================
def search_kojima(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.kojima.net/ec/prod_list.html?keyword={quote(query)}"
    selectors = [
        (".product-list-item", ".price, .itemPrice, .product-price", ".product-name a, .itemName a"),
        (".itemBox", ".itemPrice", ".itemName a"),
        (".product_item", ".product-price", ".product_name a"),
        ("li.item", ".price", "a.item-name, .name a"),
        (".goods_list li", ".price", ".goods_name a"),
    ]
    return _scrape_generic("コジマネット", search_url, selectors,
                           "https://www.kojima.net", query=query)


# ============================================================
# ヤマダウェブコム (スクレイピング)
# ============================================================
def search_yamada(query: str, _config: Config) -> ShopPrice:
    # ヤマダ: /search/query/ と ?keyword= の2パターンを試行
    search_urls = [
        f"https://www.yamada-denkiweb.com/search?keyword={quote(query)}",
        f"https://www.yamada-denkiweb.com/search/{quote(query)}/",
    ]
    selectors = [
        (".searchResult__item", ".searchResult__price, .pPrice", ".searchResult__name a, .pName a"),
        (".product", ".price, .product-price", ".product-name a"),
        (".item", ".pPrice", ".pName a"),
        ('[class*="c-product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ("li.product-item", ".price-box .price", ".product-item-link"),
    ]
    for url in search_urls:
        result = _scrape_generic("ヤマダウェブコム", url, selectors,
                                 "https://www.yamada-denkiweb.com",
                                 headers={"Referer": "https://www.yamada-denkiweb.com/"},
                                 query=query)
        if result.price is not None:
            return result
    return ShopPrice("ヤマダウェブコム", None, "", "", search_urls[0],
                     error=result.error if result else None)


# ============================================================
# Joshin webショップ (スクレイピング)
# ============================================================
def search_joshin(query: str, _config: Config) -> ShopPrice:
    # Joshin: 複数URLパターン試行（servletは遅いのでシンプルなURLを優先）
    search_urls = [
        f"https://joshinweb.jp/search?keyword={quote(query)}",
        f"https://joshinweb.jp/servlet/emall.odr_wp?QS=&REQUEST_CODE=1&category_id=&SHP=0&QK={quote(query)}&PID=srhzs",
    ]
    selectors = [
        (".productList__item", ".productList__price", ".productList__name a"),
        (".lineup_box", ".lineup_price", ".lineup_name a"),
        (".item", ".price", ".item-name a, .name a"),
        ("li.product-item", ".price-box .price", ".product-item-link"),
    ]
    for url in search_urls:
        result = _scrape_generic("Joshin webショップ", url, selectors,
                                 "https://joshinweb.jp", query=query)
        if result.price is not None:
            return result
        if result.error and "HTTP 404" not in (result.error or ""):
            if "タイムアウト" not in (result.error or ""):
                return result
    return ShopPrice("Joshin webショップ", None, "", "", search_urls[0],
                     error=result.error if result else None)


# ============================================================
# au PAY マーケット (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_aupay(query: str, _config: Config) -> ShopPrice:
    # au PAY マーケット: wowma.jp
    # 注: au PAY は bot 対策が厳しく cloudscraper では空ページを返すことが多い。
    # Phase 2 (Playwright) での再試行に期待。
    search_urls = [
        f"https://wowma.jp/itemlist?keyword={quote(query)}",
        f"https://wowma.jp/search/{quote(query)}/",
    ]
    selectors = [
        (".itemList__item", ".itemList__price, .price", ".itemList__name a, .product-name a"),
        (".product-item", ".product-price, .price", ".product-name a"),
        ('[class*="ItemCard"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ('[class*="item"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
    ] + _GENERIC_SELECTORS
    last_error = None
    for url in search_urls:
        result = _scrape_generic("au PAY マーケット", url, selectors,
                                 "https://wowma.jp", query=query)
        if result.price is not None:
            return result
        if result.error:
            last_error = result.error
    return ShopPrice("au PAY マーケット", None, "", "", search_urls[0],
                     error=last_error)


# ============================================================
# セブンネットショッピング (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_seven(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://7net.omni7.jp/search/?keyword={quote(query)}&searchKeywordFlg=1"
    selectors = [
        (".productItem", ".productPrice, .price", ".productName a, .product-name a"),
        (".product", ".price, .productPrice", ".productName a"),
        (".item", ".price, .item-price", ".item-name a, .productName a"),
        ('[class*="product"]', '[class*="price"]', '[class*="name"] a'),
    ]
    return _scrape_generic("セブンネットショッピング", search_url, selectors,
                           "https://7net.omni7.jp", query=query)


# ============================================================
# Qoo10 (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_qoo10(query: str, _config: Config) -> ShopPrice:
    search_url = f"https://www.qoo10.jp/s/{quote(query)}?keyword={quote(query)}"
    selectors = [
        (".sc-prd", ".prc .prc-dc, .prc", ".tit a, .sbj a"),
        (".item_g", ".price, .prc", ".sbj a"),
        # Qoo10 2025年版セレクタ
        ('[class*="goods"]', '[class*="price"], [class*="prc"]',
         '[class*="name"] a, [class*="sbj"] a, [class*="tit"] a'),
        (".gd_list li, .lst_cont li", ".prc, .price", ".tit a, .sbj a, .name a"),
        ('[class*="product"]', '[class*="price"]', '[class*="title"] a, [class*="name"] a'),
        (".goods_item", ".price", ".goods_name a, .title a"),
    ]
    return _scrape_generic("Qoo10", search_url, selectors,
                           "https://www.qoo10.jp", query=query)


# ============================================================
# エディオンネットショップ (HTML + JSON-LD + 埋め込みJSON)
# ============================================================
def search_edion(query: str, _config: Config) -> ShopPrice:
    # エディオン: detail_search.html と /search/ の両方を試す
    search_urls = [
        f"https://www.edion.com/search/?keyword={quote(query)}",
        f"https://www.edion.com/detail_search.html?q={quote(query)}",
    ]
    selectors = [
        (".goods-list-item, .goodsListItem", ".goods-price, .goodsPrice, .price",
         ".goods-name a, .goodsName a, .product-name a"),
        (".product-item, .item-list__item", ".price, .item-price", ".product-name a, .item-name a"),
        ('[class*="product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ('[class*="item"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ('[class*="goods"]', '[class*="price"]', '[class*="goods"] a, [class*="name"] a'),
        (".searchResultItem", ".resultPrice", ".resultName a"),
    ]
    for url in search_urls:
        result = _scrape_generic("エディオンネットショップ", url, selectors,
                                 "https://www.edion.com", query=query)
        if result.price is not None:
            return result
    # 全URL失敗時は最後のURLで結果を返す
    return ShopPrice("エディオンネットショップ", None, "", "", search_urls[0])


# ============================================================
# 汎用スクレイパー生成ファクトリ
# ============================================================

# 多くのECサイトで使える汎用CSSセレクタ
_GENERIC_SELECTORS: list[tuple[str, str, str]] = [
    ('[class*="product-item"], [class*="productItem"], [class*="ProductItem"]',
     '[class*="price"], [class*="Price"]',
     '[class*="name"] a, [class*="Name"] a, [class*="title"] a, [class*="Title"] a'),
    ('[class*="item-card"], [class*="itemCard"], [class*="ItemCard"]',
     '[class*="price"], [class*="Price"]',
     '[class*="name"] a, [class*="Name"] a, [class*="title"] a'),
    (".product", ".price", ".product-name a, .name a"),
    (".item", ".price", ".item-name a, .name a"),
    ("li.product-item", ".price-box .price", ".product-item-link"),
]


def _make_generic_scraper(
    shop_name: str,
    url_template: str,
    base_url: str,
    selectors: list[tuple[str, str, str]] | None = None,
    headers: dict | None = None,
):
    """汎用スクレイパー関数を生成するファクトリ"""

    def scraper(query: str, _config: Config) -> ShopPrice:
        search_url = url_template.replace("{query}", quote(query))
        sels = selectors or _GENERIC_SELECTORS
        return _scrape_generic(shop_name, search_url, sels, base_url,
                               headers=headers, query=query)

    scraper.__name__ = f"search_{shop_name}"
    return scraper


# ============================================================
# 追加ショップ（汎用スクレイパーで自動生成）
# ============================================================
search_uniqlo = _make_generic_scraper(
    "ユニクロオンラインストア",
    "https://www.uniqlo.com/jp/ja/search?q={query}",
    "https://www.uniqlo.com",
)

search_muji = _make_generic_scraper(
    "無印良品ネットストア",
    "https://www.muji.com/jp/ja/search?q={query}",
    "https://www.muji.com",
)

search_jalmall = _make_generic_scraper(
    "JAL Mall",
    "https://ec.jal.co.jp/shop/goods/search.aspx?keyword={query}&search=x",
    "https://ec.jal.co.jp",
    headers={"Sec-Fetch-Site": "same-origin", "Referer": "https://ec.jal.co.jp/shop/"},
)

search_bellemaison = _make_generic_scraper(
    "ベルメゾンネット",
    "https://www.bellemaison.jp/search/?keyword={query}",
    "https://www.bellemaison.jp",
)

search_lohaco = _make_generic_scraper(
    "LOHACO",
    "https://lohaco.yahoo.co.jp/search?p={query}",
    "https://lohaco.yahoo.co.jp",
)

search_nitori = _make_generic_scraper(
    "ニトリネット",
    "https://www.nitori-net.jp/ec/search/?q={query}",
    "https://www.nitori-net.jp",
)

search_zozo = _make_generic_scraper(
    "ZOZOTOWN",
    "https://zozo.jp/search/?p_keyv={query}",
    "https://zozo.jp",
)

search_dhc = _make_generic_scraper(
    "DHCオンラインショップ",
    "https://www.dhc.co.jp/goods/search.jsp?keyword={query}",
    "https://www.dhc.co.jp",
)

search_fancl = _make_generic_scraper(
    "ファンケルオンライン",
    "https://www.fancl.co.jp/search/?q={query}",
    "https://www.fancl.co.jp",
)

search_sony = _make_generic_scraper(
    "ソニーストア",
    "https://pur.store.sony.jp/search/?q={query}",
    "https://pur.store.sony.jp",
)

search_ksdenki = _make_generic_scraper(
    "ケーズデンキオンラインショップ",
    "https://www.ksdenki.com/shop/goods/search.aspx?keyword={query}",
    "https://www.ksdenki.com",
    headers={"Referer": "https://www.ksdenki.com/shop/", "Sec-Fetch-Site": "same-origin"},
)

def search_nojima(query: str, _config: Config) -> ShopPrice:
    """ノジマオンライン: 複数URLパターン試行"""
    q = quote(_normalize_query(query))
    search_urls = [
        f"https://online.nojima.co.jp/search?keyword={q}",
        f"https://online.nojima.co.jp/commodity/list/?searchWord={q}",
        f"https://online.nojima.co.jp/app/catalog/list/init?searchWord={q}",
        f"https://online.nojima.co.jp/search/?q={q}",
    ]
    selectors = [
        # ノジマ固有
        (".catalogListItem, .list-item", ".catalogPrice, .price, .item-price",
         ".catalogName a, .item-name a, .product-name a"),
        (".commodity-item", ".commodity-price, .price", ".commodity-name a"),
        ('[class*="catalog"]', '[class*="price"]', '[class*="name"] a'),
        ('[class*="commodity"]', '[class*="price"]', '[class*="name"] a'),
    ] + _GENERIC_SELECTORS
    for url in search_urls:
        result = _scrape_generic("ノジマオンライン", url, selectors,
                                 "https://online.nojima.co.jp", query=query)
        if result.price is not None:
            return result
        # HTTP 404なら次のURLを試す、それ以外のエラーなら返す
        if result.error and "HTTP 404" not in (result.error or ""):
            if "接続エラー" not in (result.error or ""):
                return result
    return ShopPrice("ノジマオンライン", None, "", "", search_urls[0],
                     error=result.error if result else None)

search_matsukiyo = _make_generic_scraper(
    "マツモトキヨシオンラインストア",
    "https://www.matsukiyo.co.jp/store/online/search?text={query}",
    "https://www.matsukiyo.co.jp",
)

search_dshopping = _make_generic_scraper(
    "dショッピング",
    "https://dshopping.docomo.ne.jp/search?keyword={query}",
    "https://dshopping.docomo.ne.jp",
    selectors=[
        (".c-productListItem, .productListItem", ".c-productListItem__price, .productPrice",
         ".c-productListItem__name a, .productName a"),
        ('[class*="ProductCard"]', '[class*="price"], [class*="Price"]',
         '[class*="name"] a, [class*="title"] a'),
        (".search-item, .item-card", ".price, .item-price", ".item-name a, .title a"),
    ] + _GENERIC_SELECTORS,
)

search_buyma = _make_generic_scraper(
    "BUYMA",
    "https://www.buyma.com/r/-{query}/?sort=2",
    "https://www.buyma.com",
    selectors=[
        # BUYMA 2025年版
        (".product_body", ".product_price .price, .product_price", ".product_name a"),
        (".product-card", ".product-card__price, .price", ".product-card__name a, .product_name a"),
        ('[class*="Product"]', '[class*="price"], [class*="Price"]', '[class*="name"] a, [class*="title"] a'),
        (".item_card", ".price", ".item_name a, .product_name a"),
        ('[class*="ProductCard"]', '[class*="price"], [class*="Price"]',
         '[class*="name"] a, [class*="title"] a'),
    ] + _GENERIC_SELECTORS,
)

search_abcmart = _make_generic_scraper(
    "ABC-MARTオンラインストア",
    "https://www.abc-mart.net/shop/goods/search.aspx?keyword={query}",
    "https://www.abc-mart.net",
)

search_gu = _make_generic_scraper(
    "GU オンラインストア",
    "https://www.gu-global.com/jp/ja/search?q={query}",
    "https://www.gu-global.com",
)

search_shopjapan = _make_generic_scraper(
    "ショップジャパン",
    "https://www.shopjapan.co.jp/search/?q={query}",
    "https://www.shopjapan.co.jp",
)

search_iherb = _make_generic_scraper(
    "iHerb",
    "https://jp.iherb.com/search?kw={query}",
    "https://jp.iherb.com",
)

search_cosme = _make_generic_scraper(
    "@cosme SHOPPING",
    "https://www.cosme.com/products/list.php?name={query}",
    "https://www.cosme.com",
)


# ============================================================
# 全ショップ検索定義
# ============================================================

# (検索関数, JALショップ名) のリスト
# JALショップ名は jal_shops.py の KNOWN_SHOPS のキーと一致させること
SCRAPERS = [
    (search_rakuten, "楽天市場"),
    (search_yahoo, "Yahoo!ショッピング"),
    (search_amazon, "Amazon.co.jp"),
    (search_biccamera, "ビックカメラ.com"),
    (search_kojima, "コジマネット"),
    (search_yamada, "ヤマダウェブコム"),
    (search_joshin, "Joshin webショップ"),
    (search_aupay, "au PAY マーケット"),
    (search_seven, "セブンネットショッピング"),
    (search_qoo10, "Qoo10"),
    (search_edion, "エディオンネットショップ"),
    # 追加ショップ（汎用スクレイパー）
    (search_uniqlo, "ユニクロオンラインストア"),
    (search_muji, "無印良品ネットストア"),
    (search_jalmall, "JAL Mall"),
    (search_bellemaison, "ベルメゾンネット"),
    (search_lohaco, "LOHACO"),
    (search_nitori, "ニトリネット"),
    (search_zozo, "ZOZOTOWN"),
    (search_dhc, "DHCオンラインショップ"),
    (search_fancl, "ファンケルオンライン"),
    (search_sony, "ソニーストア"),
    (search_ksdenki, "ケーズデンキオンラインショップ"),
    (search_nojima, "ノジマオンライン"),
    (search_matsukiyo, "マツモトキヨシオンラインストア"),
    (search_dshopping, "dショッピング"),
    (search_buyma, "BUYMA"),
    (search_abcmart, "ABC-MARTオンラインストア"),
    (search_gu, "GU オンラインストア"),
    (search_shopjapan, "ショップジャパン"),
    (search_iherb, "iHerb"),
    (search_cosme, "@cosme SHOPPING"),
]

# スクレイパー対応済みショップ名のセット（自動生成）
SCRAPER_SHOP_NAMES = {name for _, name in SCRAPERS}


def get_manual_search_shops() -> list[str]:
    """スクレイパー未対応のKNOWN_SHOPSを自動的に返す（常にKNOWN_SHOPSと同期）"""
    from .jal_shops import KNOWN_SHOPS
    return [name for name in KNOWN_SHOPS if name not in SCRAPER_SHOP_NAMES]


# ショップ別の追加CSSセレクタ（Phase 2で使用）
_SHOP_SPECIFIC_SELECTORS: dict[str, list[tuple[str, str, str]]] = {
    "Amazon.co.jp": [
        ('[data-component-type="s-search-result"]',
         ".a-price .a-price-whole",
         "h2 a span, h2 span, h2 a, .a-text-normal"),
        ('[data-component-type="s-search-result"]',
         ".a-price .a-offscreen",
         'span[class*="a-size-medium"], span[class*="a-size-base-plus"]'),
    ],
    "ビックカメラ.com": [
        (".bcs_listItem", ".bcs_price", ".bcs_title a"),
        (".prod_box", ".val", ".prod_name a"),
        (".bcs_item", ".bcs_price .val", ".bcs_title a"),
    ],
    "コジマネット": [
        (".product-list-item", ".price, .itemPrice", ".product-name a, .itemName a"),
        (".itemBox", ".itemPrice", ".itemName a"),
        (".goods_list li", ".price", ".goods_name a"),
    ],
    "ヤマダウェブコム": [
        (".searchResult__item", ".searchResult__price, .pPrice", ".searchResult__name a, .pName a"),
        (".product", ".price, .product-price", ".product-name a"),
        (".item", ".pPrice", ".pName a"),
    ],
    "エディオンネットショップ": [
        (".goods-list-item, .goodsListItem", ".goods-price, .goodsPrice, .price",
         ".goods-name a, .goodsName a"),
        ('[class*="goods"]', '[class*="price"]', '[class*="goods"] a, [class*="name"] a'),
    ],
    "ノジマオンライン": [
        (".catalogListItem", ".catalogPrice, .price", ".catalogName a, .product-name a"),
        (".catalog-item", ".price", ".product-name a"),
        ('[class*="catalog"]', '[class*="price"]', '[class*="name"] a'),
    ],
    "au PAY マーケット": [
        (".itemList__item", ".itemList__price, .price", ".itemList__name a"),
        ('[class*="ItemCard"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
    ],
    "dショッピング": [
        (".c-productListItem", ".c-productListItem__price", ".c-productListItem__name a"),
        ('[class*="ProductCard"]', '[class*="price"]', '[class*="name"] a'),
    ],
    "Joshin webショップ": [
        (".product-list__item", ".product-list__price, .price", ".product-list__name a"),
        (".productItem", ".productPrice, .price", ".productName a"),
        ('[class*="product"]', '[class*="price"]', '[class*="product"] a, [class*="name"] a'),
    ],
    "Qoo10": [
        # 2026年版: Qoo10はSPA化が進んでおり複数パターン
        (".sc-prd", ".prc .prc-dc, .prc", ".tit a, .sbj a"),
        (".item_g", ".price, .prc", ".sbj a"),
        ('[class*="goods"]', '[class*="price"], [class*="prc"]',
         '[class*="name"] a, [class*="sbj"] a, [class*="tit"] a'),
        (".gd_list li, .lst_cont li", ".prc, .price", ".tit a, .sbj a, .name a"),
        ('[class*="product"]', '[class*="price"]', '[class*="title"] a, [class*="name"] a'),
        (".goods_item", ".price", ".goods_name a, .title a"),
        # SPA描画後の追加パターン
        ('[data-gd-no]', '[class*="price"], [class*="prc"]', 'a[class*="name"], a[class*="tit"], a[href]'),
        (".search-item, .search-result-item", ".price, .sale-price", ".item-name a, .product-name a"),
        ('[class*="SearchResult"]', '[class*="price"], [class*="Price"]',
         '[class*="name"] a, [class*="Name"] a, [class*="title"] a'),
    ],
}

# Phase 2で待機するCSSセレクタ（SPA描画完了の判定）
_SHOP_WAIT_SELECTORS: dict[str, str] = {
    "Amazon.co.jp": '[data-component-type="s-search-result"], .s-result-item, .s-main-slot',
    "ビックカメラ.com": ".bcs_listItem, .prod_box, [class*='product']",
    "コジマネット": ".product-list-item, .itemBox, [class*='product']",
    "ヤマダウェブコム": ".searchResult__item, .product, [class*='product']",
    "エディオンネットショップ": ".goods-list-item, [class*='goods'], [class*='product']",
    "ノジマオンライン": ".catalogListItem, .catalog-item, [class*='catalog'], [class*='product']",
    "au PAY マーケット": ".itemList__item, [class*='ItemCard'], [class*='item'], [class*='product']",
    "dショッピング": ".c-productListItem, [class*='ProductCard'], [class*='product']",
    "Joshin webショップ": ".productList__item, .lineup_box, [class*='product']",
    "@cosme SHOPPING": ".product-list, [class*='ProductCard'], [class*='product-item'], [class*='product']",
    "Qoo10": ".sc-prd, .item_g, [class*='goods'], [data-gd-no], [class*='SearchResult'], [class*='product']",
}


def _get_selectors_for_shop(shop_name: str) -> list[tuple[str, str, str]]:
    """ショップ名から対応するCSSセレクタを取得"""
    shop_sels = _SHOP_SPECIFIC_SELECTORS.get(shop_name, [])
    return shop_sels + _GENERIC_SELECTORS + [
        ('[class*="product"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ('[class*="item"]', '[class*="price"]', '[class*="name"] a, [class*="title"] a'),
        ("li", '[class*="price"]', 'a'),
    ]


def _try_amazon_js(page, results: list, idx: int, r, query: str) -> bool:
    """Amazon Phase 2 JS抽出（個別商品の関連性チェック付き）"""
    try:
        js_products = page.evaluate("""
            () => {
                const items = document.querySelectorAll(
                    '[data-component-type="s-search-result"]');
                return Array.from(items).slice(0, 20).map(el => {
                    const sponsor = el.querySelector('.s-label-popover-default');
                    if (sponsor && sponsor.textContent.includes('スポンサー')) return null;
                    const priceEl = el.querySelector('.a-price .a-price-whole')
                        || el.querySelector('.a-price .a-offscreen');
                    const nameEl = el.querySelector('h2 a span')
                        || el.querySelector('h2 span') || el.querySelector('h2 a')
                        || el.querySelector('.a-text-normal');
                    const linkEl = el.querySelector('h2 a');
                    const imgEl = el.querySelector('img.s-image');
                    return {
                        text: (nameEl ? nameEl.textContent.trim() : '')
                            || (imgEl ? imgEl.alt || '' : ''),
                        href: linkEl ? linkEl.href : '',
                        priceText: priceEl ? priceEl.textContent.trim() : '',
                    };
                }).filter(x => x !== null);
            }
        """)
        if not js_products:
            return False
        # 簡略クエリで関連性チェック（元クエリは厳しすぎる）
        check_query = _simplify_query(query) or query
        best_price = None
        best_name = ""
        best_url = ""
        for prod in js_products:
            p = _parse_price(prod.get("priceText", ""))
            if not p or p > 99_999_999:
                continue
            pname = prod.get("text", "")[:200]
            if check_query and pname and not _is_relevant_product(check_query, pname):
                if best_price is None:
                    logger.info("Amazon JS rejected: '%s' (¥%s)", pname[:80], f"{p:,}")
                continue
            if best_price is None or p < best_price:
                best_price = p
                best_name = pname
                best_url = prod.get("href", "")
        if best_price:
            results[idx] = ShopPrice(r.shop_name, best_price, best_name, best_url, r.search_url)
            logger.info("Browser retry success (JS): %s = ¥%s (%s)",
                        r.shop_name, f"{best_price:,}", best_name[:50])
            return True
        logger.info("Browser retry: Amazon JS found %d items but no matching price",
                    len(js_products))
        return False
    except Exception as js_err:
        logger.debug("Amazon JS extraction failed: %s", js_err)
        return False


def _try_qoo10_js(page, results: list, idx: int, r, query: str) -> bool | str:
    """Qoo10 Phase 2 JS抽出（個別商品の関連性チェック付き）

    Returns:
        True: 価格抽出成功
        False: 商品が見つからなかった（汎用フォールバックへ）
        "items_no_match": 商品は見つかったが関連性チェックで全滅
                          （汎用フォールバックをスキップすべき）
    """
    try:
        js_products = page.evaluate("""
            () => {
                const selectors = [
                    '[data-gd-no]',
                    '.sc-prd', '.item_g', '.goods_item',
                    '[class*="SearchResult"] [class*="item"]',
                    '[class*="goods"]',
                    '[class*="product-card"]', '[class*="ProductCard"]',
                ];
                let items = [];
                for (const sel of selectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0 && found.length < 200) {
                        items = Array.from(found);
                        break;
                    }
                }
                if (items.length === 0) {
                    const priceEls = document.querySelectorAll(
                        '[class*="price"], [class*="prc"], [class*="Price"]');
                    for (const el of Array.from(priceEls).slice(0, 30)) {
                        let parent = el.parentElement;
                        for (let i = 0; i < 5 && parent; i++) {
                            const link = parent.querySelector('a[href]');
                            if (link && link.href && parent.textContent.length < 500) {
                                items.push(parent);
                                break;
                            }
                            parent = parent.parentElement;
                        }
                    }
                }
                return items.slice(0, 20).map(el => {
                    const link = el.querySelector('a[href]');
                    const priceEl = el.querySelector(
                        '[class*="price"], [class*="prc"], [class*="Price"]');
                    // Try to extract a clean product name from title/name elements
                    const nameEl = el.querySelector(
                        '[class*="tit"] a, [class*="sbj"] a, [class*="name"] a, '
                        + '[class*="Tit"] a, [class*="Sbj"] a, [class*="Name"] a, '
                        + 'a[class*="tit"], a[class*="sbj"], a[class*="name"], '
                        + 'h3 a, h4 a, .goods_name a, .title a, '
                        + '[class*="prd_name"] a, [class*="item-name"] a');
                    let nameText = nameEl ? nameEl.textContent.replace(/\\s+/g, ' ').trim() : '';
                    // Fallback: try img alt or a[title] for product name
                    if (!nameText) {
                        const imgEl = el.querySelector('img[alt]');
                        if (imgEl && imgEl.alt && imgEl.alt.length > 5) {
                            nameText = imgEl.alt.trim();
                        }
                    }
                    if (!nameText) {
                        const titleLink = el.querySelector('a[title]');
                        if (titleLink && titleLink.title && titleLink.title.length > 5) {
                            nameText = titleLink.title.trim();
                        }
                    }
                    return {
                        text: el.textContent.replace(/\\s+/g, ' ').trim().substring(0, 300),
                        nameText: nameText.substring(0, 200),
                        href: link ? link.href : '',
                        priceText: priceEl ? priceEl.textContent.trim() : '',
                    };
                });
            }
        """)
        if not js_products:
            return False
        # 簡略クエリで関連性チェック
        check_query = _simplify_query(query) or query
        best_price = None
        best_name = ""
        best_url = ""
        rejected_count = 0
        for prod in js_products:
            ptext = prod.get("priceText", "") or prod.get("text", "")
            p = _parse_price(ptext)
            if not p:
                p = _parse_price(prod.get("text", ""))
            if not p or p > 99_999_999:
                continue
            # Prefer clean nameText over messy full textContent
            pname = prod.get("nameText", "")
            full_text = prod.get("text", "")[:300]
            if not pname:
                # Strip prices, shipping info, etc. from full text to get cleaner name
                raw = full_text[:200]
                # Remove common noise: prices (¥1,234 / 1,234円), shipping, point info
                pname = re.sub(
                    r'[\d,]+\s*円|¥[\d,]+|送料[無料込別]*|ポイント.*?倍|'
                    r'\d+%\s*OFF|クーポン|カート|お気に入り|レビュー\s*\d+',
                    ' ', raw)
                pname = re.sub(r'\s+', ' ', pname).strip()[:100]
            # 関連性チェックはnameTextとfull_textの両方で試行
            # （Qoo10等でnameTextがブランド名のみに切り詰められている場合の対策）
            relevance_texts = [pname]
            if full_text and full_text != pname:
                relevance_texts.append(full_text)
            is_relevant = False
            if check_query:
                for rtxt in relevance_texts:
                    if _is_relevant_product(check_query, rtxt):
                        is_relevant = True
                        break
            else:
                is_relevant = True
            if not is_relevant:
                if rejected_count < 3:
                    logger.info("Qoo10 JS rejected: '%s' (¥%s)", pname[:80], f"{p:,}")
                rejected_count += 1
                continue
            if best_price is None or p < best_price:
                best_price = p
                best_name = pname
                best_url = prod.get("href", "")
        if best_price:
            results[idx] = ShopPrice(r.shop_name, best_price, best_name, best_url, r.search_url)
            logger.info("Browser retry success (JS): %s = ¥%s (%s)",
                        r.shop_name, f"{best_price:,}", best_name[:50])
            return True
        logger.info("Browser retry: Qoo10 JS found %d items, %d rejected by relevance, no match",
                    len(js_products), rejected_count)
        # Signal that items existed but none matched — caller should skip generic fallback.
        # Also skip if items were found but prices couldn't be parsed (the page has products,
        # so generic fallback would just pick up noise like mini/sample sizes).
        if len(js_products) >= 3:
            return "items_no_match"
        if len(js_products) > 0 and rejected_count > 0:
            return "items_no_match"
        return False
    except Exception as js_err:
        logger.debug("Qoo10 JS extraction failed: %s", js_err)
        return False


def _retry_with_browser(results: list[ShopPrice], query: str) -> None:
    """Phase 2: Playwright ブラウザレンダリングで失敗したショップを再試行

    Phase 1 (cloudscraper) で価格取得できなかったショップのみ対象。
    ブラウザでJavaScript描画を実行し、レンダリング後のHTMLから価格を抽出する。
    """
    if not _HAS_PLAYWRIGHT:
        return

    # 再試行対象の選定
    _SKIP_ERRORS = {"APIキー", "HTTP 404", "HTTP 410"}
    # 自社ブランド専門店：Phase 2でも他ブランド検索は無駄なのでスキップ
    _HOUSE_BRAND_SHOPS = {
        "DHCオンラインショップ": ["dhc"],
        "ファンケルオンライン": ["ファンケル", "fancl"],
    }
    query_lower = query.lower()
    retry_normal = []   # Phase1でHTMLは取れたがprice抽出失敗 → 成功見込み高
    retry_timeout = []  # Phase1でタイムアウト/アクセス制限 → ブラウザなら成功の可能性
    for i, r in enumerate(results):
        if r.price is not None:
            continue
        if r.error and any(skip in r.error for skip in _SKIP_ERRORS):
            continue
        if not r.search_url:
            continue
        # 自社ブランド専門店は、クエリにブランド名がない場合スキップ
        brand_keywords = _HOUSE_BRAND_SHOPS.get(r.shop_name)
        if brand_keywords and not any(bk in query_lower for bk in brand_keywords):
            logger.info("Phase 2: skipping %s (house brand only)", r.shop_name)
            continue
        if r.error and ("タイムアウト" in r.error or "アクセス制限" in r.error):
            retry_timeout.append(i)
        else:
            retry_normal.append(i)

    # ジャンル関連ショップを優先（動的判断）
    genres = _detect_product_genres(query)
    retry_priority = []
    retry_other = []
    for i in (retry_normal + retry_timeout):
        shop_name = results[i].shop_name
        if _is_shop_relevant(shop_name, genres):
            retry_priority.append(i)
        else:
            retry_other.append(i)
    retry_indices = retry_priority + retry_other

    if not retry_indices:
        return

    logger.info("Phase 2: Playwright browser retry for %d shops", len(retry_indices))

    # Phase 2 全体の時間制限（3分）
    phase2_start = time.time()
    PHASE2_BUDGET = 240  # 秒（家電量販店のタイムアウト対策で延長）

    def _launch_browser(pw):
        """ブラウザ起動: Chrome → Chromiumの順にフォールバック"""
        launch_args = [
            "--disable-http2",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--disable-web-security",
            "--window-size=1920,1080",
        ]
        # まず実際のChromeを試す（最もリアルなフィンガープリント）
        try:
            browser = pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=launch_args,
            )
            logger.info("Phase 2: Using installed Chrome browser")
            return browser
        except Exception:
            pass
        # フォールバック: Playwright同梱のChromium
        browser = pw.chromium.launch(
            headless=True,
            args=launch_args,
        )
        logger.info("Phase 2: Using Playwright Chromium")
        return browser

    try:
        with sync_playwright() as pw:
            browser = _launch_browser(pw)
            context = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="ja-JP",
                viewport={"width": 1920, "height": 1080},
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "DNT": "1",
                },
            )
            # ステルス: bot検出回避（包括的）
            context.add_init_script("""
                // webdriverフラグを隠す
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                // Chrome Runtime
                if (window.chrome === undefined) {
                    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
                }
                // pluginsを模倣
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
                        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                        {name: 'Native Client', filename: 'internal-nacl-plugin'},
                    ],
                });
                // languagesを模倣
                Object.defineProperty(navigator, 'languages', {get: () => ['ja', 'en-US', 'en']});
                // hardwareConcurrency
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                // deviceMemory
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
                // permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({state: Notification.permission})
                        : originalQuery(parameters);
                // WebGL vendor/renderer
                const getParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(param) {
                    if (param === 37445) return 'Google Inc. (NVIDIA)';
                    if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080, OpenGL 4.5)';
                    return getParam.call(this, param);
                };
            """)

            for idx in retry_indices:
                # 時間制限チェック
                elapsed = time.time() - phase2_start
                if elapsed > PHASE2_BUDGET:
                    remaining = len(retry_indices) - retry_indices.index(idx)
                    logger.info("Phase 2: time budget exceeded (%.0fs), skipping %d shops",
                                elapsed, remaining)
                    break

                r = results[idx]
                page = None
                try:
                    page = context.new_page()
                    base_url = f"{urlparse(r.search_url).scheme}://{urlparse(r.search_url).netloc}"

                    # ホームページ訪問が必要なショップのみ（bot検出が厳しいサイト）
                    # 家電量販店もcookieがないとbot扱いされるため追加
                    _NEEDS_HOME_VISIT = {
                        "ファンケルオンライン", "@cosme SHOPPING",
                        "au PAY マーケット", "dショッピング",
                        "ビックカメラ.com", "ヤマダウェブコム",
                        "Joshin webショップ", "ケーズデンキオンラインショップ",
                    }
                    if r.shop_name in _NEEDS_HOME_VISIT:
                        try:
                            page.goto(base_url + "/", timeout=10000,
                                      wait_until="domcontentloaded")
                            page.wait_for_timeout(800)
                            home_html = page.content()
                            if len(home_html) < 3000:
                                page.wait_for_timeout(3000)
                        except Exception:
                            pass

                    # 検索ページへ遷移（重いサイトはタイムアウト延長）
                    _page_timeout = 35000 if r.shop_name in _SLOW_SHOPS else 20000
                    page.goto(r.search_url, timeout=_page_timeout,
                              wait_until="domcontentloaded",
                              referer=base_url + "/")
                    # ネットワークアイドルを待つ（SPAのJS描画完了を待機）
                    try:
                        page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass  # タイムアウトしても続行

                    # ショップ固有のSPA描画完了待機
                    wait_sel = _SHOP_WAIT_SELECTORS.get(r.shop_name)
                    if wait_sel:
                        try:
                            page.wait_for_selector(wait_sel, timeout=5000)
                            logger.debug("Phase 2: found elements for %s", r.shop_name)
                        except Exception:
                            logger.debug("Phase 2: wait_for_selector timeout for %s", r.shop_name)

                    # 人間らしい操作を模倣（bot検出回避）
                    try:
                        page.mouse.move(random.randint(100, 800), random.randint(200, 600))
                        page.evaluate("window.scrollBy(0, 300)")
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

                    page.wait_for_timeout(800)
                    html = page.content()

                    # HTMLが極端に小さい場合はさらに待機（SPA遅延読み込み対策）
                    if len(html) < 5000:
                        page.wait_for_timeout(4000)
                        # さらにスクロールして遅延コンテンツをトリガー
                        try:
                            page.evaluate("window.scrollBy(0, 500)")
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        html = page.content()

                    # SPA検出: HTMLは大きいがテキストが少ない → 商品リストが未描画
                    # (Joshin等: 74KB HTML, 6KB text = ヘッダーのみ描画)
                    soup = BeautifulSoup(html, "lxml")
                    text_len = len(soup.get_text())
                    if len(html) > 20000 and text_len < 10000:
                        logger.debug("Phase 2: SPA detected for %s (HTML %dK, text %dK), extra wait+scroll",
                                     r.shop_name, len(html) // 1024, text_len // 1024)
                        try:
                            # 段階的にスクロールして遅延コンテンツをトリガー
                            for scroll_y in [300, 600, 900]:
                                page.evaluate(f"window.scrollTo(0, {scroll_y})")
                                page.wait_for_timeout(1000)
                            # ページトップに戻ってから再度下へ
                            page.evaluate("window.scrollTo(0, 0)")
                            page.wait_for_timeout(500)
                            page.evaluate("window.scrollBy(0, 500)")
                            page.wait_for_timeout(1500)
                        except Exception:
                            pass
                        html = page.content()
                        soup = BeautifulSoup(html, "lxml")

                    # bot検出ページチェック
                    if _is_bot_blocked_page(soup):
                        logger.info("Browser retry: bot blocked for %s", r.shop_name)
                        results[idx] = _make_error_result(r.shop_name, r.search_url,
                                                          "アクセス制限（手動で検索してください）")
                        continue

                    selectors = _get_selectors_for_shop(r.shop_name)
                    base = f"{urlparse(r.search_url).scheme}://{urlparse(r.search_url).netloc}"

                    # === SPA優先: Amazon/Qoo10はJS抽出を先に試行 ===
                    # 汎用抽出（_find_price_in_soup）は最安値を返すが、SPAサイトでは
                    # 無関係な安い商品を拾いやすい。JS抽出は個別商品の関連性チェック付き。
                    js_extracted = False

                    if r.shop_name == "Amazon.co.jp":
                        js_extracted = _try_amazon_js(page, results, idx, r, query)
                    elif r.shop_name == "Qoo10":
                        js_extracted = _try_qoo10_js(page, results, idx, r, query)

                    if js_extracted == "items_no_match":
                        # Qoo10 JS found products but none passed relevance check
                        # Skip generic fallback to avoid picking up wrong/noise prices
                        logger.info("Browser retry: skipping generic fallback for %s "
                                    "(JS found items but none matched)", r.shop_name)
                    elif not js_extracted:
                        # 汎用抽出（CSS + JSON-LD + fulltext）
                        price, name, url = _find_price_in_soup(soup, selectors, base, query=query)
                        if price:
                            results[idx] = ShopPrice(r.shop_name, price, name, url, r.search_url)
                            logger.info("Browser retry success: %s = ¥%s (%s)",
                                        r.shop_name, f"{price:,}", name[:50])
                        else:
                            logger.info("Browser retry: no price found for %s (HTML %d chars)",
                                        r.shop_name, len(html))

                except Exception as e:
                    import traceback
                    logger.warning("Browser retry error for %s: %s\n%s",
                                   r.shop_name, e, traceback.format_exc())
                finally:
                    if page:
                        try:
                            page.close()
                        except Exception:
                            pass

            context.close()
            browser.close()

    except Exception as e:
        err_msg = str(e)
        if "Executable doesn't exist" in err_msg or "browserType.launch" in err_msg:
            logger.warning(
                "Playwright browser not installed. Run: python -m playwright install chromium"
            )
        else:
            logger.error("Playwright browser failed: %s", e)


def _delayed_scrape(scraper_fn, query: str, config: Config, delay: float):
    """遅延付きスクレイパー実行（bot検出回避のためリクエストを分散）"""
    if delay > 0:
        time.sleep(delay)
    return scraper_fn(query, config)


def search_all_shops(query: str, config: Config) -> list[ShopPrice]:
    """全ショップを検索して結果を返す（2段階方式）

    Phase 1: cloudscraper + BeautifulSoup（並列、分散遅延付き）
    Phase 2: Playwright ブラウザ（Phase 1失敗分のみ、逐次）

    改善:
    - 同時接続数を5に制限（15→5、bot検出回避）
    - リクエスト間に分散遅延を追加（0〜3秒のランダム遅延）
    - bot検出ページの判定とリトライ
    """
    # クエリ正規化（全角→半角スペース・英数字）
    query = _normalize_query(query)

    results: list[ShopPrice] = []

    # === Phase 0: 商品ジャンル推定 → 不要ショップのスキップ ===
    genres = _detect_product_genres(query)
    skipped_shops = set()
    for _, name in SCRAPERS:
        if not _is_shop_relevant(name, genres):
            skipped_shops.add(name)
    if skipped_shops:
        logger.info("Genre filter: %s → skipping %d shops (%s)",
                     genres, len(skipped_shops),
                     ", ".join(sorted(skipped_shops)))

    # === Phase 1: cloudscraper（並列実行・分散遅延付き） ===
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        future_to_name: dict = {}
        for i, (scraper_fn, name) in enumerate(SCRAPERS):
            if name in skipped_shops:
                results.append(ShopPrice(name, None, "", "", "",
                                         error="取扱ジャンル外"))
                continue
            # 各ショップは別ドメインなので遅延は最小限
            delay = random.uniform(0, 0.5)
            future = executor.submit(_delayed_scrape, scraper_fn, query, config, delay)
            future_to_name[future] = name

        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result(timeout=_TIMEOUT + 10)
                results.append(result)
                if result.price:
                    logger.info("Phase1 OK: %s = ¥%s (%s)",
                                name, f"{result.price:,}", result.product_name[:50])
                elif result.error:
                    logger.info("Phase1 ERR: %s = %s", name, result.error)
                else:
                    logger.info("Phase1 EMPTY: %s (no price found)", name)
            except Exception as e:
                logger.warning("Phase1 EXCEPTION: %s = %s", name, e)
                results.append(ShopPrice(name, None, "", "", "", error=str(e)))

    phase1_found = sum(1 for r in results if r.price is not None)
    logger.info("Phase 1 complete: %d/%d shops found prices", phase1_found, len(results))

    # === Phase 1.5: 簡略クエリでリトライ ===
    # 長いクエリで結果が少ない場合、サイズ/一般語を除いた短いクエリで再検索
    simplified_query = _simplify_query(query)
    if simplified_query and phase1_found < 4:
        _SKIP_ERRORS_RETRY = {"APIキー", "HTTP 404", "HTTP 410", "取扱ジャンル外"}
        scraper_map = {name: fn for fn, name in SCRAPERS}
        retry_targets = []
        for i, r in enumerate(results):
            if r.price is not None:
                continue
            if r.error and any(s in r.error for s in _SKIP_ERRORS_RETRY):
                continue
            if r.shop_name in skipped_shops:
                continue
            # Amazon は search_amazon 内で独自にリトライ済み
            if r.shop_name == "Amazon.co.jp":
                continue
            if r.shop_name in scraper_map:
                retry_targets.append((i, r.shop_name, scraper_map[r.shop_name]))

        if retry_targets:
            logger.info("Phase 1.5: retrying %d shops with simplified query '%s'",
                        len(retry_targets), simplified_query)
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
                future_to_info: dict = {}
                for idx, name, fn in retry_targets:
                    delay = random.uniform(0, 0.5)
                    future = executor.submit(_delayed_scrape, fn, simplified_query, config, delay)
                    future_to_info[future] = (idx, name)

                for future in as_completed(future_to_info):
                    idx, name = future_to_info[future]
                    try:
                        result = future.result(timeout=_TIMEOUT + 10)
                        if result.price is not None:
                            # 元クエリに対する関連性チェック
                            # 簡略クエリで関連性チェック（元クエリの全キーワードは不要）
                            if (result.product_name
                                    and _is_relevant_product(simplified_query, result.product_name)):
                                results[idx] = result
                                logger.info("Phase1.5 OK: %s = ¥%s (%s)",
                                            name, f"{result.price:,}",
                                            result.product_name[:50])
                            else:
                                logger.info("Phase1.5 rejected: %s = ¥%s (%s) - not relevant",
                                            name, f"{result.price:,}",
                                            result.product_name[:50])
                    except Exception:
                        pass

            phase15_found = sum(1 for r in results if r.price is not None)
            if phase15_found > phase1_found:
                logger.info("Phase 1.5 recovered %d additional shops",
                            phase15_found - phase1_found)

    # === Phase 2: Playwright ブラウザレンダリング（失敗分のみ） ===
    _retry_with_browser(results, query)

    phase2_found = sum(1 for r in results if r.price is not None)
    if phase2_found > phase1_found:
        logger.info("Phase 2 recovered %d additional shops", phase2_found - phase1_found)

    # === Phase 3: クロスショップ価格バリデーション ===
    # 複数ショップの価格を比較し、明らかな外れ値（アクセサリ/無関係商品）を除外
    _validate_prices_cross_shop(results)

    return results


def _validate_prices_cross_shop(results: list[ShopPrice]) -> None:
    """クロスショップ価格バリデーション（2パス方式）

    複数ショップの結果を統計的に比較し、中央値から大きく外れた結果を
    エラーに変換する（アクセサリ・無関係商品の誤検出対策）。

    2パス方式:
    1. 明らかな下限外れ値（非商品ページの誤検出）を先に除去
    2. 残りで中央値を再計算し、上限チェック

    高級品のサイズ違い・並行輸入等で正当な3-4倍の価格差がありうるため、
    上限閾値は控えめに設定。
    """
    prices_with_idx = [(r.price, i) for i, r in enumerate(results) if r.price is not None]
    if len(prices_with_idx) < 3:
        return  # 3件未満では統計的判断不可

    prices_only = sorted(p for p, _ in prices_with_idx)
    median = prices_only[len(prices_only) // 2]

    # === Pass 1: 下限外れ値を除去 ===
    # 中央値の20%未満は明らかな外れ値（非商品ページの誤検出等）
    lower_threshold = median * 0.20
    removed_indices = set()
    for price, idx in prices_with_idx:
        if price < lower_threshold:
            r = results[idx]
            reason = f"価格が低すぎ（中央値¥{median:,}の20%未満）"
            logger.info("Cross-shop validation: %s ¥%s removed (%s)",
                        r.shop_name, f"{price:,}", reason)
            results[idx] = ShopPrice(
                r.shop_name, None, "", "", r.search_url,
                error="価格異常（他店と大きく乖離）"
            )
            removed_indices.add(idx)

    # === Pass 2: 中央値を再計算して上限チェック ===
    remaining = [(p, i) for p, i in prices_with_idx if i not in removed_indices]
    if len(remaining) < 5:
        return  # 5件未満では上限チェックしない（少数だと誤判定リスク大）

    remaining_prices = sorted(p for p, _ in remaining)
    median2 = remaining_prices[len(remaining_prices) // 2]
    # 中央値の3.5倍超は外れ値（サイズ違い・セット品等の正当な差を考慮）
    upper_threshold = median2 * 3.5
    for price, idx in remaining:
        if price > upper_threshold:
            r = results[idx]
            reason = f"価格が高すぎ（中央値¥{median2:,}の3.5倍超）"
            logger.info("Cross-shop validation: %s ¥%s removed (%s)",
                        r.shop_name, f"{price:,}", reason)
            results[idx] = ShopPrice(
                r.shop_name, None, "", "", r.search_url,
                error="価格異常（他店と大きく乖離）"
            )
