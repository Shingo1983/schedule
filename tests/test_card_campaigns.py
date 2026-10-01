"""card_campaigns の解析・画面のテスト（ネットワーク不要）

実行: python -m unittest tests.test_card_campaigns
"""

import os
import tempfile
import time
import unittest
from datetime import date
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["CARDS_DB_PATH"] = os.path.join(_TMP, "cards.db")

from card_campaigns import fetcher, store  # noqa: E402
from card_campaigns.sources import SOURCE_MAP  # noqa: E402

_LIST_HTML = """
<html><body>
<ul>
  <li class="card"><a href="/card/campaign/autocharge2610/">
    <h3>モバイルSuicaオートチャージキャンペーン</h3></a>
    <p>要エントリー 2026年10月1日(水)～2026年11月30日(月)</p></li>
  <li class="card"><a href="/card/campaign/newmember2610/">
    <h3>ビューカード新規入会キャンペーン</h3></a>
    <p>2026年10月1日～12月31日</p></li>
  <li class="card"><a href="/card/campaign/old2608/"><h3>夏のキャンペーン</h3></a>
    <p>2026年7月1日～8月31日</p></li>
  <li><a href="/en/card/campaign/foo/">English page</a></li>
  <li><a href="https://example.com/card/campaign/x/">外部サイトのキャンペーン</a></li>
</ul>
</body></html>
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.src = SOURCE_MAP["view"]
        self.items = fetcher.parse_campaigns(
            _LIST_HTML, "https://www.jreast.co.jp/card/campaign/", self.src, date(2026, 10, 1))
        self.by_url = {i["url"]: i for i in self.items}

    def test_extracts_campaign_links_only(self):
        self.assertEqual(set(self.by_url), {
            "https://www.jreast.co.jp/card/campaign/autocharge2610/",
            "https://www.jreast.co.jp/card/campaign/newmember2610/",
            "https://www.jreast.co.jp/card/campaign/old2608/",
        })

    def test_deadline_and_flags(self):
        it = self.by_url["https://www.jreast.co.jp/card/campaign/autocharge2610/"]
        self.assertEqual(it["title"], "モバイルSuicaオートチャージキャンペーン")
        self.assertEqual(it["deadline"], "2026-11-30")
        self.assertTrue(it["entry_required"])
        self.assertFalse(it["new_member"])
        self.assertTrue(self.by_url["https://www.jreast.co.jp/card/campaign/newmember2610/"]["new_member"])

    def test_year_carried_over_in_range(self):
        it = self.by_url["https://www.jreast.co.jp/card/campaign/newmember2610/"]
        self.assertEqual(it["deadline"], "2026-12-31")

    def test_year_omitted_rolls_forward(self):
        self.assertEqual(fetcher.guess_deadline("1月31日まで", date(2026, 10, 1)), date(2027, 1, 31))

    def test_numbers_are_not_dates(self):
        self.assertIsNone(fetcher.guess_deadline("最大10.5%還元 / 3.5倍 / 100/200円", date(2026, 10, 1)))
        self.assertEqual(fetcher.guess_deadline("2026.10.31まで", date(2026, 10, 1)), date(2026, 10, 31))


class ViewTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("CARDS_ACCESS_KEY", None)
        items = fetcher.parse_campaigns(
            _LIST_HTML, "https://www.jreast.co.jp/card/campaign/", SOURCE_MAP["view"], date(2026, 10, 1))
        store.save_cache("view", {"ok": True, "items": items, "error": None,
                                  "used_url": "https://www.jreast.co.jp/card/campaign/"}, time.time())
        for sid in ("jalcard", "smbc", "jcb", "ana", "dcard"):
            store.save_cache(sid, {"ok": False, "items": [], "error": "テスト用の取得失敗"}, time.time())
        from flask import Flask
        from card_campaigns import bp
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.client = app.test_client()
        self.today = mock.patch.object(fetcher, "today_jst", return_value=date(2026, 10, 1))
        self.today.start()

    def tearDown(self):
        self.today.stop()

    def test_index_hides_new_member_and_expired(self):
        html = self.client.get("/cards/").get_data(as_text=True)
        self.assertIn("モバイルSuicaオートチャージキャンペーン", html)
        self.assertNotIn("ビューカード新規入会キャンペーン", html)
        self.assertNotIn("夏のキャンペーン", html)
        self.assertIn("2026/11/30(月)", html)
        self.assertIn("https://viewsnet.jp/", html)  # 応募ボタンはログインページへ
        self.assertIn("アメリカン・エキスプレス", html)
        html_all = self.client.get("/cards/?all=1").get_data(as_text=True)
        self.assertIn("ビューカード新規入会キャンペーン", html_all)

    def test_entry_is_recorded(self):
        key = "view:https://www.jreast.co.jp/card/campaign/autocharge2610/"
        r = self.client.post("/cards/status", data={"key": key, "state": "entered", "beacon": "1"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(store.get_statuses()[key]["state"], "entered")
        html = self.client.get("/cards/").get_data(as_text=True)
        self.assertIn("登録を取消", html)
        self.client.post("/cards/status", data={"key": key, "state": "clear"})
        self.assertNotIn(key, store.get_statuses())

    def test_manual_campaign(self):
        self.client.post("/cards/manual", data={"source": "amex", "title": "テストオファー",
                                                "deadline": "2026-10-20"})
        html = self.client.get("/cards/").get_data(as_text=True)
        self.assertIn("テストオファー", html)
        self.assertIn("2026/10/20(火)", html)

    def test_access_key(self):
        os.environ["CARDS_ACCESS_KEY"] = "secret"
        try:
            self.assertEqual(self.client.get("/cards/").status_code, 403)
            r = self.client.get("/cards/?key=secret")
            self.assertEqual(r.status_code, 200)
            self.assertIn("cards_key=secret", r.headers.get("Set-Cookie", ""))
            self.assertEqual(self.client.get("/cards/").status_code, 200)  # Cookie で通過
        finally:
            os.environ.pop("CARDS_ACCESS_KEY", None)


if __name__ == "__main__":
    unittest.main()
