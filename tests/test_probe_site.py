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

钉住的三件事
============
1. **输出口径**: ①越界 200+text/html 必须判成"只能用 Content-Type 判定";
   ②Accept 被校验必须报出来; ④线路条数; ⑤自检合计与页码识别。
2. **不要编结论**: 一个存在的序号都没探到时, 必须明说"判定规则还没验出来",
   而不是把 403 当成"状态码可用"照抄一个结论下来(这是最容易误导人的一种输出)。
3. **草稿要能用**: 至少是合法 Python; 而"正则按路径段对齐"这类细节由针对性用例守。
"""

import ast
import http.server
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
        if self.always_403:
            self._send(403, "text/plain; charset=UTF-8", b"blocked", body)
            return
        path = self.path.split("?")[0]
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
                if ctype and n <= self.total and n != self.hole:
                    self._send(200, ctype, b"\xff\xd8\xff" + b"0" * 64, body)
                    return
        self._send(200, "text/html; charset=utf-8", b"<html>x</html>", body)


@pytest.fixture
def fake_site():
    """起一个本地假站点; 用完关掉。端口由系统分配, 不占固定端口。"""
    servers = []

    def start(always_403=False, hole=0):
        cls = type("_S", (_FakeSite,), {"always_403": always_403, "hole": hole})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _run(port, page_paths, extra=()):
    """跑一遍探针脚本; 直链固定用 `/photos2/...`(顺带验基址分桶的拆分)。"""
    base = "http://127.0.0.1:%d" % port
    urls = ["%s/photos2/%s/0001.jpg" % (base, GID)]
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


def test_probe_refuses_to_conclude_when_nothing_resolved(fake_site):
    """整站不可达时**不许**照抄一个结论 —— 那是本项目最忌讳的"看着有结论、其实没证据"。

    真实教训(2026-09-23): xchina 的媒体主机从这台机器整段 403, 而探针第一版会把
    "越界 403" 判成"状态码可用", 那会把一个**完全不可达**的站点写成"判定规则已确认"。
    """
    srv = fake_site(always_403=True)
    out, rc = _run(srv.server_address[1], ["/photo/id-%s.html" % GID])
    assert rc == 0, out
    assert "还没验出来" in out
    assert "状态码可用" not in out
    assert "--impersonate chrome" in out              # 给出可行动的下一步
    assert "你给的那条直链**本身没探通**" in out


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
