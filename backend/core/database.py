import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.config import settings

DB_PATH = settings.db_path

# ---- 并发写保护 ----
# ⚠️ WAL 只解决了"读不阻塞写", **写-写仍然是单写者**。多个实例/进程共用一个
#    SQLite 文件时会撞 "database is locked"; 而这个异常一旦落在业务 try 里,
#    就会被当成"这个资源下载失败", 报错离真相极远 —— 表现为任务莫名其妙
#    failed, 日志里只有一堆 locked。所以:
#      ① 连接给足等待时间(默认 5s 太短, 大事务期间必撞);
#      ② 真撞上了只对 locked/busy 退避重试, 其它异常一律照原样往上抛
#         —— 把 SQL 语法错误也重试一遍只会掩盖真正的 bug。
DB_TIMEOUT = float(os.getenv("UWC_DB_TIMEOUT", "30"))
DB_WRITE_RETRIES = int(os.getenv("UWC_DB_WRITE_RETRIES", "5"))

# 每个任务保留的日志条数(0 = 不裁剪)与"每写几条检查一次"
LOG_KEEP_PER_TASK = int(os.getenv("UWC_LOG_KEEP", "2000"))
LOG_TRIM_EVERY = int(os.getenv("UWC_LOG_TRIM_EVERY", "50"))

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
    resolved_url TEXT,
    -- 感知指纹与"疑似重复"(见 core/phash.py): 只标记, 绝不自动删除
    phash TEXT,
    duplicate_of INTEGER
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
CREATE INDEX IF NOT EXISTS idx_resources_path ON resources(local_path);
CREATE INDEX IF NOT EXISTS idx_resources_filename ON resources(filename);
CREATE INDEX IF NOT EXISTS idx_logs_task ON task_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_url ON tasks(url);
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
    # 感知指纹(dHash, 16 位十六进制)与"疑似与哪条资源重复"(指向同任务的
    # resources.id)。见 core/phash.py —— **只标记不删除**: 指纹会误判,
    # 删文件是不可逆的, 而出错的代价由用户承担。
    ("resources", "phash", "TEXT"),
    ("resources", "duplicate_of", "INTEGER"),
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
        _conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT,
                                check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(f"PRAGMA busy_timeout={int(DB_TIMEOUT * 1000)}")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.executescript(INDEXES)
        _conn.commit()
    return _conn


def _is_locked(exc):
    """这个异常是不是"别人正在写" —— 只有这类错才值得重试。"""
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _retry_write(fn):
    """在持有 _lock 的前提下执行写操作, 撞锁时退避重试。

    重试放在锁**内**: 一旦放锁, 别的线程会插进来跟我们抢同一个写锁,
    反而更难收敛。退避总时长被压在 1 秒量级, 持锁等待是可接受的。
    """
    last = None
    for attempt in range(DB_WRITE_RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            if not _is_locked(e) or attempt == DB_WRITE_RETRIES - 1:
                raise
            # 指数退避 + 抖动: 与下载层同理, 纯指数会让多个等待者同时重试
            time.sleep(min(0.05 * 2 ** attempt, 0.4) * random.uniform(0.6, 1.4))
    raise last


def execute(sql, params=()):
    with _lock:
        def _run():
            conn = get_conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        return _retry_write(_run)


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


def _like_pattern(s):
    """把用户输入的关键词转成 LIKE 模式: 转义通配符, 两侧补 %。

    ⚠️ 不转义就会出"搜索能用但结果不对"这种最难怀疑的偏差: 搜 `100%` 里的
    `%` 会被当成"匹配任意串", 搜 `a_b` 里的 `_` 会连 `axb` 一起捞出来。
    用户看到的只是"怎么多出来几条", 不会想到是自己输入的字符被吃掉了。
    """
    esc = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _task_filters(q=None, status=None):
    """搜索与状态筛选的 WHERE 片段。两个查询(取列表 / 数总数)共用一份,
    否则"分页显示 12 条"和"共 15 条"迟早对不上。"""
    where, args = [], []
    if status:
        vals = [v for v in (status if isinstance(status, (list, tuple, set)) else [status]) if v]
        if vals:
            where.append(f"status IN ({','.join('?' for _ in vals)})")
            args.extend(vals)
    if q and str(q).strip():
        # 同时匹配 URL 与任务名: 用户手上的线索常常只有一半(记得相册名不记得
        # 链接, 或反过来)。name 为 NULL 时 LIKE 结果是 NULL, 自然不匹配 —— 正确。
        where.append("(url LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
        pat = _like_pattern(str(q).strip())
        args.extend([pat, pat])
    return where, args


def list_tasks(q=None, status=None, limit=None, offset=0):
    """任务列表(搜索 / 状态筛选 / 分页), 按 id 倒序。

    三个参数都可省略(省略 = 不限制), 老调用点不用改。

    ⚠️ 分页要配**稳定排序**, 否则同一页刷新两次顺序可能不同 —— 用户会以为
    列表在乱跳。这里固定 id DESC; 翻页期间有新任务插入仍会让 OFFSET 整体
    位移(任务本来就在持续新增), 这是分页固有的, 不假装能解决。
    """
    where, args = _task_filters(q, status)
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    if limit is not None:
        # 负数 OFFSET 在 SQLite 里表示"从末尾往前数", 静默换个含义 ——
        # 前端算出负页码时不能让它悄悄成立
        sql += " LIMIT ? OFFSET ?"
        args.extend([max(int(limit), 0), max(int(offset or 0), 0)])
    return query(sql, tuple(args))


def count_tasks(q=None, status=None):
    """满足同样筛选条件的任务总数(分页要用)。"""
    where, args = _task_filters(q, status)
    sql = "SELECT COUNT(*) AS n FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = query_one(sql, tuple(args))
    return int(row["n"]) if row else 0


def find_tasks_by_urls(urls):
    """批量查这些 URL 是否已建过任务, 返回 {url: 最新任务 id}。

    一次查完而不是逐条查: 批量创建时逐条查就是 N 次扫描, 而这是一次能拿完的。
    同一 URL 可能被建过多次(上次失败又建了一次), 取**最新**那条 —— 用户想
    对照的是最近那次的结果。
    """
    keys = [u for u in urls if u]
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    rows = query(
        f"SELECT url, MAX(id) AS id FROM tasks WHERE url IN ({marks}) GROUP BY url",
        tuple(keys),
    )
    return {r["url"]: r["id"] for r in rows}


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
        def _run():
            conn = get_conn()
            cur = conn.execute(
                "UPDATE tasks SET status=? WHERE id=? AND status=?",
                (new_status, task_id, expected_status),
            )
            conn.commit()
            return cur.rowcount == 1
        return _retry_write(_run)


def transition_task_from_any(task_id, new_status, allowed_statuses):
    """从任一允许状态迁移到新状态(用于取消/看门狗)。"""
    ph = ",".join("?" for _ in allowed_statuses)
    with _lock:
        def _run():
            conn = get_conn()
            cur = conn.execute(
                f"UPDATE tasks SET status=? WHERE id=? AND status IN ({ph})",
                (new_status, task_id, *allowed_statuses),
            )
            conn.commit()
            return cur.rowcount == 1
        return _retry_write(_run)


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


def find_resource_by_path(local_path):
    """按落盘路径反查资源(跨任务): 判断"这一格现在归谁"。

    布局规则消解重名时必须知道盘上那个文件是哪条 URL 下的 —— 只按"同名就复用"
    是不行的: 下载器会把已有文件当成"同一个文件的半成品"接管续传, 不同来源的
    同名文件会被拼成一份产物还报成功(见 core/layout.py 的警告)。
    库里有记录才能分辨"同一张图重下一次"与"另一个文件恰好重名"。
    """
    if not local_path:
        return None
    return query_one(
        "SELECT * FROM resources WHERE local_path=? ORDER BY id DESC LIMIT 1",
        (str(local_path),),
    )


def find_resource_by_filename(filename):
    """按**计划落盘路径**(resources.filename)反查资源(跨任务)。

    与 `find_resource_by_path` 的分工: 那条查的是"已在盘上的绝对路径", 这条查的
    是"计划放哪" —— 覆盖**还没下载完**的占位(pending/downloading)以及下载时被
    换了扩展名(镜像回退 .jpg->.webp)的行。重名消解两个都要看: 只看盘上有没有,
    两个任务并发跑同一个相册时会各自认为自己是第一个。
    """
    if not filename:
        return None
    return query_one(
        "SELECT * FROM resources WHERE filename=? ORDER BY id DESC LIMIT 1",
        (str(filename),),
    )


def find_place_owner(relative, local_path=None):
    """这一格(相对落盘路径)现在归谁: 返回 resources 行, 空着返回 None。

    先看绝对路径(盘上真有的那个最权威), 再看计划名。两步都有索引(见 INDEXES),
    取重名消解时的"占用者", 见 core/layout.py::claim。
    """
    row = find_resource_by_path(local_path) if local_path else None
    return row or find_resource_by_filename(relative)


def count_place_refs(relative, local_path=None, exclude_task=None):
    """除 `exclude_task` 外, 还有几个任务指向这一格。

    删除任务的磁盘文件前用它判定"这份文件是不是只有我用": 内容去重会让别的任务
    复用同一个文件(见 task_manager._purge_files), 删掉它等于把那些任务的结果
    一并毁掉。
    """
    sql = ("SELECT COUNT(*) AS n FROM resources WHERE (local_path=? OR filename=?)")
    args = [str(local_path or ""), str(relative or "")]
    if exclude_task is not None:
        sql += " AND task_id<>?"
        args.append(exclude_task)
    row = query_one(sql, tuple(args))
    return int(row["n"]) if row else 0


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


def count_tasks_by_collector():
    """按采集器统计任务数量, 返回 dict。供统计面板分组展示。"""
    rows = query("SELECT collector, COUNT(*) AS n FROM tasks GROUP BY collector")
    return {r["collector"]: r["n"] for r in rows}


def count_resources_total():
    """全部资源记录数(跨任务)。"""
    row = query_one("SELECT COUNT(*) AS n FROM resources")
    return row["n"] if row else 0


def sum_downloaded_bytes():
    """已成功下载资源的体积合计(字节)。"""
    row = query_one(
        "SELECT COALESCE(SUM(size), 0) AS n FROM resources "
        "WHERE status='done' AND size IS NOT NULL"
    )
    return row["n"] if row else 0


def get_resources_with_status(task_id, statuses):
    """取出某任务下处于指定状态的所有资源(供"重试失败资源"等)。"""
    if not statuses:
        return []
    marks = ",".join("?" for _ in statuses)
    return query(
        f"SELECT * FROM resources WHERE task_id=? AND status IN ({marks}) ORDER BY id",
        (task_id, *statuses),
    )


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


def task_phashes(task_id, exclude_id=None):
    """本任务内已算出指纹的成功资源, 返回 [(resource_id, phash), ...]。

    ⚠️ 只在**同一个任务内**比对。跨任务比对看着更"彻底", 但会让提示语变成
    "与任务 #37 的第 12 张重复" —— 用户点不过去、也删不掉, 等于给了一条
    无法行动的信息。而同一个相册内部才是真正的重复高发区。
    """
    rows = query(
        "SELECT id, phash FROM resources WHERE task_id=? AND status='done'"
        " AND phash IS NOT NULL" + (" AND id<>?" if exclude_id else ""),
        (task_id, exclude_id) if exclude_id else (task_id,),
    )
    return [(r["id"], r["phash"]) for r in rows]


# ---- logs ----

def add_log(task_id, message, level="info"):
    cur = execute(
        "INSERT INTO task_logs(task_id, level, message, created_time) VALUES(?,?,?,?)",
        (task_id, level, message[:2000], _now()),
    )
    # 每 LOG_TRIM_EVERY 条裁一次: 一个几百资源的任务会写上千行日志, 任务删掉后
    # 这些行不会自己消失 —— 库会单调膨胀, "取最新 N 条"也会越来越慢。
    # 按行号取样而不是每写一条都裁, 是为了不让日志写入变成两次查询。
    if LOG_KEEP_PER_TASK > 0 and cur.lastrowid % LOG_TRIM_EVERY == 0:
        trim_logs(task_id)


def trim_logs(task_id, keep=None):
    """只保留每个任务最近的 keep 条日志。"""
    keep = keep if keep is not None else LOG_KEEP_PER_TASK
    if keep <= 0:
        return
    execute(
        "DELETE FROM task_logs WHERE task_id=? AND id NOT IN"
        " (SELECT id FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?)",
        (task_id, task_id, keep),
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
