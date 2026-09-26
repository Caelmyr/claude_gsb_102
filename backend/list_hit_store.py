"""名单命中流水：只读历史记录。

每当一条事件命中某个黑名单/白名单条目，由风控引擎调用 ``record`` 写入一条流水，
内容包含：事件主体、命中的名单类型与条目、命中时间、处置结果
（reject 拒绝 / pass 放行 / mark 标记）、相关风险分，以及命中事件的概要信息。

存储设计：
- 按天分片持久化为 JSON（list_hits/YYYYMMDD.json），写入时同步读-改-写落盘，
  与告警分片同一套并发安全原语（进程内 RLock + flock + 原子替换）；
- 内存 deque 缓存最近 ``max_keep`` 条（启动时加载最近两天），用于统计与磁盘兜底；
- 查询按时间范围定位涉及的天分片，磁盘 + 内存合并去重后筛选分页。

流水为只读历史数据：本模块只暴露 record（引擎内部写入）与查询接口，
不提供任何修改/删除入口，API 层同样只读。
"""
import os
import threading
import time
from collections import deque

from backend import config
from backend.storage import read_json, update_json, gen_id

# 事件主体字段（流水的“主体”维度）
SUBJECT_FIELDS = ["ip", "user_id", "device_id"]
# 命中事件概要保留的字段
SUMMARY_FIELDS = ["id", "type", "ts", "ip", "user_id", "device_id",
                  "channel", "amount", "country", "risk_hint"]


def _day_key(ts):
    """与告警分片一致的本地时区（UTC+8）天键。"""
    t = time.gmtime(ts - 8 * 3600)
    return f"{t.tm_year:04d}{t.tm_mon:02d}{t.tm_mday:02d}"


def _day_path(day):
    return os.path.join(config.LIST_HITS_DIR, day + ".json")


class ListHitStore:
    """名单命中流水存储（只读历史）。"""

    def __init__(self, max_keep=20000, max_query_days=31):
        self.max_keep = max_keep
        self.max_query_days = max_query_days
        self._hits = deque()     # 内存缓存（最近 max_keep 条）
        self._lock = threading.RLock()
        self._load_recent()

    def _load_recent(self):
        """启动时加载最近两天的流水到内存缓存。"""
        now = time.time()
        for i in range(1, -1, -1):
            day = _day_key(now - i * 86400)
            data = read_json(_day_path(day), {"hits": []})
            for h in data.get("hits", []):
                self._hits.append(h)

    # ------------------------------------------------------------------
    # 写入（唯一入口，仅引擎调用）
    # ------------------------------------------------------------------
    def record(self, entry, event, action, risk_score, ts=None):
        """记录一条名单命中流水，返回流水记录。"""
        if ts is None:
            ts = time.time()
        subject = {}
        for f in SUBJECT_FIELDS:
            v = event.get(f)
            if v is not None:
                subject[f] = v
        summary = {}
        for f in SUMMARY_FIELDS:
            if event.get(f) is not None:
                summary[f] = event.get(f)
        hit = {
            "id": gen_id("hit_"),
            "ts": ts,
            "list_type": entry.get("list_type"),
            "entry_id": entry.get("id"),
            "entry_field": entry.get("field"),
            "entry_value": entry.get("value"),
            "entry_reason": entry.get("reason", ""),
            "subject": subject,
            "action": action,
            "risk_score": int(risk_score or 0),
            "event_summary": summary,
        }
        with self._lock:
            self._hits.append(hit)
            while len(self._hits) > self.max_keep:
                self._hits.popleft()
        update_json(_day_path(_day_key(ts)),
                    lambda d: d.setdefault("hits", []).append(hit),
                    default={"hits": []})
        return hit

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _days_between(self, start, end):
        """返回 [start, end] 覆盖的所有天分片键（按 UTC+8）。"""
        base = int((start - 8 * 3600) // 86400)
        last = int((end - 8 * 3600) // 86400)
        days = []
        for d in range(base, last + 1):
            # 当天正午，避免边界误差
            days.append(_day_key(d * 86400 + 8 * 3600 + 43200))
        return days

    def query(self, list_type=None, entry_id=None, subject=None, action=None,
              start=None, end=None, page=1, page_size=20):
        """按名单类型、条目、主体、处置结果与时间范围筛选流水（最新在前）。"""
        now = time.time()
        end = float(end) if end else now
        start = float(start) if start else end - 86400
        # 单次查询最多回溯 max_query_days 天，防止扫描过多分片
        start = max(start, end - self.max_query_days * 86400)

        items = []
        seen = set()
        for day in self._days_between(start, end):
            data = read_json(_day_path(day), {"hits": []})
            for h in data.get("hits", []):
                hid = h.get("id")
                if hid and hid not in seen:
                    seen.add(hid)
                    items.append(h)
        # 合并内存缓存（与磁盘按 id 去重，兜底未落盘数据）
        with self._lock:
            for h in self._hits:
                hid = h.get("id")
                if hid and hid not in seen:
                    seen.add(hid)
                    items.append(h)

        items = [h for h in items if start <= h.get("ts", 0) <= end]
        if list_type:
            items = [h for h in items if h.get("list_type") == list_type]
        if entry_id:
            items = [h for h in items if h.get("entry_id") == entry_id]
        if action:
            items = [h for h in items if h.get("action") == action]
        if subject:
            kw = subject.lower()

            def _match_subject(h):
                if kw in str(h.get("entry_value", "")).lower():
                    return True
                for v in (h.get("subject") or {}).values():
                    if kw in str(v).lower():
                        return True
                return False
            items = [h for h in items if _match_subject(h)]

        items.sort(key=lambda h: -h.get("ts", 0))
        total = len(items)
        page = max(1, int(page))
        s = (page - 1) * page_size
        return total, items[s:s + page_size]

    # ------------------------------------------------------------------
    # 统计（口径：内存缓存范围内的流水）
    # ------------------------------------------------------------------
    def count_by_entry(self):
        """entry_id -> 命中次数（内存缓存范围），供名单列表展示。"""
        counts = {}
        with self._lock:
            for h in self._hits:
                eid = h.get("entry_id")
                if eid:
                    counts[eid] = counts.get(eid, 0) + 1
        return counts

    def stats(self):
        with self._lock:
            items = list(self._hits)
        now = time.time()
        today_start = int((now - 8 * 3600) // 86400) * 86400 + 8 * 3600
        by_type = {"black": 0, "white": 0}
        by_action = {"reject": 0, "pass": 0, "mark": 0}
        today = 0
        for h in items:
            t = h.get("list_type")
            if t in by_type:
                by_type[t] += 1
            a = h.get("action")
            if a in by_action:
                by_action[a] += 1
            if h.get("ts", 0) >= today_start:
                today += 1
        return {
            "cached": len(items),
            "today": today,
            "by_type": by_type,
            "by_action": by_action,
        }
