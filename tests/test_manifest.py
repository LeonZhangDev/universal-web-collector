"""manifest.json: 溯源清单的生成与容错。

它要在任务失败/取消/部分成功时都能写出来, 所以"缺列、缺文件、路径在目录外"
都必须兜住 —— 这些恰恰是最需要看 manifest 的时刻。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.manifest import (  # noqa: E402
    MANIFEST_NAME,
    build_manifest,
    manifest_enabled,
    read_manifest,
    write_manifest,
)


def _task(**over):
    t = {
        "id": 12,
        "url": "https://album.example/6aa1",
        "collector": "xchina_gallery",
        "status": "success",
        "created_time": "2026-09-18 10:00:00",
        "retry_count": 0,
        "error": None,
        "options": '{"quality": "1200"}',
    }
    t.update(over)
    return t


def _res(**over):
    r = {
        "id": 1,
        "type": "image",
        "url": "https://img.example/6aa1/00001.jpg",
        "local_path": None,
        "hash": None,
        "size": None,
        "status": "pending",
        "note": None,
        "filename": None,
        "content_type": None,
        "resolved_url": None,
    }
    r.update(over)
    return r


def test_file_path_is_relative_to_output(tmp_path):
    out = tmp_path / "12"
    out.mkdir()
    (out / "sub").mkdir()
    target = out / "sub" / "00001.jpg"
    target.write_bytes(b"x")

    data = build_manifest(_task(), [_res(local_path=str(target))], out)
    item = data["resources"][0]
    assert item["file"] == "sub/00001.jpg"  # 相对路径: 目录整体搬家后仍可解析


def test_foreign_path_is_recorded_as_null(tmp_path):
    out = tmp_path / "12"
    out.mkdir()
    other = tmp_path / "outside.jpg"
    other.write_bytes(b"x")

    data = build_manifest(_task(), [_res(local_path=str(other))], out)
    # hash 去重时可能指向别的任务目录, 不在本任务树内 -> 不冒充本任务产出
    assert data["resources"][0]["file"] is None


def test_missing_columns_do_not_crash(tmp_path):
    """旧库未迁移 filename/content_type 列时 Row 里没有这些键, 必须容忍。"""
    out = tmp_path / "12"
    out.mkdir()
    res = {"id": 3, "type": "video", "url": "https://v/x.m3u8", "status": "failed"}
    data = build_manifest(_task(), [res], out)
    item = data["resources"][0]
    assert item["sha256"] is None and item["file"] is None
    assert item["status"] == "failed"


def test_write_manifest_produces_valid_json(tmp_path):
    out = tmp_path / "12"
    (out / "a.jpg").parent.mkdir(parents=True, exist_ok=True)
    (out / "a.jpg").write_bytes(b"abc")

    p = write_manifest(
        _task(),
        [_res(local_path=str(out / "a.jpg"), status="done", hash="h" * 64,
              size=3, resolved_url="https://cdn/x.webp",
              content_type="image/webp", filename="gid/00001.webp")],
        out,
        stats={"done": 1},
    )
    assert p and p.name == MANIFEST_NAME
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["task_id"] == 12
    assert data["counts"] == {"done": 1}
    assert data["resources"][0]["resolved_url"] == "https://cdn/x.webp"
    assert data["resources"][0]["content_type"] == "image/webp"
    assert data["resources"][0]["file"] == "a.jpg"


def test_write_manifest_never_leaves_tmp_file(tmp_path):
    out = tmp_path / "12"
    write_manifest(_task(), [], out)
    leftovers = list(out.iterdir())
    assert [p.name for p in leftovers] == [MANIFEST_NAME]


def test_read_manifest_roundtrip(tmp_path):
    out = tmp_path / "12"
    write_manifest(_task(), [], out)
    assert read_manifest(out)["task_id"] == 12
    assert read_manifest(tmp_path / "nonexistent") is None


def test_corrupted_manifest_returns_none(tmp_path):
    out = tmp_path / "12"
    out.mkdir(parents=True)
    (out / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert read_manifest(out) is None


def test_error_and_stats_are_preserved(tmp_path):
    task = _task(status="partial", error="3 个资源失败")
    stats = {"done": 5, "failed": 3}
    # task.error 由 write_manifest 透传进来(build 层不认识 task 的字段语义)
    data = build_manifest(task, [], tmp_path, stats=stats, error=task["error"])
    assert data["error"] == "3 个资源失败"
    assert data["counts"] == {"done": 5, "failed": 3}


def test_write_manifest_carries_task_error(tmp_path):
    out = tmp_path / "12"
    write_manifest(_task(status="failed", error="boom"), [], out)
    assert read_manifest(out)["error"] == "boom"


def test_manifest_can_be_disabled_per_task():
    assert manifest_enabled() is True
    assert manifest_enabled({"manifest": False}) is False
    assert manifest_enabled({"quality": "1200"}) is True
