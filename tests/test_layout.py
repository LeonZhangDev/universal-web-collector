"""落盘布局: 下载目录 / 相册文件夹 / 文件, 视频平铺在下载根目录。

为什么值得单开一份用例
====================
目录结构是**用户的明确要求**, 也是用户唯一会直接看到、且会照着去找文件的产物。
它坏掉时没有任何报错: 文件确实下下来了, 任务也是 success, 只是"不在你以为的
地方"。这类问题只能靠断言守住。

规则唯一定义在 `core/layout.py`, 所以这里既测纯函数, 也测任务层**真的按它落库**
(只在纯函数上绿, 而 task_manager 自己又拼一遍路径 —— 就是这条规则有意要消灭的
那种"两处定义")。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import database as db  # noqa: E402
from core import layout  # noqa: E402
from core import task_manager as tm  # noqa: E402


# ---------------------------------------------------------------- 纯规则

def test_image_keeps_single_album_folder():
    """图片: 相册文件夹 + 原名, 只有一层。"""
    rel, album = layout.place("image", "相册/00001.jpg", "相册")
    assert (rel, album) == ("相册/00001.jpg", "相册")


def test_multi_level_collapses_to_deepest():
    """采集器/聚合页套了多层(标签层、模特层)时只留最深一段。

    "下载目录的下一级只有相册文件夹"是用户的明确要求; 上面几层是分类维度,
    完整信息在 album.json / manifest 里, 不必摊在路径上。
    """
    rel, album = layout.place("image", "丝袜-情趣内衣/模特名/套图名/00001.jpg")
    assert (rel, album) == ("套图名/00001.jpg", "套图名")


def test_template_without_folder_still_gets_album_folder():
    """自定义模板只给了文件名(`{name}`)时补一层相册目录。

    否则所有资源全堆在下载根目录 —— 那正是"相册文件夹"这条规则要挡的。
    """
    rel, _ = layout.place("image", "00001.jpg", "套图名")
    assert rel == "套图名/00001.jpg"


def test_video_is_flat_and_keeps_site_filename():
    """视频一律平铺在下载根目录, 文件名取站点原名。"""
    assert layout.place("video", "套图名/0001.mp4", "套图名")[0] == "0001.mp4"
    assert layout.place("video", "6aaa517d3f106.mp4")[0] == "6aaa517d3f106.mp4"
    assert layout.is_flat("video") and not layout.is_flat("image")


def test_no_album_means_root():
    """相册名取不到时落在下载根(而不是造一个 `None/` 目录)。"""
    assert layout.place("image", "00001.jpg")[0] == "00001.jpg"


def test_unsafe_paths_are_reduced_to_a_name():
    """绝对路径 / 含 `..` 的路径只留最后一段, 绝不落到下载根之外。"""
    assert layout.place("image", "/etc/passwd")[0] == "passwd"
    assert layout.place("image", "../../外面/重要.jpg")[0] == "重要.jpg"
    assert layout.place("image", "..")[0] == ""
    assert layout.place("image", None)[0] == ""


def test_meta_dir_is_per_task_under_meta():
    """清单落在 `下载根/_meta/<任务ID>/`。

    ⚠️ 不能跟媒体放一起: 视频平铺在下载根, 清单要是也在根目录, 每跑完一个视频
    任务就会盖掉上一个的清单。
    """
    got = layout.meta_dir(Path("D:/dl"), 101)
    assert got.as_posix() == "D:/dl/_meta/101"


# ---------------------------------------------------------------- 重名消解
#
# ⚠️ 必须在下载前消解: 下载器见到"目标已有文件"会把它当半成品续传, 不同来源的
# 同名文件会被拼成一份产物, 而且长度可能恰好对得上, 于是报成功。

def test_same_source_reuses_the_name():
    """同一条 URL: 名字不变, 交给下载层去复用/校验。"""
    assert layout.claim("套图名/00001.jpg", "套图名",
                        lambda rel: "https://img/00001.jpg",
                        "https://img/00001.jpg") == "套图名/00001.jpg"


def test_different_source_in_album_gets_index():
    """相册里的同名不同源: 加序号, **不**缀相册名(已经在相册目录里了)。"""
    def owner(rel):
        return "https://img/a/00001.jpg" if rel == "套图名/00001.jpg" else None

    got = layout.claim("套图名/00001.jpg", "套图名", owner,
                       "https://img/b/00001.jpg")
    assert got == "套图名/00001(2).jpg"


def test_flat_video_gets_album_as_disambiguator():
    """平铺的视频撞名: 加相册名 —— 文件名里看不到相册, 这一缀很有用。"""
    def owner(rel):
        return "https://v/a/0001.mp4" if rel == "0001.mp4" else None

    got = layout.claim("0001.mp4", "葡萄一番街", owner, "https://v/b/0001.mp4")
    assert got == "0001_葡萄一番街.mp4"


def test_flat_video_without_album_falls_back_to_index():
    def owner(rel):
        return "https://v/a/6aaa.mp4" if rel == "6aaa.mp4" else None

    assert layout.claim("6aaa.mp4", "", owner, "https://v/b/6aaa.mp4") == "6aaa(2).mp4"


def test_claim_walks_forward_until_free():
    taken = {"套图名/1.jpg": "u1", "套图名/1(2).jpg": "u2"}
    got = layout.claim("套图名/1.jpg", "套图名", taken.get, "u3")
    assert got == "套图名/1(3).jpg"


# ---------------------------------------------------------------- 任务层接线

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


def _spider(resources):
    class Spider:
        def crawl(self, url, **kw):
            return resources
    return Spider()


def _run_once(monkeypatch, tmp_path, resources, collector="xchina_gallery", tag="a"):
    """跑一遍真实 _run(只桩掉采集器与下载), 返回 tid。

    tag 只用来把任务 URL 区分开 —— 同一批用例里要建多个任务。
    """
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "get_collector", lambda name: _spider(resources))

    def fake_download_all(self, task_id, referer):
        for r in db.get_resources(task_id):
            if r["status"] == "pending":
                db.update_resource(r["id"], status="done", local_path="x", size=1)

    monkeypatch.setattr(tm.TaskManager, "_download_all", fake_download_all)
    tid = db.create_task(f"https://x/{tag}", collector, None, {})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        mgr.submit(tid)
        deadline = time.time() + 15
        while db.get_task(tid)["status"] in tm.ACTIVE_STATES:
            if time.time() > deadline:
                break
            time.sleep(0.05)
    finally:
        mgr.shutdown(wait=True)
    return tid


def _names(tid):
    return [r["filename"] for r in db.get_resources(tid)]


def test_task_stores_placed_paths(tmp_db, tmp_path, monkeypatch):
    """入库的 filename 已经是**归位后**的相对路径 —— 下载层照着它落盘。"""
    resources = [
        {"type": "image", "url": "https://img/1.jpg", "headers": None,
         "mirrors": [], "size": 1, "filename": "标签层/套图名/00001.jpg",
         "album": "套图名"},
        {"type": "video", "url": "https://v/0001.mp4", "headers": None,
         "mirrors": [], "size": 1, "filename": "套图名/0001.mp4",
         "album": "套图名"},
    ]
    tid = _run_once(monkeypatch, tmp_path, resources)
    assert _names(tid) == ["套图名/00001.jpg", "0001.mp4"]


def test_second_task_with_same_album_and_different_url_gets_new_name(
        tmp_db, tmp_path, monkeypatch):
    """跨任务撞名: 同一个相册名 + 不同 URL -> 加序号, 不许两处都写 `00001.jpg`。

    只按"同名就复用"是不行的 —— 那是另一个文件, 下载器会把它当半成品拼起来。
    """
    first = [{"type": "image", "url": "https://img/a/00001.jpg", "headers": None,
              "mirrors": [], "size": 1, "filename": "套图名/00001.jpg",
              "album": "套图名"}]
    tid1 = _run_once(monkeypatch, tmp_path, first)
    # 库里那份标成"已落盘", 这才模拟出"盘上有别人的文件"
    rid = db.get_resources(tid1)[0]["id"]
    db.update_resource(rid, local_path=str(tmp_path / "dl" / "套图名" / "00001.jpg"))

    second = [{"type": "image", "url": "https://img/b/00001.jpg", "headers": None,
               "mirrors": [], "size": 1, "filename": "套图名/00001.jpg",
               "album": "套图名"}]
    tid2 = _run_once(monkeypatch, tmp_path, second)

    assert _names(tid2) == ["套图名/00001(2).jpg"]


def test_second_task_with_same_url_reuses_the_name(tmp_db, tmp_path, monkeypatch):
    """同一条 URL 再采一次: 名字不变 —— 复用已有文件是下载层的本职工作。"""
    res = [{"type": "image", "url": "https://img/a/00001.jpg", "headers": None,
            "mirrors": [], "size": 1, "filename": "套图名/00001.jpg",
            "album": "套图名"}]
    tid1 = _run_once(monkeypatch, tmp_path, res)
    rid = db.get_resources(tid1)[0]["id"]
    db.update_resource(rid, local_path=str(tmp_path / "dl" / "套图名" / "00001.jpg"))

    tid2 = _run_once(monkeypatch, tmp_path, res)
    assert _names(tid2) == ["套图名/00001.jpg"]


def test_duplicates_inside_one_batch_are_separated(tmp_db, tmp_path, monkeypatch):
    """同一批里两条不同 URL 撞同一个名字: 当场分开(库查询看不到还没入库的行)。"""
    resources = [
        {"type": "video", "url": "https://v/a/0001.mp4", "headers": None,
         "mirrors": [], "size": 1, "filename": "套图名/0001.mp4",
         "album": "套图名"},
        {"type": "video", "url": "https://v/b/0001.mp4", "headers": None,
         "mirrors": [], "size": 1, "filename": "套图名/0001.mp4",
         "album": "套图名"},
    ]
    tid = _run_once(monkeypatch, tmp_path, resources, tag="dupes")
    assert _names(tid) == ["0001.mp4", "0001_套图名.mp4"]
