from typing import List, Optional

from pydantic import BaseModel


class TaskOut(BaseModel):
    id: int
    url: str
    collector: str
    status: str
    progress: int
    retry_count: int
    error: Optional[str] = None
    created_time: str
    download_dir: Optional[str] = None
    name: Optional[str] = None


class ResourceOut(BaseModel):
    id: int
    task_id: int
    type: str
    url: str
    local_path: Optional[str] = None
    hash: Optional[str] = None
    status: str
    file_url: Optional[str] = None
    size: Optional[int] = None
    note: Optional[str] = None


class TaskDetail(TaskOut):
    resources: List[ResourceOut]


class FilterIn(BaseModel):
    """资源过滤条件。全部字段可选, 留空表示该维度不限制。"""

    types: Optional[List[str]] = None          # 资源类型白名单
    exts: Optional[List[str]] = None           # 扩展名白名单
    exclude_exts: Optional[List[str]] = None   # 扩展名黑名单
    keywords: Optional[List[str]] = None       # URL 须包含其一
    exclude_keywords: Optional[List[str]] = None  # URL 包含任一则排除
    min_size: Optional[str] = None             # 最小体积, 如 "10KB" / "2MB"
    max_size: Optional[str] = None             # 最大体积


class TaskCreateIn(BaseModel):
    url: str
    # "auto" = 按 URL 自动识别; 库里存的永远是**解析后**的真实采集器名
    collector: str = "auto"
    download_dir: Optional[str] = None  # 自定义输出目录(绝对路径), 留空用全局默认
    filters: Optional[FilterIn] = None
    quality: Optional[str] = None  # 图集采集器画质档: original / 1200 / 800 / 600
    # 图集采集器采哪些媒体: auto(相册里有什么采什么) / image / video / both
    media: Optional[str] = None
    # 输出目录名的取值: clean(去掉站点尾巴的 <title>) / full(完整标题) /
    # h1(页面 <h1>, 信息通常更全) / id(图集 ID, 且完全不开浏览器)
    album_title: Optional[str] = None
    # 在相册名之外再套一层站点标签目录(如 丝袜-情趣内衣/相册名/...)
    album_tags_dir: Optional[bool] = None


class TaskCreateOut(BaseModel):
    task_id: int
    status: str
    # collector="auto" 时回填识别结论({collector, score, reason, ...}),
    # 显式指定采集器时为 None —— 前端据此显示"已识别为 X"
    resolved: Optional[dict] = None
