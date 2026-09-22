import errno
import hashlib
import json
import os
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from core.cancel import TaskCancelled
from core.config import DEFAULT_ACCEPT, settings
from core.errors import DiskFullError
from .ratelimit import describe, domain_slot, note_failure, note_rate_limited, note_success

#: 流式写入的分块。64KB 在大文件上要跑几千次 Python 层循环, 256KB 是纯收益。
CHUNK = 256 * 1024
TASK_IO_TIMEOUT = (4.0, 4.0)


# ---- 原子落盘: 先写 .part, 成功后原子改名 ----
#
# ⚠️ 直接写目标路径的话, 中断(取消/崩溃/断电/超时)会在**最终位置**留下一个半截
# 文件。它会以"已下载"的身份参与去重、manifest 和进度统计; 更糟的是断点续传会
# **接着这个半截文件继续写** —— 如果那半截来自另一个 URL(换过下载点/画质档)
# 或本身就是坏的, 拼出来的是一个内容错误但长度正确的文件, 而 sha256 是把"坏的
# 完整文件"算出来的, 于是它一路绿灯通过所有校验。这类问题没有报错、没有日志,
# 只能在用户打开文件时才被发现。
#
# 写 .part + os.replace 之后, 半成品永远只存在于最终位置之外。


def _part_path(path):
    return path.with_name(path.name + ".part")


def _part_src(path):
    """.part 的来源记录: 记下这批字节是哪个 URL 写的。

    续传的唯一凭据 —— 光比对长度挡不住"两个不同文件拼在一起恰好等长"。
    """
    return path.with_name(path.name + ".partsrc")


def _read_src(sidecar):
    try:
        return json.loads(sidecar.read_text(encoding="utf-8")).get("url")
    except Exception:
        # 读不出来(没有/损坏)就当"来源不明", 结果是丢弃重下 —— 安全方向
        return None


def _write_src(sidecar, url):
    try:
        sidecar.write_text(json.dumps({"url": url}, ensure_ascii=False),
                           encoding="utf-8")
    except OSError:
        pass  # 记不下来就记不下来: 最坏结果是下次从头重下, 不会下出坏文件


def _prepare_resume(path, url):
    """把"上次留下的内容"搬到 .part, 返回可以接着写的字节数(0 = 从头下)。"""
    part = _part_path(path)
    src = _part_src(path)
    if part.exists():
        if _read_src(src) == url:
            return part.stat().st_size
        # ⚠️ 残留是另一个 URL 写的: 接着它续就是在拼接两个不同的文件。
        part.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        return 0
    if path.exists() and path.stat().st_size > 0:
        # 已存在的完整文件: 当作"半成品"搬进 .part, 让 416 分支(服务器说范围
        # 越界 = 本地这份已完整)把它原样换回去。rename 是原子的, 中断也不丢内容。
        path.rename(part)
        _write_src(src, url)
        return part.stat().st_size
    return 0


def _commit_part(path):
    """.part -> 最终文件。os.replace 在同一分区内是原子的。"""
    part = _part_path(path)
    if not part.exists():
        return
    os.replace(part, path)
    _part_src(path).unlink(missing_ok=True)


def _hdr(resp, name):
    """取响应头; 拿不到或不是字符串(测试替身常给 Mock)一律当不存在。

    ⚠️ 不做这个 isinstance 检查的话, `re.match(模式, Mock对象)` 会抛 TypeError,
    而它发生在下载路径的 try 里 —— 又被当成"这个资源下载失败"。
    """
    try:
        v = resp.headers.get(name)
    except Exception:
        return ""
    return v if isinstance(v, str) else ""


def discard_partial(path):
    """清理某个最终路径对应的全部残留: 最终文件 + .part + 来源记录。

    用在取消/中止时。⚠️ 只删最终文件是不够的 —— 中断时内容在 .part 里,
    目标位置反而是干净的; 漏删 .part 会在用户目录里留下一堆 `xxx.jpg.part`,
    而且下次续传会接着这些残片继续写。
    """
    p = Path(path)
    for f in (p, _part_path(p), _part_src(p)):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


def _expected_size(resp, offset):
    """从响应头推出"落盘后应该是多少字节"。认不出返回 None(不校验)。"""
    # ⚠️ 有 Content-Encoding 时 Content-Length 是**压缩后**的长度, 拿它比对
    # 解压后的字节数必然误判。宁可不校验, 也别把好文件判成坏的。
    if (_hdr(resp, "Content-Encoding") or "identity").lower() != "identity":
        return None
    m = re.match(r"bytes\s+\d+-\d+/(\d+)", _hdr(resp, "Content-Range").strip())
    if m:
        return int(m.group(1))
    if not offset:
        cl = _hdr(resp, "Content-Length").strip()
        if cl.isdigit():
            return int(cl)
    return None


def _write_chunk(f, chunk):
    try:
        f.write(chunk)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise DiskFullError(
                "磁盘空间不足, 已停止下载。请清理目标磁盘后重试"
                " —— 已下好的文件会保留, 续跑不会重复下载它们。"
            )
        raise


def _timeout():
    """超时拆成 (连接, 读取) 元组。

    ⚠️ 单个数值会**同时**作用于连接和读取: 连接要快失败(10s 足够判断网络不通),
    而读取大文件/慢链路需要长得多。用 30s 卡读取, 大视频会被中途掐断。
    """
    return (settings.connect_timeout, settings.read_timeout)


class RateLimited(Exception):
    """站点返回 429。

    与别的失败不同: 429 是站点在说"慢一点", 不是"这个文件坏了"。
    所以退避时长要听它的(Retry-After), 而不是套用普通的指数退避 ——
    普通退避最长 8 秒, 而站点要求冷静几十秒时, 硬闯只会让封禁更久。
    """

    def __init__(self, wait):
        super().__init__(f"429 被限速, 站点要求等待 {wait:.1f}s")
        self.wait = wait


def _retry_after(resp, default):
    """解析 Retry-After(秒 或 HTTP 日期), 认不出用 default。"""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, min(float(raw), 300.0))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        if dt is not None:
            import datetime

            delta = (dt - datetime.datetime.now(dt.tzinfo)).total_seconds()
            return max(0.0, min(delta, 300.0))
    except Exception:
        pass
    return default


def _build_session():
    """共享会话: 连接池按并发数定, 且池满时**等待**而非建了又丢。

    urllib3 默认 `pool_maxsize=10`。并发超过它时, 默认行为是开新连接、用完丢弃,
    同时打 "Connection pool is full, discarding connection" —— 池化收益全丢,
    还白付一次 TCP+TLS 握手。`pool_block=True` 让它排队等空闲连接, 才是真池化。
    """
    sess = requests.Session()
    size = max(10, int(settings.domain_concurrency) * 2)
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=size,
        pool_maxsize=size,
        pool_block=True,
        max_retries=0,      # 重试由本项目自己管(抖动/429/冷却), 别叠两层
    )
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    if settings.proxy:
        sess.proxies.update({"http": settings.proxy, "https": settings.proxy})
    return sess


SESSION = _build_session()


class TaskSession:
    """Own one task's session and close its active streaming responses."""

    def __init__(self, session=None):
        self._session = session or _build_session()
        self._lock = threading.Lock()
        self._responses = set()
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._session, name)

    def _track(self, response):
        original_close = response.close
        closed = threading.Event()

        def close():
            if closed.is_set():
                return
            closed.set()
            with self._lock:
                self._responses.discard(response)
            original_close()

        response.close = close
        with self._lock:
            if self._closed:
                close_now = True
            else:
                self._responses.add(response)
                close_now = False
        if close_now:
            close()
            raise requests.RequestException("task session closed during request")
        return response

    def get(self, *args, **kwargs):
        return self._track(self._session.get(*args, **kwargs))

    def head(self, *args, **kwargs):
        return self._track(self._session.head(*args, **kwargs))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            responses = list(self._responses)
            self._responses.clear()
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        self._session.close()


def task_session(session=None):
    return TaskSession(session)


def _check_progress(progress_cb):
    if progress_cb:
        progress_cb()


def _interruptible_wait(seconds, progress_cb):
    deadline = time.monotonic() + max(0.0, seconds)
    waiter = threading.Event()
    while True:
        _check_progress(progress_cb)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        waiter.wait(min(0.1, remaining))


class ProxyPool:
    """任务级代理池: 支持单代理或逗号分隔的多代理轮换 + 健康熔断。

    一个任务的多个资源分散到不同代理(round-robin), 降低单代理被封风险,
    同时避免同一会话频繁切 IP 触发风控 —— 这里按"任务内资源序号"轮换, 粒度适中。

    ⚠️ 只按序号轮换有个硬伤: 某个代理已经挂了(过期/被封/连不上), 仍然会被
    分到它 1/len 的资源, 那些资源全部失败。所以这里加一层熔断 —— 连续失败
    达到阈值就把该代理**冷却**一段时间, 期间不再被 pick 到; 冷却结束后自动
    放出来再试一次(半开)。这是"失败换线"的最小实现, 不需要健康检查线程。
    """

    #: 连续失败多少次后熔断该代理
    FAIL_THRESHOLD = 3
    #: 熔断后的冷却秒数(冷却结束自动半开放出)
    COOLDOWN = 300.0

    def __init__(self, spec, fail_threshold=None, cooldown=None):
        self.spec = spec
        self.proxies = [p.strip() for p in str(spec or "").split(",") if p.strip()]
        self._i = 0
        if fail_threshold is not None:
            self.FAIL_THRESHOLD = fail_threshold
        if cooldown is not None:
            self.COOLDOWN = cooldown
        #: proxy -> 连续失败次数
        self._fails = {}
        #: proxy -> 熔断到期时间戳(0 = 未熔断)
        self._blocked_until = {}

    @property
    def empty(self):
        return not self.proxies

    def _is_blocked(self, proxy, now=None):
        """该代理是否处于熔断冷却期。"""
        until = self._blocked_until.get(proxy, 0)
        if not until:
            return False
        now = time.time() if now is None else now
        if now >= until:
            # 冷却结束: 半开放出(清掉到期时间, 失败计数保留, 再失败会立刻再次熔断)
            self._blocked_until.pop(proxy, None)
            return False
        return True

    def pick(self, n=0):
        """返回第 n 个(按资源序号)应使用的代理 URL; 无可用代理返回 None。

        从轮换起点开始向后找**第一个未熔断**的代理, 全都熔断则退化为按序号返回
        (总比不下强 —— 这时故障多半不在代理上)。
        """
        if not self.proxies:
            return None
        size = len(self.proxies)
        start = n % size
        for off in range(size):
            p = self.proxies[(start + off) % size]
            if not self._is_blocked(p):
                return p
        return self.proxies[start]

    def apply(self, session, n=0):
        """把轮换到的代理设到 session 上。"""
        p = self.pick(n)
        if p:
            session.proxies.update({"http": p, "https": p})
        else:
            session.proxies.clear()
        return session

    def note_success(self, proxy):
        """该代理成功传完一个资源: 清零失败计数(半开状态下即"恢复")。"""
        if proxy:
            self._fails.pop(proxy, None)
            self._blocked_until.pop(proxy, None)

    def note_failure(self, proxy):
        """该代理失败一次: 累计达阈值则熔断 COOLDOWN 秒。"""
        if not proxy:
            return
        c = self._fails.get(proxy, 0) + 1
        self._fails[proxy] = c
        if c >= self.FAIL_THRESHOLD:
            self._blocked_until[proxy] = time.time() + self.COOLDOWN

    def snapshot(self):
        """当前池状态(供诊断/前端展示)。"""
        return [
            {
                "proxy": p,
                "fails": self._fails.get(p, 0),
                "blocked": self._is_blocked(p),
                "blocked_for": max(0, int(self._blocked_until.get(p, 0) - time.time())),
            }
            for p in self.proxies
        ]

    def session_for(self, n=0):
        """返回一个带本池第 n 个代理的独立 session(每资源独占, 避免并发竞态)。"""
        return make_proxy_session(self.spec, n)


def make_proxy_session(spec=None, n=0):
    """构造一个带(可选)任务级代理的 requests.Session, 供单个任务/资源独占。

    与全局 SESSION 解耦: 全局 SESSION 走 settings.proxy(环境变量, 所有任务共享),
    任务级 proxy 来自 options.proxy(V30 落库), 必须独立成 session 才不会互相污染连接池。
    n: 多代理轮换时的序号(通常传资源 id), 让同一任务的资源分散到不同代理。
    """
    sess = _build_session()
    if spec:
        ProxyPool(spec).apply(sess, n)
    return sess


def build_headers(referer=None, extra=None, accept=None):
    headers = {"User-Agent": settings.user_agent, "Accept": accept or DEFAULT_ACCEPT}
    if referer:
        headers["Referer"] = referer
    if extra:
        for k in ("cookie", "referer", "user-agent", "accept"):
            if extra.get(k):
                headers[k.title()] = extra[k]
    return headers


def safe_filename(url, default_ext=".bin"):
    raw = unquote(url.split("?")[0].rstrip("/"))
    name = re.sub(r'[\\/:*?"<>|]', "_", raw.rsplit("/", 1)[-1])
    if not name or "." not in name:
        name = hashlib.sha1(url.encode()).hexdigest() + default_ext
    return name


def _stream_one(url, path, headers, retries, resume, sess, progress_cb,
                require_image=False, reject_ct=None, info=None,
                request_timeout=None):
    """对单个 URL 做带重试的流式下载, 返回 (sha256, Content-Type)。

    Content-Type 校验提供互补的两种用法:
      * require_image=True —— 白名单, 必须是 image/*。
        某些站点(如 img.xchina.io)对不存在的图片仍返回 200, 只是 Content-Type
        变成 text/html —— 不校验就会把 HTML 错误页当图片存下来, 而且因为
        "下载成功", 备用下载点也不会被触发。
      * reject_ct="text/html" —— 黑名单, 命中即拒绝。
        视频 CDN 常返回 application/octet-stream, 用白名单会误杀真实视频,
        但 HTML 错误页必须挡住。

    info: 可选的 dict 出参, 成功后写入实际生效的下载点与响应类型, 供
          manifest 做溯源(区分"原本要下的 URL"与"真正下的是哪个镜像")。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req_headers = dict(headers)
            offset = _prepare_resume(path, url) if resume else 0
            if offset:
                req_headers["Range"] = f"bytes={offset}-"

            slot = (
                domain_slot(url, progress_cb=progress_cb)
                if progress_cb is not None
                else domain_slot(url)
            )
            with slot:
                resp = sess.get(url, headers=req_headers, stream=True,
                                timeout=request_timeout or _timeout())
            # ⚠️ 必须保证关闭: 416/429/内容校验这几条**提前退出**的路径都没读过响应体,
            # 连接不会被自动归还。配合 pool_block=True 就是"泄漏到池满 → 永久阻塞",
            # 症状是任务卡死而非报错。读完的路径 close() 是幂等的。
            try:
                if resp.status_code == 416:
                    # 服务器说"你要的范围越界了" = 本地这份已经是完整的
                    _commit_part(path)
                    fill_info(info, url, None)
                    return sha256_file(path), None
                if resp.status_code == 429:
                    # 站点明确要求减速: 按它说的等, 而不是套普通退避
                    raise RateLimited(_retry_after(resp, float(settings.image_retries) * 5))
                resp.raise_for_status()

                ctype = (resp.headers.get("Content-Type") or "").lower()
                if require_image and not ctype.startswith("image/"):
                    raise ValueError(
                        f"not an image (Content-Type: {ctype or 'unknown'})"
                    )
                if reject_ct and ctype.startswith(reject_ct):
                    raise ValueError(
                        f"rejected Content-Type: {ctype} (疑似错误页而非媒体文件)"
                    )
                fill_info(info, url, ctype or None)

                part = _part_path(path)
                mode = "ab" if offset and resp.status_code == 206 else "wb"
                if mode == "wb":
                    part.unlink(missing_ok=True)
                    _part_src(path).unlink(missing_ok=True)
                h = hashlib.sha256()
                if mode == "ab":
                    h.update(part.read_bytes())
                total = _expected_size(resp, offset)
                # ⚠️ 新建时用**独占**模式("x"): 两个任务采到同一张图会落到同一个
                # .part, 两份字节交错写入的结果是"两边都报成功, 文件却是坏的",
                # 而且没有任何报错。宁可让后到的一方失败并说明原因。
                # 续传("ab")不在此列 —— 那时 .part 的来源已被校验过是本 URL 的。
                # "x" 本身就是"独占新建 + 写入", 不能与 "w" 组合("wbx" 非法)
                flags = "ab" if mode == "ab" else "xb"
                try:
                    handle = open(part, flags)
                except FileExistsError:
                    raise FileExistsError(
                        f"另一处正在下载同一个文件({part.name});"
                        f" 为避免两份字节交错写坏, 本次放弃"
                    )
                with handle:
                    for chunk in resp.iter_content(CHUNK):
                        if chunk:
                            _write_chunk(handle, chunk)
                            h.update(chunk)
                            if progress_cb:
                                # 传本 chunk 的字节数: 调用方靠它累计真实吞吐。
                                # 老实现是无参回调, 用 try 兼容签名简单的调用方
                                # (测试替身常写成 `def cb(): ...`)。
                                try:
                                    progress_cb(len(chunk))
                                except TypeError:
                                    progress_cb()
                # ⚠️ 长度不符就丢弃重来: 否则这个坏文件会以"成功"的身份落盘,
                # 之后去重/manifest/预览全都建立在错误的字节上, 且毫无报错。
                got = part.stat().st_size
                if total is not None and got != total:
                    part.unlink(missing_ok=True)
                    raise OSError(f"下载不完整: 得到 {got} 字节, 应为 {total} 字节")
                _commit_part(path)
            finally:
                resp.close()
            # 只有"2xx + 内容合规"才算一次真成功, 才计入 AIMD 增速
            note_success(url)
            return h.hexdigest(), (ctype or None)
        except TaskCancelled:
            # 用户点了"停止": 这不是一次失败。按普通失败处理会退避重睡一轮,
            # 再从头续传一个注定被放弃的文件 —— 用户体感是点了没反应。
            raise
        except RateLimited as e:
            last_err = e
            _check_progress(progress_cb)
            # ⚠️ 只让"撞墙的这一个线程"退避是不够的: 同一批里其他线程还在按原节奏
            # 猛冲, 站点看到的整体压力没变, 它的判断就不会变 —— 只会更快把整站封掉。
            # 所以把这次 429 记成**站点级冷却**, 下一轮 domain_slot 会让全站一起等,
            # 等待时长也以冷却截止时间为准(精确, 不会等两遍)。
            note_rate_limited(url, e.wait)
            if attempt == retries:
                raise
        except Exception as e:
            last_err = e
            _check_progress(progress_cb)
            if attempt == retries:
                raise
            # ⚠️ 只有"传输层/服务端"失败才算站点吃不消。内容校验失败
            # (Content-Type 不对)是这条 URL 自身的问题, 拿它去放宽节奏会白白
            # 拖慢整个相册 —— 把"URL 不对"误当成"站点限流"是最常见的误判。
            if isinstance(e, requests.RequestException):
                note_failure(url)
            # 指数退避 + 抖动: 纯指数会让一批并发失败的请求在同一时刻集体重试
            # (重试风暴, 把站点/WAF 瞬间打爆)。乘一个 0.6~1.4 的随机因子错开它们。
            backoff = min(2 ** attempt, 8)
            _interruptible_wait(
                backoff * random.uniform(0.6, 1.4), progress_cb
            )
    raise last_err


def fill_info(info, url, ctype):
    """回填"这个字节实际来自哪个 URL、服务器说它是什么类型"。

    只在 info 是 dict 时写入, 调用方不想溯源时保持零开销。
    """
    if info is None:
        return
    info["resolved_url"] = url
    if ctype:
        info["content_type"] = ctype


def stream_download(url, path, headers, retries=None, resume=True, session=None,
                    progress_cb=None, info=None):
    """带限速/重试/断点续传的流式下载, 返回文件 sha256。"""
    retries = retries if retries is not None else settings.image_retries
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sha, ctype = _stream_one(url, path, headers, retries, resume,
                             session or SESSION, progress_cb, info=info)
    fill_info(info, url, ctype)
    return sha


def _ext_of(url):
    """取 URL 末段的扩展名(含点, 小写), 无扩展名时返回空串。"""
    name = unquote(urlparse(url).path).rsplit("/", 1)[-1]
    m = re.search(r"(\.[A-Za-z0-9]{1,5})$", name)
    return m.group(1).lower() if m else ""


def _swap_ext(path, url):
    """候选 URL 的扩展名与当前路径不一致时换掉后缀。

    原图 .jpg 降级到 .webp 后若仍存成 .jpg, 文件内容与后缀不符,
    后续按扩展名做类型判断/预览时都会出错。
    """
    ext = _ext_of(url)
    if not ext or path.suffix.lower() == ext:
        return path
    return path.with_suffix(ext)


def download_with_mirrors(url, path, headers, retries=None, resume=True, session=None,
                          progress_cb=None, mirrors=None, log=None, require_image=False,
                          reject_ct=None, info=None, request_timeout=None):
    """主 URL 失败时依次尝试备用下载点(mirrors), 返回 (sha256, 实际路径)。

    mirrors 由采集器给出(如同一张图的多个尺寸/CDN 变体), 下载层只负责
    "一个挂了换下一个", 不关心这些 URL 是怎么来的。
    关闭方式: config.yaml 设 mirror_fallback: false。
    require_image / reject_ct 透传给 _stream_one 做 Content-Type 校验。
    info: 成功时回填真正生效的下载点与响应类型(见 _fill_info)。
    """
    retries = retries if retries is not None else settings.image_retries
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sess = session or SESSION

    if mirrors and settings.mirror_fallback:
        candidates = [url] + [m for m in mirrors if m and m != url]
    else:
        candidates = [url]

    last_err = None
    cur = path
    for i, cand in enumerate(candidates):
        if i:
            # 换下载点: 清掉上一个 URL 留下的半成品(不同 URL 的内容不能拼接)
            cur.unlink(missing_ok=True)
            cur = _swap_ext(path, cand)
            cur.unlink(missing_ok=True)
            if log:
                log(f"切换下载点 -> {cand.split('/')[-1]}")
        try:
            # 只有主 URL 用断点续传; 切换后是全新 URL, 必须从头下
            sha, ctype = _stream_one(cand, cur, headers, retries, resume and i == 0,
                                     sess, progress_cb, require_image, reject_ct,
                                     info, request_timeout)
            fill_info(info, cand, ctype)
            return sha, cur
        except TaskCancelled:
            # 叫停时不要再去试下一个下载点 —— 用户没有"换个源继续下"的意思
            raise
        except Exception as e:
            last_err = e
            if log:
                log(f"下载点失败 {cand.split('/')[-1]}: {type(e).__name__}")
    raise last_err


def resolve_target(save_dir, url, filename=None, default_ext=".bin"):
    """决定落盘路径: 外部指定的 filename 优先, 否则回退 URL 末段。

    filename 可以是带子目录的相对路径(统一用 / 分隔), 见 core/naming.py。
    """
    out = Path(save_dir)
    target = out / filename if filename else out / safe_filename(url, default_ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def sha256_file(path, progress_cb=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            if progress_cb:
                progress_cb()
    return h.hexdigest()
