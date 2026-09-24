"""本地相册集(`core/localalbums.py` + `api/local.py`)的判据。

这个功能的全部风险都集中在**一条边界**上: 用户登记的是**他自己的目录**。
所以下面按重要性排序, 前两组是重点:

1. **只读**: 它绝不能写/改/删根内的任何东西。这一条不只靠运行时断言(那种断言
   只能证明"这一次没写"), 还靠一条**结构化**判据 —— 直接扫源码里有没有写操作
   (`test_the_module_contains_no_write_calls`)。理由是本项目的第 ⑦ 条心法:
   同一个错出现第三遍就抽成程序可执行的规则; 而这里的错第一次出现就是不可逆的。
2. **越界**: `(root_id, rel)` 必须经得起 `../`、绝对路径、指向根外的软链接。
   判据断言的是**代号**(`RootError.kind`)而不是中文文案 —— 后者是假红。
3. **口径**: 相册照片列表是**实时**的, 随机池是**索引快照**; 两者的偏差必须被
   封顶(改一张照片后, 随机池不许再给出它), 否则"随机里点开是坏的"会成为一个
   查不到源头的现象。
4. **接口**: 出图只收 `(root_id, rel)`, 没有任何接口收裸的绝对路径。
"""

import ast
import os
import shutil
from pathlib import Path

import pytest

from core import localalbums as la
from core import thumbs
from core.config import settings


# --------------------------------------------------------------------------
# 夹具与工具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_index_cache():
    """`core/localalbums._cache` 是**进程内**的, 而根 id 在每个用例的临时库里都从 1
    重数 —— 不清就会让"上一个用例的索引"被当成这一个用例的(症状是连跑红、单跑绿)。

    `build_index` 里还比对了 `path` 兜底, 但那只挡得住"路径不同"; 路径相同而内容
    不同的情况(同一个 tmp 复用了同一段路径)挡不住, 所以还是要清。
    """
    la.invalidate()
    yield
    la.invalidate()


@pytest.fixture(autouse=True)
def _fresh_index_every_call(monkeypatch):
    """默认关掉索引 TTL, 让"扫一次"这件事在每个用例里都是显式的。

    需要测 TTL 的用例自己把它设回去。不这么做的话, 每个用例都得记住
    `settings` 是全局单例 —— 而"忘了设"的表现是**用例之间互相看见**。
    """
    monkeypatch.setattr(settings, "local_album_ttl", 0.0)
    yield


def _make_tree(base: Path):
    """一个覆盖面广的小相册集:

    * `旅行/` 三张图(含 `2.jpg` / `10.jpg` 用来验自然序)
    * `照片/` 一张图 + 一个非图片 + 一个 `_meta/`(验黑名单)
    * 根目录一张 `cover.jpg`(验"根目录自己也是一个相册")
    * `.hidden/`(验隐藏目录跳过)
    * `空目录/`(验空目录不算相册)
    """
    (base / "旅行").mkdir(parents=True)
    for n, size in (("a.jpg", 100), ("10.jpg", 200), ("2.jpg", 300)):
        (base / "旅行" / n).write_bytes(b"x" * size)
    (base / "照片").mkdir()
    (base / "照片" / "b.png").write_bytes(b"y" * 50)
    (base / "照片" / "notes.txt").write_text("不是图片", encoding="utf-8")
    (base / "照片" / "_meta").mkdir()
    (base / "照片" / "_meta" / "junk.jpg").write_bytes(b"z" * 9)
    (base / "cover.jpg").write_bytes(b"c" * 11)
    (base / ".hidden").mkdir()
    (base / ".hidden" / "h.jpg").write_bytes(b"h" * 7)
    (base / "空目录").mkdir()
    return base


def _tree(base):
    """把一棵目录树拍成指纹, 用来断言"一个字节都没动"。"""
    out = {}
    for p in sorted(base.rglob("*")):
        rel = str(p.relative_to(base))
        st = p.stat()
        out[rel] = (st.st_size, st.st_mtime_ns, p.is_dir())
    return out


def _registered(tmp_path, name="我的照片"):
    base = _make_tree(tmp_path / "photos")
    root = la.add_root(str(base), name)
    return base, root


def _album(view, rel):
    for item in view["items"]:
        if item["rel"] == rel:
            return item
    raise AssertionError("没有相册 %r, 实际有 %s" % (rel, [i["rel"] for i in view["items"]]))


# --------------------------------------------------------------------------
# ① 只读边界
# --------------------------------------------------------------------------

#: 明确**只可能**是"往磁盘写"的方法。`open` 单独处理(要看 mode)。
_FORBIDDEN_WRITES = {
    "write_text", "write_bytes", "unlink", "rmdir", "mkdir", "makedirs",
    "rmtree", "rename", "remove", "truncate", "touch", "symlink_to", "chmod",
    "copy", "copy2", "copytree", "move",
}

#: `Path.replace` **不在这里**: `rel.replace("\\", "/")` 是路径归一, 与落盘无关,
#: 而 AST 分不出接收者是 str 还是 Path。误报的代价是"下一个人为了让它绿而删掉
#: 一行正确的代码", 所以这一条靠**运行时**的 `_tree` 比对兜底, 而不是靠名字。
def _write_calls(path):
    """扫源码里所有"可能落盘"的调用点, 返回 [(行号, 名字)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name is None:
            continue
        if name in _FORBIDDEN_WRITES:
            hits.append((node.lineno, name))
        elif name == "open":
            mode = "r"
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(c in mode for c in "wax+"):
                hits.append((node.lineno, "open(%s)" % mode))
    return hits


def test_the_module_contains_no_write_calls():
    """**只读**不许只是注释里的一句承诺。

    这条判据是结构化的: 它扫 `core/localalbums.py` 里有没有任何"写磁盘"的调用。
    用户登记的是他自己的目录, 而这里的错**第一次发生就是不可逆的** —— 所以它
    不该等到某次运行时断言才发现。

    (唯一允许的落盘是缩略图缓存, 而它在**我们自己的数据目录**里, 用的是
    `core/thumbs.py` —— 所以它不经过本模块的直接调用。)
    """
    src = Path(la.__file__)
    hits = _write_calls(src)
    assert hits == [], (
        "本地相册模块出现了落盘调用 %s —— 用户的相册目录必须是只读的。"
        "真要缓存, 缓存根只能用 `thumb_base()`(在我们自己的数据目录下)。" % hits)


def test_scanning_and_browsing_leave_the_root_byte_for_byte_unchanged(tmp_path):
    """跑一遍全部读路径, 回来后根的指纹必须一模一样。

    ⚠️ 比的是 **size + mtime_ns**, 不是"文件还在不在": 往用户的照片上追加一个
    字节、或者用 `touch` 改一下时间, 都会让相册软件的排序变掉 —— 那是"改了用户
    的东西", 而不是"没删用户的东西"。
    """
    base, root = _registered(tmp_path)
    before = _tree(base)

    la.albums(refresh=True)
    la.photos(root["id"], "旅行")
    la.random_photos(count=10, seed=1)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    la.random_photos(count=10, seed=2, favorites_only=True)
    la.stats()
    la.prune_thumbs()
    la.build_index(root["id"], force=True)

    assert _tree(base) == before


def test_the_thumbnail_cache_lives_outside_the_root(tmp_path):
    """缓存必须落在我们自己的数据目录里 —— 写进用户的相册目录就是污染。"""
    base, root = _registered(tmp_path)
    cache = la.thumb_base().resolve()
    assert not cache.is_relative_to(base.resolve())
    la.albums()      # 触发一次扫描(它不产缩略图, 但这是"浏览"的正常入口)
    la.prune_thumbs()
    assert not (base / "_meta").exists(), "用户的相册目录里出现了 _meta/"


def test_registering_a_root_that_contains_our_cache_is_refused(tmp_path, monkeypatch):
    """把本程序自己的数据目录登记成相册集 = 相册列表里全是我们的缩略图。

    ⚠️ 这条判据**不依赖缓存目录已经存在**: 用 resolve() 做词法归一,
    否则"刚装好、还没生成过缩略图"时它就是一个永远绿的闸。
    """
    monkeypatch.setattr(la.db, "DB_PATH", tmp_path / "state" / "collector.db")
    la.thumb_base().resolve()          # tmp_path/state/local_albums
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    with pytest.raises(la.RootError) as e:
        la.add_root(str(state))
    assert e.value.kind == la.KIND_INSIDE_CACHE


def test_removing_a_root_does_not_touch_the_disk(tmp_path):
    base, root = _registered(tmp_path)
    before = _tree(base)
    la.remove_root(root["id"])
    assert la.roots() == []
    assert _tree(base) == before


def test_removing_a_root_keeps_the_favorites(tmp_path):
    """收藏是用户的标记, 不是这个登记的附属物 —— 一并抹掉就是"用一次点击删掉
    没让用户看过的东西"。"""
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    la.remove_root(root["id"])
    assert len(la.favorites()) == 1


# --------------------------------------------------------------------------
# ② 越界
# --------------------------------------------------------------------------

def test_a_relative_path_escaping_the_root_is_refused(tmp_path):
    base, root = _registered(tmp_path)
    (tmp_path / "secret.jpg").write_bytes(b"s" * 4)
    with pytest.raises(la.RootError) as e:
        la.safe_photo(root["id"], "../secret.jpg")
    assert e.value.kind == la.KIND_ESCAPES_ROOT


def test_an_absolute_path_outside_the_root_is_refused(tmp_path):
    """绝对路径会把 `/` 左边的根整个顶掉(`Path(root) / "D:/x.jpg"` == `D:/x.jpg`),
    所以它是最经典的一种越界 —— 判据仍然是"归一之后在不在根内"。"""
    base, root = _registered(tmp_path)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"s" * 4)
    with pytest.raises(la.RootError) as e:
        la.safe_photo(root["id"], str(secret))
    assert e.value.kind == la.KIND_ESCAPES_ROOT


def test_an_absolute_path_inside_the_root_is_just_another_way_to_name_it(tmp_path):
    """**绝对路径本身不代表越界** —— 判据是"归一之后在哪", 不是"看起来像不像绝对路径"。

    根内的绝对路径与相对路径指向同一个文件、满足同一个信任锚, 所以放行。钉住它是
    为了两件事: ① 别有人"为了安全"改成"必须以相对路径开头"的检查 —— 那会拒掉一条
    完全合法的路径, 而表现只是"有时打不开"; ② 同一个信任锚必须**只有一条判据**,
    `../` 与绝对路径走的是同一段 `resolve() + is_relative_to`。
    """
    base, root = _registered(tmp_path)
    target = la.safe_photo(root["id"], "旅行/a.jpg")
    assert la.safe_photo(root["id"], str(target)) == target


def test_a_normalised_path_that_stays_inside_the_root_is_allowed(tmp_path):
    """`旅行/../旅行/a.jpg` 里的 `..` 归一之后仍在根内 —— 放行。

    判据是"归一后在哪", 不是"字符串里有没有 `..`"。写成后者会拒掉一条完全合法的
    路径, 而这种"为了安全把人挡在门外"的错误最难被发现(用户只会觉得"有时打不开")。
    """
    base, root = _registered(tmp_path)
    assert la.safe_photo(root["id"], "旅行/../旅行/a.jpg") == (base / "旅行" / "a.jpg")


def test_a_symlink_pointing_outside_the_root_is_refused(tmp_path):
    base, root = _registered(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.jpg").write_bytes(b"o" * 5)
    link = base / "旅行" / "link.jpg"
    try:
        link.symlink_to(outside / "real.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("[platform:posix] 建软链接需要权限(Windows 上通常要管理员或开发者模式)")
    with pytest.raises(la.RootError) as e:
        la.safe_photo(root["id"], "旅行/link.jpg")
    assert e.value.kind == la.KIND_ESCAPES_ROOT


def test_a_symlinked_directory_outside_the_root_is_not_listed_as_an_album(tmp_path):
    """根外的软链接目录不该出现在相册列表里。

    它是"看得见、点不开"的来源: 列表里有它, 而每张照片都会在 `safe_photo` 上
    被拒 —— 用户看到的是"相册是空的/点开就报错", 完全联想不到软链接。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.jpg").write_bytes(b"o" * 5)
    base = _make_tree(tmp_path / "photos")
    try:
        (base / "外链").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("[platform:posix] 建软链接需要权限(Windows 上通常要管理员或开发者模式)")
    root = la.add_root(str(base))
    assert "外链" not in {a["rel"] for a in la.albums()["items"]}


def test_a_non_image_is_refused_without_pretending_it_is_missing(tmp_path):
    """非图片回 `not-image`(**接口层是 415**), 不是 404。

    伪装成 404 就把"调用方传错了"和"文件真的没了"混成同一件事 —— 而这两件事要
    两套排查方向。
    """
    base, root = _registered(tmp_path)
    with pytest.raises(la.RootError) as e:
        la.safe_photo(root["id"], "照片/notes.txt")
    assert e.value.kind == la.KIND_NOT_IMAGE


def test_a_missing_photo_is_not_found(tmp_path):
    base, root = _registered(tmp_path)
    with pytest.raises(la.RootError) as e:
        la.safe_photo(root["id"], "旅行/不存在.jpg")
    assert e.value.kind == la.KIND_NOT_FOUND


def test_an_unknown_root_is_not_found(tmp_path):
    _registered(tmp_path)
    with pytest.raises(la.RootError) as e:
        la.safe_photo(98765, "a.jpg")
    assert e.value.kind == la.KIND_UNKNOWN_ROOT


def test_an_album_path_escaping_the_root_is_refused(tmp_path):
    base, root = _registered(tmp_path)
    with pytest.raises(la.RootError) as e:
        la.photos(root["id"], "../..")
    assert e.value.kind == la.KIND_ESCAPES_ROOT


# --------------------------------------------------------------------------
# ③ 登记
# --------------------------------------------------------------------------

def test_the_same_directory_cannot_be_registered_twice(tmp_path):
    base = _make_tree(tmp_path / "photos")
    la.add_root(str(base))
    with pytest.raises(la.RootError) as e:
        la.add_root(str(base))
    assert e.value.kind == la.KIND_DUPLICATE


@pytest.mark.skipif(os.name != "nt",
                    reason="[platform:windows] 只有 Windows 的路径是大小写不敏感的")
def test_a_differently_cased_path_is_the_same_directory_on_windows(tmp_path):
    """Windows 上 `D:\\Photos` 与 `d:\\photos` 是同一个目录。

    去重因此在 Python 里用 `os.path.normcase` 判, 而**没有**在表上加 UNIQUE ——
    `normcase` 在 Linux 上是恒等函数, 而 SQLite 的 NOCASE 只折叠 ASCII 且与平台
    无关: 加一个 UNIQUE 只会在其中一边形成假判据。
    """
    base = _make_tree(tmp_path / "photos")
    la.add_root(str(base))
    with pytest.raises(la.RootError) as e:
        la.add_root(str(base).upper())
    assert e.value.kind == la.KIND_DUPLICATE


def test_a_missing_directory_is_rejected(tmp_path):
    with pytest.raises(la.RootError) as e:
        la.add_root(str(tmp_path / "不存在的目录"))
    assert e.value.kind == la.KIND_NOT_FOUND


def test_a_file_is_not_a_directory(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(la.RootError) as e:
        la.add_root(str(f))
    assert e.value.kind == la.KIND_NOT_DIR


def test_an_empty_path_is_rejected(tmp_path):
    with pytest.raises(la.RootError) as e:
        la.add_root("   ")
    assert e.value.kind == la.KIND_NOT_FOUND


def test_rename_only_changes_the_display_name(tmp_path):
    base, root = _registered(tmp_path)
    again = la.rename_root(root["id"], "改名了")
    assert again["name"] == "改名了"
    assert again["path"] == root["path"]


def test_registering_does_not_scan(tmp_path):
    """登记是**一次 INSERT**, 不扫盘。

    扫描可能要走几万个文件; 把它塞进一次 POST 里, 用户按下"添加"之后界面会僵住
    好几十秒, 而且这段时间里没有任何进度可言。
    """
    base = _make_tree(tmp_path / "photos")
    root = la.add_root(str(base))
    assert root["last_scan"] is None
    assert root["album_count"] == 0
    # 但拉一次相册列表就会扫(冷缓存必然扫)
    view = la.albums()
    assert view["roots"][0]["at"] is not None
    assert view["roots"][0]["error"] is None


def test_a_readable_root_reports_an_error_instead_of_claiming_zero(tmp_path, monkeypatch):
    """目录被拔掉/读不到时, **不许**只报"0 张照片"。

    "这个目录是空的"与"这个目录我读不到"在界面上长得一样, 而前者会让用户以为
    自己的照片没了。
    """
    base, root = _registered(tmp_path)
    monkeypatch.setattr(settings, "local_album_ttl", 999.0)
    gone = tmp_path / "photos"
    shutil.rmtree(gone)          # 模拟: 拔掉移动硬盘
    index = la.build_index(root["id"], force=True)
    assert index["error"], "读不到的根必须留下 error"
    assert index["photos"] == 0
    row = la.roots()[0]
    assert row["error"] and row["last_scan"] is not None


# --------------------------------------------------------------------------
# ④ 扫描口径
# --------------------------------------------------------------------------

def test_an_album_is_a_directory_that_directly_contains_photos(tmp_path):
    base, root = _registered(tmp_path)
    rels = {a["rel"] for a in la.albums()["items"]}
    assert rels == {"", "旅行", "照片"}


def test_the_root_album_is_named_after_the_registration(tmp_path):
    """根目录下直接放着的照片也要有入口, 且用**登记名** ——

    用户把 `D:\\photos` 登记成"2024 旅行"时, 给这批照片挂上目录名 `photos`
    只会让他以为自己点错了。
    """
    base, root = _registered(tmp_path, name="2024 旅行")
    assert _album(la.albums(), "")["name"] == "2024 旅行"


def test_empty_directories_are_not_albums(tmp_path):
    base, root = _registered(tmp_path)
    assert "空目录" not in {a["rel"] for a in la.albums()["items"]}


def test_our_own_meta_directory_is_skipped(tmp_path):
    """`downloads/_meta/` 里放的是清单与缩略图(一堆 .jpg) —— 用户把下载目录登记为
    相册集是主要用法之一, 不排除掉就会出现一堆叫 `101` 的"相册", 里面是我们自己
    生成的东西。"""
    base, root = _registered(tmp_path)
    assert "照片/_meta" not in {a["rel"] for a in la.albums()["items"]}
    assert _album(la.albums(), "照片")["photos"] == 1     # 只有 b.png


def test_hidden_directories_and_non_images_are_skipped(tmp_path):
    base, root = _registered(tmp_path)
    rels = {a["rel"] for a in la.albums()["items"]}
    assert ".hidden" not in rels
    names = [p["name"] for p in la.photos(root["id"], "照片")["items"]]
    assert names == ["b.png"]


def test_photos_within_an_album_are_in_natural_order(tmp_path):
    """`2.jpg` 必须排在 `10.jpg` 前面 —— 图集站下来的正是这种命名, 纯字符串排序
    会让相册看起来是乱的。"""
    base, root = _registered(tmp_path)
    assert [p["name"] for p in la.photos(root["id"], "旅行")["items"]] == [
        "a.jpg", "2.jpg", "10.jpg"]


def test_only_browser_renderable_suffixes_count_as_photos(tmp_path):
    """把 `.heic`/`.cr2` 收进来, 网格里就会出现一片打不开的破图 —— 而
    "格式不支持"与"文件坏了"在界面上是同一张脸。"""
    assert la._is_image("a.JPG") and la._is_image("b.webp") and la._is_image("c.png")
    assert not la._is_image("d.heic")
    assert not la._is_image("e.cr2")
    assert not la._is_image("f.txt")
    assert not la._is_image("没有扩展名")


def test_the_depth_limit_is_respected(tmp_path, monkeypatch):
    base = tmp_path / "photos"
    (base / "一级" / "二级" / "三级").mkdir(parents=True)
    (base / "一级" / "二级" / "三级" / "deep.jpg").write_bytes(b"d" * 3)
    la.add_root(str(base))

    monkeypatch.setattr(settings, "local_album_depth", 1)
    assert {a["rel"] for a in la.albums()["items"]} == set(), "深度 1 只该看根目录自己"

    monkeypatch.setattr(settings, "local_album_depth", 3)
    assert {a["rel"] for a in la.albums()["items"]} == {"一级/二级/三级"}


def test_truncation_is_reported_rather_than_silent(tmp_path, monkeypatch):
    """超过上限时**要说出来**。静默截断会让用户以为照片丢了。"""
    base = tmp_path / "photos"
    (base / "相册").mkdir(parents=True)
    for i in range(6):
        (base / "相册" / ("%d.jpg" % i)).write_bytes(b"x" * 4)
    la.add_root(str(base))
    monkeypatch.setattr(settings, "local_album_max_photos", 4)
    view = la.albums(refresh=True)
    brief = view["roots"][0]
    assert brief["truncated"] is True
    assert brief["photos"] == 4


def test_albums_can_be_searched_and_sorted(tmp_path):
    base, root = _registered(tmp_path)
    assert {a["rel"] for a in la.albums(q="旅行")["items"]} == {"旅行"}
    by_photos = la.albums(sort="photos", order="desc")["items"]
    assert by_photos[0]["rel"] == "旅行"
    by_name = la.albums(sort="name", order="asc")["items"]
    assert [a["name"] for a in by_name] == sorted([a["name"] for a in by_name])


def test_albums_can_be_limited_to_one_root(tmp_path):
    first = _make_tree(tmp_path / "one")
    second = _make_tree(tmp_path / "two")
    r1 = la.add_root(str(first), "一")
    la.add_root(str(second), "二")
    view = la.albums(root_id=r1["id"])
    assert view["roots"] and len(view["roots"]) == 1
    assert {i["root_id"] for i in view["items"]} == {r1["id"]}


def test_photos_can_be_searched_and_paginated(tmp_path):
    base, root = _registered(tmp_path)
    assert [p["name"] for p in la.photos(root["id"], "旅行", q="a")["items"]] == ["a.jpg"]
    page = la.photos(root["id"], "旅行", offset=1, limit=1)
    assert page["total"] == 3 and [p["name"] for p in page["items"]] == ["2.jpg"]


# --------------------------------------------------------------------------
# ⑤ 随机池
# --------------------------------------------------------------------------

def test_the_same_seed_replays_the_same_page(tmp_path):
    base, root = _registered(tmp_path)
    a = [p["rel"] for p in la.random_photos(count=3, seed=42)["items"]]
    b = [p["rel"] for p in la.random_photos(count=3, seed=42)["items"]]
    assert a == b


def test_pages_do_not_overlap_and_cover_the_whole_pool(tmp_path):
    """`seed` + 递增 `page` = 一副可以一直往下翻的牌:
    各页**不重叠**, 也不缺项 —— 少一张就是"有些照片永远看不到", 而那是**看不见**的 bug。"""
    base, root = _registered(tmp_path)
    seen = []
    for page in range(3):
        data = la.random_photos(count=2, page=page, seed=5)
        seen.extend(p["rel"] for p in data["items"])
    assert len(seen) == len(set(seen)) == 5      # 池里一共 5 张
    assert la.random_photos(count=2, page=3, seed=5)["items"] == []


def test_has_more_is_exact(tmp_path):
    base, root = _registered(tmp_path)
    first = la.random_photos(count=3, page=0, seed=1)
    assert (first["pool"], first["has_more"]) == (5, True)
    last = la.random_photos(count=3, page=1, seed=1)
    assert (last["count"], last["has_more"]) == (2, False)


def test_album_mode_gives_every_album_a_turn_photo_mode_does_not(tmp_path):
    """两种取向的差别要能用数字看出来:

    * `mode="album"` 按**轮**发牌 —— 每轮从每个相册各取一张, 所以最小的相册也不会
      被 100 张的大相册淹掉。取 2 张时, 两个相册必然各出一张(这条是**确定**的)。
    * `mode="photo"` 是整池洗牌 —— 大相册按张数占优。取 1 张时它几乎总被抽中。
    """
    base = tmp_path / "photos"
    (base / "小").mkdir(parents=True)
    (base / "小" / "only.jpg").write_bytes(b"s" * 4)
    (base / "大").mkdir()
    for i in range(100):
        (base / "大" / ("%03d.jpg" % i)).write_bytes(b"x" * 4)
    la.add_root(str(base))

    albums = {p["album"] for p in la.random_photos(count=2, mode="album", seed=9)["items"]}
    assert albums == {"小", "大"}, "按轮发牌时 2 张必须来自两个相册"

    small = sum(
        1 for seed in range(200)
        if la.random_photos(count=1, mode="photo", seed=seed)["items"][0]["album"] == "小")
    assert small < 20, "整池洗牌时 1/101 的小相册不该有 10% 以上的机会(实测 %d/200)" % small


def test_min_bytes_filters_small_files(tmp_path):
    """过滤掉图标/缩略图那类小文件。"""
    base, root = _registered(tmp_path)
    data = la.random_photos(count=10, seed=3, min_bytes=150)
    assert data["items"] and all(p["size"] >= 150 for p in data["items"])
    assert data["pool"] == 2      # 300 与 200 的那两张


def test_random_from_a_single_album(tmp_path):
    base, root = _registered(tmp_path)
    data = la.random_photos(count=10, root_id=root["id"], album=(root["id"], "旅行"), seed=2)
    assert {p["album"] for p in data["items"]} == {"旅行"}
    assert data["pool"] == 3


def test_random_without_any_root_is_empty_not_an_error(tmp_path):
    """还没登记任何目录时, 随机池是空的 —— 而不是 500。"""
    data = la.random_photos(count=5)
    assert data["items"] == [] and data["pool"] == 0 and data["has_more"] is False


def test_a_freshly_deleted_photo_leaves_the_random_pool(tmp_path, monkeypatch):
    """**两套口径的关键约定**: 相册列表是实时的, 随机池是索引快照 —— 但打开相册
    这一步会把索引就地修正, 所以"刚删掉的照片还出现在随机池里"不该发生。

    拿一个长 TTL 来跑, 正是为了证明"修正在 TTL 之内也生效"; 否则用户会看到
    "随机里点开是坏的", 而这类现象极难怀疑到缓存上。
    """
    base, root = _registered(tmp_path)
    monkeypatch.setattr(settings, "local_album_ttl", 99999.0)
    la.albums()                                        # 建索引
    (base / "旅行" / "a.jpg").unlink()

    assert "a.jpg" not in {p["name"] for p in la.photos(root["id"], "旅行")["items"]}
    pool = {p["rel"] for p in la.random_photos(count=10, seed=1)["items"]}
    assert "旅行/a.jpg" not in pool


def test_a_new_photo_shows_up_after_the_ttl_expires(tmp_path, monkeypatch):
    base, root = _registered(tmp_path)
    monkeypatch.setattr(settings, "local_album_ttl", 99999.0)
    la.albums()
    (base / "旅行" / "new.jpg").write_bytes(b"n" * 8)
    assert "new.jpg" not in {p["name"] for p in la.random_photos(count=10, seed=1)["items"]}

    monkeypatch.setattr(settings, "local_album_ttl", 0.0)
    assert "new.jpg" in {p["name"] for p in la.random_photos(count=10, seed=1)["items"]}


def test_the_pool_reports_the_snapshot_time(tmp_path):
    """随机池必须能报"这个池子是什么时候的", 否则"怎么没抽到我刚放的那张"
    无从解释。"""
    base, root = _registered(tmp_path)
    data = la.random_photos(count=2, seed=1)
    assert isinstance(data["at"], float) and data["at"] > 0
    assert data["seed"] == 1 and data["page"] == 0


def test_marking_favorites_does_not_write_into_the_index_cache(tmp_path):
    """`photos()` 返回的那些 dict 就是**索引缓存里的同一批对象** —— 就地给它加一个
    `favorite` 键, 会把"这次查到的收藏状态"永久写进缓存, 于是取消收藏之后界面还是
    老样子。而"改了没反应"是最难被归因的一类现象, 所以这里钉住两份都要干净。
    """
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    assert [p["favorite"] for p in la.photos(root["id"], "旅行")["items"]] == [
        True, False, False]

    la.set_favorite(root["id"], "旅行/a.jpg", False)
    assert [p["favorite"] for p in la.photos(root["id"], "旅行")["items"]] == [
        False, False, False]
    idx = la.build_index(root["id"])
    assert all("favorite" not in p for p in idx["order"]["旅行"]), "缓存被污染了"


def test_the_random_pool_also_carries_the_favorite_flag(tmp_path):
    """前端要显示星号, 但它手上没有绝对路径 —— 标记必须由后端算好。
    让前端自己拼 `root_path + rel`, 会变成一个"只在某些目录下才对"的字符串
    (Windows 的分隔符)。"""
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    items = la.random_photos(count=10, seed=1)["items"]
    marked = {p["name"]: p["favorite"] for p in items}
    assert marked["a.jpg"] is True
    assert all(v is False for k, v in marked.items() if k != "a.jpg")


# --------------------------------------------------------------------------
# ⑥ 收藏
# --------------------------------------------------------------------------

def test_favorites_round_trip(tmp_path):
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    la.set_favorite(root["id"], "cover.jpg", True)
    assert len(la.favorites()) == 2
    la.set_favorite(root["id"], "旅行/a.jpg", False)
    assert [Path(f["path"]).name for f in la.favorites()] == ["cover.jpg"]


def test_favoriting_a_non_existent_photo_is_refused(tmp_path):
    base, root = _registered(tmp_path)
    with pytest.raises(la.RootError) as e:
        la.set_favorite(root["id"], "旅行/不存在.jpg", True)
    assert e.value.kind == la.KIND_NOT_FOUND


def test_a_favorite_whose_file_vanished_is_reported_not_hidden(tmp_path):
    """用户删掉文件之后, 收藏记录仍在库里。**静默过滤掉**会让他看到"收藏少了但
    不知道少了谁"; 标出来才可解释。"""
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    (base / "旅行" / "a.jpg").unlink()
    items = la.favorites()
    assert len(items) == 1 and items[0]["exists"] is False
    assert la.random_photos(count=5, favorites_only=True, seed=1)["items"] == []


def test_the_favorites_view_separates_usable_from_stale(tmp_path):
    """收藏页: 能出图的进 `items`, 解析不出来的进 `stale` 并带上原因。

    ⚠️ 判据是**原因代号**。失效有三种完全不同的处置("文件没了"去找文件、
    "那个目录不再登记"去重新登记、"不是图片"去查扩展名), 混成一句中文提示
    等于把三种排查方向压成一种。
    """
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    la.set_favorite(root["id"], "cover.jpg", True)
    (base / "旅行" / "a.jpg").unlink()

    view = la.favorites_view()
    assert view["total"] == 1 and [Path(p["rel"]).name for p in view["items"]] == ["cover.jpg"]
    assert [s["reason"] for s in view["stale"]] == [la.KIND_NOT_FOUND]

    la.remove_root(root["id"])
    view = la.favorites_view()
    assert view["total"] == 0
    # 顺序**不在契约里**(`favorites()` 按收藏时间倒序, 而两条可能落在同一毫秒),
    # 所以两边都排序再比 —— 断言一个没有承诺过的顺序, 就是给下一个人埋一次假红。
    assert sorted(s["reason"] for s in view["stale"]) == sorted(
        [la.KIND_NOT_FOUND, la.KIND_UNKNOWN_ROOT])


def test_a_stale_favorite_can_be_forgotten(tmp_path):
    """失效条目必须**删得掉**: 一个永远清不掉的失效记录比没有收藏功能更难看。"""
    base, root = _registered(tmp_path)
    la.set_favorite(root["id"], "旅行/a.jpg", True)
    (base / "旅行" / "a.jpg").unlink()
    path = la.favorites()[0]["path"]
    assert la.forget_favorite(path) is True
    assert la.favorites() == []
    assert la.forget_favorite(path) is False      # 已经没了, 不假装删成功


def test_prune_thumbs_removes_only_the_orphans(tmp_path, monkeypatch):
    """缓存的唯一作用是加速。清掉"现在看不见的照片"的缩略图时, 可见的那些必须留着 ——
    否则下一次滚动又要全部重算, 而这是**没有任何提示**的性能退化。"""
    base, root = _registered(tmp_path)
    live = base / "旅行" / "a.jpg"
    cache = la.thumb_base()
    thumbs.thumb_dir(cache).mkdir(parents=True, exist_ok=True)
    keep = thumbs.thumb_path(cache, live)
    orphan = thumbs.thumb_path(cache, base / "旅行" / "已经删掉的.jpg")
    keep.write_bytes(b"k")
    orphan.write_bytes(b"o")

    assert la.prune_thumbs() == 1
    assert keep.is_file() and not orphan.is_file()


# --------------------------------------------------------------------------
# ⑦ 接口层
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from api.local import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _kind(resp):
    """从错误响应里取**代号**。判据只挂在它上面, 不挂在中文文案上。"""
    detail = resp.json().get("detail")
    return detail.get("kind") if isinstance(detail, dict) else None


def test_the_endpoints_take_a_root_and_a_relative_path_never_an_absolute_one():
    """**没有任何接口收裸的绝对路径** —— 这条是结构化的判据。

    收绝对路径的接口就是给"任意文件读取"开门, 而它唯一的挡板只剩一句"请传合法
    路径" —— 那句话不在代码里。所以这里直接扫 `api/local.py` 的函数签名。
    """
    tree = ast.parse(Path(la.__file__).parent.parent.joinpath("api/local.py")
                     .read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            if arg.arg in ("path", "file", "local_path", "filename"):
                offenders.append("%s(%s)" % (node.name, arg.arg))
    assert offenders == [], (
        "本地相册的接口出现了按绝对路径取文件的参数 %s —— 越权校验只有一个信任锚: "
        "用户登记过的根, 所以出图必须收 (root_id, rel)。" % offenders)


def test_roots_can_be_added_listed_and_removed(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    r = client.post("/local/roots", json={"path": str(base), "name": "我的照片"})
    assert r.status_code == 200
    rid = r.json()["root"]["id"]

    got = client.get("/local/roots").json()
    assert [x["path"] for x in got["items"]] == [str(base)]
    assert got["stats"]["root_count"] == 1

    albums = client.get("/local/albums").json()
    assert {a["rel"] for a in albums["items"]} == {"", "旅行", "照片"}
    assert albums["roots"][0]["photos"] == 5

    assert client.delete("/local/roots/%d" % rid).status_code == 200
    assert client.get("/local/roots").json()["items"] == []
    assert base.is_dir(), "取消登记绝不能删磁盘上的目录"


def test_a_bad_root_is_rejected_with_a_machine_readable_kind(client, tmp_path):
    r = client.post("/local/roots", json={"path": str(tmp_path / "就没有这个目录")})
    assert r.status_code == 404
    assert _kind(r) == la.KIND_NOT_FOUND

    base = _make_tree(tmp_path / "photos")
    client.post("/local/roots", json={"path": str(base)})
    dup = client.post("/local/roots", json={"path": str(base)})
    assert dup.status_code == 409 and _kind(dup) == la.KIND_DUPLICATE


def test_reading_outside_the_root_is_403_with_the_escape_kind(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]
    (tmp_path / "secret.jpg").write_bytes(b"s")

    r = client.get("/local/photo", params={"root_id": rid, "rel": "../secret.jpg"})
    assert r.status_code == 403
    assert _kind(r) == la.KIND_ESCAPES_ROOT


def test_asking_for_a_non_image_is_415(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]
    r = client.get("/local/photo", params={"root_id": rid, "rel": "照片/notes.txt"})
    assert r.status_code == 415
    assert _kind(r) == la.KIND_NOT_IMAGE


def test_the_photo_endpoint_serves_the_real_bytes(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]
    r = client.get("/local/photo", params={"root_id": rid, "rel": "旅行/a.jpg"})
    assert r.status_code == 200
    assert r.content == (base / "旅行" / "a.jpg").read_bytes()


def test_the_thumb_endpoint_falls_back_to_the_original(client, tmp_path, monkeypatch):
    """**生成不了缩略图就回退原图**, 而不是 404 / 破图。

    缩略图是加速手段: 没有 ffmpeg、或这张图 ffmpeg 解不开时, 用户仍然应该看得到
    内容, 只是慢一点。给一个错误等于把人要的信息拿走了 —— 而前端多半会用
    `onerror` 兜住它, 于是连"图挂了"都看不见(第 11 条坑)。
    """
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]
    monkeypatch.setattr(thumbs, "find_ffmpeg", lambda: None)   # 假装没装 ffmpeg
    r = client.get("/local/thumb", params={"root_id": rid, "rel": "旅行/a.jpg"})
    assert r.status_code == 200
    assert r.content == (base / "旅行" / "a.jpg").read_bytes()


def test_random_and_favorite_through_the_api(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]

    data = client.get("/local/random", params={"count": 5, "seed": 1}).json()
    assert data["pool"] == 5
    item = data["items"][0]
    # 前端不该自己拼这两个地址: 拼错了就是一片空图, 而且不报错。
    assert item["photo_url"].startswith("/local/photo?root_id=%d" % rid)
    assert item["thumb_url"].startswith("/local/thumb?root_id=%d" % rid)

    assert client.post("/local/favorite", json={
        "root_id": rid, "rel": "旅行/a.jpg", "value": True}).status_code == 200
    favs = client.get("/local/favorites").json()
    assert favs["total"] == 1 and favs["items"][0]["favorite"] is True
    assert favs["items"][0]["photo_url"].startswith("/local/photo?root_id=%d" % rid)
    assert favs["stale"] == []


def test_a_stale_favorite_shows_up_with_a_reason_and_can_be_forgotten(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]
    client.post("/local/favorite", json={"root_id": rid, "rel": "旅行/a.jpg"})
    (base / "旅行" / "a.jpg").unlink()

    got = client.get("/local/favorites").json()
    assert got["total"] == 0
    assert got["stale"][0]["reason"] == la.KIND_NOT_FOUND
    assert client.post("/local/favorite/forget",
                       json={"path": got["stale"][0]["path"]}).json()["removed"] is True
    assert client.get("/local/favorites").json()["stale"] == []


def test_scan_endpoint_forces_a_rescan(client, tmp_path):
    base = _make_tree(tmp_path / "photos")
    rid = client.post("/local/roots", json={"path": str(base)}).json()["root"]["id"]
    before = client.get("/local/albums").json()["roots"][0]["at"]
    again = client.post("/local/roots/%d/scan" % rid).json()["index"]
    assert again["at"] >= before
    assert again["photos"] == 5


def test_stats_reports_what_it_could_not_check(client, tmp_path):
    """顶栏的数字必须带"几点核的" —— 不含时间戳的统计过一会儿就是谎言。"""
    base = _make_tree(tmp_path / "photos")
    client.post("/local/roots", json={"path": str(base)})
    empty = client.get("/local/stats").json()
    assert empty["roots"][0]["last_scan"] is None      # 还没扫过 -> **不报** 0
    client.get("/local/albums")                        # 触发扫描
    got = client.get("/local/stats").json()
    assert got["photo_count"] == 5
    assert got["album_count"] == 3
    assert isinstance(got["roots"][0]["last_scan"], float)
    assert got["thumb_cache"]["path"]


# --------------------------------------------------------------------------
# ⑧ 配置
# --------------------------------------------------------------------------

def test_the_scan_knobs_come_from_the_environment(monkeypatch):
    from core import config as cfg

    monkeypatch.setenv("UWC_LOCAL_ALBUM_DEPTH", "5")
    monkeypatch.setenv("UWC_LOCAL_ALBUM_TTL", "12.5")
    monkeypatch.setenv("UWC_LOCAL_ALBUM_MAX_PHOTOS", "777")
    loaded = cfg.load()
    assert (loaded.local_album_depth, loaded.local_album_ttl,
            loaded.local_album_max_photos) == (5, 12.5, 777)


def test_a_garbage_scan_knob_falls_back_to_the_default(monkeypatch):
    """一条可选优化项**不该有能力让服务起不来** —— 与 `parse_bytes_per_sec` 同一条
    取舍(带宽写错的表现必须是"没限住", 不能是"服务起不来")。"""
    from core import config as cfg

    monkeypatch.setenv("UWC_LOCAL_ALBUM_DEPTH", "不是数字")
    loaded = cfg.load()
    assert loaded.local_album_depth == cfg.Config().local_album_depth
