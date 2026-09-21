from typing import List, Optional

from pydantic import BaseModel, ConfigDict


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
    # 采集器建议的文件名(相对任务输出目录)与实际生效的下载点
    filename: Optional[str] = None
    resolved_url: Optional[str] = None
    content_type: Optional[str] = None
    # 感知指纹与"疑似重复"(见 core/phash.py)。duplicate_of 指向**同一个任务内**
    # 的另一条资源 id —— 文件仍在磁盘上, 这只是给人看的标记, 前端不要据此隐藏
    # 或删除任何东西。
    phash: Optional[str] = None
    duplicate_of: Optional[int] = None


class TaskDetail(TaskOut):
    resources: List[ResourceOut]


class FilterIn(BaseModel):
    """资源过滤条件。全部字段可选, 留空表示该维度不限制。

    ⚠️⚠️ 这里必须与 `core.filters.Filters` 认识的键**保持一致**, 并且允许额外键。

    曾经踩过的坑: 本类只声明了前 8 个字段, 于是前端传上来的 `min_width` /
    `min_height` / `exclude_ad` / `min_image_bytes` 被 pydantic **静默丢弃**
    (默认行为是忽略未知字段)。表现是界面上的「尺寸下限」「排除广告位」开关
    按了没有任何效果 —— 没有报错、没有日志, 用户只会以为"这个功能没用"。
    实测确认: `FilterIn(min_width='300').model_dump()` 返回 `{}`。

    所以两条一起做:
    ① 把已知键全部声明出来(界面用得到的有类型、有文档);
    ② `extra="allow"` 兜底 —— 以后 `Filters` 再加一个维度, 老后端/新前端
       混跑时不会又把参数吞掉。`Filters` 只读它认识的键, 多余键无害。
    """

    model_config = ConfigDict(extra="allow")

    types: Optional[List[str]] = None          # 资源类型白名单
    exts: Optional[List[str]] = None           # 扩展名白名单
    exclude_exts: Optional[List[str]] = None   # 扩展名黑名单
    keywords: Optional[List[str]] = None       # URL 须包含其一
    exclude_keywords: Optional[List[str]] = None  # URL 包含任一则排除
    min_size: Optional[str] = None             # 最小体积, 如 "10KB" / "2MB"
    max_size: Optional[str] = None             # 最大体积
    # 图片专属体积下限: 挡 1x1 跟踪像素 / 广告占位图
    min_image_bytes: Optional[str] = None
    # 广告位/站点装饰识别(按路径分段精确匹配, 绝不子串)
    exclude_ad: Optional[bool] = None
    # 像素下限(仅图片, 下载后量宽高) —— 横幅/按钮/信标的最强判别特征
    min_width: Optional[str] = None
    min_height: Optional[str] = None
    min_pixels: Optional[str] = None
    # 感知去重(见 core/phash.py): 只标记不删除。默认开。
    dedup_perceptual: Optional[bool] = None
    dedup_threshold: Optional[int] = None


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
    # 聚合页采集器(xchina_aggregate): 一次最多展开多少个条目(相册 + 视频页)。
    # 模特页可能挂几十个相册、索引页挂着上百个模特 —— 不设闸门就是几万条资源。
    max_items: Optional[int] = None
    # 聚合页采集器: 还能再往下钻几层。默认 1(落地页 -> 它的相册);
    # 索引页要连模特一起展开时才需要 2。
    aggregate_depth: Optional[int] = None


class TaskCreateOut(BaseModel):
    task_id: int
    status: str
    # collector="auto" 时回填识别结论({collector, score, reason, ...}),
    # 显式指定采集器时为 None —— 前端据此显示"已识别为 X"
    resolved: Optional[dict] = None
    # **软**提示: 任务已创建, 只是提醒用户多半粘错了输入(见 api.tasks._manual_warning)。
    # 不要当错误显示 —— 界面一旦用红色报错的样子呈现它, 用户会以为创建失败了。
    warning: Optional[str] = None
