import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.config import settings

DB_PATH = settings.db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    collector TEXT NOT NULL DEFAULT 'xchina',
    status TEXT NOT NULL DEFAULT 'pending',
    progress INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_time TEXT NOT NULL,
    download_dir TEXT,
    options TEXT,
    name TEXT,
    -- 心跳时间戳(epoch 秒): 看门狗判活用的, 见 _ADD_COLUMNS 里的说明
    hb REAL
);

CREATE TABLE IF NOT EXISTS resources(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    headers TEXT NOT NULL DEFAULT '{}',
    local_path TEXT,
    hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_time TEXT NOT NULL,
    size INTEGER,
    note TEXT,
    mirrors TEXT,
    filename TEXT,
    content_type TEXT,
    resolved_url TEXT
);

CREATE TABLE IF NOT EXISTS task_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    created_time TEXT NOT NULL
);

-- 订阅巡检: 定期重跑某个 URL 的任务, 只补新增资源(走 incremental)
CREATE TABLE IF NOT EXISTS watches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    collector TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 360,
    options TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    next_run TEXT NOT NULL,
    last_task_id INTEGER,
    -- 累计新增: 每次巡检真正下载下来的资源数
    hits INTEGER NOT NULL DEFAULT 0,
    created_time TEXT NOT NULL
);
"""

# 索引在列迁移之后创建(旧库可能缺列)
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_resources_task ON resources(task_id);
CREATE INDEX IF NOT EXISTS idx_resources_hash ON resources(hash);
CREATE INDEX IF NOT EXISTS idx_logs_task ON task_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


# 旧库补列: (表名, 列名, 类型)
_ADD_COLUMNS = [
    ("resources", "hash", "TEXT"),
    ("resources", "size", "INTEGER"),
    ("resources", "note", "TEXT"),
    ("resources", "mirrors", "TEXT"),
    ("tasks", "download_dir", "TEXT"),
    ("tasks", "options", "TEXT"),
    # 任务展示名: 提取阶段从首个资源的相册名/标题回写, 列表与详情页优先显示它
    ("tasks", "name", "TEXT"),
    # 落库的心跳时间戳(epoch 秒)。看门狗靠它区分「别的进程正在跑」和
    # 「进程已经死了」—— 只靠内存字典在多实例/多 worker 共用一个库时会误杀。
    ("tasks", "hb", "REAL"),
    # 产出物命名与审计: 见 core/naming.py 与 core/manifest.py
    ("resources", "filename", "TEXT"),
    ("resources", "content_type", "TEXT"),
    ("resources", "resolved_url", "TEXT"),
]


def _migrate(conn):
    """为旧版本数据库补齐新增列(新库由 SCHEMA 直接创建, 这里幂等跳过)。"""
    for table, col, coltype in _ADD_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()

_lock = threading.Lock()
_conn = None


def get_conn():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.executescript(INDEXES)
        _conn.commit()
    return _conn


def execute(sql, params=()):
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def query(sql, params=()):
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---- tasks ----

def create_task(url, collector="xchina", download_dir=None, options=None, name=None):
    cur = execute(
        "INSERT INTO tasks(url, collector, status, created_time, download_dir, options, name)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            url,
            collector,
            "pending",
            _now(),
            download_dir,
            json.dumps(options or {}, ensure_ascii=False),
            name,
        ),
    )
    return cur.lastrowid


def get_task(task_id):
    return query_one("SELECT * FROM tasks WHERE id=?", (task_id,))


def list_tasks():
    return query("SELECT * FROM tasks ORDER BY id DESC")


def update_task(task_id, **fields):
    if not fields:
        return
    cols = ",".join(f"{k}=?" for k in fields)
    execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def update_task_status(task_id, status):
    return execute(
        "UPDATE tasks SET status=? WHERE id=?", (status, task_id)
    ).rowcount


def transition_task(task_id, new_status, expected_status):
    """状态机迁移，带乐观锁校验当前状态。返回是否迁移成功。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE tasks SET status=? WHERE id=? AND status=?",
            (new_status, task_id, expected_status),
        )
        conn.commit()
        return cur.rowcount == 1


def transition_task_from_any(task_id, new_status, allowed_statuses):
    """从任一允许状态迁移到新状态(用于取消/看门狗)。"""
    ph = ",".join("?" for _ in allowed_statuses)
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE tasks SET status=? WHERE id=? AND status IN ({ph})",
            (new_status, task_id, *allowed_statuses),
        )
        conn.commit()
        return cur.rowcount == 1


def delete_task(task_id):
    execute("DELETE FROM tasks WHERE id=?", (task_id,))


def list_active_tasks():
    return query(
        "SELECT * FROM tasks WHERE status IN ('running','extracting','downloading')"
    )


def delete_task_resources(task_id):
    execute("DELETE FROM resources WHERE task_id=?", (task_id,))


# ---- resources ----

def add_resource(task_id, rtype, url, headers=None, mirrors=None, size=None,
                 filename=None, status="pending", local_path=None, hash_value=None):
    """新增资源。

    mirrors:   备用下载点 URL 列表, 主 URL 失败时由下载器依次尝试。
    size:      采集阶段已知的大小(HEAD 探测所得), 可为空。
    filename:  采集器建议的相对输出路径, 见 core/naming.py。留空则由
               下载层回退到 URL 末段。
    status/local_path/hash_value: 增量续采复用历史成果时直接以"已完成"入库,
               避免重复下载已经躺在磁盘上的同一份内容。
    """
    cur = execute(
        "INSERT INTO resources(task_id, type, url, headers, mirrors, size, filename,"
        " status, local_path, hash, created_time)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            rtype,
            url,
            json.dumps(headers or {}, ensure_ascii=False),
            json.dumps(mirrors or [], ensure_ascii=False),
            size,
            filename,
            status,
            local_path,
            hash_value,
            _now(),
        ),
    )
    return cur.lastrowid


def find_done_resource(url):
    """增量续采: 查找历史上已成功下载的同一个 URL(跨任务)。

    只看 status='done' 且 local_path 非空的记录 —— 之前失败/被过滤的记录不该
    阻止这次重新尝试。多个任务都下过同一个 URL 时取最近一条(文件最新)。
    """
    return query_one(
        "SELECT * FROM resources WHERE url=? AND status='done'"
        " AND local_path IS NOT NULL ORDER BY id DESC LIMIT 1",
        (url,),
    )


def count_resources(task_id):
    """按状态统计资源数量, 返回 dict(不存在的状态不出现在结果里)。"""
    rows = query(
        "SELECT status, COUNT(*) AS n FROM resources WHERE task_id=? GROUP BY status",
        (task_id,),
    )
    return {r["status"]: r["n"] for r in rows}


def count_tasks_by_status():
    """按状态统计任务数量, 返回 dict。

    供"批量清理"界面做预检: 先让用户看到"将删除 12 个任务", 而不是点完
    才发现删错了一批。
    """
    rows = query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def iter_tasks_with_status(statuses):
    """按状态批量取任务(供批量删除使用)。"""
    if not statuses:
        return []
    marks = ",".join("?" for _ in statuses)
    return query(
        f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", tuple(statuses)
    )


def count_downloaded(task_id):
    """本次**真正下载**的资源数(排除增量复用的)。

    判据是 resolved_url 非空: 只有真正发过请求并落盘的记录才会由下载层回填
    它。复用历史成果的行直接以 status=done 入库, 没有这项。
    """
    row = query_one(
        "SELECT COUNT(*) AS n FROM resources WHERE task_id=? AND status='done'"
        " AND resolved_url IS NOT NULL",
        (task_id,),
    )
    return row["n"] if row else 0


def get_mirrors(res):
    """取出资源的备用下载点, 始终返回 list。

    注意: 查询结果是 sqlite3.Row, 它不支持 dict.get(), 只能下标取值。
    """
    if not res:
        return []
    try:
        raw = res["mirrors"]
    except (KeyError, IndexError, TypeError):
        return []
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return list(v) if isinstance(v, (list, tuple)) else []
    except (ValueError, TypeError):
        return []


def get_resources(task_id):
    return query("SELECT * FROM resources WHERE task_id=? ORDER BY id", (task_id,))


def get_resource(resource_id):
    return query_one("SELECT * FROM resources WHERE id=?", (resource_id,))


def update_resource(resource_id, **fields):
    if not fields:
        return
    cols = ",".join(f"{k}=?" for k in fields)
    execute(f"UPDATE resources SET {cols} WHERE id=?", (*fields.values(), resource_id))


def find_by_hash(hash_value):
    """hash 去重: 查找已成功下载的同内容资源。"""
    return query_one(
        "SELECT * FROM resources WHERE hash=? AND status='done' LIMIT 1", (hash_value,)
    )


# ---- logs ----

def add_log(task_id, message, level="info"):
    execute(
        "INSERT INTO task_logs(task_id, level, message, created_time) VALUES(?,?,?,?)",
        (task_id, level, message[:2000], _now()),
    )


def get_logs(task_id):
    return query("SELECT * FROM task_logs WHERE task_id=? ORDER BY id", (task_id,))


# ---- watches (订阅巡检) ----

DEFAULT_INTERVAL_MINUTES = 360


def create_watch(url, collector, interval_minutes=None, options=None, run_now=True):
    """新增订阅源。

    run_now=True 时 next_run 设为当前时刻 —— 订阅后立刻采一轮, 用户不必等到
    第一个周期结束才能确认配置有没有写对。
    """
    # 注意别写成 `interval_minutes or DEFAULT`: 0 是 falsy, 那样会把用户明确
    # 写的 0 悄悄变成默认 360 分钟 —— 而他期待的是"不合法, 至少给个 1 分钟"。
    minutes = (
        DEFAULT_INTERVAL_MINUTES if interval_minutes is None else int(interval_minutes)
    )
    minutes = max(1, minutes)
    now = _now()
    cur = execute(
        "INSERT INTO watches(url, collector, interval_minutes, options, enabled,"
        " next_run, created_time) VALUES(?,?,?,?,?,?,?)",
        (
            url,
            collector,
            minutes,
            json.dumps(options or {}, ensure_ascii=False),
            1,
            now if run_now else _minutes_later(minutes),
            now,
        ),
    )
    return cur.lastrowid


def _minutes_later(minutes, now=None):
    """在给定时刻上加 N 分钟。

    now 可能是 datetime, 也可能是 _now() 产出的字符串(调用方常直接把刚才的
    时间戳传回来求下一轮), 两种都得接住 —— 早期版本没考虑后者, str + timedelta
    直接 TypeError, 而这条路径只有在真的要顺延时才走到, 很容易漏测。
    """
    if isinstance(now, str):
        try:
            base = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            base = datetime.now()
    else:
        base = now or datetime.now()
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def list_watches():
    return query("SELECT * FROM watches ORDER BY id DESC")


def get_watch(watch_id):
    return query_one("SELECT * FROM watches WHERE id=?", (watch_id,))


def set_watch_enabled(watch_id, enabled):
    return execute(
        "UPDATE watches SET enabled=? WHERE id=?", (1 if enabled else 0, watch_id)
    ).rowcount


def delete_watch(watch_id):
    return execute("DELETE FROM watches WHERE id=?", (watch_id,)).rowcount


def due_watches(limit=10):
    """取到期未跑的订阅源。"""
    return query(
        "SELECT * FROM watches WHERE enabled=1 AND next_run<=? ORDER BY next_run"
        " LIMIT ?",
        (_now(), limit),
    )


def claim_watch(watch_id, minutes):
    """抢占到期源并顺延下一轮时间, 返回是否抢到。

    用乐观锁(update ... where id=? and next_run<=?)而不是"先查后改":
    调度线程与手动触发可能同时对同一个源下手, 否则会重复创建任务。
    """
    now = _now()
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE watches SET last_run=?, next_run=? WHERE id=? AND next_run<=?",
            (now, _minutes_later(minutes, now), watch_id, _now()),
        )
        conn.commit()
        return cur.rowcount == 1


def finish_watch_run(watch_id, task_id, hits):
    """巡检结束后回填: 最近一次任务 ID 与本次新增数量。"""
    execute(
        "UPDATE watches SET last_task_id=?, hits=hits+? WHERE id=?",
        (task_id, hits or 0, watch_id),
    )
