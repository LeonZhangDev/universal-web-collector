"""图片尺寸过滤: "什么算有效资源"的第三关。

为什么单靠文件名和体积不够: 一个 728x90 的广告横幅可以叫 `abc.jpg`(名字正常),
也可以有 40KB(不算"过小")。真正的判别特征是**宽高** —— 横幅、按钮、1x1 信标
都是极端长宽比或极小尺寸, 而相册里的图极少是 300x250。

判定放在**下载后**(task_manager 的尺寸终检): 那时手上是完整文件, 零额外请求、
零误判。下载前用 Range 抓头部遇到 progressive JPEG 读不到 SOF, 会误判放行,
等于没做。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import database as db  # noqa: E402
from core import task_manager as tm  # noqa: E402
from core.filters import Filters  # noqa: E402
from core.imageinfo import dimensions_from_bytes, fmt_dimensions  # noqa: E402


# ---- 各格式的最小合法文件头(只到能读出宽高为止) ----

def png(w, h):
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big") + b"IHDR"
        + w.to_bytes(4, "big") + h.to_bytes(4, "big")
        + bytes([8, 6, 0, 0, 0])
    )


def jpeg(w, h):
    # APP0(JFIF, 长度 16) 排在前面, 用来验证"要跳过非 SOF 段"
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
        + h.to_bytes(2, "big") + w.to_bytes(2, "big")
        + b"\x03" + b"\x00" * 9
    )
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def gif(w, h):
    return b"GIF89a" + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00" * 8


def bmp(w, h):
    return b"BM" + b"\x00" * 16 + w.to_bytes(4, "little", signed=True) \
        + h.to_bytes(4, "little", signed=True) + b"\x00" * 4


def webp_lossy(w, h):
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + (10).to_bytes(4, "little")
            + b"\x00\x00\x00" + b"\x9d\x01\x2a"
            + w.to_bytes(2, "little") + h.to_bytes(2, "little"))


def webp_lossless(w, h):
    bits = (w - 1) | ((h - 1) << 14)
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8L" + (5).to_bytes(4, "little")
            + b"\x2f" + bits.to_bytes(4, "little"))


def webp_extended(w, h):
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + (10).to_bytes(4, "little")
            + b"\x00" * 4 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


# ---- 解析层 ----

@pytest.mark.parametrize("blob,expect", [
    (png(1920, 1080), (1920, 1080)),
    (png(1, 1), (1, 1)),
    (jpeg(1280, 720), (1280, 720)),
    (gif(500, 400), (500, 400)),
    (bmp(300, 250), (300, 250)),
    (webp_lossy(800, 600), (800, 600)),
    (webp_lossless(640, 480), (640, 480)),
    (webp_extended(1024, 768), (1024, 768)),
])
def test_dimensions_parsed_for_common_formats(blob, expect):
    assert dimensions_from_bytes(blob) == expect


def test_bmp_negative_height_is_absolute():
    """高度为负表示自上而下存储, 尺寸仍是正数。"""
    blob = b"BM" + b"\x00" * 16 + (200).to_bytes(4, "little", signed=True) \
        + (-100).to_bytes(4, "little", signed=True) + b"\x00" * 4
    assert dimensions_from_bytes(blob) == (200, 100)


def test_unknown_format_returns_none_not_garbage():
    """认不出就返回 None, **绝不猜** —— 猜出来的尺寸会去误杀真图。"""
    assert dimensions_from_bytes(b"<html>not an image</html>" + b"x" * 40) is None
    assert dimensions_from_bytes(b"") is None
    assert dimensions_from_bytes(b"\xff\xd8truncated") is None


def test_fmt_dimensions_is_human_readable():
    assert fmt_dimensions((1920, 1080)) == "1920x1080"
    assert fmt_dimensions(None) == "尺寸未知"


# ---- 规则层 ----

def test_dimension_rules_are_off_by_default():
    """默认不启用: 相册里也有竖构图小图, 阈值该由用户按站点定。"""
    f = Filters({})
    assert f.need_dimensions is False
    assert f.match_dimensions(50, 50) is None
    # 空配置不等于"无过滤": 广告排除是**默认开**的。想真的全放行要显式关掉。
    assert f.exclude_ad is True
    assert Filters({"exclude_ad": False}).active is False


def test_min_width_catches_banner():
    """728x90 横幅: 高度达标不达标无所谓, 宽度先把它挡下。"""
    f = Filters({"min_width": 400, "min_height": 400})
    assert f.match_dimensions(728, 90), "728x90 横幅必须被挡"
    assert f.match_dimensions(88, 31), "88x31 按钮必须被挡"
    assert f.match_dimensions(1920, 1080) is None


def test_min_pixels_catches_small_squares():
    f = Filters({"min_pixels": 300000})
    assert f.match_dimensions(300, 250), "300x250 = 7.5 万像素, 应被挡"
    assert f.match_dimensions(1600, 1200) is None


def test_unknown_dimensions_are_allowed_through():
    """量不出尺寸一律放行 —— 与 size 层同一个原则: 宁可漏判不误杀。"""
    f = Filters({"min_width": 400})
    assert f.match_dimensions(None, None) is None
    assert f.match_dimensions(400, None) is None
    assert f.match_dimensions("abc", "def") is None


def test_dimensions_count_toward_active_and_summary():
    f = Filters({"min_width": 400})
    assert f.active is True
    assert "尺寸" in f.summarize()


# ---- 任务链路: 下载后终检真的会删文件 ----

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


class FakeImageDownloader:
    """把指定字节写到 save_dir, 模拟一次成功下载。"""

    def __init__(self, payload, name="banner.png"):
        self.payload = payload
        self.name = name
        self.sha = "b" * 64

    def download(self, url, **kw):
        p = Path(kw["save_dir"]) / self.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.payload)
        info = kw.get("info")
        if info is not None:
            info["resolved_url"] = url
            info["content_type"] = "image/png"
        return p, self.sha


def _setup_task(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {"image": lambda: FakeImageDownloader(payload)})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    tid = db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    rid = db.add_resource(tid, "image", "https://x/728x90.png", "{}",
                          filename="a/banner.png")
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    return mgr, tid, rid, out_dir


def test_banner_is_filtered_and_removed_after_download(tmp_db, tmp_path, monkeypatch):
    mgr, tid, rid, out_dir = _setup_task(monkeypatch, tmp_path, png(728, 90))
    try:
        mgr._download_one(tid, db.get_resource(rid), None, out_dir,
                          Filters({"min_width": 400, "min_height": 400}))
        row = db.get_resource(rid)
        assert row["status"] == "filtered", "广告横幅不能算 done"
        assert "728x90" in (row["note"] or ""), "note 要写实测尺寸, 否则用户不知为何被删"
        assert not (out_dir / "banner.png").exists(), "被判无效的文件必须删掉, 不能留盘"
    finally:
        mgr.shutdown(wait=True)


def test_real_image_is_kept_when_rule_enabled(tmp_db, tmp_path, monkeypatch):
    """反向断言: 规则开着也不能误杀真图。"""
    mgr, tid, rid, out_dir = _setup_task(monkeypatch, tmp_path, png(1920, 1080))
    try:
        mgr._download_one(tid, db.get_resource(rid), None, out_dir,
                          Filters({"min_width": 400, "min_height": 400}))
        assert db.get_resource(rid)["status"] == "done"
        assert (out_dir / "banner.png").exists()
    finally:
        mgr.shutdown(wait=True)


def test_dedup_shared_file_is_never_deleted(tmp_db, tmp_path, monkeypatch):
    """命中去重复说明别处已有同一份内容: 不能因为本任务的规则去删别人的文件。

    ⚠️ 拦 728x90 这条横幅的是 **min_height**(90 < 400), 不是 min_width ——
    横幅的宽度往往比正文图还大, 拿宽度当判据会漏掉最典型的那一类广告。
    """
    other = tmp_path / "other-task" / "same.png"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(png(728, 90))

    mgr, tid, rid, out_dir = _setup_task(monkeypatch, tmp_path, png(728, 90))
    # 造一个"别的任务已下过同一内容"的记录, 让本次下载落进去重分支
    other_tid = db.create_task("https://fake/other", "fake", None, {})
    db.add_resource(other_tid, "image", "https://x/same.png", "{}",
                    filename="same.png", status="done",
                    local_path=str(other), hash_value="b" * 64)
    try:
        mgr._download_one(tid, db.get_resource(rid), None, out_dir,
                          Filters({"min_height": 400}))
        assert db.get_resource(rid)["status"] == "filtered"
        assert other.exists(), "去重复用的文件不属于本任务, 绝不能被删"
    finally:
        mgr.shutdown(wait=True)
