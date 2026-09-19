"""album.json: 集合级元数据 sidecar。

分工是这份文件存在的理由 ——
    manifest.json   文件级: 一行一个资源(路径 / sha256 / 实际下载点)
    album.json      集合级: 目录名 / 厂牌 / 标签 / **实际生效的资源根**

`resource_roots` 是最有价值的一项: 它记录了本次枚举真正用的 CDN 基址与序号
位数, 以及那个地址是怎么定下来的(hint / probe / default)。"任务采到 0 个资源
但日志看不出为什么"时, 先看它 —— 站点悄悄换了 CDN 子路径就是靠它认出来的。

它与 manifest 同样宽松: 写不出来只记 warn, 不能让已下载成功的任务变失败。
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import database as db  # noqa: E402
from core import task_manager as tm  # noqa: E402
from core.manifest import (  # noqa: E402
    SIDECAR_NAME,
    build_sidecar,
    write_sidecar,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把数据库指到一个临时文件, 与默认库隔离。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


def _task(**over):
    t = {"id": 7, "url": "https://xchina.co/photo/id-6aa5136f606fe.html",
         "collector": "xchina_gallery"}
    t.update(over)
    return t


def _meta(**over):
    m = {
        "collector": "xchina_gallery",
        "source_url": "https://xchina.co/photo/id-6aa5136f606fe.html",
        "gid": "6aa5136f606fe",
        "album": "约啪172cm车模",
        "album_source": "clean",
        "title": "约啪172cm车模 完美肉体",
        "maker": "FENDSON",
        "tags": ["丝袜", "情趣内衣", "吊带袜", "长腿"],
        "media": ["image", "video"],
        "photos_declared": 12,
        "videos_declared": 4,
        "resource_roots": {
            "image": {"base": "https://img.xchina.io/photos2",
                      "seq_format": "{seq:04d}", "source": "probe",
                      "site_default": "https://img.xchina.io/photos"},
        },
    }
    m.update(over)
    return m


# ---------------------------------------------------------------- 纯函数层

def test_resource_roots_is_carried_through():
    """这一项是排查"为什么采不到东西"的唯一线索, 绝不能丢。"""
    data = build_sidecar(_task(), _meta())
    root = data["resource_roots"]["image"]
    assert root["base"] == "https://img.xchina.io/photos2"
    assert root["seq_format"] == "{seq:04d}"
    assert root["source"] == "probe", "要能看出这个地址是探出来的还是配置的"
    assert root["site_default"] == "https://img.xchina.io/photos"


def test_album_metadata_is_machine_readable():
    data = build_sidecar(_task(), _meta())
    assert data["gid"] == "6aa5136f606fe"
    assert data["album"] == "约啪172cm车模"
    assert data["maker"] == "FENDSON"
    assert data["tags"][:3] == ["丝袜", "情趣内衣", "吊带袜"]
    # 自报数量只作对照, 必须与真实采集结果分开(页面会改版/滞后)
    assert data["photos_declared"] == 12
    assert data["videos_declared"] == 4


def test_counts_reflect_what_was_actually_collected():
    """counts 描述本次真采到的, 与页面自报量是两个概念。"""
    resources = [{"type": "image"}] * 5 + [{"type": "video"}] * 2
    data = build_sidecar(_task(), _meta(), resources)
    assert data["counts"] == {"image": 5, "video": 2}


def test_task_fields_are_fallback_when_meta_lacks_them():
    data = build_sidecar(_task(), {"gid": "6aa5136f606fe"})
    assert data["collector"] == "xchina_gallery"
    assert data["source_url"].endswith("id-6aa5136f606fe.html")


def test_write_sidecar_creates_file(tmp_path):
    target = write_sidecar(_task(), _meta(), tmp_path)
    assert target == tmp_path / SIDECAR_NAME
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["task_id"] == 7
    assert "finished_time" in data


def test_write_sidecar_without_meta_is_a_noop(tmp_path):
    """通用采集器不提供 task_meta -> 不写文件, 也不该报错。"""
    assert write_sidecar(_task(), None, tmp_path) is None
    assert not (tmp_path / SIDECAR_NAME).exists()


def test_write_sidecar_failure_is_swallowed(tmp_path):
    """附属产物写不出来, 不能让已经下载成功的任务判定为失败。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")   # 同名文件占位, mkdir 必失败
    logs = []
    out = write_sidecar(_task(), _meta(), blocker, log=logs.append)
    assert out is None
    assert logs and "album.json" in logs[0]


# ------------------------------------------------------------ 任务层接线

def _fake_spider(resources):
    class Spider:
        def crawl(self, url, **kw):
            return resources
    return Spider()


def _run_task_with(monkeypatch, tmp_path, tmp_db, resources):
    """跑一遍真实 _run 主流程(只桩掉采集器与下载), 返回任务输出目录。"""
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "get_collector", lambda name: _fake_spider(resources))

    def fake_download_all(self, task_id, referer):
        for r in db.get_resources(task_id):
            if r["status"] == "pending":
                db.update_resource(r["id"], status="done", local_path="x", size=1)
        db.update_task(task_id, progress=100)

    monkeypatch.setattr(tm.TaskManager, "_download_all", fake_download_all)

    tid = db.create_task("https://xchina.co/photo/id-6aa5136f606fe.html",
                         "xchina_gallery", None, {})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        mgr.submit(tid)
        deadline = time.time() + 15
        while db.get_task(tid)["status"] in tm.ACTIVE_STATES:
            if time.time() > deadline:
                break
            time.sleep(0.05)
        assert db.get_task(tid)["status"] == tm.TaskStatus.SUCCESS
    finally:
        mgr.shutdown(wait=True)
    return tid, tmp_path / "dl" / str(tid)


def test_task_manager_writes_album_json(tmp_db, tmp_path, monkeypatch):
    """采集器把 task_meta 挂在资源上 -> 任务层落成专辑目录里的 album.json。"""
    shared = _meta()
    resources = [
        {"type": "image", "url": "https://img.xchina.io/photos2/6aa5136f606fe/0001.jpg",
         "headers": None, "mirrors": [], "size": 100, "filename": "相册/0001.jpg",
         "album": shared["album"], "task_meta": shared},
        {"type": "image", "url": "https://img.xchina.io/photos2/6aa5136f606fe/0002.jpg",
         "headers": None, "mirrors": [], "size": 100, "filename": "相册/0002.jpg",
         "album": shared["album"], "task_meta": shared},
    ]
    tid, out_dir = _run_task_with(monkeypatch, tmp_path, tmp_db, resources)

    side = json.loads((out_dir / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert side["task_id"] == tid
    assert side["gid"] == "6aa5136f606fe"
    assert side["counts"] == {"image": 2}
    # 落盘的是**实际生效**的根, 不是站点配置里写死的那个
    assert side["resource_roots"]["image"]["base"].endswith("photos2")
    # manifest 与 sidecar 并存, 分工不同但都在同一个目录
    assert (out_dir / "manifest.json").is_file()


def test_task_without_task_meta_still_succeeds(tmp_db, tmp_path, monkeypatch):
    """老采集器不提供 task_meta: 任务照常跑完, 只是没有 album.json。"""
    resources = [{"type": "image", "url": "https://x/1.jpg", "headers": None,
                  "mirrors": [], "size": 100, "filename": "a/1.jpg"}]
    tid, out_dir = _run_task_with(monkeypatch, tmp_path, tmp_db, resources)
    assert db.get_task(tid)["status"] == tm.TaskStatus.SUCCESS
    assert not (out_dir / SIDECAR_NAME).exists()


def test_sidecar_is_written_before_download_finishes(tmp_db, tmp_path, monkeypatch):
    """sidecar 在提取完成时就写 —— 之后下载全失败也留得下排查线索。"""
    seen = {}

    def failing_download_all(self, task_id, referer):
        seen["exists_during_download"] = (
            (tmp_path / "dl" / str(task_id) / SIDECAR_NAME).is_file()
        )
        for r in db.get_resources(task_id):
            db.update_resource(r["id"], status="failed", note="boom")

    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    resources = [{"type": "image", "url": "https://x/1.jpg", "headers": None,
                  "mirrors": [], "size": 100, "filename": "a/1.jpg",
                  "task_meta": _meta()}]
    monkeypatch.setattr(tm, "get_collector", lambda name: _fake_spider(resources))
    monkeypatch.setattr(tm.TaskManager, "_download_all", failing_download_all)

    tid = db.create_task("https://xchina.co/photo/id-6aa5136f606fe.html",
                         "xchina_gallery", None, {})
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

    assert db.get_task(tid)["status"] == tm.TaskStatus.FAILED
    assert seen.get("exists_during_download") is True, (
        "sidecar 必须在下载阶段之前落盘, 否则任务全失败时就没有资源根线索了"
    )
    assert (tmp_path / "dl" / str(tid) / SIDECAR_NAME).is_file()
