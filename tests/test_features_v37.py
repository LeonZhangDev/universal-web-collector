"""V37 测试: 把三条**被静默丢掉的建议**补上。

这三条不是新想法 —— 它们在更早那轮的深度分析里就写下来了, 只是既没进
`PROJECT_OVERVIEW` 的「后续可做」表, 也没实现, 于是从 backlog 里消失了。复核时
靠"逐条读代码"才发现: 下面这三个能力**代码里根本没有**。

1. **媒体元数据落库** —— `resources` 表原来只有 `size`, 没有宽高/时长。讽刺的是
   这两个数字**本来就算出来过**: 尺寸终检在 `filters.need_dimensions` 时调过
   `image_dimensions()`, 直链/HLS/DASH 三条收尾路径也都 ffprobe 过时长, 结果全被
   随手丢掉。所以这条改动**不新增任何探测**, 只是把已经在做的事留下结果。
2. **下载优先级** —— `futures` 一次性按采集顺序全提交, "先下哪个"完全由站点枚举
   顺序决定。线程池是固定大小的, 空闲 worker 按**提交序**取任务 —— 所以重排提交
   顺序就是事实上的优先级, 不需要另造一套优先级队列。
3. **跨任务死信重放** —— 原来的重试都是任务内的(`/tasks/{id}/retry-failed`)。同一批
   404 散在十几个任务里时, 逐个任务点进去看拼不出全貌, 也就没人会去重放它们。

贯穿三条的两条纪律(也是断言的重点):
* **排序不是过滤** —— `order_resources` 的输出长度必须与输入恒等;
* **"没测量" ≠ "0"** —— 没装 ffprobe 时 `duration` 留 NULL。写成 0 会把"能力缺失"
  伪装成"内容问题", 让用户去重下一个其实完好、只是本机缺个探测器的文件。
"""
import shutil
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db
import core.task_manager as tm
import downloaders.video as V
from core.filters import Filters


# ==================================================================
# 夹具
# ==================================================================

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v37.db")
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """只挂 router 的 TestClient。⚠️ 这里**不** stub `submit_resource` ——
    真实的那份自带护栏(任务必须终态), 而"护栏真的拦住了"正是要测的东西。

    ⚠️ 但 `submit` 必须 stub —— 它是**真 worker 的点火开关**。
    `task_manager` 是模块级全局实例(不是本用例的夹具产物), `POST /tasks/create`
    走到 `task_manager.submit()` 会往它那个固定线程池里排一个**真采集任务**。
    用例结束、夹具把 `DB_PATH` 换成下一个用例的库之后, 那些线程还活着, 并且会
    继续对**当前**这个库写心跳(`db.update_task(tid, hb=now)`) —— 而每个用例的库
    id 都从 1 开始, 于是它"恰好"在给下一个用例的任务 1 续心跳。
    症状: `test_watchdog.py` 里"遗留任务应被判 failed"的断言随机变红
    (`assert 'running' == 'failed'`), 因为看门狗看到的心跳是新鲜的;
    单独跑那个文件全绿, 合起来跑才红 —— 典型的跨用例泄漏, 不是回归。

    这几个用例要验的是 HTTP 层与落库(选项有没有写进去), 不是采集流水线。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v37api.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda *a, **kw: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app)


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    """一个**用完就关**的 TaskManager。

    ⚠️ 必须关, 而且要用 `wait=True`。`TaskManager.__init__` 会把自己登记进模块级
    `_LIVE_MANAGERS`(看门狗判"这个活动任务真没人持有了吗"要问过**所有**在册实例),
    并起两个后台线程。不关的话这两样都会活到整个会话结束 —— 而 `_LIVE_MANAGERS`
    里多一个实例, 别的用例里"我才是唯一持有者吗"的判断就多一个变量。

    这不是洁癖: 本项目其它测试文件(test_watchdog / test_phash / test_image_filter /
    test_robustness ...)全部写了 try/finally + shutdown, 只有本文件漏了。
    """
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "DOWNLOADERS", {})
    m = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        yield m
    finally:
        m.shutdown(wait=True)


def _png_bytes(w, h):
    """手写一张最小合法 PNG(8bit truecolor)。不依赖 ffmpeg/Pillow。

    `imageinfo.image_dimensions` 只读文件头, 但内容也做成真能解码的 ——
    否则一旦 phash 那条路径真的去解码, 用例会因为"图是坏的"而变红, 与
    被测的"宽高有没有落库"完全无关。
    """
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeDownloader:
    """把指定文件复制到 save_dir, 模拟一次成功下载。

    `info_keys` 用来模拟下载器往 `info` 里回填什么 —— 媒体时长落在这一层,
    所以"消费者有没有读它"必须能在这里控制。
    """

    def __init__(self, src, name, sha, content_type="image/png", info_keys=None):
        self.src = Path(src)
        self.name = name
        self.sha = sha
        self.content_type = content_type
        self.info_keys = dict(info_keys or {})

    def download(self, url, **kw):
        p = Path(kw["save_dir"]) / self.name
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.src, p)
        info = kw.get("info")
        if info is not None:
            info["resolved_url"] = url
            info["content_type"] = self.content_type
            info.update(self.info_keys)
        return p, self.sha


def _one(mgr, tid, out_dir, rtype, url, filename, fake, filters=None):
    """入库一条资源并跑一次 `_download_one`, 返回资源 id。"""
    rid = db.add_resource(tid, rtype, url, "{}", filename=filename)
    tm.DOWNLOADERS[rtype] = lambda: fake
    mgr._download_one(tid, db.get_resource(rid), None, out_dir,
                      filters or Filters({"dedup_perceptual": False}))
    return rid


def _task(tid=None):
    tid = tid or db.create_task("https://fake/album", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
    return tid


# ==================================================================
# 1. 媒体元数据落库
# ==================================================================

def test_media_columns_exist_and_the_migration_is_idempotent(tmp_db, tmp_path, monkeypatch):
    """新列必须**同时**进 SCHEMA 与 `_ADD_COLUMNS`。

    只改一处的话: 新库有列、旧库没有 —— 而旧库是用户的真库, 升级时会在第一次
    `update_resource(width=...)` 上报 "no such column"。反过来只加迁移、新库靠
    SCHEMA 建表, 同样缺列。
    """
    conn = db.get_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(resources)")}
    assert {"width", "height", "duration"} <= cols, cols

    # 再走一遍迁移(模拟"旧库第一次升级"): 必须幂等, 否则每次启动都 ALTER 一次
    db._migrate(conn)
    assert {r["name"] for r in conn.execute("PRAGMA table_info(resources)")} == cols


def test_note_duration_records_only_real_measurements():
    """`_note_duration` 是"实测时长"回填进 info 的唯一入口。"""
    info = {}
    V._note_duration(info, 12.5)
    assert info == {"probed_duration": 12.5}

    # None = 没装 ffprobe 或读不出。**不能写成 0** —— 界面会显示"时长 0:00",
    # 把"本机缺探测器"伪装成"这个视频是坏的"。
    V._note_duration(info, None)
    assert info == {"probed_duration": 12.5}
    # 0 同样算"没量到"(ffprobe 对无时长流会给 0), 不能覆盖掉上一次的真值
    V._note_duration(info, 0)
    assert info == {"probed_duration": 12.5}

    # info 不是 dict(调用方不关心溯源)时必须静默跳过而不是抛错 ——
    # 探测只是附加能力, 不该让下载失败
    V._note_duration(None, 3.0)
    V._note_duration("not-a-dict", 3.0)


def test_image_dimensions_are_recorded_on_success(tmp_db, tmp_path, mgr):
    """关键: 即使**没开**尺寸过滤, 宽高也要落库。

    原实现只在 `filters.need_dimensions` 为真时才调 `image_dimensions()`,
    关掉过滤就完全不算 —— 于是"分辨率"这个字段永远是空的, 界面也就永远没法
    按分辨率筛选或显示尺寸。
    """
    src = tmp_path / "src.png"
    src.write_bytes(_png_bytes(320, 200))

    tid = _task()
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)

    rid = _one(mgr, tid, out_dir, "image", "https://x/a.png", "a.png",
               FakeDownloader(src, "a.png", "a" * 64))
    row = db.get_resource(rid)
    assert (row["width"], row["height"]) == (320, 200)
    assert row["duration"] is None          # 图片不该有时长


def test_video_duration_is_taken_from_what_the_downloader_already_measured(
        tmp_db, tmp_path, mgr):
    """视频时长走 `info["probed_duration"]`, 而不是在任务层再探测一遍。

    下载层收尾时**已经** ffprobe 过一次(直链/HLS/DASH 三条路都量过)。再探测一遍
    等于每个视频 spawn 两次 ffprobe, 而且两处判据将来必然漂移。
    """
    src = tmp_path / "v.mp4"
    src.write_bytes(b"\x00" * 64)

    tid = _task()
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)

    fake = FakeDownloader(src, "v.mp4", "b" * 64, content_type="video/mp4",
                          info_keys={"probed_duration": 93.5})
    rid = _one(mgr, tid, out_dir, "video", "https://x/v.mp4", "v.mp4", fake)
    assert db.get_resource(rid)["duration"] == 93.5


def test_duration_stays_null_when_nothing_was_measured(tmp_db, tmp_path, mgr):
    """反向断言: 没量到就**留空**, 不许编一个 0 出来。

    这是"缺探测器 ≠ 文件坏"那条约束在落库层的体现。写 0 的后果不是报错, 而是
    界面上一个"时长 0:00"的视频 —— 用户会去重下它, 然后得到同一个"0:00"。
    """
    src = tmp_path / "v2.mp4"
    src.write_bytes(b"\x00" * 64)

    tid = _task()
    out_dir = tmp_path / "dl" / str(tid)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 下载器**没有**回填 probed_duration = 本机没有 ffprobe 或探测失败
    fake = FakeDownloader(src, "v2.mp4", "c" * 64, content_type="video/mp4")
    rid = _one(mgr, tid, out_dir, "video", "https://x/v2.mp4", "v2.mp4", fake)
    assert db.get_resource(rid)["duration"] is None


def test_media_metadata_reaches_the_resource_apis(client, tmp_path):
    """⚠️ 专治"pydantic 吞字段": `ResourceOut` 只回传**声明过**的字段。

    漏声明时不会报错 —— 字段在响应里静默消失, 前端拿不到就什么都不显示,
    而库里的值是对的。查起来会一路怀疑到落库那一步("是不是没写进去")。
    两个接口都要核: `/tasks/{id}` 走 `ResourceOut`, `/library` 走 `SELECT r.*`。
    """
    tid = db.create_task("https://fake/album", "fake", None, {})
    f = tmp_path / "x.png"
    f.write_bytes(_png_bytes(8, 8))
    rid = db.add_resource(tid, "image", "https://x/x.png", "{}",
                          filename="x.png", status="done", local_path=str(f))
    db.update_resource(rid, width=1024, height=768, size=4096, duration=None)

    detail = client.get(f"/tasks/{tid}").json()
    item = detail["resources"][0]
    assert item["width"] == 1024 and item["height"] == 768

    lib = client.get("/library").json()
    assert lib["items"][0]["width"] == 1024
    assert lib["items"][0]["height"] == 768


# ==================================================================
# 2. 下载优先级(提交顺序)
# ==================================================================

def _res(rtype, url, size=None):
    return {"type": rtype, "url": url, "size": size}


def test_order_resources_never_drops_or_adds_anything():
    """**排序不是过滤**。任何"顺手丢掉一部分"的实现都会让资源静默不下 ——
    这正是第 10 条静默坑那一族(把丢数据改装成静默)。
    """
    rows = [_res("image", "i1"), _res("video", "v1"), _res("image", "i2")]
    for mode in tm.RESOURCE_ORDERS + ("nonsense", "", None):
        out = tm.order_resources(rows, mode)
        assert len(out) == len(rows)
        assert sorted(r["url"] for r in out) == sorted(r["url"] for r in rows)


def test_video_first_keeps_the_original_order_within_each_group():
    """视频提前, 但**同组内保持采集原序** —— 同站请求的先后节奏不该因为开了个
    开关就变(`sorted` 稳定, 这条是钉住这个性质而不是钉住实现)。"""
    rows = [_res("image", "i1"), _res("video", "v1"), _res("image", "i2"),
            _res("video", "v2")]
    out = tm.order_resources(rows, "video_first")
    assert [r["url"] for r in out] == ["v1", "v2", "i1", "i2"]


def test_small_first_puts_unknown_sizes_last_not_first():
    """体积未知的必须排**最后**。

    只有视频会自报体积(采集阶段 `add_resource(size=...)`), 图片要下完才知道。
    若把 None 当 0, 一整个相册的图会全部插到已声明体积之前 —— 等于把"唯一可能
    很慢的那批"推到最前面, 与这个模式的意图正好相反。
    """
    rows = [_res("image", "unknown1", None), _res("video", "big", 900),
            _res("image", "unknown2", None), _res("video", "small", 100)]
    out = tm.order_resources(rows, "small_first")
    assert [r["url"] for r in out] == ["small", "big", "unknown1", "unknown2"]


def test_unknown_order_falls_back_to_the_original_sequence():
    """拼错的取值**不抛错**、也不重排: 选项是用户/旧前端填的, 一个错字不该让
    整个任务起不来。真正的防错在 API 层(见下一条)。"""
    rows = [_res("image", "i1"), _res("video", "v1")]
    assert tm.order_resources(rows, "video-first") == rows   # 连字符写错
    assert tm.order_resources(rows, "original") == rows


def test_create_rejects_an_unknown_resource_order(client):
    """创建接口对取值做 **400** 而不是静默忽略。

    与 `quality` / `media` 同一套处理: 静默忽略的后果是"界面选了却没效果、
    也不报错", 那正是这个项目反复踩的那类缺陷。
    """
    r = client.post("/tasks/create", json={
        "url": "https://example.com/a/1", "collector": "generic",
        "resource_order": "video-first",
    })
    assert r.status_code == 400
    assert "resource_order" in r.json()["detail"]


def test_valid_resource_order_lands_in_task_options(client):
    r = client.post("/tasks/create", json={
        "url": "https://example.com/a/2", "collector": "generic",
        "resource_order": "video_first",
    })
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    import json as _json
    opts = _json.loads(db.get_task(tid)["options"] or "{}")
    assert opts["resource_order"] == "video_first"
    # 用中文注释解释: 不传时**不写**这个键 —— 老任务/旧前端保持默认原序
    r2 = client.post("/tasks/create", json={
        "url": "https://example.com/a/3", "collector": "generic",
    })
    opts2 = _json.loads(db.get_task(r2.json()["task_id"])["options"] or "{}")
    assert "resource_order" not in opts2


def test_config_exposes_the_resource_orders(client):
    """取值由后端下发。前端硬编码一份的话, 后端加一个模式界面永远看不到 ——
    而"少一个选项"没人会当成 bug 报。"""
    cfg = client.get("/config").json()
    assert cfg["resource_orders"] == list(tm.RESOURCE_ORDERS)


# ==================================================================
# 3. 跨任务死信重放
# ==================================================================

def _fail(tid, url, kind, status="failed", rtype="image"):
    rid = db.add_resource(tid, rtype, url, "{}", status=status)
    db.update_resource(rid, error_kind=kind, note=f"boom {kind}")
    return rid


def test_failure_kinds_separates_total_from_replayable(tmp_db):
    """一个原因给**两个**数字: 总共几条 / 其中重放能救几条。

    只给总数是不够的: 界面会顺手拿它当"即将重放的条数", 于是 `corrupt` 那一类
    "提示 12 条、实际起来 0 条" —— 用户只会以为点了没生效。
    `corrupt` 且 `status='done'` 是真实存在的(文件留着、解码器说它坏了),
    它该被**看见**, 但重下救不了(源站给的就是坏字节)。
    """
    tid = _task()
    _fail(tid, "https://x/1.jpg", "network")
    _fail(tid, "https://x/2.jpg", "network")
    corrupt = db.add_resource(tid, "image", "https://x/3.jpg", "{}", status="done")
    db.update_resource(corrupt, error_kind="corrupt", note="解码失败")

    kinds = {k["kind"]: k for k in db.failure_kinds()}
    assert kinds["network"]["n"] == 2
    assert kinds["network"]["replayable"] == 2
    assert kinds["corrupt"]["n"] == 1
    assert kinds["corrupt"]["replayable"] == 0     # ← 这条是关键


def test_gone_is_excluded_from_replay_by_default(tmp_db):
    """源站已删(404/410)默认不重放: 自动重放它是纯空转。

    但它必须**看得见** —— 用户需要区分"还有救的"和"已经没了的"。
    """
    tid = _task()
    _fail(tid, "https://x/gone.jpg", "gone", status="gone")
    _fail(tid, "https://x/net.jpg", "network")

    assert [r["url"] for r in db.failure_refs()] == ["https://x/net.jpg"]
    assert db.failure_count() == 1
    both = {r["url"] for r in db.failure_refs(include_gone=True)}
    assert both == {"https://x/net.jpg", "https://x/gone.jpg"}
    assert db.failure_count(include_gone=True) == 2

    assert {k["kind"] for k in db.failure_kinds()} == {"gone", "network"}
    assert {k["kind"] for k in db.failure_kinds(include_gone=False)} == {"network"}


def test_failure_refs_carries_the_task_name_without_an_n_plus_1(tmp_db):
    """列表要能说清"这是哪个任务里的哪一条", 所以带任务名(与 library_list 同形)。"""
    tid = _task()
    db.update_task(tid, name="相册 A")
    _fail(tid, "https://x/1.jpg", "server")
    row = db.failure_refs()[0]
    assert row["task_name"] == "相册 A"
    assert row["collector"] == "fake"


def test_library_failures_total_is_the_sum_of_the_kinds(client):
    """`total` 由 kinds 求和得来, 不另查一次 COUNT(*) —— 两个数字若来自两条 SQL,
    将来加一个筛选条件只会改一处, 于是"分组加起来 37、总数 41"。"""
    tid = _task()
    _fail(tid, "https://x/1.jpg", "network")
    _fail(tid, "https://x/2.jpg", "disk")

    data = client.get("/library/failures").json()
    assert data["total"] == sum(k["n"] for k in data["kinds"])
    assert data["total"] == 2
    assert set(data["kinds"][0]) >= {"kind", "label", "n", "replayable"}
    # 中文标签由后端下发, 不是把 kind 原样回传
    assert all(k["label"] for k in data["kinds"])


def test_replay_skips_resources_of_a_task_that_is_still_running(client):
    """护栏走的是**真实的** `submit_resource`(所以这里没 stub 它)。

    任务还在跑时重放会被拒 —— 否则新起的下载会和正在工作的 worker 抢同一个
    目标路径(第 6 条静默坑)。拒绝理由必须回给用户, 不能静默计入 submitted。
    """
    tid = db.create_task("https://fake/album", "fake", None, {})   # status=pending
    _fail(tid, "https://x/1.jpg", "network")

    r = client.post("/library/replay", json={"kinds": ["network"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["requested"] == 1
    assert out["submitted"] == 0
    assert out["skipped"] == 1
    assert any("still running" in n for n in out["notes"]), out["notes"]


def test_replay_submits_through_the_single_existing_path(client, monkeypatch):
    """重放**复用** `submit_resource`(唯一的重下入口), 不另开一条"直接下"的路。

    两条入口的判据一定会漂移, 而漂移的后果是绕过护栏。
    """
    calls = []
    monkeypatch.setattr(
        T.task_manager, "submit_resource",
        lambda tid, rid: (calls.append((tid, rid)) or (True, None)),
    )
    tid = _task()
    a = _fail(tid, "https://x/a.jpg", "network")
    b = _fail(tid, "https://x/b.jpg", "network")

    out = client.post("/library/replay", json={"kinds": ["network"]}).json()
    assert out["submitted"] == 2 and out["skipped"] == 0
    assert sorted(c[1] for c in calls) == [a, b]


def test_replay_reports_why_a_target_was_skipped(client, monkeypatch):
    """跳过理由**必须**回传: 点了 30 条只起来 4 条而不解释, 等于让用户以为
    程序吞了 26 条。"""
    def fake_submit(tid, rid):
        return (rid % 2 == 0, "演示原因")

    monkeypatch.setattr(T.task_manager, "submit_resource", fake_submit)
    tid = _task()
    ids = [_fail(tid, f"https://x/{i}.jpg", "network") for i in range(6)]

    out = client.post("/library/replay", json={"kinds": ["network"]}).json()
    assert out["requested"] == 6
    assert out["submitted"] + out["skipped"] == 6
    assert out["notes"], "跳过必须留下理由"
    assert len(out["notes"]) <= 21        # 20 条理由 + 一条"另有 N 条"汇总
    assert ids


def test_replay_treats_refs_and_kinds_as_an_intersection(client, monkeypatch):
    """两者都传时取**交集**, 不是并集。

    并集会让"我在这个原因里勾了几条"变成"整个原因全下" —— 一次点击就是几百个
    请求, 而用户以为自己只选了 2 条。
    """
    seen = []
    monkeypatch.setattr(
        T.task_manager, "submit_resource",
        lambda tid, rid: (seen.append(rid) or (True, None)),
    )
    tid = _task()
    a = _fail(tid, "https://x/a.jpg", "network")
    b = _fail(tid, "https://x/b.jpg", "server")

    out = client.post("/library/replay",
                      json={"refs": [a, b], "kinds": ["network"]}).json()
    assert out["requested"] == 1
    assert seen == [a]


def test_replay_records_a_note_for_a_ref_that_does_not_exist(client, monkeypatch):
    """不存在的 ref 要给出理由, 而不是从集合里静默消失。"""
    monkeypatch.setattr(T.task_manager, "submit_resource", lambda tid, rid: (True, None))
    out = client.post("/library/replay", json={"refs": [999999]}).json()
    assert out["requested"] == 1 and out["skipped"] == 1
    assert any("不存在" in n for n in out["notes"]), out["notes"]


def test_replay_respects_the_limit(client, monkeypatch):
    """单次上限是保护: 死信常常积到几百条, 一次全放出去会同时打死站点和本机。"""
    monkeypatch.setattr(T.task_manager, "submit_resource", lambda tid, rid: (True, None))
    tid = _task()
    for i in range(5):
        _fail(tid, f"https://x/{i}.jpg", "network")

    out = client.post("/library/replay",
                      json={"kinds": ["network"], "limit": 2}).json()
    assert out["requested"] == 2
    assert out["submitted"] == 2
