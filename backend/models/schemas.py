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
    # 失败的机器可读分类(gone/forbidden/corrupt/disk/ratelimit/server/network/
    # unknown/missing, 见 core/errors.py)。前端用它归类失败原因、画分布图 —— 不再靠正则
    # 解析 note 文案(那是拿人看的字当数据用, 文案一改就静默失效)。
    # ⚠️ 注意它也出现在**成功**资源上: error_kind='corrupt' + status='done' 表示
    # "文件保留了, 但解码器说它可能坏了"(见 core/phash.py 的约束 1/4)。
    error_kind: Optional[str] = None
    # ---- 资源级遥测 ----
    # 起始/结束时刻(epoch 秒)与"真正发起过几次下载"。前端据此显示一条资源的
    # 耗时, 详情页再聚合成"最慢的几条" —— 没有它, 慢只能看到一个任务总时长,
    # 而"120 张各 1 秒"与"119 张各 0.2 秒 + 1 张卡 95 秒"的处置完全不同。
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    attempts: Optional[int] = None
    # 条件请求凭据(源站给的 ETag / Last-Modified)。界面上不必显示, 但排查
    # "内容为什么没更新 / 为什么走了 304"时要看得到, 所以随详情一并下发。
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    # 媒体元数据: 图片实测宽高 / 视频容器时长(秒)。
    # ⚠️ 必须在这里显式声明 —— pydantic 只回传声明过的字段, 漏一个就静默消失
    # (见 PITFALLS 第 5 条: "pydantic 吞字段")。视频那条在没装 ffprobe 时
    # 就是 NULL, **不代表文件坏**, 前端不要显示成"0 秒"。
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None


class ResourceTimingItem(BaseModel):
    id: int
    type: str = ""
    status: str = ""
    name: str = ""
    ms: float = 0.0
    attempts: int = 0


class ResourceTiming(BaseModel):
    """任务的逐资源耗时画像(见 database.resource_timing)。"""

    measured: int = 0        # 有完整起止时刻的资源数(未完成的没算进来)
    avg_ms: float = 0.0
    max_ms: float = 0.0
    retried: int = 0         # 尝试次数 > 1 的资源数: "平均很快但一直在重试"是另一种病
    slowest: List[ResourceTimingItem] = []


class ResourceCounts(BaseModel):
    total: int
    done: int
    failed: int
    filtered: int
    # "源站已无此资源"的条数。⚠️ 它**已经包含在 failed 里**(见
    # database.summarize_resources 的口径说明), 这里只是让界面能单独说一句
    # "其中 N 张是源站已经删了" —— 那一类不用重试。
    gone: int = 0


class TaskDetail(TaskOut):
    resources: List[ResourceOut]
    resource_counts: ResourceCounts
    # 逐资源耗时画像。放在详情里而不是单开接口: 它只在打开详情页时用得上,
    # 多一次往返只会让详情加载更慢。
    resource_timing: Optional[ResourceTiming] = None
    # 失败分类的中文标签(kind -> 文字)。随详情一起下发而不是让前端硬编码:
    # 前端只用它做展示, 而"gone 该显示成什么"是后端词汇表的一部分 —— 两处各写
    # 一套迟早对不上, 而失败措辞正是用户判断"要不要重试"的依据。
    error_kind_labels: dict = {}


# ---- 资源库批量操作 ----
# 资源库此前只读浏览。"看到那张不要的图, 得先想起它在哪个任务里、再点进那个
# 任务去删" —— 而资源库恰恰是"我手上有什么"的视角, 批量选择是这个视角下
# 最自然的动作。

class LibraryBulkDeleteIn(BaseModel):
    """删除资源库条目。

    ⚠️ `with_files` 默认 False: 删记录**不动文件**。真删文件是不可逆的, 而资源库
    里同一张图可能被多个任务引用(去重复用), 用户点"删除"时心里想的多半是
    "这条记录别显示了"。要连文件一起删必须显式打开, 且后端还会按 refs 复查。
    """

    ids: List[int]
    with_files: bool = False


class LibraryBulkOut(BaseModel):
    requested: int = 0
    deleted: int = 0
    files: int = 0            # 真正从磁盘删掉的文件数
    bytes: int = 0
    skipped: List[int] = []   # 不存在/已被别处删掉的 id
    # 文件被别的任务引用而**保留**的条目 id —— 需要明确告诉用户"文件还在",
    # 否则他会以为删干净了, 下次在别的任务里又看到同一张图, 变成"删了没用"。
    kept_files: List[int] = []
    # id(str) -> 出错原因。删除是**不可逆**的操作, 失败绝不能静默吞掉 ——
    # 用户看到"删了 3 个"而实际只删掉 2 个, 下次再看到那张图会以为程序有毛病。
    errors: dict = {}


class LibraryVerifyIn(BaseModel):
    """落盘后完整性巡检。

    ⚠️ 默认只查**有记录**的那些(status='done'): 库里说"下好了"、磁盘上却不在
    (或长度对不上), 是唯一需要用户知道的情况。失败/已删的行没有文件可核,
    把它们算进分母只会让"缺失率"这个指标失去意义。
    """

    task_id: Optional[int] = None   # 只查某个任务; 留空 = 全库
    limit: int = 500                # 单次扫描上限(大库要分批, 别把接口挂住)


class LibraryVerifyItem(BaseModel):
    id: int
    task_id: int
    name: str = ""
    path: Optional[str] = None
    kind: str = ""            # missing | corrupt
    reason: Optional[str] = None


class LibraryVerifyOut(BaseModel):
    checked: int = 0
    missing: int = 0
    truncated: int = 0
    #: 被标记的条目数。**只标记不删除** —— 与 phash / mediacheck 的一贯原则一致:
    #: 判据可能误报(尤其是"文件被外部程序改小了"这种), 删文件是不可逆的。
    marked: int = 0
    items: List[LibraryVerifyItem] = []


class FailureKindItem(BaseModel):
    kind: str
    #: 中文标签由**后端下发**(见 core/errors.KIND_LABELS)。界面不许硬编码 ——
    #: 否则后端换个措辞, 前端那份就静默对不上, 又会有人回头去正则解析文案。
    label: str = ""
    #: 这个原因下有多少条。含 `corrupt` 那些 status='done' 的(文件在但解码器说坏)。
    n: int = 0
    #: 其中**重放真的能救**的条数。⚠️ 必须与 n 一起下发: 只有 n 的话, 界面会拿它
    #: 当"即将重放多少条"来提示, 于是 `corrupt` 那一类"提示 12 条、起来 0 条",
    #: 用户只会以为点了没生效。0 就是 0, 该灰掉就灰掉。
    replayable: int = 0


class LibraryFailuresOut(BaseModel):
    """跨任务的"死信"视图: 失败资源按 `error_kind` 的分布 + 最近的一批明细。

    `total` 与 `items` 的口径**不同源**: total 来自 `failure_kinds`(含 corrupt),
    items 来自 `failure_refs`(只含可重放的那批)。前者是"出了什么问题", 后者是
    "现在能做什么" —— 两个问题, 两个数字。
    """

    total: int = 0
    kinds: List[FailureKindItem] = []
    items: List[ResourceOut] = []
    #: 当前库里能重放的条数(受 limit 限制前)。前端据此决定是否提示"还有更多"。
    replayable: int = 0


class LibraryReplayIn(BaseModel):
    """按 `refs`(指定资源) 或 `kinds`(按失败原因整批) 重放死信。

    ⚠️ 两个都给时取**交集**(refs 里 error_kind 属于 kinds 的那些), 而不是并集:
    并集会让"我在这个原因里勾了几条"变成"整个原因全下", 一次点击就是几百个请求。
    """

    refs: Optional[List[int]] = None
    kinds: Optional[List[str]] = None
    #: `gone`(源站已删/下线)默认不重放 —— 自动重放它是纯空转。
    include_gone: bool = False
    #: 单次上限。这是保护: 死信往往是几百条, 一次全放出去会同时打死站点和本机。
    limit: int = 200


class LibraryReplayOut(BaseModel):
    requested: int = 0
    submitted: int = 0
    skipped: int = 0
    #: 人可读的跳过说明(如"任务 #12 正在运行")。**必须有** —— 用户点了"重放 30 条"
    #: 却只起来 4 条, 不解释就等于让他以为程序吞了 26 条。
    notes: List[str] = []


class TaskListOut(BaseModel):
    """任务列表(分页)。

    ⚠️ 不再是裸数组: 分页必须知道总数才能算有几页, 而总数不该靠前端把全量
    数据拉回去自己数 —— 那正是分页要避免的事。
    """

    items: List[TaskOut]
    total: int        # 满足筛选条件的总数(不是本页条数)
    page: int
    page_size: int
    pages: int


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
    # 任务级代理(可选)。优先于全局 UWC_PROXY。存入 options, 由下载层读取;
    # 留空则沿用全局/环境变量配置。前端"高级设置"里可填。
    proxy: Optional[str] = None
    # Opt-in: existing callers retain the historical always-create behavior.
    deduplicate: bool = False
    # Explicit confirmation path for creating after a duplicate disposition.
    force_new: bool = False
    # Re-downloads may reuse successful resources from earlier tasks.
    incremental: Optional[bool] = None
    # 下载优先级 —— 决定资源**提交给下载池的顺序**(固定大小线程池按提交序取任务,
    # 所以提交顺序就是事实上的优先级)。取值见 task_manager.RESOURCE_ORDERS:
    #   original    采集原序(默认, 与历史行为一致)
    #   video_first 视频先行(它最慢、最容易被限速拖成长尾)
    #   small_first 已知体积的从小到大(只有视频会自报体积, 图片要下完才知道)
    # ⚠️ 必须在这里声明: 本模型没有 extra="allow", 不声明的键会被 pydantic 静默
    # 丢掉 —— 界面上选了"视频优先"却毫无效果, 且不报错(第 5 条静默坑)。
    resource_order: Optional[str] = None


class TaskCreateOut(BaseModel):
    task_id: int
    status: str
    # collector="auto" 时回填识别结论({collector, score, reason, ...}),
    # 显式指定采集器时为 None —— 前端据此显示"已识别为 X"
    resolved: Optional[dict] = None
    # **软**提示: 任务已创建, 只是提醒用户多半粘错了输入(见 api.tasks._manual_warning)。
    # 不要当错误显示 —— 界面一旦用红色报错的样子呈现它, 用户会以为创建失败了。
    warning: Optional[str] = None
    disposition: Optional[str] = None
    content_key: Optional[str] = None


# ---- 批量创建 ----
# 一次粘贴几十条 URL 是常态, 而粘贴里几乎必然混着空行、重复行和"不是链接"的行。
# 逐条调用单创建接口也能做, 但那样重复与无效要等任务真跑起来才暴露 —— 用户会
# 看到 40 个任务里 12 个失败, 还得自己猜哪 12 个是粘错了。所以批量接口在
# **创建之前**就把每一行的结论算出来, 让用户在按下"开始"之前就能看见。

class BatchTaskIn(BaseModel):
    # 原始输入行(未清洗)。保留原始值是为了让用户能对照自己粘贴的内容 ——
    # 后端悄悄 trim 之后, 用户看到的"第 3 行无效"会对不上他自己的第 3 行。
    urls: List[str]
    collector: str = "auto"
    download_dir: Optional[str] = None
    filters: Optional[FilterIn] = None
    quality: Optional[str] = None
    media: Optional[str] = None
    album_title: Optional[str] = None
    max_items: Optional[int] = None
    aggregate_depth: Optional[int] = None
    proxy: Optional[str] = None
    # 下载优先级(提交顺序)。取值同 TaskCreateIn.resource_order ——
    # 批量创建与单条创建共用 `_gallery_options` 的校验, 选项语义必须一致。
    resource_order: Optional[str] = None
    # 已存在相同 URL 的任务时是否仍然创建。默认否 —— 批量粘贴最常见的失误就是
    # 同一批粘了两次, 默认重下会把几百张图再下一遍。前端要能显式打开它,
    # 因为"上次失败了想重下"是合理诉求。
    allow_duplicates: bool = False


class BatchTaskItemOut(BaseModel):
    line: int               # 第几行(1-based), 便于对照原始输入
    raw: str                # 原始输入(未 trim), 用户要能看清自己粘了什么
    url: Optional[str] = None   # 清洗后的值; 被拒时可能为 None
    ok: bool                # 是否已创建
    task_id: Optional[int] = None
    # 被拒原因: empty / invalid / duplicate_in_batch / duplicate_existing / too_many
    reason: Optional[str] = None
    # 给用户看的一句话说明, 不是异常堆栈
    message: Optional[str] = None
    collector: Optional[str] = None
    # 已存在同 URL 的任务 id(duplicate_existing 时给出, 前端可链过去)
    existing_task_id: Optional[int] = None


class LibraryTagsIn(BaseModel):
    """批量打标签 / 去标签(可同时进行)。

    `add` 与 `remove` 放在**同一个请求**里而不是拆成两个接口: 前端"改标签"这个
    动作天然是一次编辑 —— 拆开会让"加了新的、去掉旧的"出现中间态, 中途失败就
    留下一个用户没要过的组合。校验不过时整个请求失败, 不做"能加的加上"。

    空 `remove` 且 `add` 非空 = 只加; 空 `add` 且 `remove` 非空 = 只删;
    **两者都空 = 什么都不做**(不是"清空标签" —— 清空要显式传 `clear=True`,
    免得一个笔误把标签全抹掉)。
    """

    ids: List[int]
    add: List[str] = []
    remove: List[str] = []
    clear: bool = False           # True 时先清空这些资源的全部标签


class LibraryTagsOut(BaseModel):
    added: int = 0
    removed: int = 0
    touched: int = 0              # 涉及的资源条数


class TagColorIn(BaseModel):
    """设置 / 清除一个标签的颜色。

    `color` 是**调色板键**(red/orange/... 由后端下发), 空串 = 清除。
    颜色是标签自己的属性, 不随资源增删而丢 —— 用户重新打上同一个标签时它还在。
    """

    tag: str
    color: str = ""


class TagColorOut(BaseModel):
    tag: str = ""
    color: str = ""               # 空串 = 已清除


class LibraryFavoriteIn(BaseModel):
    ids: List[int]
    value: bool = True            # False = 取消收藏


class LibraryFavoriteOut(BaseModel):
    updated: int = 0
    value: bool = True


class BulkActionIn(BaseModel):
    """批量操作: 对一组 task_id 执行同一个动作。

    action ∈ {pause, resume, cancel, retry, delete}。动作本身的可执行性由各
    task_manager 方法判断(例如对已完成任务 pause 返回 409), 这里只负责批量派发
    并把每个任务的结果归类, 让前端一次性知道"哪些成了、哪些被跳过"。
    """

    action: str
    task_ids: List[int]
    with_files: bool = False  # 仅 delete 生效: 同时删除已下载文件


class BulkActionOut(BaseModel):
    action: str
    requested: int
    ok: List[int] = []
    skipped: List[int] = []       # 任务存在但动作不适用(如 pause 已完成任务 → 409)
    not_found: List[int] = []     # 任务不存在
    errors: dict = {}             # task_id(str) -> 错误信息


class RetryFailedOut(BaseModel):
    """失败资源选择性重试: 只把 failed/skipped 的资源重新派发下载。"""

    task_id: int
    retried: List[int] = []
    count: int = 0


class TaskStatsOut(BaseModel):
    """首页统计面板用: 总量 + 按状态 + 按采集器 + 资源总量与体积。"""

    total_tasks: int = 0
    total_resources: int = 0
    total_bytes: int = 0
    by_status: dict = {}
    by_collector: dict = {}
    active: int = 0
    warning: Optional[str] = None
    by_date: dict = {}            # 资源按日期新增数(近 30 天, 供折线图)
    download_series: dict = {}     # 已下载资源体积按日期(字节)
    failure_reasons: List[dict] = []  # [{reason, count}] 失败原因聚合(按 note 全文)
    # [{kind, count}] 按**固定的失败分类**聚合。与 failure_reasons 的分工: 那个说
    # "具体报了什么"(每条 note 带各自的 URL, 几乎每组只有 1 条), 这个说"哪一类问题
    # 最多", 是"该换代理还是该改采集器"的可靠依据。kind 的取值见 core/errors.py。
    error_kinds: List[dict] = []
    # kind -> 中文标签。由后端提供而不是前端硬编码: 否则"接口里叫 gone、界面写
    # '源站已无'"这种两处维护的措辞迟早会对不上(改一处忘一处), 而失败的措辞正是
    # 用户判断"要不要重试"的依据。
    error_kind_labels: dict = {}
    duplicates: dict = {}          # {marked, bytes_saved} 感知去重报表


class NotificationOut(BaseModel):
    id: int
    task_id: Optional[int] = None
    level: str = "info"       # info | success | warning | error
    title: str
    body: Optional[str] = None
    read: bool = False
    created_time: Optional[str] = None


class NotificationListOut(BaseModel):
    items: List[NotificationOut] = []
    unread: int = 0


class MarkReadIn(BaseModel):
    ids: Optional[List[int]] = None


class BatchTaskOut(BaseModel):
    # 按输入顺序返回**全部**行的结论, 不做分组 —— 用户是按粘贴的顺序在读,
    # 分成"成功/失败"两堆反而要对回去数第几行。
    items: List[BatchTaskItemOut]
    created_count: int
    rejected_count: int
    # 超过单次上限被整行丢弃的数量(见 api.tasks.BATCH_MAX_URLS)
    truncated_count: int
