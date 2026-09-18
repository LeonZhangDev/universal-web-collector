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
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    # 任务健壮性
    stale_task_timeout: int = 900
    watchdog_interval: int = 30
    # 限速与代理
    domain_concurrency: int = 3
    domain_min_interval: float = 0.5
    # 请求间隔随机区间: max > min 时在区间内随机取值, 否则固定用 min。
    # 相册采集建议 3~10 秒, 模拟人工浏览节奏。
    domain_max_interval: float = 0.0
    # 主 URL 失败后是否自动切换到备用下载点(mirrors)
    mirror_fallback: bool = True
    # 枚举探测(HEAD)的间隔: 探测是轻量请求, 不必套用下载级的慢速节奏。
    # 实测该站 0.2~0.3s 一次 HEAD 连续 60 次不会被限流。
    probe_interval: float = 0.3
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
    # ffmpeg_path 允许写相对项目根的路径(如 tools/ffmpeg.exe)
    if cfg.ffmpeg_path:
        fp = Path(cfg.ffmpeg_path)
        cfg.ffmpeg_path = str(fp if fp.is_absolute() else ROOT / fp)
    return cfg


settings = load()
