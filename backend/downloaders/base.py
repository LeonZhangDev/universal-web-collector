import hashlib
import random
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from core.cancel import TaskCancelled
from core.config import DEFAULT_ACCEPT, settings
from .ratelimit import domain_slot, note_rate_limited

CHUNK = 64 * 1024


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


SESSION = requests.Session()
if settings.proxy:
    SESSION.proxies.update({"http": settings.proxy, "https": settings.proxy})


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
                require_image=False, reject_ct=None, info=None):
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
            offset = 0
            if resume and path.exists() and path.stat().st_size > 0:
                offset = path.stat().st_size
                req_headers["Range"] = f"bytes={offset}-"

            with domain_slot(url):
                resp = sess.get(url, headers=req_headers, stream=True,
                                timeout=settings.request_timeout)
            if resp.status_code == 416:
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

            mode = "ab" if offset and resp.status_code == 206 else "wb"
            h = hashlib.sha256()
            if mode == "ab":
                h.update(path.read_bytes())
            with open(path, mode) as f:
                for chunk in resp.iter_content(CHUNK):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
                        if progress_cb:
                            progress_cb()
            return h.hexdigest(), (ctype or None)
        except TaskCancelled:
            # 用户点了"停止": 这不是一次失败。按普通失败处理会退避重睡一轮,
            # 再从头续传一个注定被放弃的文件 —— 用户体感是点了没反应。
            raise
        except RateLimited as e:
            last_err = e
            # ⚠️ 只让"撞墙的这一个线程"退避是不够的: 同一批里其他线程还在按原节奏
            # 猛冲, 站点看到的整体压力没变, 它的判断就不会变 —— 只会更快把整站封掉。
            # 所以把这次 429 记成**站点级冷却**, 下一轮 domain_slot 会让全站一起等,
            # 等待时长也以冷却截止时间为准(精确, 不会等两遍)。
            note_rate_limited(url, e.wait)
            if attempt == retries:
                raise
        except Exception as e:
            last_err = e
            if attempt == retries:
                raise
            # 指数退避 + 抖动: 纯指数会让一批并发失败的请求在同一时刻集体重试
            # (重试风暴, 把站点/WAF 瞬间打爆)。乘一个 0.6~1.4 的随机因子错开它们。
            backoff = min(2 ** attempt, 8)
            time.sleep(backoff * random.uniform(0.6, 1.4))
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
                          reject_ct=None, info=None):
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
                                     info)
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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
