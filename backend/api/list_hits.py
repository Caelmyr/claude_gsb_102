"""名单命中流水 API：只读查询（不提供任何修改入口）。

流水由引擎在事件命中名单条目时自动写入，本模块仅提供
按名单类型 / 条目 / 主体 / 处置 / 时间范围的筛选查询与单条详情。
"""
from flask import Blueprint, request, jsonify

from backend import runtime
from backend.auth import login_required

bp = Blueprint("list_hits", __name__, url_prefix="/api/list-hits")


def _float_arg(name):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@bp.route("", methods=["GET"])
@login_required
def query_hits():
    total, items = runtime.engine.list_hits.query(
        list_type=request.args.get("list_type") or None,
        entry_id=request.args.get("entry_id") or None,
        subject=request.args.get("subject") or None,
        disposition=request.args.get("disposition") or None,
        start=_float_arg("start"),
        end=_float_arg("end"),
        page=request.args.get("page", 1, type=int) or 1,
        page_size=request.args.get("page_size", 20, type=int) or 20,
    )
    return jsonify({"ok": True, "total": total, "hits": items})


@bp.route("/stats", methods=["GET"])
@login_required
def hit_stats():
    stats = runtime.engine.list_hits.stats(
        start=_float_arg("start"), end=_float_arg("end"))
    return jsonify({"ok": True, "stats": stats})


@bp.route("/<hit_id>", methods=["GET"])
@login_required
def get_hit(hit_id):
    hit = runtime.engine.list_hits.get(hit_id)
    if hit is None:
        return jsonify({"ok": False, "error": "流水不存在"}), 404
    return jsonify({"ok": True, "hit": hit})
