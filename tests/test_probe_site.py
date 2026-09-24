"""`scripts/probe_site.py` 的自检: 用一个**本地假站点**复现已知结论。

为什么靶子不是真实站点
======================
`collectors/xchina/gallery.py` 的模块文档把五项探测结论写死了(越界返回
`200 text/html` / `Accept` 必须显式带 `image/*` / HEAD 可靠 / 资源按相册分到
`photos`、`photos2` / 7 种 URL 形态), 那本来是验证探针的最好靶子 —— **但它过期了**:

    2026-09-23 本机复测: img.xchina.io 对**任何**请求都返回 403, 且是
    `text/plain`(不是 CF 挑战页), 裸 curl 带浏览器 UA + `image/*` 也一样。
    也就是说这台机器/这条网络上整段不可达, 与 Accept、指纹、HEAD 都无关。

拿它当靶子只会得到"探针坏了"的**假警报** —— 而这恰好是本项目最爱犯的错:
把环境问题当成回归。所以把那五条行为做进一个本地 HTTP 服务, 结论就与网络无关了:
探针跑不出它们, 就真的是探针的 bug。

钉住的四件事
============
1. **输出口径**: ①越界 200+text/html 必须判成"只能用 Content-Type 判定";
   ②Accept 被校验必须报出来; ④线路条数; ⑤自检合计与页码识别。
2. **不要编结论**: 一个存在的序号都没探到时, 必须明说"判定规则还没验出来",
   而不是把 403 当成"状态码可用"照抄一个结论下来(这是最容易误导人的一种输出)。
3. **草稿要能用**: 至少是合法 Python; 而"正则按路径段对齐"这类细节由针对性用例守。
4. **看着能用 ≠ 能用**(2026-09-24 拿真实站点实测补的一节): MangaDex 与
   Lorem Picsum 上暴露的四处缺陷都不是崩溃, 而是**生成一份必然 0 资源的声明**——
   不透明后缀被当成画质后缀 / `id_samples` 里编期望值 / 纯数字路径段被当成桶号 /
   多段 base_path 切出 `//`。所以凡是"输出"的地方, 判据都要问一句
   **"这条期望值/结论, 是从哪一条实测里抽出来的?"**
"""

import ast
import http.server
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import probe_site  # noqa: E402

GID = "69ad45698f836"
SCRIPT = ROOT / "scripts" / "probe_site.py"


class _FakeSite(http.server.BaseHTTPRequestHandler):
    """复现 xchina 的五条实测行为(见 `xchina/gallery.py` 模块文档)。

    刻意**不返回 404**: 越界序号与未登记的档位一律 `200 + text/html`。这正是
    "按状态码判定会让枚举永不停止"的来源, 也是本脚本①要验的那条。
    """

    total = 5                      # 存在的序号: 1..5
    hole = 0                       # 让某个序号"缺一张"(0 = 不制造空洞)
    always_403 = False             # 整站不可达(用来验"不要编结论"那条守卫)
    #: 拦截**形态** —— 决定"被挡在门外"时正文长什么样, 而正文决定探针给出什么建议:
    #:   ""      不拦
    #:   "plain" 403 + `text/plain`  -> IP 段/地域封禁 -> 换指纹**没用**, 要代理
    #:   "cf"    403 + CF 挑战页正文 -> TLS 指纹被拦     -> 换指纹**有用**, 该自动重试
    #:   "rate"  429 + `text/plain`    -> 被限速          -> 换指纹**没用**(等一会儿/换出口)
    #: 三种都会让第 0 段不通, 但只有 "cf" 该触发自动换指纹。所以必须逐个测 ——
    #: 只测一种会漏掉另一半, 而"漏掉另一半"正是本项目记录过的老坑。
    block_mode = ""
    #: 非空则 HEAD 说"存在"、GET 直接 500 —— 用来验"HEAD 通 != 能落地"这一条。
    #: 它不是编出来的场景: 有的 CDN 只对 HEAD 放行, 真正取正文时才挡。
    get_fails = False
    #: 非空则"只有这几个序号存在" —— 用来造"直链通、但 --scan 范围内 0 命中"的场景。
    #: 档 A 门禁要拦的正是它: 直链有效, 但"序号枚举型"这个前提一个样本都没验出来。
    only_seqs = ()
    #: 非空则启用车名形态 `/hashes/<gid>/<seq>-<内容哈希>.png`(MangaDex 那种)——
    #: 只有 `1-<该哈希>` 存在。用来验"不透明后缀 -> 拒绝出草稿"。
    opaque_hash = ""
    #: 真实存在的档位 —— 只有这三个算"这张图存在"
    suffix_ext = {
        ".jpg": "image/jpeg",
        "_1200x0.webp": "image/webp",
        "_800x0.webp": "image/webp",
    }

    def log_message(self, *a):     # 静音
        pass

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def _send(self, status, ctype, payload, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def _serve(self, body):
        if body and self.get_fails:
            self._send(500, "text/plain; charset=UTF-8", b"boom", body)
            return
        mode = self.block_mode or ("plain" if self.always_403 else "")
        if mode == "plain":
            # 403 且正文是 text/plain —— 2026-09-23 复测 img.xchina.io 就是这个样子
            self._send(403, "text/plain; charset=UTF-8", b"blocked", body)
            return
        if mode == "cf":
            self._send(403, "text/html; charset=UTF-8",
                       b"<html><head><title>Just a moment...</title></head>"
                       b"<body><div id=\"challenge-platform\"></div></body></html>",
                       body)
            return
        if mode == "rate":
            self._send(429, "text/plain; charset=UTF-8", b"too many requests", body)
            return
        path = self.path.split("?")[0]
        if self.opaque_hash and re.match(r"^/hashes/[0-9a-f]+/(.+)\.png$", path):
            stem = re.sub(r"\.png$", "", path.rsplit("/", 1)[1])
            if stem == "1-" + self.opaque_hash:
                self._send(200, "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, body)
            else:
                # 刻意仍返回 200(与 xchina 同样的"不返 404"行为), 类型却不是 image/*
                self._send(200, "text/html; charset=utf-8", b"<html>x</html>", body)
            return
        if path.endswith(".html"):
            port = self.server.server_address[1]
            # 一条绝对 URL(资源在别的 host 的形态) + 一条根相对路径
            html = (
                "<html><body>"
                '<img src="http://127.0.0.1:%d/photos/%s/00001.jpg">'
                '<img src="/photos/%s/00001_1200x0.webp">'
                "</body></html>" % (port, GID, GID)
            ).encode()
            self._send(200, "text/html; charset=utf-8", html, body)
            return
        accept = self.headers.get("Accept") or ""
        # 实测行为: 不给 Accept 或给 `*/*` -> 403; 含 image/* -> 放行。
        # 注意 `*/*` **不**含 "image/" —— 按"含通配即放行"写会把这条行为抹掉。
        if "image/" not in accept and "video/" not in accept:
            self._send(403, "text/plain; charset=UTF-8", b"blocked", body)
            return
        m = re.match(r"^/photos\d*/([0-9A-Za-z_-]+)/(.+)$", path)
        if m:
            stem = m.group(2)
            mm = re.match(r"^(\d+)", stem)
            if mm:
                n = int(mm.group(1))
                ctype = self.suffix_ext.get(stem[len(mm.group(1)):])
                listed = not self.only_seqs or n in self.only_seqs
                if ctype and n <= self.total and n != self.hole and listed:
                    self._send(200, ctype, b"\xff\xd8\xff" + b"0" * 64, body)
                    return
        self._send(200, "text/html; charset=utf-8", b"<html>x</html>", body)


@pytest.fixture
def fake_site():
    """起一个本地假站点; 用完关掉。端口由系统分配, 不占固定端口。"""
    servers = []

    def start(always_403=False, hole=0, total=None, opaque_hash="", only_seqs=(),
              block_mode="", get_fails=False):
        over = {}
        if always_403:
            over["always_403"] = True
        if block_mode:
            over["block_mode"] = block_mode
        if get_fails:
            over["get_fails"] = True
        if hole:
            over["hole"] = hole
        if total is not None:
            over["total"] = total
        if opaque_hash:
            over["opaque_hash"] = opaque_hash
        if only_seqs:
            over["only_seqs"] = tuple(only_seqs)
        cls = type("_S", (_FakeSite,), over)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _run(port, page_paths, extra=(), dlink=None):
    """跑一遍探针脚本; 默认直链用 `/photos2/...`(顺带验基址分桶的拆分)。"""
    base = "http://127.0.0.1:%d" % port
    urls = [dlink or ("%s/photos2/%s/0001.jpg" % (base, GID))]
    urls += [base + p for p in page_paths]
    cmd = [sys.executable, "-u", str(SCRIPT)] + urls + [
        "--scan", "6", "--timeout", "10"] + list(extra)
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       timeout=180)
    return p.stdout + p.stderr, p.returncode


# ---------------------------------------------------------------- 单元


def test_infer_from_link_reads_base_gid_and_width():
    info = probe_site.infer_from_link("https://img.x/photos2/69ad45698f836/0001.jpg")
    assert info["base"] == "https://img.x/photos2"
    assert info["base_path"] == "photos2"
    assert info["gid"] == "69ad45698f836"
    assert info["seq_format"] == "{seq:04d}"      # 从 `0001` 读出来的宽度
    assert info["suffix"] == ".jpg"


def test_infer_from_link_refuses_non_sequence_naming():
    """文件名不以数字序号开头 = 这个站不是序号枚举型, 必须返回 None 而不是硬套模板。

    pexels 就是这种(`pexels-photo-1234567.jpeg`, 一个 ID 一张图), 硬套会得到一个
    `seq_format` 看上去正常、实际永远采不到的声明。
    """
    info = probe_site.infer_from_link(
        "https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg")
    assert info["seq_format"] is None


def test_split_digit_base_separates_the_bucket_number():
    """`.../photos2` -> (`.../photos`, 2)。写成 base=photos2 会让别的相册整批判空。"""
    assert probe_site.split_digit_base("https://img.x/photos2") == ("https://img.x/photos", 2)
    assert probe_site.split_digit_base("https://img.x/photos") == ("https://img.x/photos", 0)


def test_link_pattern_matches_every_digit_bucket():
    """直链正则必须**放行数字分桶**: 只认默认桶会让 photos2 上的直链落给通用采集器。

    这类"一半好一半坏"最难查 —— 采集本身好使, 只有预览/自动识别报错。
    """
    pat = probe_site.link_pattern("photos", r"[0-9a-f]{8,}")
    assert pat.startswith(r"/photos\d*/")
    assert re.search(pat, "https://img.x/photos/%s/0001.jpg" % GID)
    assert re.search(pat, "https://img.x/photos2/%s/0001.jpg" % GID)
    # 但仍要锚定"gid 后面跟着带媒体扩展名的文件名", 否则路径词会被当成 gid
    assert re.search(pat, "https://img.x/photos/featured/0001.jpg") is None


def test_page_pattern_aligns_to_a_path_segment():
    """相册页正则必须**按路径段对齐**, 不能从词中间切进去。

    踩过: 生成时取"gid 前 8 个字符", 而 `/photo/id-` 是 10 个字符, 于是前缀被截成
    `hoto/id-` —— 它照样能匹配, 但换一个路径深度或遇到 `notphoto` 这种词就静默
    失配/误判。所以这里直接断言它长什么样、并且**不**匹配 `notphoto/id-`。
    """
    pat = probe_site.page_pattern("https://x.site/photo/id-%s.html" % GID,
                                 GID, r"[0-9a-f]{8,}")
    assert pat == r"/photo/id\-([0-9a-f]{8,})"
    assert re.search(pat, "https://x.site/notphoto/id-%s.html" % GID) is None


def test_page_pattern_handles_path_segment_and_query_forms():
    """gid 落在**段内**(`id-<gid>`)还是**整段**(`/<gid>/`)要分别处理。

    段内形态保留上一段 + 段内前缀, 得到 `/photo/id-(...)` —— 这一条同时覆盖
    `/photo/id-X.html` 与 `/photo/id-X/10.html` 两种 URL, 比只写 `/photo/(...)` 更准。
    """
    p1 = probe_site.page_pattern("https://x.site/photo/id-%s/10.html" % GID,
                                 GID, r"[0-9a-f]{8,}")
    assert p1 == r"/photo/id\-([0-9a-f]{8,})"
    # gid 是**整段**时, 段内前缀为空 -> 只保留上一段
    p2 = probe_site.page_pattern("https://x.site/gallery/%s/10.html" % GID,
                                 GID, r"[0-9a-f]{8,}")
    assert p2 == r"/gallery/([0-9a-f]{8,})"
    # 老式 query 形态
    p3 = probe_site.page_pattern("https://x.site/photoShow.html?id=%s" % GID,
                                 GID, r"[0-9a-f]{8,}")
    assert p3 == r"[?&]id=([0-9a-f]{8,})"


def test_page_tail_evidence_flags_a_page_number():
    """`/photo/id-X/10.html` 的末段是**页码**。认成 ID 就会去枚举不存在的图集。"""
    bad = probe_site.page_tail_evidence(
        ["https://x.site/photo/id-%s/10.html" % GID,
         "https://x.site/photo/id-%s.html" % GID], GID)
    assert bad == ["https://x.site/photo/id-%s/10.html" % GID]


def test_draft_separates_blocking_gaps_from_advisory_ones():
    """门禁只认档 A —— 而"哪些字段属于档 A"必须是代码里的一条事实, 不是作者的记忆。

    在这之前, "哪个字段猜错了会致命"只存在于人脑, 于是每遇到一个新形态就得手写一个
    `if` 去堵(2026-09-24 一次性堵了五个, 而它们只是同一类错的五个化身)。
    """
    g_a = probe_site.Gap("base", probe_site.TIER_A, "没有实测", "先让它通")
    g_b = probe_site.Gap("variants", probe_site.TIER_B, "少个档位")
    d = probe_site.Draft("body", [g_a, g_b])
    assert d == "body"                                # str 子类: 正文照旧能直接用
    assert d.blocking == [g_a]
    assert d.advisory == [g_b]
    assert probe_site.Draft("x").blocking == []        # 没缺口 -> 不拦


def test_validate_draft_runs_the_real_check_site():
    """草稿要**跑起来**过一遍 `check_site()` —— 那是这份声明唯一的验收标准。

    以前这步靠人肉(存盘 -> import -> 跑 selfcheck.py)。前移到探针里之后, "草稿自己的
    `id_samples` 过不了自己的 `id_patterns`"当场就能看见, 而不是等提交后被 CI 拦下。

    这里刻意用 `gid_shape` 与样本不符来造问题: 它是 `check_site()` 里一条独立判据,
    与"正则能不能匹配"无关, 所以断言不会因为正则细节漂移而失效。
    """
    def decl(shape, sample):
        return (
            "from .. import register\n"
            "from ..gallery_base import GallerySite, SequenceGallerySpider\n"
            "T = GallerySite(\n"
            '    name="t",\n'
            '    base="https://x/photos",\n'
            '    seq_format="{seq:04d}",\n'
            '    variants=[".jpg"],\n'
            '    gid_shape=r"%s",\n'
            '    id_patterns=[r"/photos/([0-9A-Za-z]+)/"],\n'
            '    id_samples=[("https://x/photos/%s/0001.jpg", "%s")],\n'
            ")\n" % (shape, sample, sample))

    problems, note = probe_site.validate_draft(decl(r"[0-9a-f]{8,}", "ABCDEF123456"))
    assert note == ""                                  # 草稿本身是能跑起来的
    assert any("gid_shape" in p for p in problems), problems

    problems, note = probe_site.validate_draft(decl(r"[0-9a-f]{8,}", "abcdef123456"))
    assert (problems, note) == ([], "")


def test_validate_draft_reports_a_draft_that_cannot_even_run():
    """草稿连跑都跑不起来时要**说出来**, 而不是静默当成"通过"。"""
    problems, note = probe_site.validate_draft("this is not python(")
    assert problems == []
    assert note.startswith("SyntaxError")


def test_short_tail_keeps_the_part_that_carries_the_evidence():
    """URL 截断要**保尾部** —— 末段才是要人判断的那一段。

    踩过(目视输出时发现的): 提示"末段是页码"时用的是从头截断的 `short()`, 于是
    `/photo/id-X.html` 与 `/photo/id-X/10.html` 显示出来长得一样, 人根本分不清
    探针在说哪一条 —— 而这两个 URL 的处理规则**完全相反**。
    """
    a = "https://x.site/photo/id-%s.html" % GID
    b = "https://x.site/photo/id-%s/10.html" % GID
    assert probe_site.short_tail(a, 24).startswith("…")
    assert probe_site.short_tail(a, 24).endswith(".html")
    assert probe_site.short_tail(a, 24) != probe_site.short_tail(b, 24)
    assert probe_site.short_tail(a, 200) == a           # 够短就别动它


# ---------------------------------------------------------------- 端到端


def test_probe_reports_the_five_known_conclusions(fake_site, tmp_path):
    """五项结论都要从假站点上跑出来, 且草稿是合法 Python。"""
    srv = fake_site()
    port = srv.server_address[1]
    out_path = tmp_path / "draft.py"
    out, rc = _run(port,
                   ["/photo/id-%s.html" % GID,
                    "/photo/id-%s/10.html" % GID,
                    "/photoShow.html?id=%s" % GID],
                   extra=["--out", str(out_path)])
    assert rc == 0, out

    # ① 越界仍是 200, 只是类型变了 -> 结论只能是"按 Content-Type 判定"
    assert "只能用 Content-Type 判定存在性" in out
    assert "本次命中 5/6" in out                      # seq 1..5 存在
    # 末尾那次 miss 只是"相册到这儿就完了", 不该被报成空洞(否则这行永远在闪, 没人看)
    assert "中间有空洞" not in out
    # ② Accept 被校验(不给 / 给 `*/*` 都被拦)
    assert "Accept 被校验" in out
    # ③ 页面里的资源线索(绝对 + 根相对两种写法都要收到)
    assert "http://127.0.0.1:%d/photos" % port in out
    # ④ 三条线路: 原图 + 两个尺寸档
    assert "3 条线路" in out
    # ⑤ 页码被识别; 四种形态都解析出一致的 gid
    assert "page_tail" in out
    assert "自检: 4/4 条输入解析出一致的 gid" in out

    # 草稿: 基址分桶被拆出来(photos2 -> photos + 候选位数), 页码声明在
    assert 'base="http://127.0.0.1:%d/photos"' % port in out
    assert "base_candidate_digits=2" in out
    assert 'page_tail=r"^\\d+$"' in out

    draft = out_path.read_text(encoding="utf-8")
    ast.parse(draft)                                  # 草稿至少是合法 Python
    # 从 IP 取不出有意义的站点名 -> 退回 site_gallery, 而不是 `0_gallery`
    assert 'name="site_gallery"' in draft
    # 草稿自检: `check_site()` 是这份声明**唯一**的验收标准, 探针自己先跑一遍
    # (以前这步靠人肉: 存盘 -> import -> 跑 selfcheck.py)
    assert "草稿自检 (check_site)" in out
    assert "=> 通过: 每条 `id_samples` 都被解析出期望的 gid" in out
    # 相册页与它的分页会推出**同一条**正则 —— 不许写两遍(写两遍会让人以为漏了哪条,
    # 也让 check_site 白跑一遍)。目视输出时发现的。
    assert draft.count(r"/photo/id\-([0-9a-f]{8,})") == 1


def test_probe_flags_a_hole_in_the_sequence(fake_site):
    """序号中间缺一张 = **空洞**。它比"不连续"更具体, 且后果很重。

    空洞会让"指数探上界 + 二分"把上界定在缺口之前 —— 那是"300 张只采到 4 张"的
    静默截断, 比慢得多更糟。所以探针必须把它和"末尾缺失"区分开报出来。
    """
    srv = fake_site(hole=3)
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID])
    assert rc == 0, out
    assert "中间有空洞" in out
    assert "seq [3]" in out
    assert "用线性扫描" in out


def test_probe_gives_up_early_when_the_link_is_unreachable(fake_site):
    """直链不通 -> **第 0 段就早退**, 而不是跑完四项再给一屏没意义的数字。

    真实教训(2026-09-23): xchina 的媒体主机从这台机器整段 403, 而探针第一版会把
    "越界 403" 判成"状态码可用", 那会把一个**完全不可达**的站点写成"判定规则已确认"。

    这里用的是 `text/plain` 的 403 —— 即 **IP 段/地域封禁**。这一种**不该**自动换指纹:
    2026-09-23 在 xchina 上实测过, 换指纹与浏览器 UA 全是同一个 `text/plain` 403。
    自动重试的前提是"这条路有收益", 对 IP 封禁不成立 —— 多一次请求换回同一个结果,
    还额外给了用户"指纹这条路已经试过了"的错觉。
    """
    srv = fake_site(block_mode="plain")
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID])
    assert rc == 2, out
    assert "你给的那条直链**本身没探通**" in out
    assert "都没跑" in out                                # 后面四项省掉了
    assert "状态码可用" not in out                        # 绝不编结论
    # 分型 + 该给的那条建议
    assert "[ip_block]" in out
    assert "下一步 [proxy]" in out                        # 代号: 该做什么
    assert "换指纹对它**没用**" in out
    assert "--proxy" in out                               # 还得给可行动的下一步
    assert "自动改用 chrome TLS 指纹重试一次" not in out   # 对 IP 封禁**不**重试


def test_probe_retries_with_a_tls_fingerprint_on_a_cf_challenge(fake_site):
    """另一种 403: **Cloudflare 挑战页** —— 这一个才该自动换指纹。

    与上一个用例成对: 两者都返回 403, 正文决定给**相反**的建议。只测一种会漏掉一半 ——
    而"只测一半"正是本项目记录过的老坑(同族 F: 通过但理由已经不对)。
    """
    srv = fake_site(block_mode="cf")
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID])
    assert rc == 2, out
    assert "[cf_challenge]" in out
    assert "自动改用 chrome TLS 指纹重试一次" in out        # 自己试, 别让人手动重跑
    assert "[cf_challenge]" in out
    assert "下一步 [impersonate]" in out
    assert "换指纹对它**没用**" not in out
    assert "probe.py" in out                              # 需要真浏览器的那条路也说了


def test_probe_does_not_auto_retry_the_fingerprint_on_a_rate_limit(fake_site):
    """第三种成因: **429 限速** —— 换指纹同样没用, 所以也不许自动重试。

    这条补的是黑名单的一个洞: 以前的条件是 `kind != "ip_block"`, 于是 429 与 5xx
    都会白跑一次换指纹。它不会报错、也不会给出错建议, 只是**多一次注定失败的请求**,
    同时给用户"指纹这条路已经试过了"的错觉 —— 静默的浪费最难被发现, 所以要钉住。
    """
    srv = fake_site(block_mode="rate")
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID])
    assert rc == 2, out
    assert "[rate]" in out
    assert "下一步 [wait]" in out
    assert "自动改用 chrome TLS 指纹重试一次" not in out
    assert "不自动重试" in out


def test_probe_requires_a_direct_link(fake_site):
    """只给页面 URL 时必须明确拒绝, 而不是拿页面去猜基址与序号宽度。"""
    srv = fake_site()
    p = subprocess.run(
        [sys.executable, "-u", str(SCRIPT),
         "http://127.0.0.1:%d/photo/id-%s.html" % (srv.server_address[1], GID)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert p.returncode == 1
    assert "至少需要一条**资源直链**" in p.stdout


def test_probe_rejects_a_site_that_is_not_sequence_based():
    """非序号枚举型站点要**当场退出**, 不能生成一份永远采不到的声明。"""
    p = subprocess.run(
        [sys.executable, "-u", str(SCRIPT),
         "https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert p.returncode == 2
    assert "不是「序号枚举型」" in p.stdout


# ------------------------------------------------- 真实站点暴露出来的四条
#
# 2026-09-24 拿真实站点跑探针(用户要求"自行找站点测"), 探针在 MangaDex 与
# Lorem Picsum 上暴露了四处真缺陷。它们的共同点: **都不是崩溃**, 而是生成一份
# "看着能用、其实 0 资源"的声明 —— 按本项目的经验, 这种错会在生产里活很久。


def test_infer_from_link_flags_an_opaque_suffix():
    """文件名以数字开头 + 序号之后是**内容哈希** -> 同样不是序号枚举型。

    MangaDex 的直链是 `/data/<图集hash32>/1-<该页内容的sha256>.png`: 看着像
    序号枚举(以 `1` 开头), 但改序号拼出来的 URL 必然 404 —— 每一页的哈希都不同。
    光靠"以数字开头"判不出来, 所以要多这一条判据。
    """
    h = "a" * 64
    info = probe_site.infer_from_link(
        "https://cdn.x/data/7c07a7fecb2fe3868aa22aae2edf0e5a/1-%s.png" % h)
    assert info["seq_format"] == "{seq:01d}"       # 确实"看着像"序号枚举
    assert info["opaque_suffix"] is True           # 但后缀是内容哈希

    # 反向: 正常的画质后缀不能被误判(误判会把好站点挡在门外)
    ok = probe_site.infer_from_link(
        "https://img.x/photos/%s/0001_1200x0.webp" % GID)
    assert ok["suffix"] == "_1200x0.webp"
    assert ok["opaque_suffix"] is False
    assert probe_site.infer_from_link(
        "https://img.x/photos/%s/0001.jpg" % GID)["opaque_suffix"] is False


def test_split_digit_base_ignores_a_numeric_path_segment():
    """基址以数字结尾 **不等于** 那个数字是桶号。

    picsum 的直链 `https://fastly.picsum.photos/id/1040/200/300.jpg` 会把 base 推到
    `/id/1040`, 那里的 `1040` 是**图集 ID**。按桶拆会写出
    `base_candidate_digits=1040`, 让基类去展开一千多个候选 —— 而且是静默的。
    """
    assert probe_site.split_digit_base("https://p.x/id/1040") == ("https://p.x/id/1040", 0)
    assert probe_site.split_digit_base("https://img.x/photos2024") == (
        "https://img.x/photos2024", 0)             # 数字太大, 不像桶号
    assert probe_site.split_digit_base("https://img.x/photos2") == (
        "https://img.x/photos", 2)                 # 这条才是真桶号


def test_link_pattern_never_emits_a_double_slash():
    """base_path 是多段(`id/1040`)时, 去掉末尾数字会留下尾随斜杠 -> `/id//(...)`。

    那条正则几乎匹配不上任何真实 URL, 而且看着"像是写好了"。
    """
    pat = probe_site.link_pattern("id/1040", r"\d{4,}")
    assert "//" not in pat
    # 契约没变: gid 后面**紧跟**带媒体扩展名的文件名才算命中
    assert re.search(pat, "https://p.x/id/1040/0001.jpg")
    # gid 后面还夹着一层(picsum 的 `/id/1040/200/300.jpg`)**不该**匹配 ——
    # 探针在那种形态上会报「命中 0/N」并把 gid 推断错误暴露出来, 而不是硬凑一条正则
    assert re.search(pat, "https://p.x/id/1040/200/300.jpg") is None


def test_draft_omits_samples_that_failed_selfcheck():
    """`id_samples` 的期望值必须是正则**真的抽出来的**那个, 抽不出来就别写。

    踩过(MangaDex): ⑤ 明确报告页面 URL `-> None`, 草稿却照样给它配了采信直链的
    gid —— 那是**编证据**, `check_site()` 会拿着它空转, 而人看不出哪里不对。
    """
    media = ["https://img.x/photos/%s/0001.jpg" % GID]
    pages = ["https://x.site/chapter/0aaf8b27-0013-4ae0-8935-91a089466874"]
    info = probe_site.infer_from_link(media[0])
    pat = probe_site.link_pattern(info["base_path"], probe_site.gid_regex(info["gid"]))
    draft = probe_site.build_draft(
        info, media, pages, [], [("直链", pat)], r"[0-9a-f]{8,}", [], [],
        {media[0]: GID, pages[0]: None}, seq_hits=1)

    seg = draft.split("id_samples=[", 1)[1].split("],", 1)[0]
    assert media[0] in seg                 # 通过自检的那条照写
    assert pages[0] not in seg             # 没通过的那条**不写**
    assert "没通过自检" in draft            # 但要留下来, 提示人去补正则
    assert "只探到 **1 个**存在的序号" in draft   # 命中率过低 -> 草稿顶部带横幅


def test_probe_refuses_to_draft_an_opaque_suffix_site(fake_site):
    """不透明后缀的站要**拒绝出草稿**(exit 2), 而不是给一份必然 0 资源的声明。"""
    h = "a" * 64
    srv = fake_site(opaque_hash=h)
    port = srv.server_address[1]
    dlink = "http://127.0.0.1:%d/hashes/7c07a7fecb2fe3868aa22aae2edf0e5a/1-%s.png" % (port, h)
    out, rc = _run(port, [], dlink=dlink)
    assert rc == 2, out
    assert "不生成草稿" in out
    assert "不透明串" in out
    assert "GallerySite 草稿" not in out      # 一个字都不给, 免得被照抄


def test_probe_calls_a_single_hit_an_ambiguity(fake_site):
    """只命中第 1 个序号 = **歧义**, 不是"站点正常"。

    "图集只有 1 张"与"每页文件名各不相同"在报告里长得一样, 光看一条直链分不出来。
    把它当结论的代价就是照抄一份 0 资源的声明, 所以必须明说是歧义。
    """
    srv = fake_site(total=1)
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID])
    assert rc == 0, out
    assert "本次命中 1/6" in out
    assert "只命中第 1 个序号" in out
    assert "歧义" in out
    assert "只探到 **1 个**存在的序号" in out      # 草稿顶部横幅


def test_probe_refuses_a_draft_with_no_sequence_evidence(fake_site):
    """直链通、但 --scan 范围内一个存在的序号都没探到 -> **拒绝出草稿**。

    这正是 picsum 实测暴露的那处: gid 被推错, 6 次一个都不中 —— 而草稿照样打印出来,
    那份声明**没有一条有实测支持**。现在它走档 A 门禁, 一个字都不给。

    场景要造得准: 直链**自己必须有效**, 否则会在第 0 段被可达性拦下, 测不到 ① 这一层。
    """
    srv = fake_site(only_seqs=(2,))
    port = srv.server_address[1]
    out, rc = _run(port, [],
                   dlink="http://127.0.0.1:%d/photos/%s/0002.jpg" % (port, GID),
                   extra=["--scan", "1"])
    assert rc == 2, out
    assert "本次命中 0/1" in out
    assert "档 A 字段缺实测证据" in out
    assert "一个存在的序号都没探到" in out
    assert "GallerySite 草稿" not in out      # 一个字都不给, 免得被照抄


# ------------------------------------------------- 可达性分型(单元)


def test_diagnose_block_separates_causes_with_opposite_fixes():
    """七种成因给**互不通用**的建议 —— 混成一条"都试试"等于没说。

    重点是前两种: `ip_block` 与 `cf_challenge` 都是 403, 但一个**换指纹没用**、另一个
    **只有换指纹有用**。把它们混起来, 用户就会照着错的那条一直试下去。

    ⚠️ 判据读的是 `kind` 与 `step` 两个**代号**, 不是 `fix` 里的中文。这条用例以前
    断言的是 `"chrome" not in ip` —— 而 `ip_block` 的文案里**必须**写明"换指纹没用"
    并给出实测证据, 那就必然写到一个指纹名, 于是**一条正确的实现被判红**。
    代号是契约, 文案只是它的说明。(同族: `core/errors.py` 用 `error_kind` 归类。)
    """
    cf_body = b"<html><title>Just a moment...</title>challenge-platform</html>"
    cases = [
        ((403, "text/plain; charset=utf-8", {}, b"blocked"), "ip_block", "proxy"),
        ((403, "text/html; charset=utf-8", {}, cf_body), "cf_challenge", "impersonate"),
        ((403, "text/html; charset=utf-8", {"cf-mitigated": "challenge"}, b""),
         "cf_challenge", "impersonate"),
        ((401, "text/html", {}, b""), "auth", "login"),
        ((403, "text/html", {"www-authenticate": "Basic"}, b""), "auth", "login"),
        ((429, "text/plain", {}, b""), "rate", "wait"),
        ((503, "text/html", {}, b""), "down", "wait"),
        ((403, "text/html", {}, b"<html>nope</html>"), "blocked", "escalate"),
        ((0, "", {}, b""), "unknown", "inspect"),
    ]
    for (status, ctype, hdrs, body), want_kind, want_step in cases:
        kind, step, why, fix = probe_site.diagnose_block(status, ctype, hdrs, body)
        assert (kind, step) == (want_kind, want_step), (kind, step, want_kind, want_step)
        assert why and fix                       # 两件事都必须说清: 为什么 + 怎么办

    ip = probe_site.diagnose_block(403, "text/plain", {}, b"blocked")
    cf = probe_site.diagnose_block(403, "text/html", {}, cf_body)
    # 真正要断言的不变量: **两种 403 的处置不一样**。用代号说这句话, 而不是用中文 ——
    # 文案可以随便改, 这句判据不该动。
    assert ip[0] != cf[0]
    assert ip[1] != cf[1]
    # `ip_block` 的文案里出现 "chrome" 是**说它没用**, 这不是推荐。所以文案里必须
    # 同时出现"没用"这件事, 否则用户会照错的那条试 —— 断言的是这一点, 不是"有没有 chrome"。
    assert "换 TLS 指纹" in ip[3] and "没用" in ip[3], ip[3]


def test_only_an_impersonation_fix_triggers_the_automatic_retry():
    """自动换指纹是**白名单**: 只有"处置就是换指纹"的成因才重试。

    这条与上面那条成对。以前的条件是 `kind != "ip_block"`(黑名单)—— 每加一种成因
    都会**默认继承**"重试", 于是 429 限速与 5xx 也会白跑一次换指纹。现在按 `step`
    白名单判, 新成因必须显式回答"换指纹有没有用"。
    """
    assert probe_site._RETRY_STEPS == ("impersonate", "escalate")
    # 白名单里只有"换指纹有收益"的那两种; 其余成因的 step 都不在里面
    not_retry = ["proxy", "login", "wait", "inspect", "referer"]
    for step in not_retry:
        assert step not in probe_site._RETRY_STEPS, step


def test_accept_verdict_reads_the_three_way_control():
    """② 的三档对照要读成机器可比的结论(快照靠它发现"站点开始校验 Accept")。"""
    mk = lambda a, c: [("不带 Accept", a), ("Accept: */*", a), ("Accept: image/*", c)]
    assert probe_site.accept_verdict(mk(403, 200)) == "required"
    assert probe_site.accept_verdict(mk(200, 200)) == "not-required"
    assert probe_site.accept_verdict(mk(403, 403)) == "unknown"
    assert probe_site.accept_verdict([]) == "unknown"


# ------------------------------------------------- 快照(建议 1 的产出侧)


def test_probe_writes_a_machine_readable_snapshot(fake_site, tmp_path):
    """`--json` 要把实测结论落成机器可读的快照 —— 那是 drift_check 的输入。

    它必须包含"能不能进"以外的**判定规则本身**(`over_*` / `accept` / `exists_shape`),
    因为站点改版时先变的正是这些, 而它们的变化会让所有 URL 一起失效。
    """
    srv = fake_site()
    snap_file = tmp_path / "site_probe.json"
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID],
                   extra=["--json", str(snap_file)])
    assert rc == 0, out
    assert snap_file.is_file(), out

    data = json.loads(snap_file.read_text(encoding="utf-8"))
    assert len(data) == 1, data
    snap = next(iter(data.values()))
    assert snap["reachable"] is True
    assert snap["reach_status"] == 200
    assert snap["exists_hits"] == 5
    assert snap["exists_shape"] == "TTTTTF"        # 5 个序号存在, 第 6 个不存在
    assert snap["over_status"] == 200              # 越界也返回 200
    assert snap["over_ctype"].startswith("text/html")   # -> 只能按 Content-Type 判定
    assert snap["accept"] == "required"            # 不给 Accept 会被 403
    assert ".jpg" in snap["variants"]
    assert snap["opaque_suffix"] is False


def test_probe_records_unreachable_in_the_snapshot(fake_site, tmp_path):
    """**不通也要落快照** —— 它恰恰是最该被记下来的那一格。

    否则 drift_check 只能看到"上次好好的、这次没数据", 分不清"站点挂了"和"我没跑"。
    """
    srv = fake_site(block_mode="plain")
    snap_file = tmp_path / "site_probe.json"
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID],
                   extra=["--json", str(snap_file)])
    assert rc == 2, out
    assert snap_file.is_file(), out
    snap = next(iter(json.loads(snap_file.read_text(encoding="utf-8")).values()))
    assert snap["reachable"] is False
    assert snap["reach_status"] == 403
    assert snap["exists_hits"] == 0                # 后面四项没跑, 一个都没探
    assert snap["accept"] == "unknown"


def test_probe_merges_the_snapshot_by_site(tmp_path, fake_site):
    """同一个快照文件里按**站点**合并, 不能互相覆盖 —— 巡检要一次看全所有站点。"""
    snap_file = tmp_path / "site_probe.json"
    snap_file.write_text(json.dumps({"someone_else": {"base": "x"}}),
                         encoding="utf-8", newline="")
    srv = fake_site()
    out, rc = _run(srv.server_address[1], [], extra=["--json", str(snap_file)])
    assert rc == 0, out
    data = json.loads(snap_file.read_text(encoding="utf-8"))
    assert "someone_else" in data                  # 别人的基线还在
    assert len(data) == 2, sorted(data)


# ------------------------------------------------- 冒烟(建议 2)


def test_smoke_downloads_one_file_for_real(fake_site, tmp_path):
    """`--smoke` 要**真的**下一张到磁盘 —— "HEAD 通"与"能落地"之间隔着四道关卡。

    这里验的是最小那条链路: 下载 -> 写盘 -> 魔术字节/尺寸 -> `match_resource`。
    """
    srv = fake_site()
    dest = tmp_path / "smoke"
    snap_file = tmp_path / "site_probe.json"
    out, rc = _run(srv.server_address[1], [],
                   extra=["--smoke", "--smoke-dir", str(dest),
                          "--json", str(snap_file)])
    assert rc == 0, out
    assert "冒烟" in out
    assert "落盘" in out
    files = sorted(p for p in dest.glob("smoke_*") if p.is_file())
    assert files, out                              # 真的落盘了
    assert all(p.stat().st_size > 0 for p in files)
    snap = next(iter(json.loads(snap_file.read_text(encoding="utf-8")).values()))
    assert snap["smoke_ok"] >= 1
    assert snap["smoke_total"] == len(files)


def test_smoke_says_so_when_nothing_could_be_downloaded(fake_site, tmp_path):
    """HEAD 通、GET 全 500 -> 冒烟要**明说一张都没下来**, 而不是安静地跳过。

    这正是"能探测"与"能落地"之间那道缝: 五段探测全部通过、草稿也生成得出来, 可真正
    取正文时被 500 挡回来。探针此前对这一层**完全无感知** —— 它会交出一份看着完美的
    报告, 而用户下单后才发现 0 个资源。
    """
    srv = fake_site(get_fails=True)
    dest = tmp_path / "smoke"
    snap_file = tmp_path / "site_probe.json"
    out, rc = _run(srv.server_address[1], [],
                   extra=["--smoke", "--smoke-dir", str(dest),
                          "--json", str(snap_file)])
    assert rc == 0, out                              # 探测本身没问题, 所以不拦草稿
    assert "冒烟 0/" in out
    assert "一张都没下来" in out
    assert not list(dest.glob("smoke_*")) if dest.exists() else True
    snap = next(iter(json.loads(snap_file.read_text(encoding="utf-8")).values()))
    assert snap["smoke_ok"] == 0
