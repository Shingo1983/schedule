"""転売アービトラージ: Web 用のバックグラウンドジョブ管理と結果キャッシュ

一括スキャンは数分かかるため、HTTP リクエスト内で同期実行すると
gunicorn のタイムアウトやブラウザの待ち時間切れを招く。
ここではスレッドでスキャンを走らせ、進捗をポーリングで返す。

注意: プロセス内メモリに保持するため、gunicorn の複数ワーカー構成では
ジョブを開始したワーカー以外からは見えない。単一ワーカー運用、または
sticky なルーティングを前提とする（render.yaml 参照）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .resale_arbitrage import ArbitrageReport, ArbitrageResult, FeeSettings, ProgressCallback


@dataclass
class ScanJob:
    id: str
    status: str = "running"            # running / done / error
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    index: int = 0
    total: int = 0
    current_query: str = ""
    partial: list[ArbitrageResult] = field(default_factory=list)
    report: ArbitrageReport | None = None
    error: str = ""

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        done = len(self.partial)
        return int(min(100, done * 100 / self.total))

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return end - self.created_at

    def to_dict(self, include_results: bool = True) -> dict:
        d = {
            "id": self.id,
            "status": self.status,
            "index": self.index,
            "total": self.total,
            "current_query": self.current_query,
            "percent": self.percent,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
        }
        if include_results:
            if self.report:
                d["report"] = self.report.to_dict()
            else:
                d["partial"] = [r.to_dict() for r in self.partial]
        return d


class JobManager:
    """スキャンジョブをスレッドで実行し、一定時間保持する"""

    def __init__(self, ttl_seconds: int = 3600, max_concurrent: int = 1):
        self._jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_concurrent = max_concurrent

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.is_running)

    def start(self, runner: Callable[[ProgressCallback], ArbitrageReport]) -> ScanJob:
        """runner(progress_cb) を別スレッドで実行する。同時実行数を超えると ValueError"""
        self._cleanup()
        if self.running_count() >= self._max_concurrent:
            raise ValueError("別のスキャンが実行中です。完了後に再度お試しください。")

        job = ScanJob(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job

        def _progress(index: int, total: int, query: str, result: ArbitrageResult | None):
            job.index, job.total, job.current_query = index, total, query
            if result is not None:
                job.partial.append(result)

        def _run():
            try:
                job.report = runner(_progress)
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.error = str(e)
                job.status = "error"
            finally:
                job.finished_at = time.time()

        threading.Thread(target=_run, name=f"arbitrage-scan-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> ScanJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _cleanup(self) -> None:
        now = time.time()
        with self._lock:
            stale = [jid for jid, j in self._jobs.items()
                     if not j.is_running and now - j.created_at > self._ttl]
            for jid in stale:
                del self._jobs[jid]


class ResultCache:
    """単一商品分析のメモリキャッシュ（同じ条件での連続アクセスでスクレイピングを繰り返さない）"""

    def __init__(self, ttl_seconds: int = 600, max_entries: int = 200):
        self._data: dict[str, tuple[float, ArbitrageResult]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max = max_entries

    @staticmethod
    def make_key(query: str, category: str, size: str, settings: FeeSettings) -> str:
        raw = json.dumps([query.strip().lower(), category, size, settings.to_dict()],
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> ArbitrageResult | None:
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            ts, value = hit
            if time.time() - ts > self._ttl:
                del self._data[key]
                return None
            return value

    def put(self, key: str, value: ArbitrageResult) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                del self._data[oldest]
            self._data[key] = (time.time(), value)


JOBS = JobManager()
CACHE = ResultCache()
