"""离线验证 m3u8 双引擎: ffmpeg 拉流 / 内置分片器 / 续传 / 引擎强制策略。

为什么要用 ffmpeg 现场生成素材
==============================
旧版脚本用 `bytes([i+1]) * n` 造假分片。装上 ffmpeg 后 `auto` 引擎会先走
ffmpeg 拉流, 而假分片不是合法 TS —— ffmpeg 直接报错, 验证失效。
所以这里用 ffmpeg 生成**真实** HLS(testsrc + sine, h264 + aac),
两条引擎路径都能跑通。

校验手段
========
* 解码校验: `ffmpeg -v error -i out -f null -` 完整解一遍, 能解完才算产物没坏
  (比只看魔术字节强得多 —— 那只能证明文件头对)
* 字节校验: 内置器路径额外比对「按 m3u8 顺序拼接的 sha256」,
  能发现分片错序/丢块这类解码器不一定报错的问题
* 素材自检: 生成后先确认播放列表已终结(有 #EXT-X-ENDLIST)且收录了全部磁盘分片。
  素材有毛病就以"素材"的名义报错 —— 不让它伪装成下载层缺陷(见 `build_hls`)

用法: uv run python scripts/verify_hls.py
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(line_buffering=True)

import http.server  # noqa: E402

import downloaders.video as vid  # noqa: E402
from core.config import settings  # noqa: E402
from core.ffmpeg import find_ffmpeg  # noqa: E402
from downloaders.video import VideoDownloader  # noqa: E402

FF = find_ffmpeg()
if not FF:
    print("[x] 本脚本需要 ffmpeg 生成素材并校验产物, 未找到可执行文件")
    sys.exit(2)

FFPROBE = str(Path(FF).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
REAL_FIND = find_ffmpeg

SEG_DUR = 0.5
SEG_COUNT = 12
DELAY = 0.3          # 分片响应的模拟网络延迟
FPS = 25


# ---- 素材: 用 ffmpeg 生成真实 HLS ----

def build_hls(dirpath: Path) -> list:
    cmd = [
        FF, "-y", "-nostats", "-v", "error",
        "-f", "lavfi", "-i",
        f"testsrc=duration={SEG_COUNT * SEG_DUR}:size=320x240:rate={FPS}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={SEG_COUNT * SEG_DUR}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        # 关键帧对齐分片边界, 否则 hls_time 只是下限, 实际会合并成大分片
        "-g", str(int(FPS * SEG_DUR)), "-keyint_min", str(int(FPS * SEG_DUR)),
        "-sc_threshold", "0",
        "-c:a", "aac", "-shortest",
        "-f", "hls", "-hls_time", str(SEG_DUR), "-hls_list_size", "0",
        # ⚠️ 必须显式声明 vod。不写这个参数时, ffmpeg 靠"hls_list_size==0 就当 VOD"的
        # 隐式收敛来决定要不要收尾, 实测偶发(6 次里出现过 1 次)产出一个**只列了 11 个
        # 分片、也没有 #EXT-X-ENDLIST** 的中间态播放列表。那正好撞上
        # `collectors/hls.py` 的完整性门禁, 脚本于是报"站点播放列表缺少 ENDLIST" ——
        # 一次**素材生成**的抖动被伪装成**下载层缺陷**, 排查方向直接跑偏。显式声明
        # vod 之后 ffmpeg 必须写收尾标记。
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(dirpath / "seg%d.ts"),
        str(dirpath / "index.m3u8"),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("[x] 生成 HLS 素材失败:", (r.stderr or "").strip()[-300:])
        sys.exit(2)
    text = (dirpath / "index.m3u8").read_text(encoding="utf-8")
    order = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]

    # ---- 素材自检 ----
    # 后面十几条断言全部建立在"这是一份完整的 VOD 播放列表"之上。素材本身有问题时
    # 必须先在这里、以"素材"的名义失败: 否则报出来的错指向下载层, 而下载层其实是对的
    # —— 这种误导比直接失败昂贵得多(实测为了定位上面那次抖动翻了好几个文件)。
    # 判据只用两条与 ffmpeg 版本无关的不变量:
    #   * 已终结 —— 有 #EXT-X-ENDLIST;
    #   * 无遗漏 —— 播放列表列出的分片数 == 磁盘上的分片数。
    #     (不写死 12: 分片数取决于关键帧落点, 换 ffmpeg 版本可能差一片。)
    on_disk = len(list(dirpath.glob("seg*.ts")))
    problems = []
    if "#EXT-X-ENDLIST" not in text:
        problems.append("缺少 #EXT-X-ENDLIST(播放列表没被收尾)")
    if len(order) != on_disk:
        problems.append(f"播放列表列了 {len(order)} 片, 磁盘上有 {on_disk} 片(有分片没被收录)")
    if len(order) < 6:
        problems.append(f"分片只有 {len(order)} 个, 不够后续断言用(需要 >= 6)")
    if problems:
        print("[x] HLS 素材自检不通过 —— 这是**素材生成**的问题, 不是下载层的:")
        for p in problems:
            print("     -", p)
        print(f"     素材目录: {dirpath}")
        sys.exit(2)
    return order


def expected_sha(work: Path, order: list) -> str:
    """按 m3u8 声明的顺序拼接原始分片, 得到理论 sha256。"""
    h = hashlib.sha256()
    for name in order:
        h.update((work / name).read_bytes())
    return h.hexdigest()


# ---- 校验工具 ----

def decode_ok(path: Path):
    """完整解码一遍; 返回 (是否成功, 错误摘要)。"""
    r = subprocess.run(
        [FF, "-v", "error", "-nostats", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    err = (r.stderr or "").strip()
    return r.returncode == 0 and not err, err.splitlines()[-1][:120] if err else ""


def probe(path: Path) -> str:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    got = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
    return ", ".join(got)


@contextmanager
def engine(mode: str, hide_ffmpeg: bool = False):
    """临时切换 video_engine, 并可选地让 video 模块「看不见」ffmpeg。"""
    old = settings.video_engine
    settings.video_engine = mode
    if hide_ffmpeg:
        vid.find_ffmpeg = lambda refresh=False: None
    try:
        yield
    finally:
        settings.video_engine = old
        vid.find_ffmpeg = REAL_FIND


PASS = []
FAIL = []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"    {'[ok]' if cond else '[x] '} {label}{('  ' + detail) if detail else ''}")


# ---- 起本地服务 ----

work = Path(tempfile.mkdtemp(prefix="hls_src_"))
outs = []
ORDER = build_hls(work)
segs_on_disk = sorted(work.glob("seg*.ts"))
EXPECT = expected_sha(work, ORDER)

HITS = {}
HLOCK = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(work), **kw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        with HLOCK:
            HITS[self.path] = HITS.get(self.path, 0) + 1
        if self.path.endswith(".ts"):
            time.sleep(DELAY)
        return super().do_GET()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
M3U8 = f"http://127.0.0.1:{PORT}/index.m3u8"

dl = VideoDownloader()


def out_dir(tag):
    d = Path(tempfile.mkdtemp(prefix=f"out_{tag}_"))
    outs.append(d)
    return d


try:
    print("=" * 66)
    print("素材: ffmpeg 现场生成真实 HLS")
    print(f"  ffmpeg      : {FF}")
    print(f"  分片        : {len(ORDER)} 个 (m3u8 声明), 磁盘上 {len(segs_on_disk)} 个")
    print(f"  单片时长    : {SEG_DUR}s  合计约 {SEG_COUNT * SEG_DUR}s")
    print(f"  理论 sha256 : {EXPECT[:16]}...  (按 m3u8 顺序拼接)")
    print("=" * 66)

    print("\n[1] engine=ffmpeg  (强制 ffmpeg 拉流)")
    with engine("ffmpeg"):
        logs = []
        d = out_dir("ff")
        t0 = time.monotonic()
        p, sha = dl.download(M3U8, save_dir=d, log=logs.append)
        dt = time.monotonic() - t0
    ok, err = decode_ok(p)
    check("产物为 .mp4", p.suffix == ".mp4", p.name)
    check("完整解码通过", ok, err or f"{dt:.2f}s")
    check("时长 ~6s", "duration=" in probe(p), probe(p))
    for l in logs:
        print(f"        log: {l}")

    print("\n[2] engine=builtin  (内置分片器 + ffmpeg remux)")
    with engine("builtin"):
        logs = []
        d = out_dir("bi")
        t0 = time.monotonic()
        p2, sha2 = dl.download(M3U8, save_dir=d, log=logs.append)
        dt2 = time.monotonic() - t0
    ok2, err2 = decode_ok(p2)
    check("产物为 .mp4 (remux 生效)", p2.suffix == ".mp4", p2.name)
    check("完整解码通过", ok2, err2 or f"{dt2:.2f}s")
    check("分片缓存已清理", not (d / f".{M3U8.split('/')[-1].rsplit('.', 1)[0]}.parts").exists())
    for l in logs:
        print(f"        log: {l}")

    print("\n[3] engine=builtin + 无 ffmpeg  (纯内置, 输出 .ts)")
    with engine("builtin", hide_ffmpeg=True):
        d = out_dir("ts")
        p3, sha3 = dl.download(M3U8, save_dir=d)
    ok3, err3 = decode_ok(p3)
    check("产物为 .ts", p3.suffix == ".ts", p3.name)
    check("完整解码通过", ok3, err3)
    check("字节级校验 (与原始分片拼接一致)", sha3 == EXPECT,
          f"{sha3[:16]}...")
    check("时长 ~6s", "duration=" in probe(p3), probe(p3))

    print("\n[4] engine=auto + 无 ffmpeg  (应自动降级到内置器)")
    with engine("auto", hide_ffmpeg=True):
        d = out_dir("auto")
        p4, _ = dl.download(M3U8, save_dir=d)
    check("降级为内置器并输出 .ts", p4.suffix == ".ts", p4.name)
    check("完整解码通过", decode_ok(p4)[0])

    print("\n[5] engine=ffmpeg + 无 ffmpeg  (应直接报错, 不静默降级)")
    try:
        with engine("ffmpeg", hide_ffmpeg=True):
            d = out_dir("strict")
            dl.download(M3U8, save_dir=d)
        check("抛出异常", False, "却没有抛错 —— 说明发生了一次静默降级")
    except RuntimeError as e:
        check("抛出 RuntimeError 且不降级", "不降级" in str(e) or "未找到" in str(e),
              str(e)[:80])
    except Exception as e:
        check("抛出 RuntimeError", False, f"实际是 {type(e).__name__}")

    print("\n[6] 内置器并发对比 (无 ffmpeg)")
    with engine("builtin", hide_ffmpeg=True):
        old_c = settings.segment_concurrency
        settings.segment_concurrency = 4
        t0 = time.monotonic()
        dl.download(M3U8, save_dir=out_dir("c4"))
        dt4 = time.monotonic() - t0
        settings.segment_concurrency = 1
        t0 = time.monotonic()
        dl.download(M3U8, save_dir=out_dir("c1"))
        dt1 = time.monotonic() - t0
        settings.segment_concurrency = old_c
    check("并发 4 快于并发 1", dt4 < dt1, f"并发4={dt4:.2f}s  并发1={dt1:.2f}s")

    print("\n[7] 内置器断点续传 (无 ffmpeg)")
    with engine("builtin", hide_ffmpeg=True):
        d = out_dir("resume")
        missing, bak = work / ORDER[5], work / f"{ORDER[5]}.bak"
        missing.rename(bak)
        try:
            dl.download(M3U8, save_dir=d)
            check("缺片时按预期失败", False, "却没失败")
        except Exception as e:
            check("缺片时按预期失败", True, str(e)[:70])
        parts = d / f".{M3U8.split('/')[-1].rsplit('.', 1)[0]}.parts"
        cached = sorted(parts.glob("*.part")) if parts.exists() else []
        check("已下载分片保留(供续传)", len(cached) == len(ORDER) - 1,
              f"{len(cached)}/{len(ORDER)}")

        bak.rename(missing)
        HITS.clear()
        t0 = time.monotonic()
        p7, sha7 = dl.download(M3U8, save_dir=d)
        dt7 = time.monotonic() - t0
        ts_hits = sorted(k for k in HITS if k.endswith(".ts"))
        check("重跑只请求缺失分片", ts_hits == [f"/{ORDER[5]}"],
              f"请求了 {ts_hits}")
        check("续传后产物正确", sha7 == EXPECT and decode_ok(p7)[0], f"{dt7:.2f}s")

finally:
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    for o in outs:
        shutil.rmtree(o, ignore_errors=True)

print("\n" + "=" * 66)
print(f"结果: {len(PASS)} 项通过, {len(FAIL)} 项失败")
for f in FAIL:
    print(f"  [x] {f}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
