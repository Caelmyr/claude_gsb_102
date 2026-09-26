"""名单管理 API：黑白名单条目的查询与维护。"""
from flask import Blueprint, request, jsonify

from backend import runtime, config
from backend.auth import login_required, role_required

bp = Blueprint("lists", __name__, url_prefix="/api/lists")


@bp.route("", methods=["GET"])
@login_required
def list_entries():
    list_type = request.args.get("list_type")
    field = request.args.get("field")
    keyword = request.args.get("keyword")
    entries = runtime.engine.lists.list_entries(
        list_type=list_type, field=field, keyword=keyword)
    return jsonify({"ok": True, "entries": entries, "total": len(entries)})


@bp.route("/meta", methods=["GET"])
@login_required
def list_meta():
    """名单字段与处置方式的可选值（供前端表单）。"""
    return jsonify({
        "ok": True,
        "list_types": config.LIST_TYPES,
        "fields": config.LIST_FIELDS,
        "dispositions": config.LIST_DISPOSITIONS,
        "stats": runtime.engine.lists.stats(),
    })


@bp.route("", methods=["POST"])
@role_required("admin", "analyst", "viewer")
def create_entry():
    data = request.get_json(force=True, silent=True) or {}
    entry = data.get("entry", data)
    saved, err = runtime.engine.lists.create_entry(entry)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "entry": saved})


@bp.route("/<entry_id>", methods=["GET"])
@login_required
def get_entry(entry_id):
    entry = runtime.engine.lists.get(entry_id)
    if entry is None:
        return jsonify({"ok": False, "error": "条目不存在"}), 404
    return jsonify({"ok": True, "entry": entry})


@bp.route("/<entry_id>", methods=["PUT"])
@role_required("admin", "analyst", "viewer")
def update_entry(entry_id):
    data = request.get_json(force=True, silent=True) or {}
    entry = data.get("entry", data)
    saved, err = runtime.engine.lists.update_entry(entry_id, entry)
    if err:
        code = 404 if "不存在" in err else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True, "entry": saved})


@bp.route("/<entry_id>", methods=["DELETE"])
@role_required("admin", "analyst", "viewer")
def delete_entry(entry_id):
    ok = runtime.engine.lists.delete_entry(entry_id)
    if not ok:
        return jsonify({"ok": False, "error": "条目不存在"}), 404
    return jsonify({"ok": True})


@bp.route("/<entry_id>/enable", methods=["POST"])
@role_required("admin", "analyst", "viewer")
def toggle_entry(entry_id):
    data = request.get_json(force=True, silent=True) or {}
    entry = runtime.engine.lists.set_enabled(entry_id, bool(data.get("enabled", True)))
    if entry is None:
        return jsonify({"ok": False, "error": "条目不存在"}), 404
    return jsonify({"ok": True, "entry": entry})
