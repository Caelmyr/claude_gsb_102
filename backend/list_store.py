"""黑白名单管理：条目存储与事件匹配。

名单条目存于 data/lists/lists.json（单文件，条目量级小）：
    {"entries": [{id, list_type, field, value, action, risk_score,
                  remark, enabled, created_at, updated_at}, ...]}

- list_type：black（黑名单）/ white（白名单）；
- field：事件匹配字段（ip / user_id / device_id / country / channel，支持点路径）；
- action：命中后的处置——reject（拒绝）/ pass（放行）/ mark（仅标记）；
  缺省按名单类型推导：黑名单 → reject，白名单 → pass。

匹配性能：内存中维护 (list_type, field) -> {value: entry} 哈希索引，
事件匹配为 O(名单维度数) 的字典查找，与条目总数无关；每次变更后重建索引。
"""
import threading
import time

from backend import config
from backend.storage import read_json, update_json, gen_id
from backend.engine.rule_parser import _get_field

# 缺省处置：黑名单拒绝、白名单放行
DEFAULT_ACTION = {"black": "reject", "white": "pass"}


class ListStore:
    """名单条目的 CRUD 与事件匹配。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries = []            # list[dict]，全量条目
        self._index = {}              # (list_type, field) -> {str(value): entry}
        self._load()

    # ------------------------------------------------------------------
    # 加载 / 索引
    # ------------------------------------------------------------------
    def _load(self):
        data = read_json(config.LISTS_FILE, {"entries": []})
        self._entries = data.get("entries", [])
        self._rebuild_index()

    def reload(self):
        """外部变更 lists.json 后重新加载（如种子数据）。"""
        with self._lock:
            self._load()

    def _rebuild_index(self):
        idx = {}
        for e in self._entries:
            if not e.get("enabled", True):
                continue
            key = (e.get("list_type"), e.get("field"))
            idx.setdefault(key, {})[str(e.get("value"))] = e
        self._index = idx

    def _persist(self):
        update_json(config.LISTS_FILE,
                    lambda d: d.update({"entries": self._entries}),
                    default={"entries": []})

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    def match(self, event):
        """返回事件命中的全部启用条目（可能多条，如 IP 与 user_id 同时命中）。"""
        hits = []
        with self._lock:
            index = self._index
        for (list_type, field), values in index.items():
            v = _get_field(event, field)
            if v is None:
                continue
            entry = values.get(str(v))
            if entry is not None:
                hits.append(entry)
        hits.sort(key=lambda e: (e.get("list_type") != "white", e.get("id", "")))
        return hits

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_entries(self, list_type=None, field=None, keyword=None):
        with self._lock:
            items = list(self._entries)
        if list_type:
            items = [e for e in items if e.get("list_type") == list_type]
        if field:
            items = [e for e in items if e.get("field") == field]
        if keyword:
            kw = keyword.lower()
            items = [e for e in items
                     if kw in str(e.get("value", "")).lower()
                     or kw in str(e.get("remark", "")).lower()]
        items.sort(key=lambda e: -e.get("created_at", 0))
        return items

    def get(self, entry_id):
        with self._lock:
            for e in self._entries:
                if e.get("id") == entry_id:
                    return dict(e)
        return None

    # ------------------------------------------------------------------
    # 变更（变更后重建索引并持久化）
    # ------------------------------------------------------------------
    @staticmethod
    def validate(entry, partial=False):
        """校验条目字段，返回错误信息（None 表示合法）。"""
        if not partial or "list_type" in entry:
            if entry.get("list_type") not in config.LIST_TYPES:
                return "名单类型必须是 black（黑名单）或 white（白名单）"
        if not partial or "field" in entry:
            if not str(entry.get("field", "")).strip():
                return "匹配字段不能为空"
        if not partial or "value" in entry:
            if not str(entry.get("value", "")).strip():
                return "名单值不能为空"
        action = entry.get("action")
        if action is not None and action not in config.LIST_DISPOSITIONS:
            return "处置方式必须是 reject（拒绝）/ pass（放行）/ mark（标记）"
        score = entry.get("risk_score")
        if score is not None:
            try:
                int(score)
            except (TypeError, ValueError):
                return "风险分必须是整数"
        return None

    def create_entry(self, data):
        """新增条目，返回 (entry, error)。同一 (类型, 字段, 值) 不允许重复。"""
        entry = {
            "id": gen_id("lst_"),
            "list_type": data.get("list_type"),
            "field": str(data.get("field", "")).strip(),
            "value": str(data.get("value", "")).strip(),
            "action": data.get("action") or DEFAULT_ACTION.get(data.get("list_type"), "mark"),
            "risk_score": int(data.get("risk_score", 0) or 0),
            "remark": data.get("remark", ""),
            "enabled": bool(data.get("enabled", True)),
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        err = self.validate(entry)
        if err:
            return None, err
        with self._lock:
            for e in self._entries:
                if (e.get("list_type"), e.get("field"), str(e.get("value"))) == \
                        (entry["list_type"], entry["field"], entry["value"]):
                    return None, "相同类型、字段与值的条目已存在"
            self._entries.append(entry)
            self._persist()
            self._rebuild_index()
        return dict(entry), None

    def update_entry(self, entry_id, data):
        """更新条目，返回 (entry, error)。"""
        with self._lock:
            target = None
            for e in self._entries:
                if e.get("id") == entry_id:
                    target = e
                    break
            if target is None:
                return None, "条目不存在"
            merged = dict(target)
            for k in ("list_type", "field", "value", "action", "remark"):
                if k in data:
                    merged[k] = data[k]
            if "risk_score" in data:
                merged["risk_score"] = data["risk_score"]
            if "enabled" in data:
                merged["enabled"] = bool(data["enabled"])
            merged["field"] = str(merged.get("field", "")).strip()
            merged["value"] = str(merged.get("value", "")).strip()
            if not merged.get("action"):
                merged["action"] = DEFAULT_ACTION.get(merged.get("list_type"), "mark")
            err = self.validate(merged)
            if err:
                return None, err
            merged["risk_score"] = int(merged.get("risk_score", 0) or 0)
            for e in self._entries:
                if e.get("id") != entry_id and \
                        (e.get("list_type"), e.get("field"), str(e.get("value"))) == \
                        (merged["list_type"], merged["field"], merged["value"]):
                    return None, "相同类型、字段与值的条目已存在"
            merged["updated_at"] = int(time.time())
            target.update(merged)
            self._persist()
            self._rebuild_index()
            return dict(target), None

    def delete_entry(self, entry_id):
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("id") != entry_id]
            if len(self._entries) == before:
                return False
            self._persist()
            self._rebuild_index()
            return True

    def set_enabled(self, entry_id, enabled):
        with self._lock:
            for e in self._entries:
                if e.get("id") == entry_id:
                    e["enabled"] = bool(enabled)
                    e["updated_at"] = int(time.time())
                    self._persist()
                    self._rebuild_index()
                    return dict(e)
        return None

    def stats(self):
        with self._lock:
            items = list(self._entries)
        by_type = {"black": 0, "white": 0}
        enabled = 0
        for e in items:
            by_type[e.get("list_type", "black")] = by_type.get(e.get("list_type", "black"), 0) + 1
            if e.get("enabled", True):
                enabled += 1
        return {"total": len(items), "enabled": enabled, "by_type": by_type}
