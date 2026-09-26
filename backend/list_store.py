"""名单管理：黑白名单条目存储与 O(1) 匹配索引。

名单条目结构：
- list_type：black（黑名单）/ white（白名单）
- field：参与匹配的事件字段（如 ip / user_id / device_id，支持点路径嵌套字段）
- value：名单值（字符串精确匹配）
- action：命中后的处置——reject（拒绝）/ pass（放行）/ mark（仅标记不干预）；
  未显式指定时按名单类型取默认值（black→reject，white→pass）
- risk_score：条目风险分，黑名单拒绝时参与事件最终风险分合成
- reason / remark：命中原因与备注（随命中流水落盘，便于追溯）

匹配索引：field -> {value: [entry, ...]} 哈希表，仅启用条目入索引；
名单条目数量有限，每次变更后全量重建索引并在锁内原子替换引用，
匹配线程每次只读一次 ``_index`` 引用，看到的一定是完整索引（热更新）。
"""
import threading
import time

from backend import config
from backend.storage import read_json, update_json, gen_id
from backend.engine.rule_parser import _get_field

LIST_TYPES = ["black", "white"]
LIST_ACTIONS = ["reject", "pass", "mark"]
DEFAULT_ACTION = {"black": "reject", "white": "pass"}
# 常见主体字段（前端输入建议用，不限制其他字段）
COMMON_FIELDS = ["ip", "user_id", "device_id"]


def entry_action(entry):
    """返回条目的有效处置动作（未配置时按名单类型取默认）。"""
    action = entry.get("action")
    if action in LIST_ACTIONS:
        return action
    return DEFAULT_ACTION.get(entry.get("list_type"), "mark")


class ListStore:
    """黑白名单条目存储与匹配索引。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries = []       # 全量条目（含停用）
        self._index = {}         # field -> {value: [entry, ...]}（仅启用）
        self.reload()

    def reload(self):
        """从磁盘重新加载并重建匹配索引（原子替换引用）。"""
        data = read_json(config.LISTS_FILE, {"entries": []})
        entries = data.get("entries", [])
        index = {}
        for e in entries:
            if not e.get("enabled", True):
                continue
            field = e.get("field")
            value = str(e.get("value", ""))
            if not field or not value:
                continue
            index.setdefault(field, {}).setdefault(value, []).append(e)
        with self._lock:
            self._entries = entries
            self._index = index

    # ------------------------------------------------------------------
    # 匹配（引擎热路径）
    # ------------------------------------------------------------------
    def match(self, event):
        """返回事件命中的所有启用条目（可能多条）。"""
        with self._lock:
            index = self._index
        hits = []
        for field, values in index.items():
            v = _get_field(event, field)
            if v is None:
                continue
            entries = values.get(str(v))
            if entries:
                hits.extend(entries)
        return hits

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_entries(self, list_type=None, keyword=None, enabled=None):
        with self._lock:
            items = list(self._entries)
        if list_type:
            items = [e for e in items if e.get("list_type") == list_type]
        if enabled is not None:
            items = [e for e in items if bool(e.get("enabled", True)) == enabled]
        if keyword:
            kw = keyword.lower()
            items = [e for e in items
                     if kw in str(e.get("value", "")).lower()
                     or kw in str(e.get("field", "")).lower()
                     or kw in str(e.get("reason", "")).lower()
                     or kw in str(e.get("remark", "")).lower()]
        items.sort(key=lambda e: -e.get("updated_at", 0))
        return items

    def get(self, entry_id):
        with self._lock:
            for e in self._entries:
                if e.get("id") == entry_id:
                    return dict(e)
        return None

    # ------------------------------------------------------------------
    # 校验与 CRUD（返回 (ok, 结果或错误信息)）
    # ------------------------------------------------------------------
    def _validate(self, data, exclude_id=None):
        list_type = data.get("list_type")
        if list_type not in LIST_TYPES:
            return False, "名单类型必须是 black（黑名单）或 white（白名单）"
        field = str(data.get("field", "")).strip()
        if not field:
            return False, "匹配字段不能为空"
        value = str(data.get("value", "")).strip()
        if not value:
            return False, "名单值不能为空"
        action = data.get("action") or DEFAULT_ACTION[list_type]
        if action not in LIST_ACTIONS:
            return False, "处置动作必须是 reject / pass / mark"
        try:
            risk_score = int(data.get("risk_score", 0))
        except (TypeError, ValueError):
            return False, "风险分必须是整数"
        risk_score = max(0, min(risk_score, 100))
        with self._lock:
            for e in self._entries:
                if exclude_id and e.get("id") == exclude_id:
                    continue
                if (e.get("list_type") == list_type and e.get("field") == field
                        and str(e.get("value")) == value):
                    return False, "相同类型、字段与值的条目已存在"
        return True, {
            "list_type": list_type,
            "field": field,
            "value": value,
            "action": action,
            "risk_score": risk_score,
            "reason": str(data.get("reason", "")).strip(),
            "remark": str(data.get("remark", "")).strip(),
        }

    def add_entry(self, data, author=None):
        ok, result = self._validate(data)
        if not ok:
            return False, result
        now = int(time.time())
        result.update({
            "id": gen_id("lst_"),
            "enabled": bool(data.get("enabled", True)),
            "created_by": author or "system",
            "created_at": now,
            "updated_at": now,
        })

        def mutate(d):
            d.setdefault("entries", []).append(result)
        update_json(config.LISTS_FILE, mutate, default={"entries": []})
        self.reload()
        return True, result

    def update_entry(self, entry_id, data):
        with self._lock:
            existing = self.get(entry_id)
        if existing is None:
            return False, "条目不存在"
        merged = dict(existing)
        for k in ("list_type", "field", "value", "action", "risk_score",
                  "reason", "remark"):
            if k in data:
                merged[k] = data[k]
        ok, result = self._validate(merged, exclude_id=entry_id)
        if not ok:
            return False, result
        result["id"] = entry_id
        result["created_by"] = existing.get("created_by", "system")
        result["created_at"] = existing.get("created_at", int(time.time()))
        result["enabled"] = bool(data.get("enabled", existing.get("enabled", True)))
        result["updated_at"] = int(time.time())

        def mutate(d):
            entries = d.setdefault("entries", [])
            for i, e in enumerate(entries):
                if e.get("id") == entry_id:
                    entries[i] = result
                    return True
            return False
        updated = update_json(config.LISTS_FILE, mutate, default={"entries": []})
        if not updated:
            return False, "条目不存在"
        self.reload()
        return True, result

    def delete_entry(self, entry_id):
        def mutate(d):
            entries = d.setdefault("entries", [])
            before = len(entries)
            d["entries"] = [e for e in entries if e.get("id") != entry_id]
            return len(d["entries"]) < before
        deleted = update_json(config.LISTS_FILE, mutate, default={"entries": []})
        if deleted:
            self.reload()
        return deleted

    # ------------------------------------------------------------------
    def stats(self):
        with self._lock:
            items = list(self._entries)
        by_type = {"black": 0, "white": 0}
        enabled = 0
        for e in items:
            t = e.get("list_type")
            if t in by_type:
                by_type[t] += 1
            if e.get("enabled", True):
                enabled += 1
        return {"total": len(items), "enabled": enabled, "by_type": by_type}
