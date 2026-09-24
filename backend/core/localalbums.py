"""本地相册集: 把**用户自己的目录**当"相册集"来浏览。

它补的是哪一块
==============
采集侧解决的是"网上有什么, 怎么搬下来"; 而用户手上早就有一堆**已经在本地**的
照片(手机导出、旧的收藏、别的工具存下来的)。它们散在若干个目录里, 想看的时候
只能开文件管理器一张张翻 —— 没有相册视图、没有随机、没有"从全部里抽一批"。

于是这里把"一个用户显式登记过的目录"定义成一个**相册集**:
`相册集 = 一棵被登记的目录树`, 树里每个**直接装着照片**的目录算一个相册,
组里的照片就是它的内容。加上"随机池", 就有了 xchina 那类图集站没有的东西 ——
随机抽的是**跨相册**的, 每次都能撞见自己忘了的照片。

只读边界(最重要的一条)
======================
用户登记的是**他自己的目录**。所以这个模块:

* **绝不写、绝不改、绝不删根内的任何文件。** 只有一处落盘: 缩略图缓存在
  本程序自己的数据目录里(见 `thumb_base()`), 与用户的相册目录无关。
  (对比: 下载器的 `layout.claim` 会写 `_meta/`; 那是在**本程序创建**的目录里,
  不是这里。)
* **收藏之类的元数据也只写我们自己的库。** 不往相册目录里塞 `.favorite` 之类的
  标记文件 —— 那会污染用户的目录, 而且用户一旦把它当垃圾清掉, 数据就没了。
* 根内的路径必须**经得起越界检验**才读: 见 `safe_photo`。信任锚是"用户显式登记
  过这个根" —— 有了它, 根内的路径才算可信; 跨出这个根一律拒。

为什么"相册"的定义是这个
========================
"直接装着照片的目录"是唯一不需要额外配置、也不需要猜的定义。换成"递归收集
子孙目录里的照片"会让 `2024/` 这个相册里混进 `2024/旅行/` 的内容, 于是
"这个相册有多少张"永远说不清; 换成"只看根目录的下一层"又会让
`照片/2024/旅行/` 这种真实结构整片消失。层数是可调的(`local_album_depth`),
但口径只有一条。

索引与实时的分工(一个刻意的两套口径)
====================================
* **打开某个相册时永远实时列目录** —— 用户看到的就是磁盘上此刻的真实内容。
* **随机池用索引快照** —— 把一个 5 万张的库每次随机都重扫一遍是没道理的。

这两条能同时成立, 靠的是"相册照片列表每次都会把索引里那一条**就地修正**"
(`_patch_album`), 所以索引不会长期偏离事实; 剩下的偏差被 `local_album_ttl`
封顶, 而且界面上会显示这个数字是几点核出来的。**"没验到"与"验过了没问题"
不能混在一句话里** —— 所以 `last_scan` 是 NULL 时界面必须显示"未扫描"。
"""

from __future__ import annotations

import fnmatch
import json
import os
import random
import threading
import time
from pathlib import Path

from core import database as db
from core import thumbs
from core.config import settings
from core.layout import META_DIR_NAME

__all__ = [
    "IMAGE_SUFFIXES",
    "KIND_DUPLICATE",
    "KIND_ESCAPES_ROOT",
    "KIND_INSIDE_CACHE",
    "KIND_NOT_DIR",
    "KIND_NOT_FOUND",
    "KIND_NOT_IMAGE",
    "KIND_UNKNOWN_ROOT",
    "RootError",
    "MAX_EXCLUDE_PATTERNS",
    "add_root",
    "albums",
    "build_index",
    "duplicates",
    "exclude_report",
    "favorite_keys",
    "favorites",
    "favorites_view",
    "forget_favorite",
    "get_root",
    "on_this_day",
    "parse_exclude",
    "photos",
    "prune_thumbs",
    "random_photos",
    "remove_root",
    "rename_root",
    "roots",
    "safe_photo",
    "set_exclude",
    "set_favorite",
    "stats",
    "thumb_base",
]

#: 能被当成照片的扩展名。**刻意只收浏览器能直接渲染的那些** ——
#: 把 `.heic` / `.cr2` / `.nef` 收进来, 相册网格里就会出现一片打不开的破图,
#: 而"格式不支持"和"文件坏了"在界面上是同一张脸。宁可先不列。
IMAGE_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".avif", ".bmp",
})

#: 目录名黑名单(小写比较)。这些目录里的图片**不是用户的照片** ——
#: 缩略图缓存、版本库内部、系统回收站。扫进来会让"这个相册有多少张"变成假数字,
#: 而且这些目录动辄几千个文件, 扫描成本全花在它们身上。
SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", ".svn", ".hg", ".venv", "venv",
    "$recycle.bin", "system volume information", "lost+found",
    "@eadir",                 # 群晖的缩略图/元数据目录
    ".thumbnails", ".thumb", "thumbs", "@thumbs",
    ".cache", ".trash", ".trash-1000", ".snapshot",
    # ⚠️ 我们自己下载产物的附属目录: 用户把 `downloads/` 登记为相册集是**主要
    # 用法之一**, 而 `_meta/` 里放着清单与缩略图(一堆 .jpg) —— 不排除掉,
    # 相册列表里会冒出一堆叫 `101` 的"相册", 里面是我们自己生成的缩略图。
    META_DIR_NAME,
})

#: 文件名黑名单。这些是操作系统/相册软件留下的**非照片**文件:
#: 明明扩展名像图片(或干脆没扩展名), 内容却不是用户的照片。
JUNK_FILE_NAMES = frozenset({
    "thumbs.db", "desktop.ini", ".ds_store", "folder.jpg", "albumartsmall.jpg",
})

#: 一次最多返回多少张照片(单个相册分页、随机池一次抽多少的上限)。
MAX_PAGE_SIZE = 200

#: 缩略图缓存目录名, 落在 `<库文件同级>/local_albums/` 下。
CACHE_DIR_NAME = "local_albums"

# ---- 机器可读的失败代号 ----------------------------------------------------
# 与 core/errors.py 的 error_kind 同一条纪律: `kind` 给机器判(接口映射成 HTTP
# 状态码), `message` 给人看。**测试只断言 kind** —— 断言中文文案的判据是假红,
# 因为文案会改措辞。
KIND_NOT_FOUND = "not-found"          # 目录/文件不存在
KIND_NOT_DIR = "not-a-dir"            # 路径存在, 但不是目录
KIND_INSIDE_CACHE = "inside-cache"    # 指到了本程序自己的缓存目录
KIND_DUPLICATE = "duplicate"          # 已经登记过同一个目录
KIND_UNKNOWN_ROOT = "unknown-root"    # 没有这个 id
KIND_ESCAPES_ROOT = "escapes-root"    # 路径跨出了已登记的根
KIND_NOT_IMAGE = "not-image"          # 不是本接口会提供的图片


class RootError(ValueError):
    """一个根/一张照片为什么不能被使用。"""

    __slots__ = ("kind", "message")

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---- 索引缓存 --------------------------------------------------------------
# 键是 local_roots.id, 值是 _stack_scan 的结果。⚠️ 缓存里**存了路径**:
# 登记行被改过(理论上不会, 但别假设)或目录被换成另一个同名目录时, 只认 id
# 会把旧内容的索引当成新目录的 —— 所以取用时要比 `path`。
_cache: dict = {}
_cache_lock = threading.Lock()


def invalidate(root_id=None):
    """作废索引快照。登记表一变就必须调 —— 否则界面会拿着旧目录的数字报账。"""
    with _cache_lock:
        if root_id is None:
            _cache.clear()
        else:
            _cache.pop(int(root_id), None)


# ---- 小工具 ----------------------------------------------------------------
def _natural_key(name):
    """给文件名排序用的键: 让 `2.jpg` 排在 `10.jpg` 前面。

    ⚠️ 纯字符串排序会把 `10.jpg` 排到 `2.jpg` 前面 —— 而图集站下来的文件正是
    `1.jpg ... 10.jpg` 这种命名, 于是相册里的顺序看起来是乱的。这个方法只做
    "把连续数字当成数来比", 不引入任何依赖。
    """
    parts = []
    digits = ""
    for ch in str(name):
        if ch.isdigit():
            digits += ch
        else:
            if digits:
                parts.append((1, int(digits), ""))
                digits = ""
            parts.append((0, 0, ch.casefold()))
    if digits:
        parts.append((1, int(digits), ""))
    return parts


def _is_image(name):
    return Path(str(name)).suffix.lower() in IMAGE_SUFFIXES


#: 一个相册集最多配多少条排除模式。不是为了省内存, 是为了让"配了 500 条规则"
#: 这种用法在门口就被拒, 而不是变成一次慢到没人愿意等的扫描。
MAX_EXCLUDE_PATTERNS = 50


def _split_exclude(raw):
    """把各种输入形态拆成"非空字符串的列表" —— **截断之前**的条数。

    ⚠️ 解析逻辑只有这一份: `parse_exclude`(要截断)与 `exclude_report`(要报
    "丢了几条")都需要它。写成两份的话, 两边会各自漂移, 而漂移的表现正是
    `dropped` 恒为 0 —— 一个"看起来没坏"的静默失败。
    """
    if raw is None:
        return []
    items = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            loaded = json.loads(text)
        except (ValueError, TypeError):
            # 不是 JSON 就当"一行一条"读: 用户在输入框里最自然的写法就是换行。
            loaded = text.replace(",", "\n").splitlines()
        items = loaded if isinstance(loaded, (list, tuple)) else [loaded]
    return [str(i).strip() for i in (items or []) if str(i or "").strip()]


def parse_exclude(raw):
    """把"排除模式"的各种输入形态归一成字符串元组(超上限的丢掉)。

    接受 None / 字符串(JSON 数组或一行一条) / 列表。⚠️ **坏值一律跳过而不是
    抛异常**: 一条写错的规则让整个相册集扫不出来, 是典型的"一个附件能力把主
    流程搞挂"(见 core/phash.py 约束 2)。
    """
    return tuple(_split_exclude(raw)[:MAX_EXCLUDE_PATTERNS])


def exclude_report(raw, patterns=None):
    """把配置还原成"界面能如实转述"的形态: `(生效的, 被丢掉的条数)`。

    ⚠️ 为什么要单独有这个函数: **"没有排除规则"和"规则写了但一条都没生效"
    必须长得不一样。** 前者是用户的意图, 后者是用户以为自己在排除、其实一张
    都没排除 —— 那是最难自查的一类静默失败(界面上唯一可见的差别就是"0 项被
    跳过", 而这个数字如果不显示, 就没有任何东西能让你发现它)。
    """
    pats = parse_exclude(raw) if patterns is None else tuple(patterns)
    return list(pats), max(0, len(_split_exclude(raw)) - len(pats))


def _excluded(rel_path, patterns):
    """这个相对路径(文件或目录)是否被排除。

    三条判据, 命中任一即排除。之所以是三条而不是严格照 glob 语义只判一条:
    用户随手写的一个词(`Raw`、`*_edited*`)是**最常见**的输入形态, 而严格的
    glob 会让 `Raw` 这种写法**什么都不排除** —— 规则静静地失效。

      1. 完整相对路径匹配(`2024/Raw/a.jpg` vs `**/Raw/*`);
      2. 末段文件名匹配(`a.jpg` vs `*_edited*`);
      3. **任一祖先目录**匹配(`Raw` 命中 `2024/Raw/a.jpg` 的祖先 `2024/Raw`)
         —— 这一条让"排除整个目录"不必写成 `Raw/**`。
    """
    if not patterns:
        return False
    text = str(rel_path).replace("\\", "/")
    head, _, tail = text.rpartition("/")
    for pat in patterns:
        if fnmatch.fnmatch(text, pat):
            return True
        if tail and fnmatch.fnmatch(tail, pat):
            return True
        if head and fnmatch.fnmatch(head.rpartition("/")[2], pat):
            return True
    return False


def _within(path, root):
    """`path` 是否在 `root` 之内。两者都应当已经 resolve 过。"""
    try:
        return Path(path).is_relative_to(Path(root))
    except (OSError, ValueError):
        return False


def _norm_key(path):
    """跨平台一致的"同一个路径"判据。

    ⚠️ 用 `os.path.normcase` 而不是 `str.lower()`: Windows 上它顺手把 `/` 换成
    `\\`; 而 Linux 上它是**恒等函数** —— 那正是我们要的(那里 `A.jpg` 与
    `a.jpg` 是两个文件)。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def thumb_base() -> Path:
    """缩略图缓存根: **在我们自己的数据目录里**, 不在用户的相册目录里。

    ⚠️ 由库文件的位置推出来(而不是写成固定路径): 测试与多实例部署都会把
    `DB_PATH` 指到别处, 缓存自然跟着走 —— 否则测试会往真实数据目录里写缩略图。
    ⚠️ 仍然复用 `thumbs`(键、渲染、原子换入都只有那一份实现), 所以最终路径是
    `<库同级>/local_albums/_meta/thumb/`。多出来的那层是 `thumbs` 的约定,
    不值得为好看而在别处再写一个缩略图实现。
    """
    return Path(str(db.DB_PATH)).parent / CACHE_DIR_NAME


# ---- 登记表 ----------------------------------------------------------------
def roots():
    """所有已登记的相册集(按登记顺序)。"""
    return [dict(r) for r in db.query("SELECT * FROM local_roots ORDER BY id")]


def get_root(root_id):
    row = db.query_one("SELECT * FROM local_roots WHERE id=?", (int(root_id),))
    if not row:
        raise RootError(KIND_UNKNOWN_ROOT, "没有 id=%s 的本地相册集" % root_id)
    return dict(row)


def add_root(raw, name=None, exclude=None):
    """登记一个目录为相册集。**只登记, 不扫描** ——

    扫描可能要走几万个文件, 不该发生在一次 POST 里。前端拿到返回后立刻拉一次
    相册列表, 那一步才会真正扫描(`build_index` 冷缓存必然扫)。

    `exclude` 是排除模式(glob 列表或一行一条的字符串), 见 `parse_exclude`。
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        raise RootError(KIND_NOT_FOUND, "目录路径不能为空")
    try:
        path = Path(text).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise RootError(KIND_NOT_FOUND, "路径无法解析: %s" % exc)
    if not path.exists():
        raise RootError(KIND_NOT_FOUND, "目录不存在: %s" % path)
    if not path.is_dir():
        raise RootError(KIND_NOT_DIR, "这不是一个目录: %s" % path)

    # 指到我们自己的缓存目录(或把它包含进来)是没有意义的: 相册列表里会出现
    # 一堆我们自己生成的缩略图。这不是"防用户", 是把一个必然难看的结果挡在门口。
    cache = thumb_base().resolve()
    if _within(path, cache) or _within(cache, path):
        raise RootError(
            KIND_INSIDE_CACHE,
            "这个目录与本程序的缩略图缓存重叠, 登记了会把缓存本身当成相册: %s" % cache,
        )

    key = _norm_key(path)
    for row in roots():
        if _norm_key(row["path"]) == key:
            raise RootError(KIND_DUPLICATE, "这个目录已经登记过了: %s" % path)

    cur = db.execute(
        "INSERT INTO local_roots(path, name, created_at, exclude) VALUES(?,?,?,?)",
        (
            str(path),
            (str(name).strip() or None) if name else None,
            db.now_ts(),
            json.dumps(list(parse_exclude(exclude))) if exclude else None,
        ),
    )
    invalidate()
    return get_root(cur.lastrowid)


def update_root_cache(root_id):
    """登记行变了之后让索引下一轮重算, 并返回新的登记行。

    ⚠️ 改名也要作废索引: 根目录那一层的相册名**就是**登记名(见 `_album_name`),
    不改的话用户改完名立刻看到的还是旧名字 —— "改了没反应"。
    """
    invalidate(int(root_id))
    return get_root(int(root_id))


def rename_root(root_id, name):
    """只改显示名, 不动 `path`。"""
    row = get_root(root_id)
    db.execute(
        "UPDATE local_roots SET name=? WHERE id=?",
        ((str(name).strip() or None) if name else None, row["id"]),
    )
    return update_root_cache(row["id"])


def set_exclude(root_id, exclude):
    """改排除模式。⚠️ 改完必须**作废索引** ——

    否则界面会拿着排除前的索引继续报账, 表现为"我加了规则, 但那些照片还在",
    而用户只会怀疑规则没写对(其实是还没重扫)。
    """
    row = get_root(root_id)
    pats = parse_exclude(exclude)
    db.execute(
        "UPDATE local_roots SET exclude=? WHERE id=?",
        (json.dumps(list(pats)) if pats else None, row["id"]),
    )
    invalidate(row["id"])
    return get_root(row["id"])


def update_root_cache(root_id):
    """登记行变了但**路径没变**时, 只让索引下一轮重算, 返回新行。

    (改名字不影响扫描结果, 所以这里不 invalidate —— 但改 `exclude` 必须, 见
    `set_exclude`。)
    """
    return get_root(int(root_id))


def remove_root(root_id):
    """取消登记。

    ⚠️ **只删登记, 不碰磁盘上的文件, 也不清收藏。** 两件事都不是"取消登记"的
    含义: 用户的照片不是我们放进去的, 收藏是用户自己的标记 —— 移除一个根就顺手
    抹掉它们, 属于"用一次点击删掉没让用户看过的东西"。
    """
    row = get_root(root_id)
    db.execute("DELETE FROM local_roots WHERE id=?", (row["id"],))
    invalidate(row["id"])
    return row


# ---- 扫描 ------------------------------------------------------------------
def _album_name(root, rel, display=None):
    if not rel:
        # 根目录下直接放着的照片也归一个相册 —— 它的名字就用登记名或目录名,
        # 否则这批照片会**没有入口**(它们不在任何子目录里)。
        # 优先用登记名: 用户把 `D:\\photos` 登记成"2024 旅行"时, 给这批照片挂上
        # 目录名 `photos` 只会让他以为自己点错了。
        return (str(display).strip() if display else "") or Path(str(root)).name or str(root)
    return str(rel).replace("\\", "/").rsplit("/", 1)[-1]


def _stack_scan(root, depth, max_photos, display=None, exclude=()):
    """走一遍目录树, 返回 (albums, order, photos, bytes, truncated, unreadable, excluded)。

    `albums` 是相册元数据, `order` 是 `{相册rel: [照片...]}` —— 分成两份是为了
    让"相册列表"这条最热的接口不必把每张照片都拖出来。

    `excluded` 是 `{"dirs": n, "files": n}` —— 被排除模式跳过的目录/文件数。
    ⚠️ 它必须被数出来并报上去: 排除规则**没生效**和**没有排除规则**在结果上
    长得一模一样(都是"这些照片不见了"), 而只有这个数字能把两者分开。
    """
    albums = []
    order = {}
    total = 0
    total_bytes = 0
    truncated = False
    unreadable = 0
    excluded = {"dirs": 0, "files": 0}

    stack = [("", 0)]
    while stack:
        rel, level = stack.pop()
        here = root if not rel else root / rel
        try:
            entries = list(os.scandir(here))
        except OSError:
            # 权限不足/目录被拔了。**计数而不是吞掉**: 一个读不到的子树与
            # "这个子树是空的"在界面上长得一样, 而后者会让用户以为照片丢了。
            unreadable += 1
            continue

        files = []
        for entry in entries:
            try:
                if entry.is_dir():
                    child_rel = "%s/%s" % (rel, entry.name) if rel else entry.name
                    if _excluded(child_rel, exclude):
                        excluded["dirs"] += 1
                        continue
                    if entry.name.startswith(".") or entry.name.lower() in SKIP_DIR_NAMES:
                        continue
                    if level < depth:
                        child = here / entry.name
                        # 软链接指向根外: 跟进去的话, 相册里会列出一批
                        # `safe_photo` 必然拒掉的路径 —— "看得见、点不开"比
                        # "看不见"更让人困惑, 也会让"这个相册有几张"变成假数字。
                        if not _within(child.resolve(), root):
                            continue
                        stack.append((child_rel, level + 1))
                elif entry.is_file():
                    if not _is_image(entry.name):
                        continue
                    if entry.name.lower() in JUNK_FILE_NAMES:
                        continue
                    if _excluded(
                        ("%s/%s" % (rel, entry.name) if rel else entry.name), exclude
                    ):
                        excluded["files"] += 1
                        continue
                    st = entry.stat()
                    files.append({
                        "name": entry.name,
                        "size": int(st.st_size),
                        "mtime": float(st.st_mtime),
                    })
            except OSError:
                continue

        if not files:
            continue
        if total + len(files) > max_photos:
            files = files[: max(0, max_photos - total)]
            truncated = True
        files.sort(key=lambda f: _natural_key(f["name"]))
        size = sum(f["size"] for f in files)
        total += len(files)
        total_bytes += size
        albums.append({
            "rel": rel,
            "name": _album_name(root, rel, display),
            "photos": len(files),
            "bytes": size,
            "mtime": max(f["mtime"] for f in files),
            # 封面 = 自然序的第一张。放在索引里(而不是让前端再为每个相册发一次
            # 请求要封面): 40 个相册就是 40 次请求, 而这份数据**扫描时本来就在手上**。
            "cover": "%s/%s" % (rel, files[0]["name"]) if rel else files[0]["name"],
        })
        order[rel] = files
        if truncated:
            break

    albums.sort(key=lambda a: _natural_key(a["rel"]))
    return albums, order, total, total_bytes, truncated, unreadable, excluded


def _snapshot_keys(index):
    """索引里全部照片的身份集合, 形如 `{"旅行/a.jpg", "cover.jpg"}`。

    ⚠️ 用"相册 rel + 文件名"而不是绝对路径: 对账要比的是"这张照片还在不在",
    而根被改名/移动时绝对路径会**整体变一次** —— 那会被算成"全部消失 + 全部
    新增", 而实际上一张都没动。
    """
    out = set()
    for rel, files in (index or {}).get("order", {}).items():
        for f in files:
            out.add("%s/%s" % (rel, f["name"]) if rel else f["name"])
    return out


def _prev_snapshot(root_id, path):
    """上一次索引快照 —— 只在**还是同一个目录**时才算数。"""
    with _cache_lock:
        old = _cache.get(int(root_id))
    return old if old and old.get("path") == path else None


def _diff_keys(prev, order):
    """与上一次快照对账; 返回 None 或 `{"added": n, "removed": n}`。

    ⚠️ **None 的含义是"没得比"**(进程刚启动 / 从没扫过 / 根被换成了另一个目录),
    与 `{"added": 0, "removed": 0}`(**比过了, 一张没变**)是两件必须分得开的事。
    合并成 0 的话, 界面永远显示"没有变化", 而"这次其实没有可比对象"这个事实
    被抹掉了 —— 那正是 Immich 那种"先标 offline 再清理"要解决的问题: 用户得能
    看出"有东西不见了", 而不是只看到一个总数变小。
    """
    if prev is None:
        return None
    old = _snapshot_keys(prev)
    new = _snapshot_keys({"order": order})
    return {"added": len(new - old), "removed": len(old - new)}


def build_index(root_id, force=False):
    """取这个根的索引快照; 缓存过期或不存在就现扫。

    ⚠️ 返回的是**缓存里的那个 dict**, 调用方不要就地改它 —— 要改用 `_patch_album`。
    """
    row = get_root(root_id)
    root = Path(row["path"])
    ttl = max(0.0, float(getattr(settings, "local_album_ttl", 30.0)))
    now = time.time()
    if not force:
        with _cache_lock:
            hit = _cache.get(row["id"])
        if hit and hit["path"] == row["path"] and (now - hit["at"]) < ttl:
            return hit

    depth = max(0, int(getattr(settings, "local_album_depth", 3)))
    cap = max(1, int(getattr(settings, "local_album_max_photos", 50000)))
    patterns, dropped = exclude_report(row.get("exclude"))
    error = None
    try:
        if not root.is_dir():
            albums, order, total, total_bytes, truncated, unreadable = [], {}, 0, 0, False, 0
            excluded = {"dirs": 0, "files": 0}
            error = "目录不存在或不是目录: %s" % root
        else:
            (albums, order, total, total_bytes, truncated,
             unreadable, excluded) = _stack_scan(
                root, depth, cap, display=row.get("name"), exclude=patterns
            )
    except OSError as exc:
        # `_stack_scan` 内部已经按目录粒度兜住了 OSError; 走到这里说明是更外面
        # 的失败(例如 root / rel 拼出来的路径非法)。同样要**留痕**。
        albums, order, total, total_bytes, truncated, unreadable = [], {}, 0, 0, False, 0
        excluded = {"dirs": 0, "files": 0}
        error = "扫描失败: %s" % exc

    index = {
        "root_id": row["id"],
        "path": row["path"],
        "name": row.get("name"),
        "at": time.time(),
        "albums": albums,
        "order": order,
        "photos": total,
        "bytes": total_bytes,
        "truncated": truncated,
        "unreadable": unreadable,
        "error": error,
        # 排除规则"配了什么 / 真的生效了吗" —— 见 exclude_report
        "exclude": list(patterns),
        "exclude_dropped": dropped,
        "excluded": excluded,
        # 与上一次快照的对账, 见 _diff_keys
        "delta": _diff_keys(_prev_snapshot(row["id"], row["path"]), order),
    }
    with _cache_lock:
        _cache[row["id"]] = index

    # 回写统计。⚠️ `last_scan` 是"最近一次**尝试**扫描的时刻", 扫失败也要写 ——
    # 否则界面上的时间戳会一直停在历史上那个成功的时刻, 而内容早就读不到了。
    db.execute(
        "UPDATE local_roots SET last_scan=?, album_count=?, photo_count=?, error=? WHERE id=?",
        (index["at"], len(albums), total, error, row["id"]),
    )
    return index


def index_brief(index):
    """索引的摘要(不含照片清单)。给列表接口带上, 让界面能说"几点核的"。"""
    return {
        "root_id": index["root_id"],
        "path": index["path"],
        "name": index["name"],
        "at": index["at"],
        "albums": len(index["albums"]),
        "photos": index["photos"],
        "bytes": index["bytes"],
        "truncated": index["truncated"],
        "unreadable": index["unreadable"],
        "error": index["error"],
        "exclude": index.get("exclude", []),
        "exclude_dropped": index.get("exclude_dropped", 0),
        "excluded": index.get("excluded", {"dirs": 0, "files": 0}),
        "delta": index.get("delta"),
    }


def _patch_album(index, rel, photos):
    """把一个相册的**实时**内容写回索引快照。

    这一步是"两套口径"能同时成立的关键: 用户打开相册看到的是磁盘此刻的真内容,
    而随机池读索引 —— 如果不回写, 一个刚删掉一半照片的相册会在随机池里继续
    出现那些已经不存在的照片, 表现为"随机里点开是坏的", 极难怀疑到缓存上。
    """
    size = sum(p["size"] for p in photos)
    meta = {
        "rel": rel,
        "name": _album_name(Path(index["path"]), rel, index.get("name")),
        "photos": len(photos),
        "bytes": size,
        "mtime": max([p["mtime"] for p in photos], default=0.0),
        "cover": photos[0]["rel"] if photos else None,
    }
    with _cache_lock:
        for i, a in enumerate(index["albums"]):
            if a["rel"] == rel:
                index["albums"][i] = meta
                break
        else:
            index["albums"].append(meta)
            index["albums"].sort(key=lambda a: _natural_key(a["rel"]))
        index["order"][rel] = photos
        index["photos"] = sum(a["photos"] for a in index["albums"])
        index["bytes"] = sum(a["bytes"] for a in index["albums"])


# ---- 相册视图 --------------------------------------------------------------
_SORT_KEYS = {
    "name": lambda a: _natural_key(a["name"]),
    "path": lambda a: _natural_key(a["rel"]),
    "photos": lambda a: a["photos"],
    "bytes": lambda a: a["bytes"],
    "mtime": lambda a: a["mtime"],
}


def _sorted(items, sort, order):
    key = _SORT_KEYS.get(str(sort or ""), _SORT_KEYS["mtime"])
    return sorted(items, key=key, reverse=str(order or "desc").lower() != "asc")


def albums(root_id=None, q=None, sort="mtime", order="desc", min_photos=1, refresh=False):
    """跨相册集列相册。

    `min_photos` 默认 1: 空目录不算相册(列出来只会是噪声)。
    """
    rows = [get_root(root_id)] if root_id else roots()
    items = []
    briefs = []
    for row in rows:
        index = build_index(row["id"], force=refresh)
        briefs.append(index_brief(index))
        display = row.get("name") or Path(row["path"]).name or row["path"]
        for album in index["albums"]:
            if album["photos"] < max(0, int(min_photos or 0)):
                continue
            if q and str(q).casefold() not in (
                "%s %s" % (album["name"], album["rel"])
            ).casefold():
                continue
            item = dict(album)
            item["root_id"] = row["id"]
            item["root_name"] = display
            item["root_path"] = row["path"]
            if not item["rel"]:
                # 根目录这一层的相册(rel 为空)用登记名 —— 见 `_album_name`。
                item["name"] = display
            items.append(item)
    items = _sorted(items, sort, order)
    return {"items": items, "total": len(items), "roots": briefs}


def _list_album(root, rel, root_id=None):
    """实时列一个相册目录里的照片。**每次都重新列** —— 这是"所见即磁盘现状"。

    `root_id` 会写进每一条上: 出图要靠 `(root_id, rel)` 定位, 而收藏标记要靠
    `root_id` 反查根路径。少了它, 上层就只能自己再拼一遍 —— 而"自己再拼一遍"
    正是这一层不该做的事。
    """
    here = root if not rel else root / rel
    if not _within(here.resolve(), root):
        raise RootError(KIND_ESCAPES_ROOT, "这个相册不在已登记的相册集里")
    if not here.is_dir():
        raise RootError(KIND_NOT_FOUND, "相册目录不存在")
    photos = []
    try:
        entries = list(os.scandir(here))
    except OSError as exc:
        raise RootError(KIND_NOT_FOUND, "相册目录读不到: %s" % exc)
    for entry in entries:
        try:
            if not entry.is_file() or not _is_image(entry.name):
                continue
            if entry.name.lower() in JUNK_FILE_NAMES:
                continue
            st = entry.stat()
            photos.append({
                "root_id": root_id,
                "name": entry.name,
                "rel": "%s/%s" % (rel, entry.name) if rel else entry.name,
                "album": rel,
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
            })
        except OSError:
            continue
    photos.sort(key=lambda p: _natural_key(p["name"]))
    return photos


def photos(root_id, rel="", q=None, sort="name", order="asc", offset=0, limit=200):
    """一个相册里的照片(分页)。"""
    row = get_root(root_id)
    root = Path(row["path"]).resolve()
    rel = str(rel or "").replace("\\", "/").strip("/")
    found = _list_album(root, rel, row["id"])
    index = build_index(row["id"])
    _patch_album(index, rel, found)

    if q:
        needle = str(q).casefold()
        found = [p for p in found if needle in p["name"].casefold()]
    found = _sorted(found, sort if sort in _SORT_KEYS else "name", order)
    total = len(found)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), MAX_PAGE_SIZE))
    page = _with_favorite(found[offset: offset + limit], {row["id"]: row["path"]})
    display = row.get("name") or Path(row["path"]).name or row["path"]
    return {
        "items": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "album": {
            "root_id": row["id"],
            "root_name": display,
            "root_path": row["path"],
            "rel": rel,
            "name": _album_name(root, rel, row.get("name")),
        },
    }


def safe_photo(root_id, rel):
    """把 `(root_id, rel)` 还原成一个**可信**的本地图片路径。

    三道判据, 缺一不可:

      1. **必须在已登记的根之内。** 这是唯一的信任锚 —— 用户显式登记过这个根,
          所以根内的路径才算可信。`resolve()` 之后再判, 于是 `../`、绝对路径、
          指向根外的软链接全都落在这一条上。
      2. **必须是磁盘上真实存在的普通文件。**
      3. **必须是图片扩展名。** 这一条不是防越界(第 1 条已经管了), 而是防
          调用方传错: 相册接口只该吐照片, 不该因为一个拼错的参数变成"任意文件
          读取器"。所以它回的是**明确的 415**, 不是 404 —— 让"传错了"和
          "文件没了"在日志里长得不一样。
    """
    row = get_root(root_id)
    root = Path(row["path"]).resolve()
    raw = str(rel or "").replace("\\", "/").strip()
    if not raw:
        raise RootError(KIND_NOT_FOUND, "缺少照片相对路径")
    try:
        path = (root / raw).resolve()
    except (OSError, ValueError):
        raise RootError(KIND_NOT_FOUND, "路径无法解析")
    if not _within(path, root):
        raise RootError(KIND_ESCAPES_ROOT, "这个路径不在已登记的相册集里")
    if not path.is_file():
        raise RootError(KIND_NOT_FOUND, "文件不存在")
    if not _is_image(path.name):
        raise RootError(KIND_NOT_IMAGE, "这个接口只提供图片")
    return path


# ---- 随机池 ----------------------------------------------------------------
def _with_favorite(items, roots_by_id):
    """给一批照片补上 `favorite` 标记。**返回副本**, 不就地改。

    ⚠️ `photos()` 拿到的那些 dict 就是**索引缓存里的同一批对象**
    (`_patch_album` 把 `_list_album` 的结果直接挂进 `index["order"]`)。就地加一个
    键, 等于把"这次查到的收藏状态"永久写进缓存 —— 之后取消了收藏, 界面还是老样子,
    而"改了没反应"是最难被归因的一类现象。

    (前端也**不该自己拼绝对路径**: 它手上只有 `root_path` + `rel`, 而 Windows 的
    分隔符会让那次拼接变成一个只在某些目录下才对的字符串。)
    """
    keys = favorite_keys()
    out = []
    for item in items:
        copy = dict(item)
        root = roots_by_id.get(item.get("root_id"))
        copy["favorite"] = bool(root) and _norm_key(Path(root) / item["rel"]) in keys
        out.append(copy)
    return out


def _deck(pool, rng, mode):
    """把"若干相册"摊成一副牌。

    `mode="album"`: **按轮发牌** —— 每轮从每个相册取一张。这样 3 张的小相册
    与 3000 张的大相册在牌面上的出现频率接近, 不会被大相册整个淹掉; 而
    `mode="photo"` 是整池洗牌, 大相册按张数占优 —— 两种取向都有人要, 都留着。

    同一个 `seed` + 递增的 `page` 就是"一副可以一直往下翻的牌": 各页不重叠、
    也不缺项(整副牌恰好覆盖池里每一张)。
    """
    if mode == "photo":
        flat = list(pool)
        rng.shuffle(flat)
        return flat

    by_album = {}
    for item in pool:
        by_album.setdefault(item["album_key"], []).append(item)
    lists = []
    for key in sorted(by_album):
        group = by_album[key]
        rng.shuffle(group)
        lists.append(group)
    rng.shuffle(lists)

    deck = []
    rounds = max([len(g) for g in lists], default=0)
    for i in range(rounds):
        for group in lists:
            if i < len(group):
                deck.append(group[i])
    return deck


def _candidates(root_rows, album=None, min_bytes=0):
    """把选中的相册集摊成候选池。`album` 是 `(root_id, rel)` 时只取那一个相册。"""
    pool = []
    truncated = False
    for row in root_rows:
        index = build_index(row["id"])
        truncated = truncated or bool(index["truncated"])
        display = row.get("name") or Path(row["path"]).name or row["path"]
        if album is not None:
            wanted_root, wanted_rel = album
            if int(wanted_root) != row["id"]:
                continue
            wanted = [a for a in index["albums"] if a["rel"] == wanted_rel]
            if not wanted:
                # 快照里没有不代表没有 —— 这里退回实时列目录, 顺手把索引修正。
                wanted = [{
                    "rel": wanted_rel,
                    "name": _album_name(Path(row["path"]), wanted_rel, row.get("name")),
                    "photos": 0, "bytes": 0, "mtime": 0.0, "cover": None,
                }]
                _patch_album(index, wanted_rel,
                             _list_album(Path(row["path"]).resolve(), wanted_rel,
                                         row["id"]))
            targets = wanted
        else:
            targets = index["albums"]
        for meta in targets:
            rel = meta["rel"]
            for photo in index["order"].get(rel, ()):
                if photo["size"] < min_bytes:
                    continue
                pool.append({
                    "root_id": row["id"],
                    "root_name": display,
                    "root_path": row["path"],
                    "album": rel,
                    "album_name": meta["name"],
                    "album_key": "%d|%s" % (row["id"], rel),
                    "name": photo["name"],
                    "rel": "%s/%s" % (rel, photo["name"]) if rel else photo["name"],
                    "size": photo["size"],
                    "mtime": photo["mtime"],
                })
    return pool, truncated


def random_photos(count=60, root_id=None, album=None, mode="album", page=0, seed=None,
                  min_bytes=0, favorites_only=False):
    """从相册集里抽一批照片。

    ⚠️ 与"打开相册"不同, 这里读的是**索引快照**: 池子可能就是几万条, 每次重扫
    不合理。代价是刚拷进来的照片最多晚 `local_album_ttl` 秒才进池子 —— 所以
    返回里带上 `at`(这个池子是什么时候的)与 `pool`(池里一共多少张),
    界面必须把它们显示出来, 否则"随机怎么没抽到我刚放的那张"就无从解释。
    """
    rows = [get_root(root_id)] if root_id else roots()
    if not rows:
        return {"items": [], "seed": seed, "page": 0, "cursor": 0, "count": 0,
                "pool": 0, "has_more": False, "at": None, "truncated": False}

    count = max(1, min(int(count or 60), MAX_PAGE_SIZE))
    page = max(0, int(page or 0))
    count = int(count)
    if seed is None or str(seed).strip() == "":
        seed = random.randrange(1, 2 ** 31 - 1)
    else:
        seed = int(seed)
    rng = random.Random(seed)

    album_pair = None
    if album:
        if isinstance(album, (list, tuple)) and len(album) == 2:
            album_pair = (int(album[0]), str(album[1]))
        else:
            album_pair = (rows[0]["id"], str(album))

    if favorites_only:
        pool = _favorite_pool(rows, min_bytes=min_bytes)
        truncated = False
        at = None
    else:
        pool, truncated = _candidates(rows, album=album_pair, min_bytes=min_bytes)
        at = min([build_index(r["id"])["at"] for r in rows]) if rows else None

    deck = _deck(pool, rng, mode)
    cursor = page * count
    items = _with_favorite(
        deck[cursor: cursor + count], {row["id"]: row["path"] for row in rows})
    return {
        "items": items,
        "seed": seed,
        "page": page,
        "cursor": cursor,
        "count": len(items),
        "pool": len(pool),
        "has_more": cursor + len(items) < len(pool),
        "at": at,
        "truncated": truncated,
    }


# ---- 往年今日 --------------------------------------------------------------
def on_this_day(root_id=None, per_year=6, limit=60, today=None):
    """"去年的今天、前年的今天" —— 同月同日、但**不是今年**的照片, 按年份分组。

    对标的 Immich `Memories` / Google Photos 的 "On This Day": 两者都把它当作
    "离开之后最想念的那一个功能"。理由不难理解 —— 一个相册集越是庞大, 人越不会
    翻到三年前那个文件夹; 而"今天"这个锚点不需要用户记得任何事。

    ⚠️ **口径是文件的修改时间(mtime), 不是拍摄时间。** 这是刻意的取舍:
    拍照片的 EXIF 需要新增依赖(且大量下载来的图片根本没有 EXIF), 而 mtime 是
    文件系统白给的。代价必须说清楚 —— 复制/移动/重新导出会**刷新 mtime**, 于是
    "整批导入的照片"会挤在同一天, 而不是分散在它们的拍摄日。所以界面上要写
    "按文件修改时间", 不能写成"按拍摄时间"。宁可少说, 不可说错。

    ⚠️ `today` 参数是为了**不拿墙钟当判据**(第 25 条): 测试要能传入一个确定的
    日期, 而不是靠"把系统时间改掉"或"造一个恰好是今天的文件"。

    返回 `{date, years, total, roots}`: `years` 按年份倒序, 每年最多 `per_year`
    张(用当天日期做种子抽, 于是**同一天刷新页面看到的是同一批**, 不会每次都不一样)。
    """
    now = today or time.localtime()
    month, day, this_year = now.tm_mon, now.tm_mday, now.tm_year
    rows = [get_root(root_id)] if root_id else roots()

    by_year = {}
    briefs = []
    for row in rows:
        index = build_index(row["id"])
        briefs.append(index_brief(index))
        root_path = Path(row["path"])
        for rel, files in index["order"].items():
            for f in files:
                shot = time.localtime(f["mtime"])
                if shot.tm_mon != month or shot.tm_mday != day or shot.tm_year == this_year:
                    continue
                item = {
                    "root_id": row["id"],
                    "root_name": row.get("name") or root_path.name or row["path"],
                    "name": f["name"],
                    "rel": "%s/%s" % (rel, f["name"]) if rel else f["name"],
                    "album": rel,
                    "size": f["size"],
                    "mtime": f["mtime"],
                    "year": shot.tm_year,
                }
                by_year.setdefault(shot.tm_year, []).append(item)

    years = []
    total = 0
    # ⚠️ 种子用"今天"而不是"每次随机": 一年内可能有一千张符合, 每次刷新换一批
    # 就等于这个功能没有记忆点 —— 而"今天看到的该是同一批"正是 Memories 的形态。
    rng = random.Random("%04d-%02d-%02d" % (this_year, month, day))
    for year in sorted(by_year, reverse=True):
        picks = by_year[year]
        rng.shuffle(picks)
        if total >= max(1, int(limit or 60)):
            break
        room = max(1, int(limit or 60)) - total
        page = picks[: min(max(1, int(per_year or 6)), room)]
        if not page:
            continue
        total += len(page)
        years.append({"year": year, "age": this_year - year, "photos": page})
    return {
        "date": "%02d-%02d" % (month, day),
        "years": years,
        "total": total,
        "roots": briefs,
        "basis": "mtime",
    }


# ---- 重复标记 --------------------------------------------------------------
#: 一次查重复最多解码多少张。每张要起一次 ffmpeg 子进程(见 core/phash),
#: 500 张就是 500 次 —— 放在一次 HTTP 请求里跑完 5 万张是不现实的, 所以宁可
#: **明确截断并报出来**, 也不要让请求挂到超时(挂到超时的后果是用户以为坏了)。
MAX_DUPLICATE_SCAN = 400


def duplicates(root_id, rel="", threshold=None, limit=MAX_DUPLICATE_SCAN):
    """找出一个相册里**疑似重复**的照片对。只标记, 绝不删(见 core/phash 约束 1)。

    对标的 Immich duplicate detection / Billfish 的重复检测。它们的价值不在
    "省空间"(删文件才省), 而在**指着告诉你哪几张是同一张** —— 人自己看不出来
    两个不同尺寸/不同压缩的同源图是不是一张。

    ⚠️ 三个必须分开的结果(合并任何一个都会造出假绿):

      * `reason="no-decoder"` —— 本机没有 ffmpeg, **一张都没算**。这时候返回空
        列表等于说"没有重复", 而真实情况是"没验过"。这是本函数最要紧的一条:
        一个附件能力在环境缺失时**假装成功**, 比失败糟糕得多。
      * `undecodable=n` —— 有那么几张解不开(坏文件/冷门格式), 其余照常比。
      * `pairs=[]` 且 reason 为 None —— 真比过了, 确实没找到重复。

    返回 `{pairs, scanned, skipped, undecodable, flat, threshold, reason}`。`pairs`
    里每项 `{a, b, distance}`, `distance` 是 64 位指纹的海明距离(越小越像)。

    ⚠️ `flat` 是**纯色/无梯度**的照片数: dHash 只比较相邻像素谁更亮, 所以一张纯红
    和一张纯蓝的指纹完全相同。让它们参与比对, 结果是一个文件夹里的纯色截图彼此
    互指成"全是重复" —— 所以这类图**不进比对池**, 并且把数量报出来(理由同
    `excluded`: 没有计数的话, "一张都没比"和"没有重复"长得一样)。
    """
    from core import phash

    row = get_root(root_id)
    root = Path(row["path"]).resolve()
    rel = str(rel or "").replace("\\", "/").strip("/")
    found = _list_album(root, rel, row["id"])

    cap = max(1, min(int(limit or MAX_DUPLICATE_SCAN), MAX_DUPLICATE_SCAN))
    scanned_items = found[:cap]
    skipped = max(0, len(found) - len(scanned_items))
    limit_dist = phash.DEFAULT_THRESHOLD if threshold is None else int(threshold)

    hashes = []
    undecodable = 0
    flat = 0
    # ⚠️ `no_decoder` 与 `undecodable` **必须分开计数**, 而且 reason 只能由前者决定。
    # 第一版图省事写成了 `reason = "no-decoder" if not hashes`, 于是"相册里全是坏图"
    # (每张都 DECODE_FAILED)会被报成"本机没有 ffmpeg" —— 这正是第 26 条要防的那种
    # 混淆: 一个关于**文件**的结论, 被说成了关于**机器**的结论。
    no_decoder = 0
    for item in scanned_items:
        try:
            path = safe_photo(row["id"], item["rel"])
        except RootError:
            continue
        raw, why = phash.decode_gray_ex(str(path))
        if not raw:
            if why == phash.NO_DECODER:
                no_decoder += 1      # 机器的事: 这一张**根本没机会被验**
            else:
                undecodable += 1     # 文件的事: 验了, 解不开
            continue
        # 纯色/无梯度图: 它们的指纹恒等, 参与了就会互相指认成一整片假重复。
        # 所以这里不是"算出来再过滤", 而是**根本不让它们进比对池**。
        if phash.is_flat_gray(raw):
            flat += 1
            continue
        value = phash.dhash_from_gray(raw)
        if not value:
            undecodable += 1
            continue
        hashes.append((item, value))

    reason_out = "no-decoder" if no_decoder else None
    if not hashes:
        return {
            "pairs": [],
            "scanned": 0,
            "skipped": skipped,
            "undecodable": undecodable,
            "flat": flat,
            "threshold": limit_dist,
            "reason": reason_out,
            "album": {"root_id": row["id"], "rel": rel},
        }

    pairs = []
    for i in range(len(hashes)):
        a_item, a_hash = hashes[i]
        for j in range(i + 1, len(hashes)):
            b_item, b_hash = hashes[j]
            dist = phash.distance(a_hash, b_hash)
            if dist is None or dist > limit_dist:
                continue
            pairs.append({"a": a_item, "b": b_item, "distance": dist})
    # 距离小的排前面: 最像的那几对才是用户想看的
    pairs.sort(key=lambda p: (p["distance"], _natural_key(p["a"]["rel"])))
    return {
        "pairs": pairs,
        "scanned": len(hashes),
        "skipped": skipped,
        "undecodable": undecodable,
        "flat": flat,
        "threshold": limit_dist,
        "reason": reason_out,
        "album": {"root_id": row["id"], "rel": rel},
    }


# ---- 收藏 ------------------------------------------------------------------
def favorites():
    """所有收藏(含源文件已经不在的那些)。

    ⚠️ 返回里带上 `exists`: 用户删掉/移走文件之后, 收藏记录仍然在库里。
    静默把它过滤掉, 用户就会看到"收藏数少了但不知道少了谁"; 标出来才可解释。
    """
    out = []
    for row in db.query("SELECT path, created_at FROM local_favorites ORDER BY created_at DESC"):
        path = row["path"]
        try:
            exists = Path(path).is_file()
        except OSError:
            exists = False
        out.append({"path": path, "created_at": row["created_at"], "exists": exists})
    return out


def favorite_keys():
    """收藏的**归一化绝对路径**集合。给"这一张收藏了吗"做 O(1) 判定。

    用 `_norm_key` 而不是原样字符串: Windows 上同一个文件可能有三种写法
    (大小写 / `/` 与 `\\` / 短名), 不归一就会出现"收藏了但星号不亮"。
    """
    return {_norm_key(r["path"]) for r in db.query("SELECT path FROM local_favorites")}


def _resolve_favorite(fav, resolved_roots, min_bytes=0):
    """一条收藏记录现在指向哪张照片。返回 `(照片, 失效原因)` —— 两者必有一个。

    **只有这一处**做这件事是有原因的: 随机池的"只看收藏"与收藏页的列表必须给出
    同一批照片。分成两处实现, 迟早一边认得出、一边认不出, 而表现是"收藏了 5 张,
    随机池里只出现 3 张" —— 差的那两张没有任何提示。
    """
    if not fav.get("exists"):
        return None, KIND_NOT_FOUND
    try:
        path = Path(fav["path"]).resolve()
        if not _is_image(path.name):
            return None, KIND_NOT_IMAGE
        size = path.stat().st_size
    except OSError:
        return None, KIND_NOT_FOUND
    if size < min_bytes:
        # 用户把体积门槛调高了 —— 这不是"收藏失效", 但两者在界面上都表现为
        # "我收藏的那张不在这儿", 所以同样要能被说出来。
        return None, KIND_NOT_FOUND
    best = None
    for root, row in resolved_roots:
        # 取**最长**的那个根 = 最具体的一个(同一个文件可能落在两个嵌套的根里)。
        if _within(path, root) and (best is None or len(str(root)) > len(str(best[0]))):
            best = (root, row)
    if best is None:
        # 收藏还在, 但那个目录已经不再被登记了 —— 这与"文件没了"不是一回事,
        # 处置方式也不同(一个是重新登记某个目录, 一个是去找文件)。
        return None, KIND_UNKNOWN_ROOT
    root, row = best
    rel = str(path.relative_to(root)).replace("\\", "/")
    album = rel.rsplit("/", 1)[0] if "/" in rel else ""
    display = row.get("name") or Path(row["path"]).name or row["path"]
    return {
        "root_id": row["id"],
        "root_name": display,
        "root_path": row["path"],
        "album": album,
        "album_name": _album_name(root, album, row.get("name")),
        "album_key": "%d|%s" % (row["id"], album),
        "name": path.name,
        "rel": rel,
        "size": size,
        "mtime": path.stat().st_mtime,
        "created_at": fav.get("created_at"),
    }, None


def _resolved_roots(rows):
    return [(Path(r["path"]).resolve(), r) for r in rows]


def favorites_view(offset=0, limit=200, min_bytes=0):
    """收藏页的数据: `items` 是能出图的, `stale` 是解析不出来的。

    ⚠️ 失效的那些**不许静默丢掉**: 用户删了文件、或取消了某个目录的登记之后,
    收藏数会变小而他不知道少了谁。"少了东西但不说"与"东西坏了"在界面上必须是
    两件事 —— 与本项目第 9 条坑同源(把"人看的文案"当数据)。
    """
    resolved = _resolved_roots(roots())
    items, stale = [], []
    for fav in favorites():
        item, reason = _resolve_favorite(fav, resolved, min_bytes)
        if item is None:
            stale.append({"path": fav["path"], "created_at": fav["created_at"],
                          "reason": reason})
        else:
            items.append(item)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), MAX_PAGE_SIZE))
    return {"items": items[offset: offset + limit], "total": len(items),
            "stale": stale, "offset": offset, "limit": limit}


def forget_favorite(raw):
    """删掉一条收藏记录。**只删我们库里那一行, 不碰磁盘。**

    存在的理由: 源文件已经没了、或者那个目录不再被登记之后, 这条记录就没有任何
    办法被清掉 —— 一个永远删不掉的失效条目比没有收藏功能更难看。
    """
    text = str(raw or "").strip()
    if not text:
        return False
    cur = db.execute("DELETE FROM local_favorites WHERE path=?", (text,))
    return bool(cur.rowcount)


def _favorite_pool(rows, min_bytes=0):
    """把收藏解析成"能直接出图"的条目(随机池的 favorites_only 模式用)。"""
    resolved = _resolved_roots(rows)
    pool = []
    for fav in favorites():
        item, _reason = _resolve_favorite(fav, resolved, min_bytes)
        if item is not None:
            pool.append(item)
    return pool


def set_favorite(root_id, rel, value=True):
    """收藏/取消收藏一张照片。

    ⚠️ 只接受**此刻真实存在、且在本相册集内**的照片(`safe_photo` 会验证):
    收藏一个拼错的名字没有意义, 而验过之后存的绝对路径一定指向真实文件。
    """
    path = safe_photo(root_id, rel)
    key = str(path)
    if value:
        db.execute(
            "INSERT OR IGNORE INTO local_favorites(path, created_at) VALUES(?,?)",
            (key, db.now_ts()),
        )
    else:
        db.execute("DELETE FROM local_favorites WHERE path=?", (key,))
    return {"path": key, "favorite": bool(value)}


# ---- 缓存维护 --------------------------------------------------------------
def _dir_bytes(path):
    total = 0
    count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                        count += 1
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return total, count


def thumb_cache_info():
    """缩略图缓存的占用 —— 不做成界面就等于"程序在偷偷吃盘"。"""
    total, count = _dir_bytes(thumbs.thumb_dir(thumb_base()))
    return {"bytes": total, "files": count, "path": str(thumb_base())}


def prune_thumbs():
    """清掉"现在看不见的照片"的缩略图, 返回删除数。

    判据只有一条: **当前索引里能看见的照片**。所以未登记的根、超出深度或落在
    跳过名单里的目录中的缩略图会被一并清掉 —— 代价只是下次重新生成, 不是丢数据。
    (反过来"宁可留着"的做法也成立, 但这里选择清: 缓存唯一的作用是加速, 而
    一个只增不减的缓存最终会变成用户磁盘上看不见的账。)
    """
    known = []
    for row in roots():
        index = build_index(row["id"])
        base = Path(index["path"])
        for rel, photos in index["order"].items():
            here = base if not rel else base / rel
            known.extend(str(here / p["name"]) for p in photos)
    return thumbs.prune(thumb_base(), lambda: known)


def stats():
    """顶栏用的合计。数字来自**登记表**(= 最近一次扫描的结果), 带时刻。"""
    rows = roots()
    return {
        "roots": [
            {
                "id": r["id"],
                "path": r["path"],
                "name": r.get("name"),
                "last_scan": r.get("last_scan"),
                "albums": r.get("album_count") or 0,
                "photos": r.get("photo_count") or 0,
                "error": r.get("error"),
            }
            for r in rows
        ],
        "root_count": len(rows),
        "album_count": sum((r.get("album_count") or 0) for r in rows),
        "photo_count": sum((r.get("photo_count") or 0) for r in rows),
        "favorite_count": len(favorites()),
        "thumb_cache": thumb_cache_info(),
        "image_suffixes": sorted(IMAGE_SUFFIXES),
    }
