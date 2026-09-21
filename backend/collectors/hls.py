"""HLS 播放列表: 过期感知 + 健全性校验 + 真实体积估算。

为什么要有这个模块
==================

签名播放列表过期时, 某些 CDN **不返回错误码**, 而是回一个语法完全合法的
m3u8(200 + `application/vnd.apple.mpegurl`), 里面指向一个占位分片::

    #EXTM3U
    #EXT-X-VERSION:3
    #EXT-X-TARGETDURATION:20
    #EXTINF:20,
    /fallback/placeholder.ts
    #EXT-X-ENDLIST

实测 video.xchina.download(2026-09-19, gid=6aaa517d3f106): 那个占位分片是
**603856 字节的真实可播放 TS**。也就是说: 把过期链接直接丢给 ffmpeg 或内置
分片器, 会一路畅通地下完、退出码 0、**任务报 success** —— 用户拿到一段几十秒
的占位画面, 只会以为是站点换片了或自己下错了。

这是最难查的一类失败: 没有异常、没有非零退出码、日志全绿,
连"这段视频怎么这么短"都只能靠肉眼察觉。所以下载前必须**先读一遍播放列表
本体**, 看它说了什么, 而不是"HTTP 状态码是不是 200"。

第二件事: 带签名的 URL 是**短命凭证**(实测约 30 分钟)。把它当普通资源 URL 存进
库里排队慢慢下、或者失败后一路重试, 都是白费力气 —— 重试一百次也只会拿到同一
份占位列表。这里把过期时间解析出来, 让调用方能在开工前就判断"这条凭证还够不够
用到下完", 过期时给出**人话解释和下一步**, 而不是让用户对着 403 猜。

第三件事(反直觉): 播放列表自己的 Content-Length 只有几 KB, 它只是个文本清单,
**完全不代表视频体积**。core/filters 的 min_size/max_size 若直接对 `.m3u8`
做 HEAD 预检, 会拿 2.5KB 去比 10MB 的下限, 于是把整段视频判成"太小"过滤掉 ——
用户看到的是"资源被过滤", 根本想不到原因在这里。所以本模块提供
`estimate_size()`: 用「单片体积 x 分片数」估出真实大小, 顺带让 size 类过滤
规则对 HLS 资源第一次变得可用。

本模块只做"读清单、下判断", 不碰下载、不碰合并 —— 那是 downloaders/video.py
的事。
"""

import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

#: 占位列表的特征。命中任一即认为这不是真要下的那部片。
#: 别小看这一条 —— 少了它, 过期凭证会以 success 的姿态骗过所有人。
DEFAULT_PLACEHOLDER_HINTS = ("/fallback/", "placeholder")

#: 常见的签名过期参数名。刻意**不收** "e" / "t" 这种单字母: 太宽泛, 容易把
#: 无关数字误读成过期时间(比如把随机串的数字部分当成 1970 年的时间戳)。
_EXPIRE_KEYS = ("expires", "expire", "expires_at", "valid_until", "deadline")

#: 毫秒级时间戳从 2001-09-09 起就是 10^12 量级; 用它区分秒/毫秒两种写法。
_MS_BOUNDARY = 10 ** 11

_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9.]+)", re.I)
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', re.I)
_STREAM_RE = re.compile(r"#EXT-X-STREAM-INF:([^\n]*)\n\s*([^\n#][^\n]*)")


def _as_epoch(value):
    """把 query 里的过期值转成 Unix 秒; 认不出返回 None。"""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # 毫秒写法: 13 位数字除以 1000 才是秒
    if n >= _MS_BOUNDARY:
        n = n / 1000.0
    # 合理性门槛: 与当前时刻相差不超过 10 年。不设的话 "expires=123456" 会被
    # 当成 1970 年的时间戳, 于是每条播放列表都"已过期"。
    if abs(n - time.time()) > 10 * 365 * 86400:
        return None
    return int(n)


def playlist_expires_at(url):
    """从 URL 的 query 里解析凭证过期时间(Unix 秒); 没有就返回 None。"""
    try:
        query = parse_qs(urlparse(url or "").query)
    except Exception:
        return None
    for key in _EXPIRE_KEYS:
        for raw in query.get(key) or []:
            ts = _as_epoch(raw)
            if ts is not None:
                return ts
    return None


def seconds_left(url, now=None):
    """这条带签名的 URL 还剩多少秒可用; 没有签名参数返回 None(即永不过期)。"""
    exp = playlist_expires_at(url)
    if exp is None:
        return None
    return int(exp - (now if now is not None else time.time()))


def _attrs(line):
    """把 `#EXT-X-KEY:METHOD=AES-128,URI="/key/enc.key"` 拆成 dict。"""
    if ":" not in line:
        return {}
    body = line.split(":", 1)[1]
    out = {}
    for key, val in _ATTR_RE.findall(body):
        out[key.upper()] = val.strip().strip('"')
    return out


def _absolute(url, base):
    """清单里的 URI 可能是协议相对或根相对, 一律补成绝对 URL。"""
    url = (url or "").strip().replace("\\/", "/")
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") and base:
        # 根相对: 挂在播放列表自身的 origin 上(**不是** cdn 的)
        parts = urlparse(base)
        return f"{parts.scheme}://{parts.netloc}{url}"
    if url.startswith("http"):
        return url
    return urljoin(base, url)


def parse_playlist(text, base_url=""):
    """解析单层 m3u8 文本, 返回结构化描述(**不发任何请求**)。

    返回 dict::{ media, count, duration, encrypted, key_url, iv,
    variants, target_duration }。master playlist 的 media 为空但有 variants。
    """
    out = {
        "media": [], "count": 0, "duration": 0.0, "encrypted": False,
        "key_url": "", "iv": "", "variants": [], "target_duration": 0.0,
        "endlist": False, "playlist_type": "",
        # 一次播放列表里可以出现**多把** #EXT-X-KEY(VOD 按分片换钥是合法写法)。
        # 只记最后一把会让校验放过"拿不到的那把" —— 下到一半才炸, 而且那一段
        # 已经落盘了, 排查成本极高。所以这里全部收下来, 由校验层逐把确认。
        "keys": [],
    }
    if not text:
        return out

    duration = 0.0
    media = []
    # ⚠️ 紧跟 #EXT-X-STREAM-INF 的下一行是**变体地址**而不是分片。不排除的话
    # master playlist 会被当成有 2 个媒体的普通列表, 于是绕过占位校验。
    prev_stream = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _EXTINF_RE.match(line)
        if m:
            try:
                duration += float(m.group(1))
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                out["target_duration"] = float(line.split(":", 1)[1])
            except ValueError:
                pass
            continue
        if line.upper() == "#EXT-X-ENDLIST":
            out["endlist"] = True
            continue
        if line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            out["playlist_type"] = line.split(":", 1)[1].strip().upper()
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = _attrs(line)
            method = (attrs.get("METHOD") or "").upper()
            enabled = bool(method) and method != "NONE"
            if enabled:
                out["encrypted"] = True
            if attrs.get("IV"):
                out["iv"] = attrs["IV"]
            # URI 可能是**相对**的(`enc.key` / `/key/enc.key`): 必须相对播放列表
            # 自身解析。ffmpeg 对相对 URI 的处理随版本而异, 而我们自己校验时必须
            # 拿到绝对地址才能确认"这把钥拿不拿得到"。
            uri = _absolute(attrs.get("URI"), base_url) if attrs.get("URI") else ""
            if enabled and uri:
                if uri not in [k["url"] for k in out["keys"]]:
                    out["keys"].append({
                        "url": uri,
                        "method": method,
                        "iv": attrs.get("IV") or "",
                        "keyformat": (attrs.get("KEYFORMAT") or "").upper(),
                    })
                if not out["key_url"]:
                    out["key_url"] = uri
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            prev_stream = True
            continue
        if line.startswith("#"):
            prev_stream = False
            continue
        if prev_stream:
            prev_stream = False
            continue
        media.append(line)

    for attrs_line, uri in _STREAM_RE.findall(text):
        bw = re.search(r"BANDWIDTH=(\d+)", attrs_line)
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs_line)
        out["variants"].append({
            "url": _absolute(uri.strip(), base_url),
            "bandwidth": int(bw.group(1)) if bw else 0,
            "width": int(res.group(1)) if res else 0,
            "height": int(res.group(2)) if res else 0,
        })

    out["media"] = [_absolute(u, base_url) for u in media]
    out["count"] = len(out["media"])
    out["duration"] = round(duration, 3)
    return out


def _looks_like_placeholder(info, hints):
    """这是不是一张"占位的清单"而不是真的下饭视频。"""
    needles = tuple(hints or DEFAULT_PLACEHOLDER_HINTS)
    if not needles:
        return False
    media = info.get("media") or []
    if not media:
        return False
    # 任何一个分片指向 sentinel 路径就够定罪
    return any(needle.lower() in u.lower() for u in media for needle in needles)


def inspect_playlist(url, headers=None, session=None, hints=None,
                     check_key=True, timeout=30, log=None):
    """下载前先看一眼播放列表: 它到底说的是真是假。

    返回 dict, 关键字段::

        ok          bool  是否放行(False 时不要下载, reason 里有原因)
        kind        str   ok/expired/placeholder/empty/unreachable/not-playlist
        reason      str   给人看的原因, 回答"接下来会怎样"
        expires_at  int|None  凭证过期时刻(没有签名则为 None)
        seconds_left int|None 还剩多少秒
        encrypted / key_url / iv
        duration / count / variants
        segments    list  绝对化的分片 URL
    """
    import requests

    result = {
        "ok": False, "kind": "unreachable", "reason": "", "url": url,
        "expires_at": None, "seconds_left": None, "encrypted": False,
        "key_url": "", "iv": "", "duration": 0.0, "count": 0,
        "variants": [], "segments": [], "key_bytes": 0,
        "keys": [], "key_count": 0, "keys_checked": 0,
        "endlist": False, "playlist_type": "",
    }

    # 1) 凭证过期: 放在最前面 —— 过期 URL 通常还能返回 200(占位列表),
    #    先报过期比先报占位对用户有用得多(他知道该去重新拿链接, 而不是以为站点换片)
    exp = playlist_expires_at(url)
    result["expires_at"] = exp
    left = seconds_left(url)
    result["seconds_left"] = left
    if exp is not None and left is not None and left <= 0:
        result["kind"] = "expired"
        result["reason"] = (
            "播放列表签名已过期, 站点会返回占位内容而不是视频。"
            "请重新打开视频页获取新链接(这类链接一般只有几十分钟有效期)"
        )
        return result

    # 2) 取清单本体
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(url, headers=headers or {}, timeout=timeout)
    except Exception as e:
        result["reason"] = f"无法获取播放列表: {type(e).__name__}: {e}"
        return result
    if resp.status_code >= 400:
        result["kind"] = "unreachable"
        result["reason"] = f"播放列表返回 HTTP {resp.status_code}"
        return result

    text = getattr(resp, "text", "") or ""
    if "#EXTM3U" not in text[:200].upper():
        # 又一个陷阱: 有些网关对过期链接返回 HTML 错误页, 但状态码仍是 200
        result["kind"] = "not-playlist"
        head = text[:60].replace("\n", " ").strip()
        result["reason"] = f"响应不是播放列表: {head or '空响应'}"
        return result

    info = parse_playlist(text, base_url=url)
    result.update({
        "encrypted": info["encrypted"], "key_url": info["key_url"],
        "iv": info["iv"], "duration": info["duration"],
        "count": info["count"], "variants": info["variants"],
        "segments": info["media"], "keys": info["keys"],
        "key_count": len(info["keys"]),
        "endlist": info["endlist"], "playlist_type": info["playlist_type"],
    })

    # 3) master playlist 不在这一层判伪: 它本来就不含分片, 由调用方下钻
    if info["variants"] and not info["media"]:
        result["ok"] = True
        result["kind"] = "ok"
        result["reason"] = f"master 播放列表, {len(info['variants'])} 档码率"
        return result

    if not info["media"]:
        result["kind"] = "empty"
        result["reason"] = "播放列表里没有任何分片"
        return result

    # A finite download must be a complete media playlist. A missing ENDLIST
    # means either a live/sliding window or a truncated VOD/EVENT response;
    # both are unsafe to persist as a terminal-success file.
    if not info["endlist"]:
        declared = info["playlist_type"]
        result["kind"] = "unfinished" if declared in {"VOD", "EVENT"} else "live"
        label = f"{declared} " if declared else "live/sliding "
        result["reason"] = (
            f"{label}播放列表缺少 #EXT-X-ENDLIST，无法确认已经完整终止；"
            "当前仅支持可完整下载的已结束播放列表"
        )
        return result

    # 4) 占位清单: 状态码、Content-Type、语法全都合法, 只有内容在撒谎
    if _looks_like_placeholder(info, hints):
        result["kind"] = "placeholder"
        reason = "站点返回的是占位列表(签名无效或清晰度不存在)"
        if left is not None:
            reason += f", 当前凭证还剩 {left} 秒"
        result["reason"] = reason + "。下载它只会得到一个占位视频, 已阻止"
        return result

    # 5) 加密流: 顺手确认**每一把**密钥都拿得到 —— 别等下完几百兆才发现解不开。
    #    多密钥轮换时只验一把等于没验: 拿不到的那把对应的分片会整段解不出来,
    #    而那时文件已经落盘、任务已经报过进度, 排查得从几百兆里往外刨。
    if info["encrypted"] and check_key:
        keys = list(info["keys"])
        if not keys and info["key_url"]:
            keys = [{"url": info["key_url"], "method": "", "iv": "", "keyformat": ""}]
        if not keys:
            result["kind"] = "no-key"
            result["reason"] = "播放列表声明加密但没给出密钥地址"
            return result

        bad = []
        for k in keys:
            name = k["url"].rsplit("/", 1)[-1] or k["url"]
            try:
                kr = getter(k["url"], headers=headers or {}, timeout=timeout)
            except Exception as e:
                result["keys_checked"] += 1
                bad.append(f"{name}: {type(e).__name__}")
                continue
            n = len(kr.content or b"")
            result["keys_checked"] += 1
            if kr.status_code >= 400 or n not in (16, 24, 32):
                bad.append(f"{name} HTTP {kr.status_code} {n}B")
            else:
                result["key_bytes"] = n
        if bad:
            result["kind"] = "bad-key"
            result["reason"] = (
                f"{len(bad)}/{len(keys)} 把密钥不可用({'; '.join(bad[:3])}); "
                "AES-128 每把应为 16 字节。加密流必须拿齐全部密钥才能解密, 已阻止下载"
            )
            return result

    result["ok"] = True
    result["kind"] = "ok"
    bits = [f"{info['count']} 个分片", f"时长 {_hhmmss(info['duration'])}"]
    if info["encrypted"]:
        method = next((k["method"] for k in info["keys"] if k["method"]), "AES-128")
        bits.append(f"{method} 加密")
        if len(info["keys"]) > 1:
            bits.append(f"{len(info['keys'])} 把密钥(已全部验证可达)")
    if left is not None and left < 600:
        # 不阻止(拉列表只要几秒), 但要让用户知道这条凭证快凉了
        bits.append(f"凭证仅剩 {left} 秒")
    result["reason"] = ", ".join(bits)
    if log and result["ok"]:
        log(f"播放列表校验通过: {result['reason']}")
    return result


def estimate_size(segments, headers=None, session=None, samples=2,
                  timeout=30, log=None):
    """用「单片体积 x 分片数」估算整段视频大小。

    `.m3u8` 的 Content-Length 只有几 KB(它只是文本清单), 拿它当视频体积会让
    min_size/max_size 规则误杀整段视频。VOD 的分片通常大小接近, 采样头尾各一片
    求平均再乘总数就够准 —— 这里要的是量级, 不是字节级精确。
    """
    import requests

    if not segments:
        return None
    getter = session.head if session is not None else requests.head
    getter_get = session.get if session is not None else requests.get

    picked = []
    if len(segments) <= 2:
        picked = list(segments)
    else:
        picked = [segments[0], segments[-1]] if samples >= 2 else [segments[0]]

    sizes = []
    for url in picked:
        try:
            r = getter(url, headers=headers or {}, timeout=timeout,
                       allow_redirects=True)
            length = r.headers.get("Content-Length")
            # 有些 CDN 不回 Content-Length, 或干脆不支持 HEAD: 退化成 GET 一小段
            if not length:
                rg = dict(headers or {})
                rg["Range"] = "bytes=0-0"
                r = getter_get(url, headers=rg, timeout=timeout)
                cr = r.headers.get("Content-Range") or ""
                length = cr.split("/")[-1] if "/" in cr else None
            if length and str(length).isdigit():
                sizes.append(int(length))
        except Exception:
            continue
    if not sizes:
        return None
    avg = sum(sizes) / len(sizes)
    total = int(avg * len(segments))
    if log:
        log(f"按 {len(sizes)} 片采样估算整段约 {_human_bytes(total)}")
    return total


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _hhmmss(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
