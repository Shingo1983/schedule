"""保有カードの発行会社ごとのキャンペーン取得元定義

対象は Evernote で確認した保有カードの発行会社のみ（家族カード・法人カード・
デビット/プリペイドは対象外）。

kind:
  "scrape"     公開のキャンペーン一覧ページを読み取って一覧化する
  "login_only" 既存会員向けキャンペーンがログイン後にしか表示されない。
               会員サイトへのボタンと、手動登録フォームのみ提供する。

list_urls は上から順に試し、キャンペーンが1件以上取れた最初のURLを採用する。
link_pattern は一覧ページ内のリンクのうち「キャンペーン詳細ページ」とみなす href の正規表現。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    id: str
    issuer: str                 # 発行会社・提携元の表示名
    cards: tuple[str, ...]      # この取得元が対象とする保有カード
    kind: str                   # "scrape" | "login_only"
    login_url: str              # 応募ボタンの遷移先（会員ログインページ）
    official_url: str           # 公式のキャンペーン一覧（人が見る用）
    list_urls: tuple[str, ...] = ()
    link_pattern: str = ""
    exclude_href_pattern: str = ""
    note: str = ""
    needs_browser: bool = False  # 一覧が JavaScript 描画の場合 True
    extra: dict = field(default_factory=dict)


SOURCES: tuple[Source, ...] = (
    Source(
        id="amex",
        issuer="アメリカン・エキスプレス",
        cards=("センチュリオン", "Marriott Bonvoy アメックス・プレミアム"),
        kind="login_only",
        login_url="https://global.americanexpress.com/offers/eligible",
        official_url="https://global.americanexpress.com/offers/eligible",
        note="既存会員向けは「アメックス・オファー」としてログイン後のみ表示されます。",
    ),
    Source(
        id="trustclub",
        issuer="三井住友トラストクラブ",
        cards=("ダイナースクラブ プレミアム", "TRUST CLUB プラチナマスターカード",
               "TRUST CLUB カード (VISA)"),
        kind="login_only",
        login_url="https://www.sumitclub.jp/JPCRD/col/action/WA2010101Action/RWA2010101",
        official_url="https://www.sumitclub.jp/JPCRD/col/action/WA2010101Action/RWA2010101",
        note="クラブ・オンラインにログイン後、右上「キャンペーン登録」に一覧があります。",
    ),
    Source(
        id="jalcard",
        issuer="JALカード",
        cards=("JALカード VISA", "JALカードSuica (JCB)"),
        kind="scrape",
        login_url="https://www.jal.co.jp/jp/ja/jalcard/",
        official_url="https://www.jal.co.jp/jp/ja/jalcard/information/",
        list_urls=("https://www.jal.co.jp/jp/ja/jalcard/information/",
                   "https://www.jal.co.jp/jp/ja/jalcard/information/index.html"),
        link_pattern=r"/jalcard/(?:information|campaign|cp)/[^?#]+",
        exclude_href_pattern=r"/jalcard/information/(?:index\.html)?$",
    ),
    Source(
        id="view",
        issuer="ビューカード (JR東日本)",
        cards=("JALカードSuica (JCB)",),
        kind="scrape",
        login_url="https://viewsnet.jp/",
        official_url="https://www.jreast.co.jp/card/campaign/",
        list_urls=("https://www.jreast.co.jp/card/campaign/",),
        link_pattern=r"/card/campaign/[^/?#]+",
        exclude_href_pattern=r"^https?://www\.jreast\.co\.jp/(?:en|de|fr|ko|zh-?\w*)/",
    ),
    Source(
        id="smbc",
        issuer="三井住友カード",
        cards=("ANAカード VISA Suica",),
        kind="scrape",
        login_url="https://www.smbc-card.com/mem/index.jsp",
        official_url="https://www.smbc-card.com/mem/campaign/index.jsp",
        list_urls=("https://www.smbc-card.com/mem/campaign/index.jsp",
                   "https://www.smbc-card.com/camp/index.jsp"),
        link_pattern=r"/mem/cardinfo/(?:\d+/)?cardinfo\d+\.jsp",
        needs_browser=True,
    ),
    Source(
        id="jcb",
        issuer="JCB",
        cards=("ANA JCB (SFC)",),
        kind="scrape",
        login_url="https://my.jcb.co.jp/Login",
        official_url="https://www.jcb.co.jp/campaign/",
        list_urls=("https://www.jcb.co.jp/campaign/",),
        link_pattern=r"/campaign/[a-z0-9_\-]+/",
        exclude_href_pattern=r"/(?:ordercard|corporate)/",
        needs_browser=True,
    ),
    Source(
        id="ana",
        issuer="ANAカード (ANAマイレージクラブ)",
        cards=("ANAカード VISA Suica", "ANA JCB (SFC)"),
        kind="scrape",
        login_url="https://www.ana.co.jp/ja/jp/amc/",
        official_url="https://www.ana.co.jp/ja/jp/mycampaign/",
        list_urls=("https://www.ana.co.jp/ja/jp/mycampaign/",),
        link_pattern=r"/(?:mycampaign|amc/cpn)/[a-z0-9_\-]+/",
        exclude_href_pattern=r"/mycampaign/archive/",
        note="ANAのMYキャンペーンは会員ごとに表示が変わるため、公開ページに出ない対象キャンペーンがあります。",
        needs_browser=True,
    ),
    Source(
        id="dcard",
        issuer="dカード (NTTドコモ)",
        cards=("dカード",),
        kind="scrape",
        login_url="https://dcard.docomo.ne.jp/",
        official_url="https://dcard.docomo.ne.jp/st/campaign/index.html",
        list_urls=("https://dcard.docomo.ne.jp/st/campaign/index.html",),
        link_pattern=r"/(?:st/campaign|std/campaigns?|st/cp)/[^?#]+",
        exclude_href_pattern=r"/st/campaign/(?:index\.html)?$",
        needs_browser=True,
    ),
)

SOURCE_MAP = {s.id: s for s in SOURCES}

# 新規入会・紹介など「既存会員がエントリーするキャンペーン」ではないものの判定語
NEW_MEMBER_KEYWORDS = (
    "入会", "新規", "お申し込みで", "お申込みで", "ご紹介", "紹介キャンペーン",
    "切り替え", "切替", "発行で", "申込特典",
)
