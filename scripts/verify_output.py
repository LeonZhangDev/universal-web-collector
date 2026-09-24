"""端到端验证: 命名模板 -> 下载 -> manifest -> 打包。

本地起一个静态服务当"站点", 用一个固定清单的采集器跑完整任务流程, 检查
产出目录与清单是否真的按预期落地。单元测试验证的是函数, 这里验证的是**整条
流水线接起来之后还对不对** —— 尤其是命名模板里的子目录, 它要穿过
task_manager、downloader、filesystem 三层才生效。

用法: python scripts/verify_output.py
"""

import base64
import io
import json
import shutil
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from api.tasks import _zip_stream  # noqa: E402
from core import database as db  # noqa: E402
from core.config import settings  # noqa: E402
from core.manifest import read_manifest  # noqa: E402
from collectors import COLLECTORS, register  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 512 + b"\xff\xd9"
#: 一段**真实**的最短 mp4(64x64 / 10fps / 1 秒, ffmpeg 生成), base64 内嵌约 2KB。
#:
#: ⚠️ 必须是真容器, 不能再像以前那样拼一段假字节冒充。V33 给直链下载补了内容终检
#: (`core/mediacheck.py` 的算术判据 + ffprobe 时长探测), 一段 ffprobe 读不出来的
#: 假 mp4 现在会被**正确地**判成坏文件并删除 —— 那时红的是"视频任务应当成功"这条
#: 断言, 而根因在 fixture 说谎, 排查方向会被彻底带偏。
#:
#: 内嵌而不是现场用 ffmpeg 生成: 既不让这个脚本依赖本机装没装 ffmpeg, 也保证服务
#: 出去的字节每次完全一致。顺带钉住一种最刁钻的输入 —— 下面的 Handler 会往 mp4
#: 后**追加**一段 `<!--文件名-->`, 追加数据会让 box 链错位(追加的首字节被当成新的
#: 长度字段), 正是 `mediacheck` 最容易被骗到的地方。
MP4 = base64.b64decode(
    """
    AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAANJbW9vdgAAAGxtdmhkAAAAAAAA
    AAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAA
    AAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAnR0cmFrAAAAXHRr
    aGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA
    AAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAA
    AAEAAAPoAAAAAAABAAAAAAHsbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAAKABVxAAA
    AAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABl21pbmYA
    AAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAA
    AQAAAVdzdGJsAAAAt3N0c2QAAAAAAAAAAQAAAKdhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAA
    AAAAAEAAQABIAAAASAAAAAAAAAABFExhdmM2My4xLjEwMSBsaWJ4MjY0AAAAAAAAAAAAAAAA
    GP//AAAALWF2Y0MBQsAK/+EAFmdCwAraEJsBEAAAAwAQAAADAUDxImoBAARozg/IAAAAEHBh
    c3AAAAABAAAAAQAAABRidHJ0AAAAAAAAFmAAAAAAAAAAGHN0dHMAAAAAAAAAAQAAAAoAAAQA
    AAAAFHN0c3MAAAAAAAAAAQAAAAEAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAoAAAABAAAAPHN0
    c3oAAAAAAAAAAAAAAAoAAAJyAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAK
    AAAAFHN0Y28AAAAAAAAAAQAAA3kAAABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAA
    AABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2
    ZjYzLjEuMTAxAAAACGZyZWUAAALUbWRhdAAAAlQGBf//UNxF6b3m2Ui3lizYINkj7u94MjY0
    IC0gY29yZSAxNjUgcjMyMjMgMDQ4MGNiMCAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBD
    b3B5bGVmdCAyMDAzLTIwMjUgLSBodHRwOi8vd3d3LnZpZGVvbGFuLm9yZy94MjY0Lmh0bWwg
    LSBvcHRpb25zOiBjYWJhYz0wIHJlZj0xIGRlYmxvY2s9MDowOjAgYW5hbHlzZT0wOjAgbWU9
    ZGlhIHN1Ym1lPTAgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBtaXhlZF9yZWY9MCBtZV9yYW5n
    ZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTAgOHg4ZGN0PTAgY3FtPTAgZGVhZHpvbmU9MjEs
    MTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9MCB0aHJlYWRzPTIgbG9va2FoZWFk
    X3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2Vk
    PTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0wIHdlaWdo
    dHA9MCBrZXlpbnQ9MjUwIGtleWludF9taW49MTAgc2NlbmVjdXQ9MCBpbnRyYV9yZWZyZXNo
    PTAgcmM9Y3JmIG1idHJlZT0wIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02
    OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTAAgAAAABZliIQ6JigACQLJycnXXXXXXXXX
    XXXgAAAABkGaIBGgjAAAAAZBmkASoIwAAAAGQZpgEqCMAAAABkGagBKgjAAAAAZBmqASoIwA
    AAAGQZrAEqCMAAAABkGa4BKgjAAAAAZBmwASoIwAAAAGQZsgEqCM
    """
)
PAGES = 3

#: 断言总数 —— 与 README 的「`verify_output.py` … (40 项断言)」是同一个数,
#: `scripts/gateguard.py` 会拿两边对账。
#:
#: 为什么要有这个常量: 这个脚本的失败方式是"**少核了几项**"。有人删掉一段用例,
#: 脚本照样印「✓ 全部 39 项断言通过」—— 少了的那一项**不会说话**。声明一个数,
#: 少一项就当场红。这是本项目记过的那类假绿("空转"的近亲: 不是没核, 是少核)。
EXPECTED_CHECKS = 40

FAILURES = []
CHECKS = [0]


def check(cond, label):
    CHECKS[0] += 1
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def meta_dir(out_root, task_id):
    """清单目录 `下载根/_meta/<任务ID>/`。

    刻意手写而不调用被测的 `core.layout.meta_dir`: 否则"清单落在哪儿"这条断言
    就成了同义反复(实现改错时两边一起错)。
    """
    return out_root / "_meta" / str(task_id)


def check_eq(got, want, label):
    """断言相等并在失败时打印两边取值。

    只打印标签的话, 失败信息对排错毫无帮助 —— 比如 manifest 里某个字段
    偶尔为空, 看不出是""还是 None 还是别的 URL。
    """
    CHECKS[0] += 1
    if got == want:
        print(f"  ok   {label}")
        return
    got_s = json.dumps(got, ensure_ascii=False) if isinstance(got, (dict, list)) else repr(got)
    want_s = json.dumps(want, ensure_ascii=False) if isinstance(want, (dict, list)) else repr(want)
    print(f"  FAIL {label}\n         期望: {want_s}\n         实际: {got_s}")
    FAILURES.append(f"{label} (期望 {want_s}, 实际 {got_s})")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        if name.endswith(".mp4"):
            body = MP4 + f"<!--{name}-->".encode()
            ctype = "video/mp4"
        else:
            body = JPEG + f"<!--{name}-->".encode()
            ctype = "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if "/slow/" in self.path:
            # 分块慢速吐出: 让"停止"有机会落在一次传输的中途, 而不是资源边界上
            for i in range(0, len(body), 32):
                self.wfile.write(body[i:i + 32])
                self.wfile.flush()
                time.sleep(0.15)
        else:
            self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main():
    # 每次从零开始: 连上一轮的下载产物一起清掉。
    # 只删 DB 是不够的 —— 残留的 downloads/1/gid01/0001.jpg 会让续传逻辑
    # 拿着"上一轮的文件"去跑这一轮, 断言看到的是混合状态, 偶发失败且无法复现。
    tmp = ROOT / "data" / "_verify_output"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    db_path = tmp / "verify.db"

    db.DB_PATH = db_path
    db._conn = None
    out_root = tmp / "downloads"
    out_root.mkdir(parents=True, exist_ok=True)

    srv, port = start_server()
    base = f"http://127.0.0.1:{port}"

    # 限速全部关掉: 这里是功能验证, 不是压测, 没必要真等 3~10 秒
    settings.domain_min_interval = 0
    settings.domain_max_interval = 0
    settings.download_dir = out_root
    settings.write_manifest = True

    urls = [f"{base}/photos/gid01/{i:05d}.jpg" for i in range(1, PAGES + 1)]

    @register("verify_static")
    class StaticSpider:
        def crawl(self, url, options=None, log=None):
            return [{"type": "image", "url": u, "mirrors": []} for u in urls]

    from core.task_manager import DOWNLOADS_DIR as _  # noqa: F401
    import core.task_manager as tm

    tm.DOWNLOADS_DIR = out_root

    print("[1] 命名模板: {album}/{seq4}.{ext}")
    task_id = db.create_task(
        base + "/album/gid01", "verify_static", None,
        {"name_template": "{album}/{seq4}.{ext}", "album": "gid01"},
    )
    mgr = tm.TaskManager(max_workers=1, download_workers=2)
    mgr.submit(task_id)

    deadline = time.time() + 60
    while time.time() < deadline:
        t = db.get_task(task_id)
        if t["status"] in ("success", "partial", "failed", "cancelled"):
            break
        time.sleep(0.2)
    task = db.get_task(task_id)
    check(task["status"] == "success", f"任务成功完成 (实际 {task['status']})")

    # 媒体落在 `下载根/相册名/`(视频平铺在下载根目录), 清单落在
    # `下载根/_meta/<任务ID>/` —— 规则见 core/layout.py
    out_dir = out_root / "gid01"
    expected = [out_dir / f"{i:04d}.jpg" for i in range(1, PAGES + 1)]
    check(all(p.is_file() for p in expected), "文件按 {album}/{seq4} 落在相册文件夹里")
    check(not (out_root / "00001.jpg").exists(), "下载根目录没有平铺的同名文件")

    print("[2] manifest.json 溯源字段")
    data = read_manifest(meta_dir(out_root, task_id))
    CHECKS[0] += 1
    if data is None:
        print("  FAIL manifest.json 存在")
        FAILURES.append("manifest exists")
    else:
        print("  ok   manifest.json 存在")
        check(not (out_dir / "manifest.json").exists(),
              "清单与媒体分开放(视频平铺时同一个 manifest.json 会互相覆盖)")
        items = data["resources"]
        first = items[0]
        check_eq(first["file"], "gid01/0001.jpg", "file 是相对路径")
        check(len(first["sha256"] or "") == 64, "sha256 已记录")
        expect_size = len(JPEG) + len("<!--00001.jpg-->")
        check_eq(first["size"], expect_size, "size 正确")
        check_eq(first["resolved_url"], urls[0], "resolved_url = 实际下载的 URL")
        check_eq(first["content_type"], "image/jpeg", "content_type 已回填")
        check_eq(data["counts"].get("done"), PAGES, "counts.done 正确")

    print("[3] 打包导出")
    rows = [r for r in db.get_resources(task_id) if r["status"] == "done"]
    items = [(r, Path(r["local_path"])) for r in rows]
    blob = b"".join(_zip_stream(out_root, items))
    zf = zipfile.ZipFile(io.BytesIO(blob))
    check(zf.testzip() is None, "zip 结构完整(可解压)")
    check(sorted(zf.namelist()) == sorted(["gid01/%04d.jpg" % i for i in range(1, PAGES + 1)]),
          "包内保留了目录结构")
    check(zf.read("gid01/0001.jpg") == JPEG + b"<!--00001.jpg-->", "内容与磁盘一致")

    print("[4] 增量续采: 同 URL 再跑一遍不再传输")
    task2 = db.create_task(
        base + "/album/gid01", "verify_static", None,
        {"incremental": True},
    )
    mgr.submit(task2)
    deadline = time.time() + 60
    while time.time() < deadline:
        t = db.get_task(task2)
        if t["status"] in ("success", "partial", "failed", "cancelled"):
            break
        time.sleep(0.2)
    check(db.get_task(task2)["status"] == "success", "第二轮任务成功")
    reuse = [r for r in db.get_resources(task2) if r["status"] == "done"]
    check(len(reuse) == PAGES, f"资源全部列出 ({len(reuse)})")
    check(db.count_downloaded(task2) == 0, "本次真正下载数为 0(全部复用历史成果)")
    check(all(r["local_path"] for r in reuse), "复用资源仍指向磁盘上的真实文件")

    print("[5] 订阅巡检的数据闭环")
    wid = db.create_watch(base + "/album/gid01", "verify_static", 60, {})
    due = [w["id"] for w in db.due_watches()]
    check(due == [wid], "新建的订阅源立即到期")
    got = mgr.run_watch(wid)
    check(bool(got), "run_watch 创建了任务")
    check(db.claim_watch(wid, 60) is False, "同一次到期的源不会被抢占两次")
    deadline = time.time() + 60
    while time.time() < deadline:
        t = db.get_task(got)
        if t["status"] in ("success", "partial", "failed", "cancelled"):
            break
        time.sleep(0.2)
    w = db.get_watch(wid)
    check(w["last_task_id"] == got, "巡检任务 ID 已回填")
    check(w["hits"] == 0, "全部是已下载过的, hits 记为 0")

    print("[6] 停止: 中断传输且不留半成品")
    slow_urls = [f"{base}/slow/gid02/{i:05d}.jpg" for i in range(1, 5)]

    @register("verify_slow")
    class SlowSpider:
        def crawl(self, url, options=None, log=None):
            return [{"type": "image", "url": u, "mirrors": []} for u in slow_urls]

    tid = db.create_task(
        base + "/album/gid02", "verify_slow", None,
        {"name_template": "{album}/{seq4}.{ext}", "album": "gid02"},
    )
    mgr2 = tm.TaskManager(max_workers=1, download_workers=1)
    mgr2.submit(tid)
    deadline = time.time() + 20
    while time.time() < deadline:
        if db.get_task(tid)["status"] == "downloading" and any(
            r["status"] == "downloading" for r in db.get_resources(tid)
        ):
            break
        time.sleep(0.05)
    mid_target = out_root / "gid02"
    check(any(r["status"] == "downloading" for r in db.get_resources(tid)),
          "取消前已有一个资源正在传输")

    t0 = time.time()
    ok, msg = mgr2.cancel(tid)
    check(ok, f"cancel 接受请求 ({msg})")
    deadline = time.time() + 30
    while time.time() < deadline:
        if db.get_task(tid)["status"] in (
            "cancelled", "success", "failed", "partial"
        ):
            break
        time.sleep(0.05)
    took = time.time() - t0
    check(db.get_task(tid)["status"] == "cancelled", "任务状态 -> cancelled")
    # 单个资源还能再吐 2 秒多, 真等它下完就得 2s+; 能在一个数据块内退出才叫中断
    check(took < 1.0, f"停止在 {took:.2f}s 内生效(未等剩余内容传完)")
    # status 是秒改的(为了让界面立刻有反馈), 但 worker 还要收拾现场:
    # 清半成品、写 manifest。要看 CI 结果得等 is_active 落下去。
    deadline = time.time() + 10
    while time.time() < deadline and mgr2.is_active(tid):
        time.sleep(0.05)
    check(not mgr2.is_active(tid), "worker 已收尾")
    rows = db.get_resources(tid)
    check(not any(r["status"] == "downloading" for r in rows),
          "没有资源卡在 downloading")
    check(any(r["status"] == "skipped" for r in rows), "中断的资源标记为 skipped")
    check(not any(r["status"] == "done" and r["url"].endswith("00004.jpg")
                  for r in rows), "队列尾部资源未被继续下载")
    check(not any(r["status"] == "downloading" for r in rows),
          "没有资源卡在 downloading")
    leftovers = sorted(p.name for p in mid_target.glob("*.jpg")) if mid_target.exists() else []
    check(not leftovers, f"半成品已清理 ({leftovers or '无残留'})")
    check(read_manifest(meta_dir(out_root, tid)) is not None,
          "取消后仍产出 manifest(告诉用户哪几个已下好)")
    mgr2.shutdown()

    print("[7] 视频平铺在下载根目录, 文件名取站点原名")
    video_url = f"{base}/videos/6aaa517d3f106.mp4"

    @register("verify_video")
    class VideoSpider:
        def crawl(self, url, options=None, log=None):
            return [{"type": "video", "url": video_url, "mirrors": [],
                     "filename": "6aaa517d3f106.mp4", "album": "某视频标题"}]

    vtid = db.create_task(base + "/video/gid03", "verify_video", None, {})
    mgr.submit(vtid)
    deadline = time.time() + 60
    while time.time() < deadline:
        if db.get_task(vtid)["status"] in ("success", "partial", "failed", "cancelled"):
            break
        time.sleep(0.2)
    check(db.get_task(vtid)["status"] == "success",
          f"视频任务成功完成 (实际 {db.get_task(vtid)['status']})")
    flat = out_root / "6aaa517d3f106.mp4"
    check(flat.is_file(), "视频直接落在下载根目录")
    check(not (out_root / "某视频标题").exists(), "视频没有建相册文件夹")
    vrow = db.get_resources(vtid)[0]
    check_eq(vrow["filename"], "6aaa517d3f106.mp4", "入库的相对路径就是平铺名")
    vdata = read_manifest(meta_dir(out_root, vtid))
    check(vdata is not None, "视频任务也有自己的清单")
    if vdata:
        check_eq(vdata["resources"][0]["file"], "6aaa517d3f106.mp4",
                 "manifest 的 file 相对**下载根**(不是清单所在的 _meta 目录)")

    mgr.shutdown()
    srv.shutdown()

    print()
    total = CHECKS[0]
    if FAILURES:
        print(f"✗ {len(FAILURES)}/{total} 项未通过:")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    if total != EXPECTED_CHECKS:
        # 比失败更坏的一种绿: 它一项没少地"通过"了, 只是**比声明的少核了几项**。
        print(f"✗ 只跑了 {total} 项断言, 声明的是 {EXPECTED_CHECKS} 项"
              f"(差 {total - EXPECTED_CHECKS:+d})。"
              f"\n   要么有判据被删了, 要么新加了一项 —— 两种都得同步 README 与"
              f"\n   `EXPECTED_CHECKS`(它是「40 项断言」那句话的唯一来源)。")
        return 1
    print(f"✓ 全部 {total} 项断言通过")
    print(f"  媒体目录: {out_root}")
    print(f"  清单目录: {meta_dir(out_root, task_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
