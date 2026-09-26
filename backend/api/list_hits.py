"""名单命中流水 API：只读查询。

流水由风控引擎在事件命中名单条目时自动记录，属于只读历史数据，
本模块不提供任何新增/修改/删除接口。
"""
from flask import Blueprint, request, jsonify

from backend import runtime
from backend.auth import login_required

bp = Blueprint("list_hits", __name__, url_prefix="/api/list_hits")


@bp.route("", methods=["GET"])
@login_required
def query_hits():
    page = request.args.get("page", 1, type=int) or 1
    page_size = request.args.get("page_size", 20, type=int) or 20
    page_size = max(1, min(page_size, 200))
    total, items = runtime.engine.list_hits.query(
        list_type=request.args.get("list_type"),
        entry_id=request.args.get("entry_id"),
        subject=request.args.get("subject"),
        action=request.args.get("action"),
        start=request.args.get("start", type=float),
        end=request.args.get("end", type=float),
        page=page, page_size=page_size)
    return jsonify({"ok": True, "total": total, "hits": items,
                    "page": page, "page_size": page_size})


@bp.route("/stats", methods=["GET"])
@login_required
def hit_stats():
    stats = runtime.engine.list_hits.stats()
    return jsonify({"ok": True, "stats": stats})
