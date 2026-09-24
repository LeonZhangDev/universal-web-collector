import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta

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
    -- 媒体元数据: 图片实测宽高 / 视频容器时长(秒)。
    -- ⚠️ 这两个数字**本来就被算出来过**, 只是随手丢了: 尺寸终检在
    -- `filters.need_dimensions` 时已经调过 `image_dimensions()`, 直链与 HLS 的
    -- 内容终检也早就用 ffprobe 量过时长 —— 都用完即弃。落库后它们才产生价值:
    -- 可以按分辨率筛选/排序、列表直接显示尺寸、认出 0x0 之类的坏产物。
    -- 没装 ffprobe 时 duration 留 NULL: **缺探测器不等于文件坏**(见
    -- `downloaders/video.py::_resolve_ffprobe` 那段两种 None 的区分)。
    width INTEGER,
    height INTEGER,
    duration REAL,
    note TEXT,
    mirrors TEXT,
    filename TEXT,
    content_type TEXT,
    resolved_url TEXT,
    -- 感知指纹与"疑似重复"(见 core/phash.py): 只标记, 绝不自动删除
    phash TEXT,
    duplicate_of INTEGER,
    -- 失败的机器可读分类: gone/forbidden/corrupt/disk/ratelimit/server/
    -- network/unknown/missing。见 core/errors.py 的 classify() —— 界面与"按原因重试"
    -- 都读这一列, 不再靠正则解析 note 文案。
    error_kind TEXT,
    -- 资源级遥测(epoch 秒浮点): "这个任务为什么慢" 的唯一可靠依据。
    -- ⚠️ 用 REAL 而不是 created_time 那种秒级字符串: 单张图常常几百毫秒,
    -- 秒级精度会把 0.4s 与 1.4s 记成同一件事, 遥测就白做了。
    started_at REAL,
    finished_at REAL,
    -- 这是第几次真正发起下载。与 tasks.retry_count(任务级) 不是一回事:
    -- 单个资源的失败重试同样会让它 +1, 是"这条为什么一直不成功"的证据。
    attempts INTEGER NOT NULL DEFAULT 0,
    -- 条件请求凭据(HTTP 校验器), 见 downloaders/base.py 的 _stream_one。
    -- 存下来有两个用处: ① 重试时带 If-None-Match, 源站回 304 -> 不传字节;
    -- ② 全量重下后比对 etag, 能发现"同一个 URL 的内容被源站换掉了"
    -- —— 增量模式原来只按 URL 判断"下过就复用", 对此完全无感。
    etag TEXT,
    last_modified TEXT,
    -- 收藏(0/1)。与"标签"分开是刻意的: 收藏是**一个布尔属性**(排序、计数、批量
    -- 切换都很直接), 而标签是**多值**。把它也做成一个特殊标签的话, "星标"和
    -- "标签"两套 UI 就得共用一条数据通路, 取值冲突时要额外规则去仲裁。
    favorite INTEGER NOT NULL DEFAULT 0
);

-- 资源标签(多对多)。⚠️ 用**明细表**而不是 resources 里的一列:
-- 一列只能靠分隔符拼串, 于是"按标签筛"退化成 LIKE '%x%' —— 既走不了索引,
-- 又会把 "sunset" 命中 "sunset-beach", 而且要统计"有哪些标签"还得先全表扫一遍再拆串。
CREATE TABLE IF NOT EXISTS resource_tags(
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    -- COLLATE NOCASE: 让 "Sunset" 与 "sunset" 视为同一个标签。SQLite 的 NOCASE
    -- 只折叠 ASCII —— 中文标签本来就没有大小写, 这个选择两边都合适。
    -- 列上声明一次, 主键去重、筛选比较、GROUP BY 统计就全都跟着走, 不必每处
    -- 手写 COLLATE(漏一处就会出现"统计里两个、筛出来一个")。
    tag TEXT NOT NULL COLLATE NOCASE,
    created_time TEXT NOT NULL,
    PRIMARY KEY (resource_id, tag)
);

-- 标签自身的外观(目前只有颜色)。⚠️ 与 resource_tags **分表**的道理:
-- 颜色属于"标签"这个对象, 不属于"某个资源带了某个标签"这一行。放进 resource_tags
-- 就是同一个标签在 N 个资源上各存一份, 改一次色要改 N 行 —— 少改一行就是
-- "同一个标签在这里是红的, 在那里是蓝的"。层级则更轻: 直接用名字里的 `/`
-- 表达(见 tag_parent), 不另立树表 —— 一张只有父子两列的表撑不起移动/重命名,
-- 而名字本身已经携带了全部信息。
CREATE TABLE IF NOT EXISTS tag_meta(
    -- 与 resource_tags.tag 同样 NOCASE: 否则 "Sunset" 和 "sunset" 会有两条颜色
    tag TEXT PRIMARY KEY COLLATE NOCASE,
    -- 存**调色板键**(如 "red")而不是色值: 色值是渲染细节, 改了要回头迁移全表;
    -- 键还能被校验(见 normalize_color), 存色值的写法没法拦住 `#zzz`
    color TEXT NOT NULL DEFAULT '',
    updated_time TEXT NOT NULL
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

-- 任务结算通知: 任务进入终态(success/partial/failed)时写入, 供前端通知中心拉取。
-- 也可被 SMTP 钩子消费(若配置了 UWC_SMTP_*)。
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    level TEXT NOT NULL DEFAULT 'info',   -- info | success | warning | error
    title TEXT NOT NULL,
    body TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    created_time TEXT NOT NULL
);

-- 本地相册集的**登记表**(见 core/localalbums.py)。只有"用户显式注册过哪个目录"
-- 需要落库 —— 目录里有什么照片是本机文件系统的事, 每次现扫即可, 不建索引表。
--
-- ⚠️ 这两张新表的时刻列是 **`created_at` / `last_scan` + REAL(epoch 秒)**, 不是
-- 老表那种 `created_time` + `'%Y-%m-%d %H:%M:%S'` 字符串。理由: 读它们的每一处
-- 都要拿来做算术(和 now 比、排序、算 TTL), 而格式化字符串只能先解析回来; 更要紧的
-- 是名字里带 `time` 的列会被下一个人当成老约定, **类型才是不会说谎的那一半**。
--
-- ⚠️ 为什么不复用 tasks/resources 那张表: 本地相册是**只读的既有文件**, 而
-- resources 的每一行都代表"本程序生产的一个产物" —— 它被 bulk-delete 的
-- `_purge_files` 当作"可以删的文件"处理。把用户的照片登记进 resources, 就等于
-- 把"删我下过的东西"和"删用户自己的东西"合成同一个语义。分开是唯一安全的做法。
CREATE TABLE IF NOT EXISTS local_roots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 绝对路径。⚠️ **不加 UNIQUE**: Windows 路径大小写不敏感、Linux 敏感, 而
    -- SQLite 既没有"平台相关的 NOCASE"也无法表达这个区别。去重放在 Python 里
    -- 用 os.path.normcase 判(见 localalbums.add_root) —— 那样两边都对,
    -- 而一个写死的 UNIQUE 只会在其中一边形成假判据。
    path TEXT NOT NULL,
    name TEXT,
    created_at REAL NOT NULL,
    -- 最近一次扫描完成时刻(epoch 秒)与那次扫出来的规模。存下来是为了让界面
    -- 能说"这个数字是几点几分核出来的" —— 不含时间戳的统计数字过一会儿就是谎言。
    -- NULL = **从来没扫过**(与"扫过、结果是 0 张"是两件必须分得开的事)。
    last_scan REAL,
    album_count INTEGER NOT NULL DEFAULT 0,
    photo_count INTEGER NOT NULL DEFAULT 0,
    -- 扫描时的异常(目录被拔了/权限不足)。**留着不吞**: 一个读不到的根如果
    -- 只表现为"0 张照片", 用户会以为是自己目录是空的。
    error TEXT
);

-- 本地照片的收藏。⚠️ 按**绝对路径**记账, 不按 (root_id, rel):
-- 同一个目录可以既是根 A 的内容、又是根 B 的子目录, 按 (root_id, rel) 会变成
-- 同一张照片两条收藏; 而路径是这张照片在磁盘上的唯一身份。
-- 代价要说清楚: 用户**把文件改名/移走**之后收藏就断了 —— 这是已知且可接受的,
-- 因为反过来(按 root+rel)在目录整体挪位置时断得更彻底。
CREATE TABLE IF NOT EXISTS local_favorites(
    path TEXT PRIMARY KEY,
    created_at REAL NOT NULL
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
-- 标签是"按标签找资源"的入口, 没有这条索引时每次筛选都是全表扫 tags
CREATE INDEX IF NOT EXISTS idx_tags_tag ON resource_tags(tag);
CREATE INDEX IF NOT EXISTS idx_resources_favorite ON resources(favorite);
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
    # 失败的**机器可读**分类(gone/forbidden/corrupt/disk/ratelimit/server/
    # network/unknown/missing, 见 core/errors.py 的 classify)。界面此前靠正则去解析
    # note 文案来归类失败原因 —— 那是拿人看的字当数据用, 文案一改归类就静默失效, 而且
    # 没法支持"按原因筛选/批量重试"。
    ("resources", "error_kind", "TEXT"),
    # 资源级遥测: 起止时刻(epoch 秒)与实际尝试次数。没有它们, "这个任务为什么慢"
    # 只能看到一个聚合后的总时长, 无从下判断(哪个资源慢、是慢还是卡在重试)。
    ("resources", "started_at", "REAL"),
    ("resources", "finished_at", "REAL"),
    ("resources", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    # 条件请求凭据(ETag / Last-Modified), 见 downloaders/base.py。
    ("resources", "etag", "TEXT"),
    ("resources", "last_modified", "TEXT"),
    # 收藏(0/1)。标签另立 resource_tags 明细表(见 SCHEMA) —— 那是多值, 放列里
    # 就只能拼串, 筛选和统计都会退化成全表扫 + 拆串。
    ("resources", "favorite", "INTEGER NOT NULL DEFAULT 0"),
    # V37: 媒体元数据(图片宽高 / 视频时长)。旧库靠这里补齐, 新库由 SCHEMA 直接建。
    ("resources", "width", "INTEGER"),
    ("resources", "height", "INTEGER"),
    ("resources", "duration", "REAL"),
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


def now_ts():
    """当前时刻(epoch 秒, 浮点) —— 资源级遥测专用。

    ⚠️ 刻意不复用 `_now()`: 那个是秒级字符串, 用来给人看时间点; 遥测要的是
    **时长**, 单张图常常只有几百毫秒, 秒级精度会把 0.4s 和 1.4s 记成同一件事。
    两种时间观混用是"数据看着有、用时发现没用"的典型来源, 所以分开两个函数。
    """
    return time.time()


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


def _like_pattern(s, prefix=""):
    """把用户输入的关键词转成 LIKE 模式: 转义通配符, 两侧补 %(`prefix` 则只补右)。

    ⚠️ 不转义就会出"搜索能用但结果不对"这种最难怀疑的偏差: 搜 `100%` 里的
    `%` 会被当成"匹配任意串", 搜 `a_b` 里的 `_` 会连 `axb` 一起捞出来。
    用户看到的只是"怎么多出来几条", 不会想到是自己输入的字符被吃掉了。
    标签层级的前缀匹配(`tag_children`)走的是同一条路 —— 标签是用户自己起的
    名字, 里面出现 `%`/`_` 这种事迟早会发生。
    """
    esc = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{esc}{prefix}%" if prefix else f"%{esc}%"


def _task_filters(q=None, status=None, collector=None):
    """搜索与状态筛选的 WHERE 片段。两个查询(取列表 / 数总数)共用一份,
    否则"分页显示 12 条"和"共 15 条"迟早对不上。"""
    where, args = [], []
    if status:
        vals = [v for v in (status if isinstance(status, (list, tuple, set)) else [status]) if v]
        if vals:
            where.append(f"status IN ({','.join('?' for _ in vals)})")
            args.extend(vals)
    if collector and str(collector).strip() and str(collector).strip() != "auto":
        # 采集器筛选: 精确匹配 tasks.collector。刻意排除 "auto" —— 它是"让系统
        # 自己识别"的**输入意图**, 不是落库后的采集器名, 拿它筛必然 0 条。
        where.append("collector = ?")
        args.append(str(collector).strip())
    if q and str(q).strip():
        # 同时匹配 URL 与任务名: 用户手上的线索常常只有一半(记得相册名不记得
        # 链接, 或反过来)。name 为 NULL 时 LIKE 结果是 NULL, 自然不匹配 —— 正确。
        where.append("(url LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
        pat = _like_pattern(str(q).strip())
        args.extend([pat, pat])
    return where, args


def list_tasks(q=None, status=None, collector=None, limit=None, offset=0):
    """任务列表(搜索 / 状态筛选 / 采集器筛选 / 分页), 按 id 倒序。

    参数都可省略(省略 = 不限制), 老调用点不用改。

    ⚠️ 分页要配**稳定排序**, 否则同一页刷新两次顺序可能不同 —— 用户会以为
    列表在乱跳。这里固定 id DESC; 翻页期间有新任务插入仍会让 OFFSET 整体
    位移(任务本来就在持续新增), 这是分页固有的, 不假装能解决。
    """
    where, args = _task_filters(q, status, collector)
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


def count_tasks(q=None, status=None, collector=None):
    """满足同样筛选条件的任务总数(分页要用)。"""
    where, args = _task_filters(q, status, collector)
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


def find_tasks_by_content_key(content_key):
    """Return all exact matches newest-first, lazily keying historical rows."""
    from core.content_identity import canonical_content_key

    matches = []
    for task in query("SELECT * FROM tasks ORDER BY id DESC"):
        try:
            options = json.loads(task["options"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(options, dict):
            continue
        stored_key = options.get("content_key")
        computed_key = canonical_content_key(task["url"], task["collector"])
        if stored_key != computed_key:
            if computed_key is None:
                options.pop("content_key", None)
            else:
                options["content_key"] = computed_key
            execute(
                "UPDATE tasks SET options=? WHERE id=?",
                (json.dumps(options, ensure_ascii=False), task["id"]),
            )
        if computed_key == content_key:
            matches.append(task)
    return matches


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


def summarize_resources(task_id):
    """返回任务资源总数与面向任务摘要的终态计数。

    ⚠️ `failed` 把 `gone` **算进去**: 任务摘要只需要回答"成没成", 而"源站已经没有
    这个资源"与"下载失败"对任务整体是同一件事(要 56 张只拿到 0 张)。分开的那一份
    单独放在 `gone` 里, 给界面做更细的提示用。这里的口径必须与
    `task_manager._final_status` 一致, 否则会出现"摘要说 0 失败、状态却是 failed"。
    """
    by_status = count_resources(task_id)
    failed = by_status.get("failed", 0)
    gone = by_status.get("gone", 0)
    return {
        "total": sum(by_status.values()),
        "done": by_status.get("done", 0),
        "failed": failed + gone,
        "filtered": by_status.get("filtered", 0),
        "gone": gone,
    }


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


# ---- 通知 ----
def add_notification(task_id, level, title, body=None):
    """写入一条任务结算通知。调用方已在 try 内, 写失败不影响任务本身。"""
    execute(
        "INSERT INTO notifications(task_id, level, title, body, created_time) "
        "VALUES (?,?,?,?,?)",
        (task_id, level, title, body, _now()),
    )
    return get_conn().execute("SELECT last_insert_rowid()").fetchone()[0]


def list_notifications(limit=50, unread_only=False):
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read=0"
    sql += " ORDER BY id DESC LIMIT ?"
    return query(sql, (limit,))


def unread_notification_count():
    row = query_one("SELECT COUNT(*) AS n FROM notifications WHERE read=0")
    return row["n"] if row else 0


def mark_notifications_read(ids=None):
    if ids:
        marks = ",".join("?" for _ in ids)
        execute(f"UPDATE notifications SET read=1 WHERE id IN ({marks})", tuple(ids))
    else:
        execute("UPDATE notifications SET read=1")


# ---- 统计聚合(供首页图表) ----
def resources_by_date(days=30):
    """按日期统计每日新增资源数(done, 用资源创建日期近似下载日期)。"""
    rows = query(
        "SELECT substr(created_time,1,10) AS d, COUNT(*) AS n "
        "FROM resources WHERE created_time >= date('now', ?) "
        "GROUP BY d ORDER BY d",
        (f"-{days} days",),
    )
    return {r["d"]: r["n"] for r in rows}


def failure_reasons(limit=8):
    """失败资源的 note 聚合(去重计数), 帮用户判断要不要换代理/采集器。"""
    rows = query(
        "SELECT note, COUNT(*) AS n FROM resources "
        "WHERE status IN ('failed','skipped') AND note IS NOT NULL AND note <> '' "
        "GROUP BY note ORDER BY n DESC LIMIT ?",
        (limit,),
    )
    return [{"reason": r["note"], "count": r["n"]} for r in rows]


def error_kinds():
    """按 `error_kind` 聚合 —— "哪一类问题最多"的可靠答案。

    ⚠️ 与 `failure_reasons()` 的分工要说清: 那个按 note **全文**分组, 回答
    "具体报了什么"(每条 note 里都带着各自的 URL, 所以真实数据上几乎每组只有 1 条,
    聚不出东西); 这个按固定的 kind 取值分组, 回答"该换代理还是该改采集器"。

    统计**所有**带 error_kind 的资源, 不按 status 过滤 —— 因为 `corrupt` 也可能
    落在 `done` 上(文件保留、只标记损坏, 见 core/phash.py 的约束 1), 那正是最需要
    被看见的一类。
    """
    rows = query(
        "SELECT error_kind AS kind, COUNT(*) AS n FROM resources "
        "WHERE error_kind IS NOT NULL AND error_kind <> '' "
        "GROUP BY error_kind ORDER BY n DESC"
    )
    return [{"kind": r["kind"], "count": r["n"]} for r in rows]


def duplicate_stats():
    """感知去重报表: 被标记 duplicate_of 的资源数与省下的体量。"""
    row = query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
        "FROM resources WHERE duplicate_of IS NOT NULL"
    )
    return {"marked": row["n"] if row else 0, "bytes_saved": row["bytes"] if row else 0}


def stats_download_series(days=30):
    """按日期聚合已下载资源体积(字节), 与 resources_by_date 配套。"""
    rows = query(
        "SELECT substr(created_time,1,10) AS d, COALESCE(SUM(size),0) AS b "
        "FROM resources WHERE status='done' AND created_time >= date('now', ?) "
        "GROUP BY d ORDER BY d",
        (f"-{days} days",),
    )
    return {r["d"]: r["b"] for r in rows}


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


# ---- 资源级遥测 ----

def resource_timing(task_id, slowest=5):
    """这个任务的**逐资源**耗时画像, 回答"慢在哪"。

    没有它时只有任务级总时长: 一个 120 秒的任务可能是"120 张图各 1 秒", 也可能是
    "119 张各 0.2 秒 + 1 张卡了 95 秒", 而这两种情况的处置完全不同(前者调间隔,
    后者查那一个资源)。`attempts` 一并给出, 用来区分"慢"与"在重试链里空转"。

    只统计 finished_at 与 started_at 都有的行 —— 未完成的资源没有时长可言,
    拿 0 充数会把平均值拉低成一个假象。
    """
    rows = query(
        "SELECT id, type, filename, local_path, status, attempts, started_at,"
        " finished_at FROM resources WHERE task_id=? AND started_at IS NOT NULL"
        " AND finished_at IS NOT NULL",
        (task_id,),
    )
    items = []
    for r in rows:
        try:
            ms = max(0.0, (float(r["finished_at"]) - float(r["started_at"])) * 1000.0)
        except (TypeError, ValueError):
            continue
        items.append(
            {
                "id": r["id"],
                "type": r["type"],
                "status": r["status"],
                "attempts": int(r["attempts"] or 0),
                "ms": round(ms, 1),
                "name": r["filename"] or r["local_path"] or "",
            }
        )
    if not items:
        return {"measured": 0, "avg_ms": 0.0, "max_ms": 0.0, "slowest": [],
                "retried": 0}
    total = sum(i["ms"] for i in items)
    items.sort(key=lambda x: x["ms"], reverse=True)
    return {
        "measured": len(items),
        "avg_ms": round(total / len(items), 1),
        "max_ms": round(items[0]["ms"], 1),
        # 有重试的资源数: 单独给一条, 因为"平均很快但一堆资源重试过"是另一种病
        "retried": sum(1 for i in items if i["attempts"] > 1),
        "slowest": items[: max(1, int(slowest))],
    }


def done_resources_for_scan(task_id=None, limit=None):
    """巡检用: 取出"库里说它下好了"的资源(带路径), 供逐条核对磁盘。

    ⚠️ 只取 status='done' —— 失败/已删的行没有文件可核, 把它们算进分母会让
    "缺失率"这个指标失去意义。`limit` 是为了让大库上的巡检可以分批跑。
    """
    sql = ("SELECT * FROM resources WHERE status='done'"
           " AND local_path IS NOT NULL AND local_path <> ''")
    args = []
    if task_id:
        sql += " AND task_id=?"
        args.append(task_id)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return query(sql, tuple(args))


#: 单个标签的长度上限。32 个字符远超任何"人真的会打"的标签, 存在的意义是挡住
#: 误把整段文本贴进来的情况 —— 那会让标签列表彻底不可用。
MAX_TAG_LEN = 32
#: 一个资源最多带几个标签。同样是防呆: 没有上限时"全选 + 打标签"能瞬间造出
#: 上万行明细, 而用户根本不会去用那么多标签。
MAX_TAGS_PER_RESOURCE = 20

#: 标签层级的**唯一**分隔符。用 `/` 而不是别的: 它天然可读(`系列/角色`),
#: 用户不用学任何东西就能用, 而"层级"这件事本身只是个前缀 —— 不需要一张树表。
TAG_SEP = "/"
#: 最多几层。与 `MAX_TAG_LEN` 同属防呆: 它挡住的是"把一整条路径粘进来"。
MAX_TAG_DEPTH = 4

#: 标签颜色的调色板: 键 -> (中文名, 色值)。
#: ⚠️ **由后端定义并下发**(接口回 `colors`), 与 `error_kind` 的中文标签同一条
#: 理由(第 9 条静默坑): 界面上"人看的文案"只有一个来源, 前端不许自己编一套。
TAG_COLORS = {
    "red": ("红", "#e5484d"),
    "orange": ("橙", "#f76b15"),
    "yellow": ("黄", "#d9a900"),
    "green": ("绿", "#30a46c"),
    "cyan": ("青", "#00a2c7"),
    "blue": ("蓝", "#0091ff"),
    "purple": ("紫", "#8e4ec6"),
    "pink": ("粉", "#e93d82"),
    "gray": ("灰", "#8b8d98"),
}


def normalize_color(color):
    """调色板键归一化。空串 = "没有颜色"(合法的取值, 用来取消已有的颜色)。

    非法取值**抛错**而不是悄悄退回默认色: 用户点了一个不存在的颜色, 界面上却
    变成灰的, 那比报错更难查。
    """
    s = str(color or "").strip().lower()
    if not s:
        return ""
    if s not in TAG_COLORS:
        raise ValueError(
            f"未知的颜色 {s!r}, 可选: {', '.join(TAG_COLORS)}"
        )
    return s


def tag_parent(tag):
    """标签的父标签(名字里最后一个 `/` 之前的部分)。没有 `/` 则是顶层。

    ⚠️ 尾随 `/` 要在归一化阶段就被挡住(见 `normalize_tag`), 否则 `a/` 与 `a`
    是两个标签却互为父子, 层级里会出现一个空的父节点。
    """
    s = str(tag or "")
    i = s.rfind(TAG_SEP)
    return s[:i] if i > 0 else ""


def tag_depth(tag):
    """层级深度(顶层 = 0), 供界面缩进。"""
    s = str(tag or "")
    return s.count(TAG_SEP)


def normalize_tag(tag):
    """标签归一化: 去首尾空白 + 折叠内部空白 + 校验长度。

    ⚠️ **不做小写化**: 那会把用户特意写成 "MacBook" 的标签变成 "macbook"。
    大小写不敏感由列的 `COLLATE NOCASE` 负责(见 SCHEMA), 它管的是"比较时
    等不等价", 与"存成什么样"是两件事 —— 保留原样用户才不会觉得被改了。
    """
    if tag is None:
        return ""
    s = " ".join(str(tag).split())
    if not s:
        return ""
    if len(s) > MAX_TAG_LEN:
        raise ValueError(
            f"标签太长({len(s)} 字), 上限 {MAX_TAG_LEN} 字: {s[:MAX_TAG_LEN]}…"
        )
    if any(ord(c) < 32 for c in s):
        raise ValueError("标签不能包含控制字符")
    # 层级(`/`)的三种坏形状。它们都会让同一件东西有多个写法, 而层级是按名字
    # 算出来的 —— `a/` 与 `a` 互为父子、"a//b" 里有个看不见的空节点。
    parts = s.split(TAG_SEP)
    if any(not p.strip() for p in parts):
        raise ValueError(
            f"标签的层级分隔符 {TAG_SEP!r} 两侧都要有内容(现在是 {s!r}),"
            f" 例如 `系列{TAG_SEP}角色`"
        )
    if len(parts) > MAX_TAG_DEPTH:
        raise ValueError(
            f"标签层级太深(最多 {MAX_TAG_DEPTH} 层): {s}"
        )
    return TAG_SEP.join(p.strip() for p in parts)


def library_filters(q=None, kind=None, task_id=None, album=None, status="done",
                    tag=None, favorite=None, tag_children=False):
    """跨任务资源库的 WHERE 片段(与 task 联表后才能按采集器/相册筛)。

    单独抽出来是为了让 count 与 list 用**完全相同**的条件 —— 两处各写一遍
    是分页错位最常见的来源(总数与页内容口径不一致, 表现为"翻到最后一页
    数量对不上", 很难察觉)。

    status 默认 "done": 资源库只该显示真正落盘的产物。把 failed/pending 也列
    出来会让"库"变成"所有见过的 URL", 那不是浏览而是调试。

    tag / favorite 是**两个独立维度**, 可叠加(既筛标签又只看收藏)。

    tag_children=True 时连**子标签**一起收(点 `系列` 能看到 `系列/角色A`):
    层级是名字里的前缀, 所以这是一次前缀匹配。默认关 —— 精确匹配是老行为,
    也是"我就要这一个标签"时唯一正确的语义。
    """
    where = ["1=1"]
    args = []

    if status:
        where.append("r.status=?")
        args.append(status)
    else:
        # 显式传 None/"" 表示"不限状态"(供调用方需要时使用)
        pass

    if task_id:
        where.append("r.task_id=?")
        args.append(task_id)

    if kind and kind not in ("all", ""):
        where.append("r.type=?")
        args.append(kind)

    if album:
        # 按相册名匹配: 相册名落在 tasks.name, 用精确匹配 —— 模糊匹配会让
        # "ABP-123" 同时命中 "ABP-1234", 用户看到一堆不相干的东西。
        where.append("t.name=?")
        args.append(album)

    if tag:
        # EXISTS 子查询而不是 JOIN resource_tags: JOIN 会让"一个资源带 3 个标签"
        # 变成 3 行, 于是分页数量与总数全错(除非再加 DISTINCT, 而那又会掩盖
        # 真正的重复)。EXISTS 天然只判在不在, 配合 idx_tags_tag 是一次索引探测。
        # ⚠️ 不要手写 `LOWER(tag)=LOWER(?)`: 列的 COLLATE NOCASE 已经在管这件事,
        # 两套写法混用会让"能走索引"变成"函数包住列 → 走不了索引"。
        clean = normalize_tag(tag)
        if tag_children:
            # 含子标签: 前缀匹配(`系列/` 开头)。用 ESCAPE 声明转义符, 与 `%`
            # 的语义分开 —— 否则用户起的 `a_b` 会连带命中的东西一起进来。
            where.append(
                "EXISTS (SELECT 1 FROM resource_tags rt "
                "WHERE rt.resource_id = r.id AND (rt.tag = ? OR rt.tag LIKE ? ESCAPE '\\'))"
            )
            args += [clean, _like_pattern(clean, prefix=TAG_SEP)]
        else:
            where.append(
                "EXISTS (SELECT 1 FROM resource_tags rt "
                "WHERE rt.resource_id = r.id AND rt.tag = ?)"
            )
            args.append(clean)

    if favorite:
        where.append("r.favorite=1")

    if q:
        # 同时搜本地路径与来源 URL: 用户有时记得文件名, 有时只记得站点。
        # ⚠️ `%` `_` 必须转义, 否则搜 "100%" 会变成"匹配任意串"。
        pat = _like_pattern(q)
        where.append("(r.local_path LIKE ? ESCAPE '\\' OR r.url LIKE ? ESCAPE '\\')")
        args.extend([pat, pat])

    return " AND ".join(where), args


def failure_kinds(include_gone=True, include_corrupt=True):
    """按 `error_kind` 统计失败资源(跨任务) —— 死信面板的分布。

    每项给**两个**数字:
      * `n`          —— 这个原因下有多少条(`corrupt` 含 `status='done'` 的那些);
      * `replayable` —— 其中**重放真的能救**的条数(口径 = `_failure_where`)。

    只给一个数字是不够的: 界面会顺手拿它当"即将重放的条数"用, 于是点 `corrupt`
    那一类时"提示 12 条、实际起来 0 条" —— 用户只会以为点了没生效。

    ⚠️ "总数"与"可重放"是两个问题, 但"什么算可重放"只能有**一个定义**, 两条 SQL
    共用 `_failure_where` 构造。
    """
    where, args = _failure_where(None, include_gone)
    replayable = {
        r["kind"]: r["n"]
        for r in query(
            f"SELECT r.error_kind AS kind, COUNT(*) AS n FROM resources r "
            f"WHERE {where} GROUP BY r.error_kind",
            tuple(args),
        )
    }
    seen = ["r.error_kind IS NOT NULL", "r.error_kind <> ''"]
    if not include_gone:
        seen.append("r.error_kind <> 'gone'")
    if not include_corrupt:
        seen.append("r.error_kind <> 'corrupt'")
    rows = query(
        f"SELECT r.error_kind AS kind, COUNT(*) AS n FROM resources r "
        f"WHERE {' AND '.join(seen)} GROUP BY r.error_kind ORDER BY n DESC, kind"
    )
    return [
        {"kind": r["kind"], "n": r["n"], "replayable": replayable.get(r["kind"], 0)}
        for r in rows
    ]


def _failure_where(kinds=None, include_gone=False):
    """「可重放的死信」的**唯一定义**。

    ⚠️ 列表(`failure_refs`)与计数(`failure_count`)必须共用这一份 —— 分开写的话
    界面上的"能重放 37 条"与实际起来 40 条(或 31 条)会对不上, 而这种偏差不会
    报错, 只会在用户数了两遍之后变成"这软件有毛病"。

    `status` 的取值与 `TaskManager.submit_resource` 的放行条件**逐字对齐**:
    它才是真正干活的入口, 两边条件漂移的后果是"列出来的重放不了"。
    """
    where = ["r.status IN ('failed','skipped','gone')",
             "r.error_kind IS NOT NULL", "r.error_kind <> ''"]
    args = []
    if not include_gone:
        # `gone` = 源站已删/已下线。自动重放它是纯空转; 手动点单条重试仍然放行
        # (403 可能只是代理/Referer 变了, 见 submit_resource 的注释)。
        where.append("r.error_kind <> 'gone'")
    picked = [k for k in (kinds or []) if k]
    if picked:
        where.append("r.error_kind IN (" + ",".join("?" * len(picked)) + ")")
        args.extend(picked)
    return " AND ".join(where), args


def failure_refs(kinds=None, include_gone=False, limit=200):
    """挑出**值得重放**的失败资源(跨任务), 最近失败的优先。

    带任务名与采集器(与 `library_list` 同形): 死信面板要能说清"这是哪个任务里
    的哪一条", 逐行再去查任务就是典型的 N+1。
    """
    where, args = _failure_where(kinds, include_gone)
    return query(
        f"""SELECT r.*, t.name AS task_name, t.collector AS collector
            FROM resources r JOIN tasks t ON t.id = r.task_id
            WHERE {where} ORDER BY r.id DESC LIMIT ?""",
        tuple(args) + (int(limit),),
    )


def failure_count(kinds=None, include_gone=False):
    """可重放的死信条数(口径同 `failure_refs`)。"""
    where, args = _failure_where(kinds, include_gone)
    row = query_one(
        f"SELECT COUNT(*) AS n FROM resources r WHERE {where}", tuple(args)
    )
    return row["n"] if row else 0


def library_count(q=None, kind=None, task_id=None, album=None, status="done",
                  tag=None, favorite=None, tag_children=False):
    """资源库总数(与 library_list 同一口径)。"""
    where, args = library_filters(q, kind, task_id, album, status, tag, favorite,
                                  tag_children)
    row = query_one(
        f"SELECT COUNT(*) AS n FROM resources r JOIN tasks t ON t.id = r.task_id "
        f"WHERE {where}",
        tuple(args),
    )
    return row["n"] if row else 0


def library_list(q=None, kind=None, task_id=None, album=None, status="done",
                 limit=50, offset=0, tag=None, favorite=None,
                 tag_children=False):
    """跨任务资源库列表, 带任务侧的采集器/任务名(供前端分组展示)。"""
    where, args = library_filters(q, kind, task_id, album, status, tag, favorite,
                                  tag_children)
    rows = query(
        f"""SELECT r.*, t.name AS task_name, t.collector AS collector,
                   t.created_time AS task_time
            FROM resources r JOIN tasks t ON t.id = r.task_id
            WHERE {where}
            ORDER BY r.id DESC LIMIT ? OFFSET ?""",
        tuple(args) + (int(limit), int(offset)),
    )
    return rows


def library_albums(limit=200):
    """有产物的相册名单(供资源库的相册筛选下拉)。

    只列出**真的有 done 资源**的任务, 否则下拉里会出现一堆点进去空的条目。
    """
    return query(
        """SELECT t.name AS album, COUNT(r.id) AS n,
                  COALESCE(SUM(r.size), 0) AS bytes
           FROM tasks t JOIN resources r ON r.task_id = t.id
           WHERE r.status='done' AND t.name IS NOT NULL AND t.name <> ''
           GROUP BY t.name
           ORDER BY MAX(r.id) DESC LIMIT ?""",
        (int(limit),),
    )


def library_stats():
    """资源库概览: 资源数 / 体积 / 相册数 / 类型分布 / 收藏数 / 标签数。"""
    row = query_one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(r.size), 0) AS bytes,
                  COUNT(DISTINCT t.name) AS albums,
                  COALESCE(SUM(CASE WHEN r.favorite=1 THEN 1 ELSE 0 END), 0) AS favs
           FROM resources r JOIN tasks t ON t.id = r.task_id
           WHERE r.status='done'"""
    )
    by_kind = query(
        """SELECT r.type AS type, COUNT(*) AS n, COALESCE(SUM(r.size),0) AS bytes
           FROM resources r WHERE r.status='done' GROUP BY r.type ORDER BY n DESC"""
    )
    tags = query_one(
        """SELECT COUNT(DISTINCT rt.tag) AS n
           FROM resource_tags rt JOIN resources r ON r.id = rt.resource_id
           WHERE r.status='done'"""
    )
    return {
        "resources": row["n"] if row else 0,
        "bytes": row["bytes"] if row else 0,
        "albums": row["albums"] if row else 0,
        "favorites": row["favs"] if row else 0,
        "tags": tags["n"] if tags else 0,
        "by_kind": [dict(r) for r in by_kind],
    }


def tags_of(resource_ids):
    """批量取标签: {resource_id: [tag, ...]}。

    ⚠️ 一次查完而不是"每行查一次" —— 资源库一页 40 条, 逐行查就是 40 次往返,
    而列表接口是最高频的调用。缺的键不会出现在结果里, 调用方用 `.get(i, [])`。
    """
    ids = [int(i) for i in (resource_ids or [])]
    if not ids:
        return {}
    out = {}
    # 分批: SQLite 的变量上限默认 999(旧版)/32766(新版), 一页 40 条够用,
    # 但"全选本页 + 跨页"时调用方可能传进来上千个 id, 别在这里踩边界。
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in query(
            f"SELECT resource_id, tag FROM resource_tags "
            f"WHERE resource_id IN ({marks}) ORDER BY tag",
            tuple(chunk),
        ):
            out.setdefault(row["resource_id"], []).append(row["tag"])
    return out


def add_tags(resource_ids, tags):
    """给一批资源打标签, 返回真正新增的条数(已存在的不重复计)。

    ⚠️ 必须**逐条 INSERT OR IGNORE**: 直接 INSERT 会在"重复打同一个标签"时抛
    UNIQUE 冲突, 而那是用户的正常操作(选中一批再打一次同样的标签)。
    """
    clean = [t for t in (normalize_tag(t) for t in (tags or [])) if t]
    ids = [int(i) for i in (resource_ids or [])]
    if not clean or not ids:
        return 0
    now = _now()
    added = 0
    for rid in ids:
        # 上限按资源的**最终**标签数算, 而不是"本次加几个"
        have = {r["tag"] for r in query(
            "SELECT tag FROM resource_tags WHERE resource_id=?", (rid,)
        )}
        room = MAX_TAGS_PER_RESOURCE - len(have)
        if room <= 0:
            raise ValueError(
                f"资源 {rid} 的标签已达上限 {MAX_TAGS_PER_RESOURCE} 个,"
                " 请先删掉一些再加"
            )
        for tag in clean:
            if tag in have:
                continue
            if room <= 0:
                raise ValueError(
                    f"加完这批会让资源 {rid} 的标签超过上限"
                    f" {MAX_TAGS_PER_RESOURCE} 个, 请减少要加的标签"
                )
            cur = execute(
                "INSERT OR IGNORE INTO resource_tags(resource_id, tag, created_time)"
                " VALUES (?,?,?)",
                (rid, tag, now),
            )
            if cur.rowcount:
                added += 1
                room -= 1
    return added


def remove_tags(resource_ids, tags):
    """去掉一批资源上的某些标签, 返回删除条数。空 tags = 清空这些资源的标签。"""
    ids = [int(i) for i in (resource_ids or [])]
    if not ids:
        return 0
    clean = [t for t in (normalize_tag(t) for t in (tags or [])) if t]
    marks = ",".join("?" * len(ids))
    if clean:
        qs = ",".join("?" * len(clean))
        cur = execute(
            f"DELETE FROM resource_tags WHERE resource_id IN ({marks})"
            f" AND tag IN ({qs})",
            tuple(ids) + tuple(clean),
        )
    else:
        cur = execute(
            f"DELETE FROM resource_tags WHERE resource_id IN ({marks})", tuple(ids)
        )
    return cur.rowcount or 0


def all_tags(limit=200):
    """标签清单(带资源数 + 颜色 + 层级), 供筛选下拉与树。

    ⚠️ 只统计**已落盘**资源上的标签: 资源库只显示 done, 若这里把 failed 资源
    的标签也算进去, 用户会看到一个点进去什么都没有的标签(数量非 0 但列表空)。

    每条回:
      * `n`      —— 这个标签**自己**身上的资源数;
      * `n_tree` —— 连子标签一起的数量(点父节点时能筛出多少);
      * `parent` / `depth` —— 层级(名字里的 `/` 算出来的, 见 `tag_parent`);
      * `color`  —— 调色板键, 空串 = 没设过。
    层级是**算出来的**: 父标签未必自己是个标签(用户可能只打了 `系列/角色`),
    所以界面不能只渲染"有资源的那些节点", 得按 `parent` 把中间层补齐(见
    `tag_tree`)。
    """
    rows = query(
        """SELECT rt.tag AS tag, COUNT(*) AS n
           FROM resource_tags rt JOIN resources r ON r.id = rt.resource_id
           WHERE r.status='done'
           GROUP BY rt.tag
           ORDER BY n DESC, rt.tag LIMIT ?""",
        (int(limit),),
    )
    colors = tag_colors()
    items = []
    for r in rows:
        tag = r["tag"]
        items.append({
            "tag": tag,
            "n": int(r["n"] or 0),
            "n_tree": int(r["n"] or 0),
            "parent": tag_parent(tag),
            "depth": tag_depth(tag),
            "color": colors.get(tag.lower(), ""),
        })
    # n_tree: 从后往前累加(父标签一定排在子标签**前面**吗? 不一定 —— 排序按
    # 数量。所以这里按名字长度做分组累加, 与出现顺序无关。
    by_tag = {it["tag"]: it for it in items}
    for it in sorted(items, key=lambda x: -x["depth"]):
        p = it["parent"]
        while p:
            up = by_tag.get(p)
            if up is None:
                # 父标签自己没资源: 补齐一个只有统计数的节点, 否则树上会缺一层
                up = {"tag": p, "n": 0, "n_tree": 0, "parent": tag_parent(p),
                      "depth": tag_depth(p), "color": colors.get(p.lower(), "")}
                by_tag[p] = up
                items.append(up)
            up["n_tree"] += it["n"]
            p = up["parent"]
    items.sort(key=lambda x: (x["tag"].lower(),))
    return items


def tag_colors():
    """已设置的颜色: `{小写标签: 调色板键}`。

    ⚠️ 键取小写: `tag_meta.tag` 是 NOCASE 列, 但 Python 字典不是 —— 不折一下
    就会出现"库里查得到颜色、字典里查不到"。
    """
    return {str(r["tag"]).lower(): r["color"] for r in query(
        "SELECT tag, color FROM tag_meta WHERE color <> ''"
    )}


def set_tag_color(tag, color):
    """设置/清除一个标签的颜色, 返回生效后的键(空串 = 已清除)。

    颜色是**标签**的属性(不挂在资源上), 所以与"哪些资源带这个标签"完全无关:
    标签还没有任何资源也能先设色, 反之资源全删了颜色也不会跟着丢 —— 用户重新
    打上同一个标签时它还在。
    """
    clean = normalize_tag(tag)
    if not clean:
        raise ValueError("标签不能为空")
    key = normalize_color(color)
    if key:
        execute(
            "INSERT INTO tag_meta(tag, color, updated_time) VALUES (?,?,?)"
            " ON CONFLICT(tag) DO UPDATE SET color=excluded.color,"
            " updated_time=excluded.updated_time",
            (clean, key, _now()),
        )
    else:
        # 清色 = 删行, 不留 `color=''` 的空壳(否则 tag_meta 会随着"点了一下
        # 又取消"慢慢涨)
        execute("DELETE FROM tag_meta WHERE tag=?", (clean,))
    return key


def set_favorite(resource_ids, value=True):
    """批量设置/取消收藏, 返回影响条数。"""
    ids = [int(i) for i in (resource_ids or [])]
    if not ids:
        return 0
    n = 0
    marks = ",".join("?" * len(ids))
    cur = execute(
        f"UPDATE resources SET favorite=? WHERE id IN ({marks})",
        (1 if value else 0,) + tuple(ids),
    )
    n = cur.rowcount or 0
    return n


def resource_refs(local_path):
    """同一个物理文件被几个资源记录引用(跨任务去重后会 >1)。

    ⚠️ 这是**删除语义**的关键: sha256 去重时后到的任务只是复用路径、不复制
    文件, 所以删任务时不能无脑删文件 —— 一删就把先到的那个任务也掏空了。
    调用方按这个计数决定"删到最后一份才真删文件"。
    """
    if not local_path:
        return 0
    row = query_one(
        "SELECT COUNT(*) AS n FROM resources WHERE local_path=? AND status='done'",
        (local_path,),
    )
    return row["n"] if row else 0


def resource_refs_many(local_paths):
    """批量版 `resource_refs`: {local_path: 引用数}。

    资源库一页几十条, 逐条查是典型的 N+1 —— 而列表页正是最高频的接口。
    未出现的路径不会在结果里, 调用方用 `.get(path, 0)`。
    """
    paths = [p for p in dict.fromkeys(local_paths or []) if p]
    out = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in query(
            f"SELECT local_path, COUNT(*) AS n FROM resources "
            f"WHERE status='done' AND local_path IN ({marks}) GROUP BY local_path",
            tuple(chunk),
        ):
            out[row["local_path"]] = row["n"]
    return out


def get_resource(resource_id):
    return query_one("SELECT * FROM resources WHERE id=?", (resource_id,))


def delete_resource(resource_id):
    """删除单条资源记录(资源库批量删除用)。返回是否真的删掉了一行。

    ⚠️ 只删记录, 不碰文件 —— "这份文件还有没有别人在用"是 `resource_refs` 的
    判断, 属于调用方的语义, 不能藏在这里(藏进来的话, 将来任何调用点都会
    "顺手"把文件删了, 而删文件是不可逆的)。
    """
    return execute("DELETE FROM resources WHERE id=?", (resource_id,)).rowcount


def begin_resource_attempt(resource_id):
    """标记"这一条资源开始下载了", 返回起始时刻。

    ⚠️ 用一条 SQL 自增 `attempts`, 不要"先读出来 +1 再写回": 同一个资源被手动重试
    与自动流程同时碰到时, 读-改-写会丢掉一次计数 —— 而次数正是判断"是不是一直
    在重试链里空转"的唯一依据, 丢一次结论就反了。
    同时把 finished_at 清空: 它此刻代表的是上一次的结束, 留着会让界面显示一个
    已经"完成"的资源又回到下载中(见 resource_timing 的口径)。
    """
    ts = now_ts()
    execute(
        "UPDATE resources SET started_at=?, finished_at=NULL,"
        " attempts=COALESCE(attempts,0)+1 WHERE id=?",
        (ts, resource_id),
    )
    return ts


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
