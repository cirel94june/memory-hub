"""
活动日志：记录小模型的所有操作，供前端监控面板查看。
使用内存环形缓冲 + SQLite 持久化最近 500 条。
"""
import time
import json
import logging
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger("memory_hub.activity")

# ── 内存缓冲（最近 200 条，快速读取）──
_log_buffer: deque[dict] = deque(maxlen=200)

# ── 活动类型 ──
# analyze: 分析记忆（打标/分类）
# merge: 合并记忆
# digest: 拆分长文
# decay: 衰减/归档
# remember: 新增记忆
# update: 更新记忆
# recall: 搜索记忆
# error: 错误
# config: 配置变更
# relation: 关系分类


def log_activity(
    action: str,
    detail: str,
    *,
    memory_id: str = "",
    ai_id: str = "",
    model: str = "",
    tokens_used: int = 0,
    duration_ms: int = 0,
    success: bool = True,
    extra: dict | None = None,
):
    """记录一条活动日志"""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "epoch": time.time(),
        "action": action,
        "detail": detail[:500],  # 截断长文本
        "memory_id": memory_id,
        "ai_id": ai_id,
        "model": model,
        "tokens_used": tokens_used,
        "duration_ms": duration_ms,
        "success": success,
    }
    if extra:
        entry["extra"] = extra

    _log_buffer.append(entry)

    # 同时写入 SQLite（后台，不阻塞）
    try:
        _persist(entry)
    except Exception:
        pass  # 日志持久化失败不影响主流程


def get_recent(limit: int = 50, action_filter: str = "", since_epoch: float = 0) -> list[dict]:
    """获取最近的活动日志"""
    results = []
    for entry in reversed(_log_buffer):
        if action_filter and entry["action"] != action_filter:
            continue
        if since_epoch and entry["epoch"] < since_epoch:
            continue
        results.append(entry)
        if len(results) >= limit:
            break
    return results


def get_stats() -> dict:
    """获取统计摘要"""
    now = time.time()
    hour_ago = now - 3600
    day_ago = now - 86400

    stats = {
        "total_in_buffer": len(_log_buffer),
        "last_hour": {"total": 0, "errors": 0, "by_action": {}},
        "last_day": {"total": 0, "errors": 0, "by_action": {}, "tokens_total": 0},
    }

    for entry in _log_buffer:
        epoch = entry["epoch"]

        if epoch >= day_ago:
            stats["last_day"]["total"] += 1
            stats["last_day"]["tokens_total"] += entry.get("tokens_used", 0)
            if not entry["success"]:
                stats["last_day"]["errors"] += 1
            act = entry["action"]
            stats["last_day"]["by_action"][act] = stats["last_day"]["by_action"].get(act, 0) + 1

        if epoch >= hour_ago:
            stats["last_hour"]["total"] += 1
            if not entry["success"]:
                stats["last_hour"]["errors"] += 1
            act = entry["action"]
            stats["last_hour"]["by_action"][act] = stats["last_hour"]["by_action"].get(act, 0) + 1

    return stats


# ── SQLite 持久化（可选，仅当 database 模块可用时）──

_db_ready = False


def init_activity_table():
    """在 SQLite 中创建活动日志表"""
    global _db_ready
    try:
        import database
        conn = database._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                epoch REAL NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                memory_id TEXT,
                ai_id TEXT,
                model TEXT,
                tokens_used INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                success INTEGER DEFAULT 1,
                extra TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_epoch ON activity_log(epoch DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_action ON activity_log(action)
        """)
        conn.commit()
        _db_ready = True

        # 加载最近的日志到内存缓冲
        rows = conn.execute(
            "SELECT ts, epoch, action, detail, memory_id, ai_id, model, "
            "tokens_used, duration_ms, success, extra "
            "FROM activity_log ORDER BY epoch DESC LIMIT 200"
        ).fetchall()
        for row in reversed(rows):
            entry = {
                "ts": row[0], "epoch": row[1], "action": row[2],
                "detail": row[3] or "", "memory_id": row[4] or "",
                "ai_id": row[5] or "", "model": row[6] or "",
                "tokens_used": row[7] or 0, "duration_ms": row[8] or 0,
                "success": bool(row[9]),
            }
            if row[10]:
                try:
                    entry["extra"] = json.loads(row[10])
                except Exception:
                    pass
            _log_buffer.append(entry)

        logger.info(f"Activity log initialized, loaded {len(rows)} recent entries")
    except Exception as e:
        logger.warning(f"Activity log table init failed: {e}")


def _persist(entry: dict):
    """写入一条日志到 SQLite"""
    if not _db_ready:
        return
    try:
        import database
        conn = database._get_conn()
        extra_json = json.dumps(entry.get("extra")) if entry.get("extra") else None
        conn.execute(
            "INSERT INTO activity_log (ts, epoch, action, detail, memory_id, ai_id, "
            "model, tokens_used, duration_ms, success, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["ts"], entry["epoch"], entry["action"], entry["detail"],
                entry["memory_id"], entry["ai_id"], entry["model"],
                entry["tokens_used"], entry["duration_ms"],
                1 if entry["success"] else 0, extra_json,
            ),
        )
        conn.commit()

        # 清理超过 500 条的旧记录
        conn.execute(
            "DELETE FROM activity_log WHERE id NOT IN "
            "(SELECT id FROM activity_log ORDER BY epoch DESC LIMIT 500)"
        )
        conn.commit()
    except Exception:
        pass
