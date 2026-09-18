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
    collector: str = "generic"
    download_dir: Optional[str] = None  # 自定义输出目录(绝对路径), 留空用全局默认
    filters: Optional[FilterIn] = None
    quality: Optional[str] = None  # 图集采集器画质档: original / 1200 / 800 / 600


class TaskCreateOut(BaseModel):
    task_id: int
    status: str
