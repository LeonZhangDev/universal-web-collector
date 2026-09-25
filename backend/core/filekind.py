"""扩展名**声称**的格式, 与文件头**实际**的格式, 对不对得上。

与 `core/mediacheck.py` 的分工
==============================
两个模块答的是两个不同的问题, 混在一起会让每个数字都没法解释:

* `mediacheck` 答"这个文件**完不完整**"(容器长度算术);
* 本模块答"这个文件**是不是它自称的那个东西**"(magic sniff)。

Czkawka 把它做成独立的一种扫描模式("bad file extensions"), 是有道理的:
这两类问题**来源不同、后果也不同** —— 截断意味着这次下载失败了(重下能救),
而"名字叫错"多半是源站把错误页/占位图当成图片发过来了, 重下还是同一个东西。

最典型也最值得抓的一类
======================
CDN 回了一个 **HTML 错误页**, 但 `Content-Length` 是那个 HTML 的长度 ——
于是下载层的长度校验**通过**, 文件被当成图片落盘, 名字是 `00001.jpg`。
之后缩略图、dHash、manifest 全都建立在一份 HTML 上, 全程不报错; 只有用户
双击打开时才看到"文件已损坏"。这是本模块存在的第一理由。

判据的松紧
==========
和 `mediacheck` 同款取舍: **宁可漏报, 不可误报**。所以

* 不认识的扩展名 -> 不下结论;
* 认不出文件头 -> 不下结论;
* `ftyp` 家族(mp4 / mov / m4a / heic / avif)视为**同一族** —— 它们共用
  ISOBMFF 容器, 只靠前 12 字节区分不出来。若按 brand 细分, `.heic` 会被判成
  "其实是 mp4", 那是**误报**: 它本来就是那个文件, 只是我们没细看。

误报的代价是给一个好文件贴上"不对"的标签(虽然不删文件, 但会污染筛选与计数),
漏报只是少抓一个 —— 与 mediacheck 的取舍方向一致。
"""

from pathlib import Path

#: 读多少字节做 sniff。16 足够覆盖所有 magic(offset 4 的 ftyp、offset 8 的 WEBP)。
_HEAD = 16

#: 扩展名 -> 期望的格式族。⚠️ 只列**能确定**的; 不在这个表里的一律不下结论。
_EXT_KIND = {
    "jpg": "jpeg", "jpeg": "jpeg", "jpe": "jpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
    "bmp": "bmp",
    "tif": "tiff", "tiff": "tiff",
    "ico": "ico",
    # ISOBMFF 一族: 容器相同, 靠 brand 细分会误报, 所以统一成 "isobmff"
    "mp4": "isobmff", "m4v": "isobmff", "mov": "isobmff", "m4a": "isobmff",
    "heic": "isobmff", "heif": "isobmff", "avif": "isobmff",
    "webm": "matroska", "mkv": "matroska",
    "mp3": "mp3",
    "wav": "wav",
    "flac": "flac",
    "ogg": "ogg", "oga": "ogg", "ogv": "ogg",
    "pdf": "pdf",
    "zip": "zip",
    "rar": "rar",
    "7z": "7z",
}

#: 格式族 -> 中文名(给人看的说明里用)。文案在后端, 前端只拿键。
_KIND_LABEL = {
    "jpeg": "JPEG 图片", "png": "PNG 图片", "gif": "GIF 图片",
    "webp": "WebP 图片", "bmp": "BMP 图片", "tiff": "TIFF 图片",
    "ico": "ICO 图标", "isobmff": "MP4/MOV/HEIC 类容器",
    "matroska": "WebM/MKV 视频", "mp3": "MP3 音频", "wav": "WAV 音频",
    "flac": "FLAC 音频", "ogg": "OGG 媒体", "pdf": "PDF 文档",
    "zip": "ZIP 压缩包", "rar": "RAR 压缩包", "7z": "7z 压缩包",
    "html": "网页/HTML 文本",
}


def ext_kind(name_or_path):
    """扩展名声称的格式族; 认不出(或不该管)时返回 None。"""
    if not name_or_path:
        return None
    text = str(name_or_path)
    dot = text.rfind(".")
    if dot < 0:
        return None
    ext = text[dot + 1:].strip().lower()
    return _EXT_KIND.get(ext)


def sniff_kind(path):
    """读文件头判断实际格式族; 认不出时返回 None(不下结论)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(_HEAD)
    except OSError:
        return None
    if not head:
        return None

    # ---- 网页文本: 排在最前面, 因为它是最常见也最要紧的一类误存 ----
    # 只认前 16 字节里出现的明文开头, 不做大小写敏感的整文件扫描 ——
    # 那是"猜"而不是"读"。
    probe = head[:16].lstrip().lower()
    if probe.startswith(b"<!doctype html") or probe.startswith(b"<html"):
        return "html"

    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if head[4:8] == b"ftyp":
        # mp4 / mov / m4a / heic / avif 共用这个容器; 细分靠 brand, 而 brand
        # 的取值在不断扩(avif/heic/mif1/msf1...)。细分只会带来误报, 不细看
        # 只会漏报 —— 按既定取舍, 停在这一层。
        return "isobmff"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"):
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06", b"\x07\x08"):
        return "zip"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:4] == b"7z\xbc\xaf":
        return "7z"
    return None


def inspect(path):
    """返回 (声称, 实际); 任一侧认不出时那一侧是 None。

    调用方据此判断: 两侧都认得出且不等 -> 名字与内容不符。
    """
    claimed = ext_kind(path)
    actual = sniff_kind(path)
    return claimed, actual


#: 格式族 -> 建议用的扩展名(改名时用)。与 `_EXT_KIND` 反向, 但只取**一个代表**。
#:
#: ⚠️ 故意**不含 `isobmff`**: mp4 / mov / m4a / heic / avif 共用同一个容器,
#: 只靠前 12 字节区分不出来。把一个 `.heic` 改成 `.mp4` 是**误判**(它本来就是
#: 那个文件, 只是我们没细看), 而漏掉一次改名只是少修一个。与上面 sniff 的
#: 取舍方向一致 —— 拿不准就别动, 尤其改名是**会落到磁盘上**的动作。
#:
#: `html` 在表里: 那是 CDN 把错误页当图片发过来了, 改名成 `.html` 才是把问题
#: **暴露**出来(用户双击就知道这不是图), 继续叫 `.jpg` 只会让损坏的文件
#: 一直混在图库里。
KIND_PRIMARY_EXT = {
    "jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp",
    "bmp": "bmp", "tiff": "tif", "ico": "ico", "matroska": "mkv",
    "mp3": "mp3", "wav": "wav", "flac": "flac", "ogg": "ogg",
    "pdf": "pdf", "zip": "zip", "rar": "rar", "7z": "7z",
    "html": "html",
}


def suggest_ext(path):
    """按文件头给出**建议的新扩展名**(不带点); 不该改/改不了时返回 None。

    只在"两侧都认得出且不相等"时给结论 —— 认不出的一侧说明我们没有把握,
    这时动手改名就是用猜测覆盖用户磁盘上的文件。
    """
    claimed, actual = inspect(path)
    if not claimed or not actual or claimed == actual:
        return None
    return KIND_PRIMARY_EXT.get(actual)


def suggest_rename(path):
    """按文件头给出建议的新**文件名**; 不该改时返回 None。

    只换扩展名, 不动主名 —— 主名里有序号(`00001`)与来源信息, 换了会破坏
    "按名字找文件"这条用户习惯。
    """
    ext = suggest_ext(path)
    if not ext:
        return None
    p = Path(path)
    return f"{p.stem}.{ext}"


def mismatch_reason(path):
    """名字与内容不符时返回**给人看**的说明; 相符或无法判断时返回 None。

    ⚠️ 返回的文案会进 `resources.note`, 所以要说清"声称是什么 / 实际是什么" ——
    只说"格式不符"的话, 用户没法判断下一步该干什么。
    """
    claimed, actual = inspect(path)
    if not claimed or not actual or claimed == actual:
        return None
    name = Path(path).name if path else ""
    return (
        f"扩展名声称是 {_KIND_LABEL.get(claimed, claimed)}, "
        f"文件头实际是 {_KIND_LABEL.get(actual, actual)}"
        + (f"({name})" if name else "")
    )
