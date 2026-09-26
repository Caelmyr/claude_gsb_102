"""名单管理 API：黑白名单条目 CRUD（命中流水见 list_hits.py）。"""
from flask import Blueprint, request, jsonify

from backend import runtime
from backend.auth import login_required, role_required

bp = Blueprint("lists", __name__, url_prefix="/api/lists")


@bp.route("", methods=["GET"])
@login_required
def list_entries():
    list_type = request.args.get("list_type")
    keyword = request.args.get("keyword")
    raw_enabled = request.args.get("enabled")
    enabled = None
    if raw_enabled in ("1", "true"):
        enabled = True
    elif raw_enabled in ("0", "false"):
        enabled = False
    items = runtime.engine.lists.list_entries(
        list_type=list_type, keyword=keyword, enabled=enabled)
    # 每条目附带命中次数（内存缓存范围内的流水统计）
    counts = {}
    if runtime.engine.list_hits is not None:
        counts = runtime.engine.list_hits.count_by_entry()
    for e in items:
        e["hit_count"] = counts.get(e.get("id"), 0)
    return jsonify({"ok": True, "entries": items, "total": len(items)})


@bp.route("", methods=["POST"])
@role_required("admin", "analyst", "viewer")
def add_entry():
    data = request.get_json(force=True, silent=True) or {}
    ok, result = runtime.engine.lists.add_entry(data)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "entry": result})


@bp.route("/<entry_id>", methods=["PUT"])
@role_required("admin", "analyst", "viewer")
def update_entry(entry_id):
    data = request.get_json(force=True, silent=True) or {}
    ok, result = runtime.engine.lists.update_entry(entry_id, data)
    if not ok:
        code = 404 if result == "条目不存在" else 400
        return jsonify({"ok": False, "error": result}), code
    return jsonify({"ok": True, "entry": result})


@bp.route("/<entry_id>", methods=["DELETE"])
@role_required("admin", "viewer")
def delete_entry(entry_id):
    ok = runtime.engine.lists.delete_entry(entry_id)
    if not ok:
        return jsonify({"ok": False, "error": "条目不存在"}), 404
    return jsonify({"ok": True})


@bp.route("/stats", methods=["GET"])
@login_required
def list_stats():
    return jsonify({"ok": True, "stats": runtime.engine.lists.stats()})
