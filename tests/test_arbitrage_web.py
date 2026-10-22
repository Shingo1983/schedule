"""転売アービトラージ Web ルート・ジョブ管理のテスト（ネットワーク不要）"""

import threading
import time

import pytest

from jal_shopping_tool import app as app_module
from jal_shopping_tool import arbitrage_jobs as jobs
from jal_shopping_tool import resale_arbitrage as ra
from jal_shopping_tool.site_scrapers import ShopPrice


def _make_result(query="X", buy=40000, amazon=46000, mercari=44000, category="electronics",
                 size="60", settings=None):
    r = ra.ArbitrageResult(query=query, category=category, size=size)
    r.buy_quotes = {
        "楽天市場": ShopPrice("楽天市場", buy, "品", "https://x/1", "https://x/s1"),
        "Yahoo!ショッピング": ShopPrice("Yahoo!ショッピング", None, "", "", "https://x/s2",
                                      error="接続エラー"),
        "コジマネット": ShopPrice("コジマネット", buy + 500, "品", "", "https://x/s3"),
    }
    r.platforms = {
        "Amazon": ra.PlatformResult("Amazon", ra.SellQuote("Amazon", amazon, basis="新品最安値",
                                                          search_url="https://a/s")),
        "メルカリ": ra.PlatformResult("メルカリ", ra.SellQuote("メルカリ", mercari,
                                                            basis="売却済み(新品)中央値",
                                                            search_url="https://m/s", sample_count=5)),
    }
    return ra.evaluate(r, settings or ra.FeeSettings(point_rates={"楽天市場": 0.1}))


@pytest.fixture
def client(monkeypatch):
    # 実スクレイピングを封じる
    monkeypatch.setattr(app_module, "run_single_arbitrage",
                        lambda q, cfg, category="general", size="60", settings=None:
                        _make_result(q, category=category, size=size, settings=settings))
    monkeypatch.setattr(app_module, "JOBS", jobs.JobManager())
    monkeypatch.setattr(app_module, "CACHE", jobs.ResultCache())
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_index_lists_candidates(client):
    res = client.get("/arbitrage")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "候補商品リスト" in body
    assert ra.ARBITRAGE_CANDIDATES[0].query in body
    assert "スキャン開始" in body


def test_single_result_renders_and_caches(client):
    res = client.get("/arbitrage?q=SONY+WF-1000XM5&category=electronics&size=コンパクト")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "SONY WF-1000XM5" in body
    assert "最安仕入れ" in body
    assert "接続エラー" in body          # 取得失敗ショップも表示
    assert "キャッシュ結果" not in body
    res2 = client.get("/arbitrage?q=SONY+WF-1000XM5&category=electronics&size=コンパクト")
    assert "キャッシュ結果" in res2.get_data(as_text=True)


def test_single_result_json(client):
    res = client.get("/arbitrage?q=ABC&format=json")
    assert res.status_code == 200
    data = res.get_json()
    assert data["query"] == "ABC"
    assert data["verdict"] in ra.VERDICT_LABELS
    assert "platforms" in data and "Amazon" in data["platforms"]


def test_settings_from_query_affect_judgement(client):
    # ポイント除外 + 許容損失0% → 名目損益で判定
    res = client.get("/arbitrage?q=ABC&submitted=1&tolerance=0&min_price=30000&format=json")
    data = res.get_json()
    assert data["judged_on_effective"] is False
    res2 = client.get("/arbitrage?q=ABC&submitted=1&use_points=1&format=json")
    assert res2.get_json()["judged_on_effective"] is True


def test_scan_job_flow(client, monkeypatch):
    done = threading.Event()

    def fake_scan(config, candidates=None, max_items=0, settings=None, progress_cb=None):
        cands = ra.ARBITRAGE_CANDIDATES[:max_items or 2]
        rep = ra.ArbitrageReport(total_searched=len(cands), settings=settings,
                                 started_at=time.time())
        for i, c in enumerate(cands, 1):
            progress_cb(i, len(cands), c.query, None)
            r = _make_result(c.query, category=c.category, size=c.size, settings=settings)
            rep.results.append(r)
            progress_cb(i, len(cands), c.query, r)
        rep.finished_at = time.time()
        done.wait(timeout=2)      # 進捗ページを確認できるよう少し待つ
        return rep

    monkeypatch.setattr(app_module, "run_arbitrage_scan", fake_scan)

    res = client.post("/arbitrage/scan", data={"limit": "2", "submitted": "1", "use_points": "1"})
    assert res.status_code == 302
    url = res.headers["Location"]
    assert "/arbitrage/scan/" in url

    # 実行中: 進捗 + 途中結果
    time.sleep(0.2)
    prog = client.get(url)
    assert prog.status_code == 200
    body = prog.get_data(as_text=True)
    assert "スキャン実行中" in body
    status = client.get(url + "?format=json").get_json()
    assert status["status"] == "running"
    assert status["total"] == 2 and len(status["partial"]) == 2

    # 2本目は同時実行制限で拒否される
    res_dup = client.post("/arbitrage/scan", data={"limit": "2"})
    assert res_dup.status_code == 302 and res_dup.headers["Location"].endswith("/arbitrage")

    done.set()
    for _ in range(50):
        status = client.get(url + "?format=json").get_json()
        if status["status"] == "done":
            break
        time.sleep(0.05)
    assert status["status"] == "done"
    assert status["report"]["total_searched"] == 2

    final = client.get(url)
    body = final.get_data(as_text=True)
    assert "スキャン結果" in body
    assert "ほぼ同額以上で売れる商品" in body
    assert "http-equiv" not in body        # 完了後は自動更新しない


def test_scan_unknown_job_404(client):
    assert client.get("/arbitrage/scan/nope").status_code == 404


def test_job_manager_error_and_cleanup():
    mgr = jobs.JobManager(ttl_seconds=0)

    def runner(cb):
        raise RuntimeError("bad")
    job = mgr.start(runner)
    for _ in range(50):
        if not job.is_running:
            break
        time.sleep(0.02)
    assert job.status == "error" and "bad" in job.error
    assert job.to_dict()["status"] == "error"
    mgr._cleanup()
    assert mgr.get(job.id) is None


def test_result_cache_ttl_and_eviction():
    cache = jobs.ResultCache(ttl_seconds=0, max_entries=1)
    s = ra.FeeSettings()
    k1 = jobs.ResultCache.make_key("A", "general", "60", s)
    k2 = jobs.ResultCache.make_key("a ", "general", "60", s)
    assert k1 == k2                                    # 正規化
    assert k1 != jobs.ResultCache.make_key("A", "general", "80", s)
    r = ra.ArbitrageResult(query="A")
    cache.put(k1, r)
    assert cache.get(k1) is None                       # TTL 0 → 即失効
    cache2 = jobs.ResultCache(ttl_seconds=100, max_entries=1)
    cache2.put("x", r)
    cache2.put("y", r)
    assert cache2.get("x") is None and cache2.get("y") is r
