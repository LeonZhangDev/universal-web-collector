"""枚举快路径: 用"抽样校验过的区间"替代逐张探测。

为什么需要它
------------
逐张探测是 O(N) 次 HEAD, 每次之间还要停顿 —— 300 张图约 90 秒**纯等待**,
而真正传数据只占 5%。相册页既然把数量写在了 HTML 里, 就没必要再一个个去问。

但快路径的本质是"用抽样推断全体", 所以这组用例守的是**它什么时候必须放弃**:
抽样不中、数量不对、功能关掉, 都要老老实实退回逐张扫描。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from collectors import gallery_base as G
from collectors.xchina.gallery import XCHINA
from core.config import settings

GID = "69ad45698f836"
BASE = "https://img.xchina.io/photos/" + GID + "/"


class Fake:
    """序号 1..n 存在, 其余返回该站的越界指纹(200 + text/html)。"""

    def __init__(self, n):
        self.n = n
        self.seen = []

    def head(self, url, **kw):
        self.seen.append(url)
        try:
            tail = url.rsplit("/", 1)[-1].split(".")[0]
            seq = int(tail)
        except ValueError:
            seq = -1
        ok = 1 <= seq <= self.n
        return type("R", (), {
            "status_code": 200,
            "headers": {
                "Content-Type": "image/jpeg" if ok else "text/html",
                "Content-Length": "656713" if ok else "1024",
            },
        })()

    def close(self):
        pass


def run(n_exist, declared, **kw):
    sess = Fake(n_exist)
    diag = {}
    items = list(G.discover(
        XCHINA, GID, media="image", album="g", session=sess,
        min_interval=0, max_interval=0, diag=diag,
        declared_count=declared, **kw))
    return items, sess, diag


def test_sample_points_always_cover_both_ends():
    """首尾必测: "数量错一个"和"起点错一位"是最常见的两种不一致。"""
    pts = G._sample_points(1, 300, 6)
    assert pts[0] == 1 and pts[-1] == 300
    assert G._sample_points(5, 5, 6) == [5]
    assert G._sample_points(1, 2, 6) == [1, 2]
    assert len(set(G._sample_points(1, 10, 4))) == 4


def test_declared_count_skips_per_item_probing():
    """核心收益: 数量来自页面时, 探测次数从 O(N) 降到常数。"""
    items, sess, diag = run(50, 50)
    assert len(items) == 50
    # 第一张 + 抽样点(不含已探过的第一张)
    assert len(sess.seen) <= settings.enumeration_samples + 2, len(sess.seen)
    assert diag["enumeration"]["mode"] == "declared"
    assert diag["enumeration"]["end"] == 50
    assert [i["seq"] for i in items] == list(range(1, 51))


def test_declared_count_is_verified_before_being_trusted():
    """页面多报了(60)而实际只有 50 张: 抽样在末尾不中 -> 退回逐张扫描。

    宁可慢, 也不能照着一个错的上界给出一批 404。
    """
    items, sess, diag = run(50, 60)
    assert len(items) == 50, "退回逐张扫描后仍应拿到真实数量"
    assert "enumeration" not in diag, "没走快路径就不该留下快路径的记录"
    assert len(sess.seen) > settings.enumeration_samples + 2


def test_missing_item_in_the_middle_also_falls_back():
    """中间缺一张(30)时, 抽样会撞上并退回 —— 不给用户一张不存在的图。"""
    class Hole(Fake):
        def head(self, url, **kw):
            if url.rsplit("/", 1)[-1].startswith("00030."):
                self.seen.append(url)
                return type("R", (), {
                    "status_code": 200,
                    "headers": {"Content-Type": "text/html", "Content-Length": "1024"},
                })()
            return super().head(url, **kw)

    sess = Hole(50)
    diag = {}
    items = list(G.discover(XCHINA, GID, media="image", album="g", session=sess,
                            min_interval=0, max_interval=0, diag=diag,
                            declared_count=50))
    assert len(items) == 49
    assert "enumeration" not in diag


def test_fast_path_can_be_turned_off(monkeypatch):
    """关掉就必须是老老实实的逐张扫描(可观测、可回退)。"""
    monkeypatch.setattr(settings, "enumeration_fast", False)
    items, sess, diag = run(50, 50)
    assert len(items) == 50
    assert len(sess.seen) > 50
    assert "enumeration" not in diag


def test_no_declared_count_means_no_guessing_by_default(monkeypatch):
    """⚠️ 页面没给数量时默认**不去**自己找边界。

    二分假定序号连续, 中间恰好缺一张就会把上界定在缺口之前 ——
    那是"300 张只采到 4 张"的静默截断, 比慢得多更糟。
    """
    monkeypatch.setattr(settings, "enumeration_search", False)
    items, sess, diag = run(20, None)
    assert len(items) == 20
    assert len(sess.seen) > 20
    assert "enumeration" not in diag


def test_upper_bound_search_is_opt_in(monkeypatch):
    """确认目标站点序号连续后, 可以打开指数探上界 + 二分。"""
    monkeypatch.setattr(settings, "enumeration_search", True)
    items, sess, diag = run(50, None)
    assert len(items) == 50
    assert diag["enumeration"]["mode"] == "search"
    assert len(sess.seen) < 30, "应当远少于 50 次逐张探测"


def test_fast_path_keeps_the_first_size_and_leaves_the_rest_to_the_downloader():
    """体积过滤会自己补 HEAD(见 task_manager), 所以不必为它多探 N 次。"""
    items, _sess, _diag = run(50, 50)
    assert items[0]["size"] == 656713
    assert items[1]["size"] is None


def test_declared_count_clamped_by_max_count():
    """预览用小 max_count 限时, 快路径不能越过它。"""
    sess = Fake(50)
    items = list(G.discover(XCHINA, GID, media="image", album="g", session=sess,
                            min_interval=0, max_interval=0, max_count=12,
                            declared_count=50))
    assert [i["seq"] for i in items] == list(range(1, 13))
