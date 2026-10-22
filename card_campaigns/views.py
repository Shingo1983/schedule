"""保有カードのキャンペーン一覧（/cards）"""

import hmac
import os
import time
from datetime import date, datetime

from flask import (Blueprint, abort, jsonify, redirect, render_template, request,
                   url_for)

from . import fetcher, store
from .sources import SOURCE_MAP, SOURCES

bp = Blueprint("cards", __name__, url_prefix="/cards", template_folder="templates")

_COOKIE = "cards_key"
_WEEKDAYS = "月火水木金土日"


def _access_key() -> str:
    return os.environ.get("CARDS_ACCESS_KEY", "")


@bp.before_request
def _require_key():
    """CARDS_ACCESS_KEY が設定されていれば、?key= か Cookie で一致した場合のみ通す。"""
    key = _access_key()
    if not key:
        return None
    given = request.args.get("key") or request.cookies.get(_COOKIE) or ""
    if not hmac.compare_digest(given, key):
        abort(403)
    return None


@bp.after_app_request
def _remember_key(resp):
    key = _access_key()
    if key and request.blueprint == "cards" and request.args.get("key") == key:
        resp.set_cookie(_COOKIE, key, max_age=400 * 24 * 3600, httponly=True,
                        secure=request.is_secure, samesite="Lax")
    return resp


def fmt_date(value) -> str:
    """YYYY/M/D(曜)"""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        d = datetime.fromtimestamp(value, fetcher.JST)
        return f"{d.year}/{d.month}/{d.day}({_WEEKDAYS[d.weekday()]}) {d.hour:02d}:{d.minute:02d}"
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.year}/{value.month}/{value.day}({_WEEKDAYS[value.weekday()]})"


def _build_view(show_all: bool) -> dict:
    today = fetcher.today_jst()
    cache = store.load_cache()
    statuses = store.get_statuses()
    checks = store.get_source_checks()
    manual = store.list_manual()

    sections = []
    totals = {"open": 0, "entered": 0, "soon": 0}
    for src in SOURCES:
        res = cache.get(src.id, {})
        raw = [dict(it, manual_id=None) for it in res.get("items", [])]
        for m in manual:
            if m["source_id"] == src.id:
                raw.append({"title": m["title"], "url": m["url"], "summary": "",
                            "deadline": m["deadline"], "new_member": False,
                            "entry_required": True, "manual_id": m["id"]})

        items, hidden = [], {"new_member": 0, "expired": 0, "dismissed": 0}
        for it in raw:
            key = f"manual:{it['manual_id']}" if it["manual_id"] else f"{src.id}:{it['url']}"
            st = statuses.get(key, {})
            it["key"] = key
            it["state"] = st.get("state")
            it["state_at"] = st.get("updated_at")
            dl = date.fromisoformat(it["deadline"]) if it.get("deadline") else None
            it["days_left"] = (dl - today).days if dl else None
            reason = None
            if it["new_member"]:
                reason = "new_member"
            elif dl and dl < today:
                reason = "expired"
            elif it["state"] == "dismissed":
                reason = "dismissed"
            if reason:
                hidden[reason] += 1
                if not show_all:
                    continue
            it["hidden_reason"] = reason
            items.append(it)
            if not reason:
                if it["state"] == "entered":
                    totals["entered"] += 1
                else:
                    totals["open"] += 1
                    if it["days_left"] is not None and it["days_left"] <= 7:
                        totals["soon"] += 1

        items.sort(key=lambda x: (x["state"] == "entered", x["hidden_reason"] is not None,
                                  x["days_left"] if x["days_left"] is not None else 10**6,
                                  x["title"]))
        sections.append({
            "src": src,
            "items": items,
            "hidden": hidden,
            "error": res.get("error"),
            "stale": res.get("stale", False),
            "fetched_at": res.get("fetched_at"),
            "used_url": res.get("used_url"),
            "checked_at": checks.get(src.id),
        })
    return {"sections": sections, "totals": totals}


@bp.route("/")
def index():
    show_all = request.args.get("all") == "1"
    refreshing = fetcher.ensure_fresh()
    view = _build_view(show_all)
    return render_template(
        "cards/index.html",
        **view,
        show_all=show_all,
        refreshing=refreshing,
        persistent=store.is_persistent(),
        protected=bool(_access_key()),
        fmt_date=fmt_date,
        now=time.time(),
    )


@bp.route("/api/state")
def api_state():
    return jsonify({"refreshing": fetcher.is_refreshing()})


@bp.route("/status", methods=["POST"])
def set_status():
    key = request.form.get("key", "")
    state = request.form.get("state", "")
    if not key or state not in ("entered", "dismissed", "clear"):
        abort(400)
    store.set_status(key, None if state == "clear" else state,
                     request.form.get("title", "")[:200], request.form.get("source", ""))
    if request.headers.get("X-Requested-With") == "fetch" or request.form.get("beacon"):
        return ("", 204)
    return redirect(url_for("cards.index", all=request.args.get("all")))


@bp.route("/checked", methods=["POST"])
def checked():
    sid = request.form.get("source", "")
    if sid not in SOURCE_MAP:
        abort(400)
    store.mark_source_checked(sid)
    return ("", 204)


@bp.route("/manual", methods=["POST"])
def manual():
    if request.form.get("delete"):
        store.delete_manual(int(request.form["delete"]))
    else:
        sid = request.form.get("source", "")
        title = request.form.get("title", "").strip()
        if sid not in SOURCE_MAP or not title:
            abort(400)
        deadline = request.form.get("deadline", "").strip()
        if deadline:
            date.fromisoformat(deadline)
        store.add_manual(sid, title[:200], request.form.get("url", "").strip()[:500], deadline)
    return redirect(url_for("cards.index"))


@bp.route("/refresh", methods=["POST"])
def refresh():
    fetcher.ensure_fresh(force=True)
    return redirect(url_for("cards.index"))
