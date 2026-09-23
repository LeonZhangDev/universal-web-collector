import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent

# Accept 头不可省: 部分 WAF 会校验它, 只给 */* 或不给会直接 403
# (实测 img.xchina.io: 无 Accept 或 */* -> 403, 显式 image/* -> 200)
DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8"
)
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
# 视频资源用独立的 Accept: 声明接受 video/*, 同时保留 image/* ——
# 有些站点把视频封面与视频放在同一目录, 用同一套头最省心。
# ⚠️ 实测 img.xchina.io 的 .mp4 对**任何** Accept(含不带 Accept)都返回 206 video/mp4,
#    即"Accept 白名单"在该站只约束部分路径; 保留独立常量是为了对其它站点正确,
#    不要据此推断"Accept 无关紧要"。
VIDEO_ACCEPT = "video/mp4,video/webm,video/*;q=0.9,image/*;q=0.8,*/*;q=0.5"


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: Path = ROOT / "data" / "collector.db"
    download_dir: Path = ROOT / "downloads"
    browser_state_dir: Path = ROOT / "browser_state"
    max_task_workers: int = 2
    max_download_workers: int = 4
    image_retries: int = 3
    video_retries: int = 3
    request_timeout: int = 30
    # 下载专用: 拆成 (连接, 读取) 两个超时。单值会同时约束两者 —— 连接要快失败,
    # 而读取大文件/慢链路需要长得多, 用 30s 卡读取会把大视频中途掐断。
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    # 任务健壮性
    stale_task_timeout: int = 900
    watchdog_interval: int = 30
    # 目标盘的最低剩余水位: 低于它就停止下载剩余资源(core/disk.py)。
    # ⚠️ 满盘后再下是纯空转 —— 每个资源都会各自走完一整条重试链才失败。
    # 设 0 关闭该检查。
    min_free_bytes: int = 200 * 1024 * 1024
    # 限速与代理
    domain_concurrency: int = 3
    # 起始(也是固定模式下的)请求间隔。启用自适应后会被 AIMD 在下面两个界之间调整。
    domain_min_interval: float = 0.5
    # 请求间隔随机区间: max > min 时在区间内随机取值, 否则固定用 min。
    # 相册采集建议 3~10 秒, 模拟人工浏览节奏。
    domain_max_interval: float = 0.0
    # ---- 自适应节流(AIMD) ----
    # 只减不增的话, 站点被限过一次就永久卡在最慢档, 只能人工改配置恢复。
    # 所以"增"这一半必须有: 连续成功后缓慢收紧间隔, 出问题立即翻倍放宽。
    adaptive_throttle: bool = True
    # 增速能达到的最快间隔(地板)。别设太小 —— 提速省下的时间远不够赔一次封禁。
    domain_fast_interval: float = 0.1
    # 减速能到的最慢间隔(天花板), 防止单次抖动把节奏拖到不可用时。
    domain_slow_interval: float = 5.0
    # 令牌桶容量: 允许短簇突发。⚠️ 它**不改变长程平均速率**, 只是在攒下来的配额里
    # 花 —— 让"刚下完一个大文件"这类空档后面的几个请求不必干等。
    domain_burst: int = 3
    # ---- 全局字节速率上限(bytes/s, 0 = 不限) ----
    # ⚠️ 与 domain_min_interval 是**两个正交的维度**: 前者限"请求数/秒"(保护站点,
    # 防封禁), 这个限"字节/秒"(保护本机带宽)。图集站上 1000 张小图可以请求数很低
    # 却把带宽占满; 一个大视频则相反。想"边下边看视频"要压住的正是这一项。
    # 进程内全局共享一个桶 —— 按站点分会让"3 个任务 = 3 倍带宽", 那就不叫上限了。
    max_download_bytes_per_sec: int = 0
    # 主 URL 失败后是否自动切换到备用下载点(mirrors)
    mirror_fallback: bool = True
    # 枚举探测(HEAD)的间隔: 探测是轻量请求, 不必套用下载级的慢速节奏。
    # 实测该站 0.2~0.3s 一次 HEAD 连续 60 次不会被限流。
    probe_interval: float = 0.3
    # ---- 枚举快路径 ----
    # 相册页给出了数量时, 用**抽样校验过的区间**替代逐张探测:
    # 300 张图从 300 次 HEAD(~90s 纯等待)降到 ~8 次。
    enumeration_fast: bool = True
    # 快路径的抽样点数(含首尾)。抽样全过才敢跳过逐张探测。
    enumeration_samples: int = 6
    # ⚠️ 页面没给数量时是否用「指数探上界 + 二分」自己找边界。
    # 默认**关**: 它假定序号连续, 而"中间恰好缺一张"会让二分把上界定在缺口之前
    # —— 结果是 300 张只采到 4 张的**静默截断**, 比慢更糟。打开前先确认目标站点
    # 序号确实连续。
    enumeration_search: bool = False
    # m3u8 分片下载: 分片是同一段视频的连续片段, 不能套用图片的慢速节奏
    # (否则 100 片 x 6.5s ≈ 11 分钟)。但仍保留节流, 避免被 WAF 判为异常。
    segment_concurrency: int = 4
    segment_min_interval: float = 0.15
    segment_max_interval: float = 0.35
    segment_retries: int = 3
    # 站点级限速分组: 显式声明哪些域名属于同一站点, 共享一个限速器。
    # 例: {"xchina": ["xchina.io", "xchina.co"]}
    # 留空时按注册域自动推断: img.xchina.io 与 cdn.xchina.io 都归到 xchina.io。
    site_groups: dict = field(default_factory=dict)
    # 视频引擎:
    #   auto    有 ffmpeg 就用它, 失败自动降级内置分片器(默认)
    #   ffmpeg  强制 ffmpeg, 失败直接报错(不降级, 避免误以为走了 ffmpeg)
    #   builtin 强制内置分片器(不碰 ffmpeg)
    # ⚠️ 走 ffmpeg 时请求由 ffmpeg 自己发出, 本项目的限速器/DomainLimiter
    #    不参与, mirrors 也不会被轮换 —— 对限速敏感的站点请用 builtin。
    video_engine: str = "auto"
    # ffmpeg 可执行文件路径。留空则自动探测:
    # 显式配置 -> PATH -> 常见安装位置(winget/scoop/choco/手工解压目录),
    # 见 core/ffmpeg.py。环境变量 UWC_FFMPEG 覆盖。
    ffmpeg_path: Optional[str] = None
    proxy: Optional[str] = None
    # 产出物命名模板(见 core/naming.py 的变量表)。
    # 默认 `{name}` —— 与历史行为一致: 平铺在任务目录下, 文件名取 URL 末段。
    # 想要按站点/相册分目录, 改成 `{album}/{name}` 之类; 任务创建时用
    # options.name_template 可覆盖全局值。
    name_template: str = "{name}"
    # 每个任务跑完是否在输出目录写 manifest.json(溯源清单)
    write_manifest: bool = True
    # 订阅巡检: 调度线程检查到期订阅源的间隔(秒)
    watch_interval: int = 60
    extra: dict = field(default_factory=dict)


_PATH_FIELDS = {"db_path", "download_dir", "browser_state_dir"}

#: 带宽上限的单位后缀 -> 倍数。**按 1024 而不是 1000** —— 用户说"5MB/s"时
#: 心里的参照是文件管理器里显示的 MB(1024 进), 用 1000 会显得"限了还是超"。
_BPS_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
}


def parse_bytes_per_sec(value):
    """把 `5MB` / `5m` / `512k` / `1048576` 解析成 bytes/s; 认不出返回 0(不限速)。

    ⚠️ 认不出时退回"不限速"而**不是抛异常**: 带宽上限写错的表现必须是"没限住",
    不能是"服务起不来" —— 否则一个可选优化项会把整个程序挡在门外, 而用户根本
    想不到是那一行 yaml 的问题。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().lower().replace("/s", "").replace("ps", "")
    if not text:
        return 0
    num = ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            break
    unit = text[len(num):].strip()
    if unit not in _BPS_UNITS:
        return 0
    try:
        return max(0, int(float(num) * _BPS_UNITS[unit]))
    except (TypeError, ValueError):
        return 0


def load(path: Path = None) -> Config:
    cfg = Config()
    data = {}
    p = Path(path) if path else ROOT / "config.yaml"
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    for f in cfg.__dataclass_fields__:
        if f == "extra":
            continue
        if f in data:
            v = data[f]
            if f in _PATH_FIELDS:
                v = Path(v)
                if not v.is_absolute():
                    v = ROOT / v
            setattr(cfg, f, v)
    cfg.extra = {k: v for k, v in data.items() if k not in cfg.__dataclass_fields__}

    cfg.db_path = Path(os.environ.get("UWC_DB_PATH", cfg.db_path))
    cfg.download_dir = Path(os.environ.get("UWC_DOWNLOAD_DIR", cfg.download_dir))
    cfg.browser_state_dir = Path(
        os.environ.get("UWC_BROWSER_STATE_DIR", cfg.browser_state_dir)
    )
    cfg.host = os.environ.get("UWC_HOST", cfg.host)
    cfg.port = int(os.environ.get("UWC_PORT", cfg.port))
    cfg.proxy = os.environ.get("UWC_PROXY", cfg.proxy)
    cfg.video_engine = os.environ.get("UWC_VIDEO_ENGINE", cfg.video_engine)
    cfg.ffmpeg_path = os.environ.get("UWC_FFMPEG", cfg.ffmpeg_path)
    # 带宽上限: 允许写 "5MB"/"5m" 这类人话, 由解析器换算成 bytes/s
    if os.environ.get("UWC_MAX_BPS"):
        cfg.max_download_bytes_per_sec = parse_bytes_per_sec(
            os.environ["UWC_MAX_BPS"]
        )
    # ffmpeg_path 允许写相对项目根的路径(如 tools/ffmpeg.exe)
    if cfg.ffmpeg_path:
        fp = Path(cfg.ffmpeg_path)
        cfg.ffmpeg_path = str(fp if fp.is_absolute() else ROOT / fp)
    return cfg


settings = load()
