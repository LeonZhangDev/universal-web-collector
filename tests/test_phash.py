"""感知去重(dHash): "同一张图换了身衣服"也能认出来。

为什么必须单独测: 这个功能的失败方式是**静默**的 —— 指纹算错不会报错, 只会
让两条不相干的图被判成重复(误标), 或者让真重复一个都没标出来(漏标)。两种
都不影响任务成功, 所以只有断言才看得见。

分层测:
- 纯算法层(灰度 -> 指纹 / 距离 / 择优)不依赖 ffmpeg, 永远能跑;
- 解码层与任务链路依赖 ffmpeg 现场生成真图, 探测不到就 skip ——
  **不 mock ffmpeg**: 这个功能的价值全在"真的解出了像素", 把解码 mock 掉
  等于只测了自己写的位运算。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import database as db  # noqa: E402
from core import phash  # noqa: E402
from core import task_manager as tm  # noqa: E402
from core.ffmpeg import find_ffmpeg  # noqa: E402
from core.filters import Filters  # noqa: E402

FFMPEG = find_ffmpeg()


# ---- 纯算法层: 不碰 ffmpeg ----

def test_gray_to_dhash_is_deterministic():
    """同一份灰度必须产出同一个指纹 —— 指纹不可复现, 后面一切比对都无意义。"""
    raw = bytes(range(72))
    a = phash.dhash_from_gray(raw)
    assert a == phash.dhash_from_gray(raw)
    assert a is not None and len(a) == 16


def test_dhash_is_64_bits():
    """9x8 取样 -> 每行 8 次相邻比较 -> 正好 64 位。少一位就说明尺寸写错了。"""
    raw = bytes([0, 255] * 36)
    val = int(phash.dhash_from_gray(raw), 16)
    assert 0 <= val < (1 << 64)


def test_flat_gray_yields_all_zero():
    """纯色图: 相邻像素永远相等 -> 全 0。这不是 bug, 是 dHash 的已知盲区。"""
    assert phash.dhash_from_gray(bytes([128] * 72)) == "0000000000000000"


def test_too_little_data_returns_none_not_partial_hash():
    """字节不够就返回 None —— 拿残缺数据算出来的指纹是"看起来正常"的错值。"""
    assert phash.dhash_from_gray(b"\x01" * 10) is None
    assert phash.dhash_from_gray(b"") is None
    assert phash.dhash_from_gray(None) is None


def test_distance_basic():
    assert phash.distance("0000000000000000", "0000000000000000") == 0
    assert phash.distance("0000000000000000", "0000000000000001") == 1
    assert phash.distance("0000000000000000", "ffffffffffffffff") == 64


def test_uncomparable_returns_none_not_zero():
    """⚠️ 这是本模块最重要的一条断言。

    "没法比"(形态不对)与"完全相同"(距离 0)必须是两个不同的返回值。把前者
    当成后者去标重, 就是凭空冤枉用户的文件 —— 而"只有 16 位十六进制才算指纹"
    这件事, 恰恰是最容易被后来的改动破坏的。
    """
    assert phash.distance(None, "0000000000000000") is None
    assert phash.distance("0000000000000000", "") is None
    assert phash.distance("0000", "0000000000000000") is None
    assert phash.distance("zzzzzzzzzzzzzzzz", "0000000000000000") is None
    assert phash.is_duplicate(None, None) is False, "算不出指纹必须判为不重复"


def test_threshold_is_inclusive():
    a = "0000000000000000"
    b = "0000000000000007"        # 距离 3
    assert phash.is_duplicate(a, b, threshold=3) is True
    assert phash.is_duplicate(a, b, threshold=2) is False


def test_find_duplicate_picks_nearest_not_first():
    """要报"与第 N 张最像", 所以必须取**最近**的。

    取第一个命中的会让提示语里那个距离看着莫名其妙(明明有更像我的一张却说
    跟另一张像), 用户一旦觉得提示不准, 整个标记就没人看了。
    """
    target = "0000000000000000"
    known = [(1, "000000000000ffff"), (2, "0000000000000003"), (3, "00ff000000000000")]
    assert phash.find_duplicate(target, known, threshold=8) == (2, 2)


def test_find_duplicate_respects_threshold_and_empty():
    assert phash.find_duplicate("0" * 16, [(1, "f" * 16)], threshold=8) is None
    assert phash.find_duplicate("0" * 16, [], threshold=8) is None
    assert phash.find_duplicate(None, [(1, "0" * 16)]) is None


def test_dhash_of_missing_file_is_none():
    """约束 2: 失败即放行 —— 路径不存在也不许抛异常。"""
    assert phash.dhash("Z:/definitely/not/here.jpg") is None


# ---- 解码层: 现场生成真图, 探测不到 ffmpeg 就跳过 ----

pytestmark_ffmpeg = pytest.mark.skipif(
    not FFMPEG, reason="未探测到 ffmpeg, 跳过真实解码用例"
)


def _make(path, source, size, extra=None):
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
           f"{source}=size={size}", "-frames:v", "1"]
    cmd += list(extra or [])
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return path


@pytestmark_ffmpeg
def test_same_image_recompressed_is_detected(tmp_path):
    """同图不同尺寸 + 重压成 jpg: 这是图集站上最常见的重复形态。

    ⚠️ 实测这两条距离都是 0, 但断言只写"<= 阈值"而不是"== 0" —— 换 ffmpeg
    版本后缩放算法可能微调, 把断言钉死在 0 上会让测试变成"ffmpeg 版本探测器"。
    要钉的是**判别力**(同图近、异图远), 不是某个具体数值。
    """
    a = _make(tmp_path / "a.png", "testsrc2", "320x240")
    b = _make(tmp_path / "b.png", "testsrc2", "640x480")
    d = _make(tmp_path / "d.jpg", "testsrc2", "320x240", ["-q:v", "20"])
    c = _make(tmp_path / "c.png", "smptebars", "320x240")

    ha, hb, hd, hc = phash.dhash(a), phash.dhash(b), phash.dhash(d), phash.dhash(c)
    assert all([ha, hb, hd, hc]), "真图必须能算出指纹"

    assert phash.is_duplicate(ha, hb), "同一张图换尺寸必须判为重复"
    assert phash.is_duplicate(ha, hd), "同一张图重压 jpg 必须判为重复"
    assert not phash.is_duplicate(ha, hc), "完全不同的图不能被判成重复"

    # 灰区留白: 判别力要"拉得开", 否则阈值怎么定都难受
    assert phash.distance(ha, hc) > phash.distance(ha, hb) + 10


@pytestmark_ffmpeg
def test_animated_input_only_reads_first_frame(tmp_path):
    """动图不能把指纹算歪: 少了 `-frames:v 1`, ffmpeg 会一路吐帧。

    输出字节数超过 9x8 时, 旧实现会把后面的帧当成同一张图的像素接着算 ——
    得到的是一个"稳定但错误"的指纹, 比报错难查得多。
    """
    gif = _make(tmp_path / "anim.gif", "testsrc2", "160x120",
                ["-t", "1", "-r", "10"])
    got = phash.dhash(gif)
    assert got is not None
    # 与同一来源的静帧相比仍应接近(首帧就是它)
    still = _make(tmp_path / "still.png", "testsrc2", "160x120")
    assert phash.distance(got, phash.dhash(still)) <= phash.DEFAULT_THRESHOLD


# ---- 任务链路: 只标记, 绝不删除 ----

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


class FakeImageDownloader:
    """把指定文件复制到 save_dir, 模拟一次成功下载(内容由调用方给)。"""

    def __init__(self, src, name, sha):
        self.src = Path(src)
        self.name = name
        self.sha = sha

    def download(self, url, **kw):
        p = Path(kw["save_dir"]) / self.name
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.src, p)
        info = kw.get("info")
        if info is not None:
            info["resolved_url"] = url
            info["content_type"] = "image/png"
        return p, self.sha


def _run_two(mgr, tid, out_dir, first, second):
    """依次"下载"两条资源, 返回 (第一条 id, 第二条 id)。"""
    rid1 = db.add_resource(tid, "image", "https://x/one.png", "{}", filename="one.png")
    rid2 = db.add_resource(tid, "image", "https://x/two.png", "{}", filename="two.png")
    tm.DOWNLOADERS["image"] = lambda: FakeImageDownloader(first, "one.png", "a" * 64)
    mgr._download_one(tid, db.get_resource(rid1), None, out_dir, Filters({}))
    tm.DOWNLOADERS["image"] = lambda: FakeImageDownloader(second, "two.png", "b" * 64)
    mgr._download_one(tid, db.get_resource(rid2), None, out_dir, Filters({}))
    return rid1, rid2


@pytestmark_ffmpeg
def test_duplicate_is_marked_but_file_kept(tmp_db, tmp_path, monkeypatch):
    """核心契约: 判重只写 `duplicate_of`, **不删文件、不改 status**。

    dHash 会误判(纯色图、连拍都可能撞), 而删文件不可逆。所以这里的断言不是
    "少了一个文件", 而是"两个文件都在, 只是多了一个标记"。
    """
    same = _make(tmp_path / "src_a.png", "testsrc2", "320x240")
    same2 = _make(tmp_path / "src_b.png", "testsrc2", "480x360")

    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    tid = db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rid1, rid2 = _run_two(mgr, tid, out_dir, same, same2)
        r1, r2 = db.get_resource(rid1), db.get_resource(rid2)
        assert r1["status"] == "done" and r2["status"] == "done"
        assert r1["phash"] and r2["phash"], "两张图都应留下指纹, 便于事后核对"
        assert r2["duplicate_of"] == rid1, "第二条应指向第一条"
        assert r1["duplicate_of"] is None, "先下的那条不该被反向标记"
        assert (out_dir / "one.png").exists() and (out_dir / "two.png").exists(), \
            "判重绝不删文件 —— 删了就没法人工复核了"
    finally:
        mgr.shutdown(wait=True)


@pytestmark_ffmpeg
def test_distinct_images_are_not_marked(tmp_db, tmp_path, monkeypatch):
    """反向断言: 不能把不相干的图标成重复, 否则标记就是噪音。"""
    a = _make(tmp_path / "s1.png", "testsrc2", "320x240")
    b = _make(tmp_path / "s2.png", "smptebars", "320x240")

    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    tid = db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rid1, rid2 = _run_two(mgr, tid, out_dir, a, b)
        assert db.get_resource(rid2)["duplicate_of"] is None
    finally:
        mgr.shutdown(wait=True)


def test_perceptual_dedup_can_be_turned_off(tmp_db, tmp_path, monkeypatch):
    """关掉之后必须**一次解码都不做** —— 用户嫌慢时的退路要真的有效。"""
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {})
    calls = []
    monkeypatch.setattr(phash, "dhash", lambda p, *a, **k: calls.append(p) or None)

    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    tid = db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "x.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    try:
        rid = db.add_resource(tid, "image", "https://x/x.png", "{}", filename="x.png")
        tm.DOWNLOADERS["image"] = lambda: FakeImageDownloader(src, "x.png", "c" * 64)
        mgr._download_one(tid, db.get_resource(rid), None, out_dir,
                          Filters({"dedup_perceptual": False}))
        assert calls == [], "显式关掉后不该再去解码"
        assert db.get_resource(rid)["status"] == "done"
    finally:
        mgr.shutdown(wait=True)


def test_video_is_not_perceptually_hashed(tmp_db, tmp_path, monkeypatch):
    """只对 image 做 —— 视频首帧的 dHash 没有判别意义, 却要付一次解码代价。

    `_mark_perceptual_dup` 自己不判类型(判在调用点), 所以这里必须从
    `_download_one` 走一遍: 只测私有方法的"类型判断"是没有的, 会得到一个
    永远通过的空断言。
    """
    marked = []
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    monkeypatch.setattr(
        mgr, "_mark_perceptual_dup",
        lambda *a, **k: marked.append(a) or None,
    )
    tid = db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "v.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    try:
        rid = db.add_resource(tid, "video", "https://x/v.mp4", "{}", filename="v.mp4")
        tm.DOWNLOADERS["video"] = lambda: FakeImageDownloader(src, "v.mp4", "e" * 64)
        mgr._download_one(tid, db.get_resource(rid), None, out_dir, Filters({}))
        assert marked == [], "video 类型不该触发感知指纹"
        assert db.get_resource(rid)["status"] == "done"
    finally:
        mgr.shutdown(wait=True)


def test_decode_failure_never_breaks_download(tmp_db, tmp_path, monkeypatch):
    """约束 2 的端到端版本: 坏文件(算不出指纹)必须照样算下载成功。"""
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {})
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    tid = db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"<html>not an image at all</html>")
    try:
        rid = db.add_resource(tid, "image", "https://x/junk.png", "{}", filename="junk.png")
        tm.DOWNLOADERS["image"] = lambda: FakeImageDownloader(junk, "junk.png", "d" * 64)
        mgr._download_one(tid, db.get_resource(rid), None, out_dir, Filters({}))
        row = db.get_resource(rid)
        assert row["status"] == "done", "算不出指纹不该让下载失败"
        assert row["phash"] is None and row["duplicate_of"] is None
    finally:
        mgr.shutdown(wait=True)


def test_phash_is_scoped_to_the_same_task(tmp_db, tmp_path, monkeypatch):
    """比对只在任务内 —— 跨任务的"重复"用户点不过去也删不掉, 是不可行动的信息。"""
    other = db.create_task("https://fake/other", "fake", None, {})
    oid = db.add_resource(other, "image", "https://x/o.png", "{}", filename="o.png")
    db.update_resource(oid, status="done", phash="0000000000000000")
    mine = db.create_task("https://fake/mine", "fake", None, {})
    rid = db.add_resource(mine, "image", "https://x/m.png", "{}", filename="m.png")
    db.update_resource(rid, status="done", phash="0000000000000001")
    assert db.task_phashes(mine, exclude_id=rid) == []
    assert len(db.task_phashes(other)) == 1


def test_task_phashes_skips_unfinished_and_self(tmp_db):
    t = db.create_task("https://fake/a", "fake", None, {})
    done = db.add_resource(t, "image", "https://x/1.png", "{}", filename="1.png")
    db.update_resource(done, status="done", phash="00000000000000ff")
    pending = db.add_resource(t, "image", "https://x/2.png", "{}", filename="2.png")
    db.update_resource(pending, status="pending", phash="ffffffffffffffff")
    failed = db.add_resource(t, "image", "https://x/3.png", "{}", filename="3.png")
    db.update_resource(failed, status="failed", phash="ffffffffffff0000")

    got = db.task_phashes(t)
    assert got == [(done, "00000000000000ff")], "未完成/失败的资源不参与比对"
    assert db.task_phashes(t, exclude_id=done) == []
