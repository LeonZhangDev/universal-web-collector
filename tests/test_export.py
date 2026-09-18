"""打包导出与登录态管理。

导出最容易出问题的是"同名文件互相覆盖"和"DB 里记的路径已经不在磁盘上";
登录态最容易出问题的是"domain 来自 URL, 可以直接拼进文件名"(路径穿越)。
"""
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from api.tasks import _dedup_name, _resolve_safely, _zip_stream  # noqa: E402
from core.config import settings  # noqa: E402
from core import sessions  # noqa: E402


def _collect(base, items):
    blob = b"".join(_zip_stream(base, items))
    return zipfile.ZipFile(io.BytesIO(blob))


def test_zip_contains_files_with_relative_names(tmp_path):
    base = tmp_path / "12"
    (base / "gid").mkdir(parents=True)
    (base / "gid" / "00001.jpg").write_bytes(b"AAA")
    (base / "gid" / "00002.jpg").write_bytes(b"BBBB")

    items = [(None, base / "gid" / "00001.jpg"), (None, base / "gid" / "00002.jpg")]
    zf = _collect(base, items)
    assert zf.namelist() == ["gid/00001.jpg", "gid/00002.jpg"]
    assert zf.read("gid/00001.jpg") == b"AAA"


def test_duplicate_basenames_do_not_overwrite(tmp_path):
    """不同子目录里的同名文件进同一个包, 直接写会静默丢文件。"""
    base = tmp_path / "12"
    for sub in ("a", "b"):
        (base / sub).mkdir(parents=True)
        (base / sub / "cover.jpg").write_bytes(sub.encode())

    items = [(None, base / "a" / "cover.jpg"), (None, base / "b" / "cover.jpg")]
    zf = _collect(base, items)
    names = zf.namelist()
    assert len(names) == 2 and len(set(names)) == 2
    assert zf.read("a/cover.jpg") == b"a"
    assert zf.read("b/cover.jpg") == b"b"


def test_zip_stream_yields_central_directory_last(tmp_path):
    """最后必须吐出完整的 zip 尾部, 否则下载到的包是损坏的。"""
    base = tmp_path / "12"
    base.mkdir(parents=True)
    (base / "x.jpg").write_bytes(b"payload-" * 100)
    chunks = list(_zip_stream(base, [(None, base / "x.jpg")]))
    assert len(chunks) >= 2  # 至少一个数据段 + tail
    zf = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert zf.testzip() is None
    assert zf.read("x.jpg") == b"payload-" * 100


def test_dedup_name_numbering():
    counter = {}
    assert _dedup_name("a/cover.jpg", counter) == "a/cover.jpg"
    assert _dedup_name("a/cover.jpg", counter) == "a/cover (1).jpg"
    assert _dedup_name("a/cover.jpg", counter) == "a/cover (2).jpg"
    # 没有扩展名时也要能加序号
    assert _dedup_name("README", counter) == "README"


def test_resolve_safely_rejects_outside_and_missing(tmp_path):
    base = tmp_path / "12"
    base.mkdir(parents=True)
    inside = base / "ok.jpg"
    inside.write_bytes(b"x")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"x")

    assert _resolve_safely(base, str(inside)) == inside
    assert _resolve_safely(base, str(outside)) is None
    assert _resolve_safely(base, str(base / "gone.jpg")) is None
    assert _resolve_safely(base, None) is None


# ---- 登录态 ----

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "browser_state_dir", tmp_path)
    return tmp_path


def test_domain_key_is_sanitized():
    # netloc 可以带端口, 冒号在 Windows 文件名里非法
    assert sessions.domain_of("http://localhost:8080/login") == "localhost_8080"
    assert sessions.domain_of("https://xchina.co/x") == "xchina.co"


def test_state_file_never_leaves_directory(tmp_state):
    p = sessions.state_file("../../evil")
    assert p.parent == tmp_state
    assert p.name == ".._.._evil.json"  # 分隔符被换掉, 只剩一个普通文件名


def test_list_and_delete_session(tmp_state):
    f = sessions.state_file("xchina.co")
    f.write_text("{}", encoding="utf-8")

    rows = sessions.list_sessions()
    assert [r["domain"] for r in rows] == ["xchina.co"]
    assert rows[0]["size"] > 0

    assert sessions.delete_session("xchina.co") is True
    assert sessions.delete_session("xchina.co") is False
    assert sessions.list_sessions() == []


def test_list_sessions_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "browser_state_dir", tmp_path / "nope")
    assert sessions.list_sessions() == []
