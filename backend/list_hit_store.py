"""名单命中流水：每一次名单命中的完整记录（只读历史数据）。

每当一条事件命中某个黑名单/白名单条目时记录一条流水，包含：
- 事件主体（subject：ip / user_id / device_id）与命中的字段值（subject_value）；
- 命中的名单类型（list_type）与条目（entry_id / entry_value）；
- 命中时间（ts）、处置结果（disposition：reject 拒绝 / pass 放行 / mark 标记）；
- 相关风险分（risk_score）与命中事件的概要信息（event_summary）。

存储：按天分片 JSON 文件（list_hits/YYYYMMDD.json，{"hits": [...]}），
追加采用「读-改-写 + 原子替换」（storage.update_json），与告警分片一致。

流水为只读历史数据：本模块只提供 record（引擎写入）与查询接口，
不提供任何修改 / 删除流水的入口。
"""
import os
import threading
import time
from datetime import date, timedelta

from backend import config
from backend.storage import read_json, update_json, gen_id
from backend.engine.rule_parser import _get_field

# 单次查询允许跨越的最大天数，防止时间范围过大扫爆磁盘
MAX_RANGE_DAYS = 62

# 事件概要字段（命中事件的概要信息，供流水详情查看）
SUMMARY_FIELDS = ("id", "type", "ts", "ip", "user_id", "device_id",
                  "channel", "amount", "country")

# 事件主体字段
SUBJECT_FIELDS = ("ip", "user_id", "device_id")


def _day_key(ts):
    """与告警分片一致的时区口径（UTC+8）。"""
    t = time.gmtime(ts - 8 * 3600)
    return f"{t.tm_year:04d}{t.tm_mon:02d}{t.tm_mday:02d}"


def _shard_path(day):
    return os.path.join(config.LIST_HITS_DIR, f"{day}.json")


def _days_between(start, end):
    """生成 [start, end] 覆盖的所有天分片键（YYYYMMDD）。"""
    d0 = _day_key(start)
    d1 = _day_key(end)
    cur = date(int(d0[:4]), int(d0[4:6]), int(d0[6:8]))
    last = date(int(d1[:4]), int(d1[4:6]), int(d1[6:8]))
    days = []
    while cur <= last and len(days) <= MAX_RANGE_DAYS:
        days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return days


class ListHitStore:
    """名单命中流水：追加写入 + 条件查询（只读历史）。"""

    def __init__(self):
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 写入（仅引擎调用）
    # ------------------------------------------------------------------
    def record(self, entry, event, disposition, ts=None):
        """记录一条名单命中流水，返回流水记录。

        entry：命中的名单条目；event：命中事件；disposition：处置结果
        （reject / pass / mark）。
        """
        if ts is None:
            ts = time.time()
        subject = {}
        for f in SUBJECT_FIELDS:
            if event.get(f) is not None:
                subject[f] = event.get(f)
        summary = {}
        for f in SUMMARY_FIELDS:
            if event.get(f) is not None:
                summary[f] = event.get(f)
        hit = {
            "id": gen_id("hit_"),
            "ts": ts,
            "list_type": entry.get("list_type"),
            "entry_id": entry.get("id"),
            "entry_value": entry.get("value"),
            "field": entry.get("field"),
            "subject": subject,
            "subject_value": _get_field(event, entry.get("field", "")),
            "disposition": disposition,
            "risk_score": int(entry.get("risk_score", 0) or 0),
            "event_id": event.get("id"),
            "event_type": event.get("type"),
            "event_summary": summary,
        }
        path = _shard_path(_day_key(ts))
        with self._lock:
            update_json(path,
                        lambda d: d.setdefault("hits", []).append(hit),
                        default={"hits": []})
        return hit

    # ------------------------------------------------------------------
    # 查询（只读）
    # ------------------------------------------------------------------
    def _load_range(self, start, end):
        """读取时间范围内的全部流水（跨天合并分片）。"""
        hits = []
        for day in _days_between(start, end):
            data = read_json(_shard_path(day), {"hits": []})
            hits.extend(data.get("hits", []))
        return hits

    def query(self, list_type=None, entry_id=None, subject=None, disposition=None,
              start=None, end=None, page=1, page_size=20):
        """按名单类型 / 条目 / 主体 / 处置 / 时间范围筛选流水，最新在前。

        返回 (total, items)：total 为筛选后总条数，items 为当前页。
        """
        if end is None:
            end = time.time()
        if start is None:
            start = end - 86400
        if end < start:
            start, end = end, start

        hits = [h for h in self._load_range(start, end)
                if start <= h.get("ts", 0) <= end]
        if list_type:
            hits = [h for h in hits if h.get("list_type") == list_type]
        if entry_id:
            hits = [h for h in hits if h.get("entry_id") == entry_id]
        if disposition:
            hits = [h for h in hits if h.get("disposition") == disposition]
        if subject:
            kw = str(subject).lower()
            def _subject_ok(h):
                if kw in str(h.get("subject_value", "")).lower():
                    return True
                sub = h.get("subject") or {}
                return any(kw in str(v).lower() for v in sub.values())
            hits = [h for h in hits if _subject_ok(h)]

        hits.sort(key=lambda h: -h.get("ts", 0))
        total = len(hits)
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        begin = (page - 1) * page_size
        return total, hits[begin:begin + page_size]

    def get(self, hit_id):
        """按流水 ID 查询单条（含命中事件概要）。"""
        # 流水 ID 形如 hit_<毫秒时间戳>_<哈希>，可直接定位分片
        ts = None
        try:
            ts = int(str(hit_id).split("_")[1]) / 1000.0
        except (IndexError, ValueError):
            ts = None
        days = []
        if ts:
            days.append(_day_key(ts))
        else:
            now = time.time()
            days = [_day_key(now - i * 86400) for i in range(7)]
        for day in days:
            data = read_json(_shard_path(day), {"hits": []})
            for h in data.get("hits", []):
                if h.get("id") == hit_id:
                    return h
        return None

    def stats(self, start=None, end=None):
        """统计时间范围内的流水：总量、按名单类型、按处置结果。"""
        if end is None:
            end = time.time()
        if start is None:
            start = end - 86400
        hits = [h for h in self._load_range(start, end)
                if start <= h.get("ts", 0) <= end]
        by_type = {"black": 0, "white": 0}
        by_disposition = {"reject": 0, "pass": 0, "mark": 0}
        for h in hits:
            lt = h.get("list_type")
            by_type[lt] = by_type.get(lt, 0) + 1
            dp = h.get("disposition")
            by_disposition[dp] = by_disposition.get(dp, 0) + 1
        return {
            "total": len(hits),
            "by_type": by_type,
            "by_disposition": by_disposition,
            "start": start,
            "end": end,
        }
