"""测试隔离回归: 用例不许碰到用户的真实目录。

这些用例盯的不是"功能对不对", 而是**隔离机制本身有没有失效** —— 它失效时不会
报错, 只会悄悄把数据写进用户的库里, 等到在界面上看见多余的任务时, 已经隔了好几轮
提交、不知道是哪个用例写的。所以每一条都做成"失效就红"。
"""

import sqlite3
import tempfile
from pathlib import Path

import core.database as db
import isolation
from core.config import settings


def test_every_shared_path_points_into_temp(tmp_path):
    """清单上的每一处共享路径都必须落在本次用例的临时目录里。"""
    real = isolation.real_paths()

    assert db.DB_PATH.parent.is_relative_to(tmp_path)
    assert str(db.DB_PATH) != str(real["db"])

    from core.task_manager import DOWNLOADS_DIR
    assert DOWNLOADS_DIR.is_relative_to(tmp_path)
    assert DOWNLOADS_DIR != real["downloads"]

    for field in isolation.SETTINGS_TARGETS:
        assert getattr(settings, field).is_relative_to(tmp_path), field


def test_new_task_lands_in_temp_db_not_the_users(tmp_path):
    """一个完全不用夹具的普通写操作, 也必须落在临时库里。

    这是当初最担心的一条: 新用例忘了加 tmp_db, 直接 `db.create_task(...)`
    做前置数据 —— 于是用户的任务列表里多一条假记录, 界面上看不出来源。
    """
    real_before = isolation.snapshot_real()

    tid = db.create_task("http://example.com/album", "xchina_gallery")
    db.add_resource(tid, "image", "http://example.com/1.jpg")

    assert db.get_task(tid) is not None
    assert isolation.changed_keys(real_before, isolation.snapshot_real()) == []
    assert db.DB_PATH.is_relative_to(tmp_path)


def test_connection_singleton_follows_the_redirect(tmp_path, monkeypatch):
    """⚠️ 光改 DB_PATH 不够: 已建的连接会一直指向旧文件。

    症状极具迷惑性 —— 打印 DB_PATH 完全正确, 数据却写进了上一个用例的库。
    这段必须在换库的同时把连接单例一并复位。
    """
    first = Path(tempfile.mkdtemp()) / "first.db"
    db.DB_PATH = first
    db._conn = None
    db.create_task("http://example.com/one")          # 在"第一个库"里建连接
    assert db._conn is not None

    isolation.isolate(monkeypatch, tmp_path / "second")
    assert db._conn is None, "换库后必须丢弃旧连接"

    db.create_task("http://example.com/two")
    assert first.exists()
    assert len(_tasks_in(first)) == 1, "旧库不该多出新数据"
    assert len(_tasks_in(db.DB_PATH)) == 1


def test_registry_entries_all_exist():
    """清单里的名字必须真实存在 —— 拼错了就该炸, 而不是静默少隔离一处。"""
    import importlib

    for mod_name, attr in isolation.MODULE_TARGETS:
        assert hasattr(importlib.import_module(mod_name), attr), \
            f"{mod_name}.{attr} 不存在, isolation.py 里的清单要跟着改"
    for field in isolation.SETTINGS_TARGETS:
        assert hasattr(settings, field), f"settings.{field} 不存在"


def test_registry_is_not_empty():
    """清单空了等于全面失效。宁可红, 也不要"看起来在隔离"。"""
    assert isolation.MODULE_TARGETS and isolation.SETTINGS_TARGETS


def test_changed_keys_detects_writes():
    """守卫的比对逻辑本身要能发现变化 —— 否则整道守卫是摆设。"""
    same = {"db": (100, 12345)}
    assert isolation.changed_keys(same, dict(same)) == []
    assert isolation.changed_keys(same, {"db": (200, 12345)}) == ["db"]
    assert isolation.changed_keys(same, {"db": (100, 99999)}) == ["db"], \
        "只改内容不改大小也要能发现"


def test_changed_keys_notices_missing_entries():
    """键被整个删掉(如库文件被重建)同样算脏写。"""
    assert isolation.changed_keys({"db": (1, 2)}, {}) == ["db"]


def _tasks_in(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT id FROM tasks").fetchall()
    finally:
        con.close()
