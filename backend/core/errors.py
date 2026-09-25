"""异常分类。

⚠️ 为什么要分类: 之前所有失败都是裸 `Exception`, 于是这三种完全不同性质的事
长得一模一样 ——

    1. 站点挂了 / URL 不对      (用户能在下一次重试中自行恢复)
    2. 磁盘满了 / 没有写权限    (环境问题, 继续跑只会空转)
    3. 我们自己写错了           (bug, 必须留堆栈才能查)

调用方只能靠 `str(e)` 去猜, 而**猜测往往发生在 try 块里**, 于是一错再错 ——
典型事故: `sqlite3.Row.get()` 抛的 AttributeError 落在业务 try 中, 被当成
"这个资源下载失败", 最后报成 `failed == filtered`。分类之后每种错有确定的去处。

第二层分类是给**程序**看的: 每类错带一个固定的 `kind` 取值(见文件末尾的
`classify()`), 写进 `resources.error_kind`。界面此前靠正则去解析 note 文案来
归类失败原因, 那是拿人看的字当数据用 —— 文案一改, 归类就静默失效。
"""

import errno
from pathlib import Path


class CollectorError(Exception):
    """外部世界的问题: 站点、URL、输入、环境。

    消息是**写给用户看的**, 所以必须回答"接下来怎么办"。
    这类错不打堆栈 —— 堆栈对用户没有意义, 只会把真正的原因挤到屏幕外。
    """


class TransientError(Exception):
    """可以重试的失败(连接中断、超时、5xx)。

    与 CollectorError 的区别在于**谁负责**: 这个交给重试逻辑,
    重试耗尽后才升级成 CollectorError 交给用户。
    """


class DiskFullError(CollectorError):
    """磁盘空间不足(或不可写)。

    ⚠️ 必须**中止整个任务**而不是跳过当前资源: 磁盘满了之后, 剩下的每个资源
    都会各自走完一整条重试链才失败 —— 几百个资源就是长时间空转, 而用户看到的是
    一堆毫无意义的 "No space left on device"。
    """


class GoneError(CollectorError):
    """资源在源站已经不可获取(4xx, 408/429 除外)。

    ⚠️ 与"下载失败"是**两件事**, 把它们混为一谈会同时错两处:

      1. **重试是纯浪费。** 404/410 的下一百次请求结果一样, 却要走满
         `image_retries` 次指数退避 —— 一张被删掉的图 ≈ 20 秒空转, 一个
         200 张的相册就是十几分钟。
      2. **会误伤整个站点。** 下载层的 `note_failure(url)` 是"这个站点吃不消了"
         的信号(见 `downloaders/ratelimit.py` 的 AIMD), 它会让该域名后续**所有**
         请求的间隔翻倍。而 403/404 是**这一个 URL** 的问题 —— 拿它去惩罚整个
         站点, 是最典型的误判, 症状是"加了几张失效图之后全站都变慢"。

    `status` 保留原始 HTTP 状态码, 供 `kind` 与文案区分"确实没有"和"不让访问"。
    """

    def __init__(self, status, url="", detail=""):
        self.status = int(status)
        #: 401/403/451 是"不让你访问", 其余 4xx 是"这东西不存在" —— 前者换代理
        #: 或改 headers 可能有用, 后者通常没救, 所以分开报。
        self.kind = KIND_FORBIDDEN if self.status in _FORBIDDEN_STATUS else KIND_GONE
        msg = f"源站返回 {self.status}"
        if detail:
            msg += f" ({detail})"
        if self.status in _FORBIDDEN_STATUS:
            msg += " —— 拒绝访问, 检查 Referer/Cookie/代理, 或该资源已下架"
        else:
            msg += " —— 资源已不存在(重试无用)"
        super().__init__(msg)


class CorruptMediaError(CollectorError):
    """字节下载"完整", 但内容不是有效媒体。

    ⚠️ 长度校验(Content-Length 对得上)只能证明**字节数**没少, 证明不了内容可用。
    能通过长度校验却仍然是坏文件的三种真实形态:

      * 中间设备/代理截断了响应体, 但保留了正确的 Content-Length;
      * CDN 返回了一个长度正确、内容是错误页或占位图的对象;
      * 服务器上的源文件本身已损坏(转码失败的半成品)。

    这三种都会以"下载成功"的身份落盘, 之后 sha256 去重、manifest、缩略图全都
    建立在一份坏字节上 —— 而且没有任何报错, 只有用户打开文件时才发现。

    判据分两级(见 `core/mediacheck.py`): 容器自述的尺寸/尾部标记是**算术级**
    证据(可以为它删文件); 解码器报错是**判断级**证据(只标记, 不删)。
    """

    def __init__(self, reason, path=""):
        self.kind = KIND_CORRUPT
        self.reason = reason
        msg = f"文件内容校验未通过: {reason}"
        if path:
            msg += f" ({Path(path).name})"
        super().__init__(msg)


# ---- 失败的机器可读分类 ----
#
# ⚠️ 为什么要有这个: 界面此前靠**正则解析 note 字符串**来归类失败原因
# (TaskDetail.vue 里的 /403|forbidden/、/timeout/ ...)。那是拿人看的文案当数据用:
# 文案一改归类就失效, 而且换个语言/措辞就全落进"其他"。有了固定取值之后,
# 前端只需一张 label 映射表, "按原因筛选/批量重试"也才可能实现。

KIND_GONE = "gone"              # 404/410: 源站已无此资源
KIND_FORBIDDEN = "forbidden"    # 401/403/451: 拒绝访问
KIND_CORRUPT = "corrupt"        # 字节完整但内容不可用
KIND_DISK = "disk"              # 磁盘满/不可写
KIND_RATELIMIT = "ratelimit"    # 429: 站点要求减速
KIND_SERVER = "server"          # 5xx: 站点自己出错
KIND_NETWORK = "network"        # 连接/超时/TLS
#: 库里有记录、磁盘上却没有(或长度变了)。与 gone/corrupt 的分工:
#: gone = 源站没了, corrupt = 字节坏了, missing = **我们本地这部分不见了**。
#: ⚠️ 单独一类是必要的: 用户看到"文件不见了"时的第一个反应是"程序把我的文件删了",
#: 而这三者的取证方向完全不同(查源站 / 查网络 / 查本地是谁动的)。
KIND_MISSING = "missing"        # 落盘后本地文件被外部删除
#: 扩展名声称的格式与文件头对不上(见 core/filekind.py)。⚠️ 与 corrupt **分开**:
#: corrupt = 字节下来了但解不开(重下有可能救), mismatch = 下来的是别的东西
#: (多半是源站把错误页/占位图当图片发了, 重下还是它)。两者混在一处会让
#: "内容损坏"这个数字没法解释。
#: ⚠️ 它由**巡检**写入(`/library/verify`), 资源的 status 仍是 done —— 所以
#: 它不会进死信重放队列(那条路径只收 failed/skipped/gone), 这是有意的:
#: 重下解决不了它。
KIND_MISMATCH = "mismatch"      # 名字与内容不符(重下也救不了)
KIND_UNKNOWN = "unknown"        # 分类之外 —— 通常是我们自己的 bug

#: 归属于"不让你访问"而非"不存在"的状态码。
_FORBIDDEN_STATUS = frozenset({401, 403, 407, 451})

_NETWORK_NAMES = frozenset({
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "ChunkedEncodingError", "IncompleteRead", "SSLError", "ProxyError",
    "ProtocolError", "NewConnectionError",
})


def classify(exc):
    """把一个异常映射成固定的 kind 取值, 写进 `resources.error_kind`。

    ⚠️ 用**名字**而不是 import 来识别 `RateLimited`/`HTTPError`: 那两类都定义在
    `downloaders/base.py`, 而本模块在依赖链的更底层(base 反过来 import 本模块)。
    在这里 import 它们会形成循环。按名字判断是本项目已有的做法(见 `is_retryable`)。
    """
    kind = getattr(exc, "kind", None)
    if kind:
        return kind
    if isinstance(exc, DiskFullError):
        return KIND_DISK
    if isinstance(exc, CorruptMediaError):
        return KIND_CORRUPT
    if isinstance(exc, GoneError):
        return exc.kind
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        # 没被包成 DiskFullError 的满盘(裸 OSError 从写入路径漏出来)
        return KIND_DISK
    name = type(exc).__name__
    # 4xx 已在下载层分流成 GoneError, 能走到这里的 HTTPError 基本都是 5xx
    if name in ("RateLimited",):
        return KIND_RATELIMIT
    if name == "HTTPError":
        return KIND_SERVER
    if name in _NETWORK_NAMES:
        return KIND_NETWORK
    return KIND_UNKNOWN


#: kind -> 给用户看的一句话。放后端而不是前端, 是为了让 API 与界面说的是同一套话。
KIND_LABELS = {
    KIND_GONE: "源站已无此资源",
    KIND_FORBIDDEN: "被拒绝访问",
    KIND_CORRUPT: "文件内容损坏",
    KIND_DISK: "磁盘空间不足",
    KIND_RATELIMIT: "被站点限速",
    KIND_SERVER: "源站服务端错误",
    KIND_NETWORK: "网络中断或超时",
    KIND_MISSING: "本地文件已丢失",
    KIND_MISMATCH: "名字与内容不符",
    KIND_UNKNOWN: "未知原因",
}


def is_retryable(exc):
    """这个异常值得重试吗(用于把裸异常升级成 TransientError)。"""
    if isinstance(exc, (CollectorError,)):
        # 用户/环境的问题, 重试多少次都一样
        return False
    if isinstance(exc, TransientError):
        return True
    name = type(exc).__name__
    return name in ("ConnectionError", "Timeout", "ReadTimeout", "ChunkedEncodingError")


def describe(exc):
    """给用户看的一句话: 分类异常自带人话, 其它异常补上类型名。

    ⚠️ 不要把 `str(e)` 原样丢给界面: 底层异常的消息常常是 "No space left on
    device" 这类只对开发者有意义的话, 用户需要的下一步动作一个字都没有。
    """
    if isinstance(exc, CollectorError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
