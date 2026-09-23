# Universal Web Collector 项目介绍

更新日期：2026-09-22。软件版本为 `0.10.0`，应用名称为 Universal Web Collector v10；仓库目录中的 v9 为历史名称。

## 项目定位

Universal Web Collector 是一个面向个人使用的网页资源采集与下载管理平台。用户通过 Web 界面输入网址，系统选择采集器、发现资源、执行过滤与下载，并展示进度、产出和错误原因。

当前重点适配 XChina 相册、视频和聚合内容页，同时提供普通网页的通用媒体发现能力。通用采集器主要处理当前页面加载或引用的资源，不能保证任意网站的全部内容都能采集。项目以规则、HTTP 请求和浏览器自动化驱动，目前没有接入大模型推理。

## 已实现能力

| 能力 | 当前实现 |
| --- | --- |
| Web 控制台 | Vue 3 页面，任务列表、进度、日志、资源图库（列表/网格切换）、清单查看、ZIP 导出和大图预览 |
| 自动识别 | 根据 URL 形态匹配采集器，回显结果，允许手动覆盖 |
| 通用资源发现 | Playwright 结合 API 响应、网络请求、JS 状态和 DOM 提取资源 |
| 资源类型 | 图片、视频、音频、文档和网页文本，发现范围取决于页面和采集器 |
| 相册采集 | 图集 ID 解析、HTTP 序号枚举、CDN 基址与序号格式探测、画质和媒体类型选择 |
| 视频采集 | MP4、HLS/m3u8；ffmpeg 与内置分片下载器；签名过期和占位播放列表检查 |
| 聚合采集 | 从模特、演员、系列或索引页发现入口，再委派给相册、视频采集器；限制深度和条目数 |
| 创建前预览 | 专用采集器提供标题、数量或体积；抽样数量明确标为下限 |
| 批量创建 | 多行粘贴或拖入 `.txt`，创建前逐行预检重复与无效输入并给出逐条结论 |
| 任务检索 | 按 URL/名称搜索、按状态组/采集器筛选、分页，返回总数与页数 |
| 任务操作 | 暂停、继续、停止、任务重试、资源级重试、失败资源选择性重试、批量操作与批量清理 |
| 下载预设 | 内置「仅图片/高质量视频/全量」并支持另存自定义；可导出/导入 JSON 分享 |
| 过滤与去重 | 类型、扩展名、关键词、体积、图片尺寸和广告路径过滤；SHA-256 去重和 dHash 疑似重复标记（附去重报表） |
| 统计面板 | 任务量与产出概览、近 30 天资源新增折线、采集器分布饼图、失败原因聚合 |
| 任务通知 | 结算时写入通知中心（成功/部分/失败），可选 SMTP 推送 |
| 环境诊断 | 检测 Python、浏览器、ffmpeg、磁盘与下载目录可用性，给出修复提示 |
| 增量与订阅 | 复用历史已下载资源；订阅源按间隔创建巡检任务，展示上次运行时间 |
| 登录态 | 打开可见浏览器手动登录，保存并复用站点浏览器状态 |
| 任务级代理 | 创建时填写代理，可写多个（逗号分隔）按资源轮换，注入下载会话 |
| 下载可靠性 | 并发限制、重试、429 冷却、自适应限速、备用地址、断点续传、原子落盘和磁盘检查 |
| 中断恢复 | 心跳、看门狗和启动补偿恢复异常任务状态 |

感知去重只标记疑似重复，不自动删除文件。继续任务复用已有资源清单，重试任务重新采集。站点验证、签名时效和页面结构变化仍可能导致失败，降级策略不代表可以稳定通过站点验证。

## 键盘快捷键

| 按键 | 作用 |
| --- | --- |
| `/` | 聚焦任务搜索框 |
| `j` / `k`（或上下方向键） | 上下移动高亮任务 |
| `Enter` | 打开高亮任务的详情 |
| `Space` | 暂停 / 继续高亮任务 |
| `Esc` | 关闭任务详情抽屉 |
| 灯箱内 `+` / `-` / `0` / `R` | 放大、缩小、复位、旋转 |


## 架构与技术栈

```text
Vue 3 控制台
    ↓ HTTP API / SSE 事件
FastAPI → TaskManager → Collector → 标准资源记录
                            ↓
                    过滤 / Downloader
                            ↓
                 本地媒体文件 + JSON 清单
                            ↕
              SQLite 任务 / 资源 / 日志 / 订阅
```

Collector 负责发现资源，Downloader 负责下载，TaskManager 负责调度与状态。专用采集器可以直接使用 HTTP，通用页面主要通过 Playwright 处理，并非每个任务都需要浏览器。

| 路径 | 职责 |
| --- | --- |
| `backend/main.py` | FastAPI 入口、启动恢复、异常处理、托管前端构建产物 |
| `backend/api/` | 任务、文件、目录选择、订阅、通知、统计和环境诊断接口 |
| `backend/collectors/` | 插件注册、通用解析、相册枚举和站点适配 |
| `backend/downloaders/` | 图片、视频、文件、文本下载、限速和代理会话 |
| `backend/core/` | 状态机、数据库、事件、过滤、存储布局、清单、通知和恢复 |
| `backend/models/` | API 输入输出模型 |
| `frontend/src/` | Vue 界面、任务表格、详情抽屉、统计面板、通知中心与样式 |
| `scripts/` | 启动、自检、探针和端到端验证 |
| `tests/` | 自动化测试和隔离检查 |

技术栈：Python 3.11+、FastAPI、Pydantic、Requests、Playwright、SQLite，以及 Vue 3、Vite、Axios。Python 依赖由 uv 管理；ffmpeg 用于视频处理和感知指纹计算。

## 存储与部署边界

配置位于 [config.yaml](../config.yaml)。默认数据库为 `data/collector.db`，浏览器状态位于 `browser_state/`，下载目录可按任务指定。

当前布局由 [backend/core/layout.py](../backend/core/layout.py) 统一定义：

```text
downloads/
  相册名称/00001.jpg
  视频原名或图集ID.mp4
  _meta/任务ID/manifest.json
  _meta/任务ID/album.json
```

图片等非视频资源采用一层相册目录；视频平铺在下载根目录。不同来源发生重名时消解冲突。清单保存来源、文件路径、状态及校验信息；相册元数据包含标题、标签、数量声明和实际资源根。清单的文件路径相对于下载根目录。

当前默认监听 `0.0.0.0`，没有用户鉴权，同时提供本机目录与文件管理接口，适合个人电脑或可信网络使用。公网多用户部署需要增加访问控制和文件、任务隔离；多进程部署需要进一步验证任务归属与调度协调。浏览器登录态属于敏感本地数据，不应提交到版本库。

## 启动与验证

Windows 使用 `./start.ps1`，开发模式使用 `./start.ps1 --dev`；Linux/macOS 使用 `./start.sh`。启动器检查依赖、浏览器和前端构建，然后启动服务。详细参数和 API 见 [项目 README](../README.md)。

- 自动化测试：`make test`。
- 站点声明自检：`python scripts/selfcheck.py`。
- 输出链路验证：`python scripts/verify_output.py`。
- HLS 验证：`python scripts/verify_hls.py`。

真实站点验证可能发出网络请求并创建产物，运行前应检查脚本的输入和输出位置。本机最近一次全量验证结果为 587 项测试通过，另有 `verify_output.py` 40 项、`verify_hls.py` 18 项断言通过，`selfcheck.py` 站点声明自洽。测试数量随功能增长，最新结果以当次运行输出为准。

## 接口一览（与前端对应）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/tasks` | 任务列表，支持 `q` / `status`（可重复，接受组代号）/ `collector` / `page` / `page_size`，返回 `{items,total,page,page_size,pages}` |
| POST | `/tasks/create` | 单条创建，可带 `proxy` 等选项 |
| POST | `/tasks/batch-create` | 多行粘贴批量创建，逐行预检重复与无效输入 |
| POST | `/tasks/bulk-action` | 对 `task_ids` 批量执行 `pause/resume/cancel/retry/delete`，逐任务归类 ok/skipped/not_found |
| POST | `/tasks/{id}/retry-failed` | 只重下 `failed`/`skipped`/`gone` 资源，保留已成功部分 |
| POST | `/tasks/{id}/resources/{rid}/retry` | 单个资源重试或强制下载 |
| GET | `/tasks/stats` | 总量、按状态、按采集器、按日期序列、失败原因聚合（按 note 全文 **与** 按 `error_kind` 两种口径）、去重报表 |
| GET | `/library` | 跨任务资源库：`q` / `kind` / `album`（精确匹配）/ `task_id` / 分页，每条带 `refs` |
| GET | `/library/albums` | 资源库内出现过的相册名（仅含有已完成资源的相册） |
| GET | `/tasks/{id}/proxy` | 该任务代理池状态：脱敏 spec + 每线路失败数与熔断截止时刻 |
| GET | `/notifications` | 通知列表与未读数 |
| POST | `/notifications/read` | 标记通知已读（不传 ids 表示全部） |
| GET | `/env/diagnose` | Python / 浏览器 / ffmpeg / 磁盘 / 下载目录五项诊断 |
| GET | `/tasks/storage` | 占用概览，供清理预检 |

## 功能扩展建议（已实施）

| 建议 | 落地情况 |
| --- | --- |
| 批量 URL 导入与预检 | 已实现：多行粘贴 + 拖入 `.txt`，逐行标注重复/无效 |
| 任务搜索、分页与组合筛选 | 已实现：搜索 + 状态组 + 采集器 + 分页 |
| 下载预设 | 已实现：内置三套 + 用户自定义，支持 JSON 导入导出 |
| 环境诊断面板 | 已实现：五项诊断并给出修复提示 |
| 失败诊断与批量补下 | 已实现：失败资源选择性重试、失败原因聚合、批量操作 |
| 下载速度与阶段进度 | 已实现：进度条、阶段进度，以及详情内**字节级实时速率曲线**（差分 + SSE `task.bytes`） |
| 代理支持 | 已实现：任务级代理、多代理轮换、注入下载会话、**失败熔断与自动换线** |
| 任务通知 | 已实现：通知中心 + 可选 SMTP 推送 |
| 统计可视化 | 已实现：SVG 折线 + 饼图 + 失败原因 + 去重报表 |
| 跨任务资源库 | 已实现：按内容聚合的全局视图，支持搜索/类型/相册/任务筛选与分页 |
| 新站点插件 | 已实现：Pexels 采集器（复用 `GallerySite` 声明式契约与既有下载/清单机制） |
| 失败分类（4xx 不再拖慢全站） | 已实现：`PERMANENT_STATUS` 分流 + 资源状态 `gone` + `error_kind` 落库与聚合 |
| 内容完整性终检 | 已实现：`core/mediacheck.py` 算术判据（RIFF/BMP 长度、ISOBMFF box 链、JPEG/PNG/GIF 尾标记）+ 直链 ffprobe 时长校验 |
| 坏文件信号可见 | 已实现：`phash.decode_gray_ex` 区分「无解码器」与「解码失败」，后者标记 `error_kind='corrupt'`（保留文件） |
| CDN 画像并发写 | 已实现：`cdn_profile` 读者/写者共用 `RLock`，`tmp.replace` 加 5 次退避重试 —— 修掉并发采集时命中记录静默丢一半 |
| 资源级遥测 | 已实现：`resources` 加 `started_at`/`finished_at`/`attempts`，任务详情展示耗时与重试次数 |
| 缩略图 | 已实现：`core/thumbs.py` + `/files/thumb`（`_meta/thumb/{sha}.jpg` 缓存）；顺带修掉前端引用的 `/files/raw` **根本不存在**（图片全是 404） |
| 条件请求 | 已实现：下载带 `If-None-Match`/`If-Modified-Since`，304 且本地在 → 复用不传字节；有 `Range` 时让位（否则收尾路径永远走不到） |
| 全局字节速率上限 | 已实现：全局共享字节令牌桶 + `max_download_bytes_per_sec` / `UWC_MAX_BPS`，节流点保持可取消 |
| 资源库批量操作 | 已实现：`/library/bulk-delete`（按 `refs` 决定是否真删文件）与 `/library/archive`（zip 流），前端多选工具条 |
| Pexels 集合/搜索页 | 已实现：`/v1/search` 与 `/v1/collections/{id}` 分页，兼容 `photos`/`media` 两种返回形状 |
| 熔断状态持久化 | 已实现：`core/proxy_health.py`（跨任务全局、按代理 URL 分桶），与 CDN 画像共用 `core/jsonstore.py` 的并发纪律 |
| 落盘后完整性巡检 | 已实现：`POST /library/verify` 用 `mediacheck` 巡检缺失/截断，只标记不删；新增 `missing` 分类 |
| 测试隔离即时守卫 | 已实现：`isolation.install_real_db_guard()` 在**打开真实库的那一刻**带调用栈失败（指纹守卫只能事后发现，且 `_migrate` 幂等） |
| 断点续传持久化 | 已实现：`core/partials.py` 按 **URL** 寻址的暂存区（`_meta/partial/{sha1(url)}.part` + `index.json`），取消/失败时 park、坏文件/满盘时 discard，TTL + 总量预算按「最久没用」淘汰；`GET/DELETE /library/partials` + 首页可见可清 |
| DASH（`.mpd`） | 已实现：`downloaders/dash.py` 解析 `SegmentTemplate`（`$Number$` / `$Time$`+`SegmentTimeline`）、`SegmentList`、`SegmentBase` / `mediaRange`（字节区间）与多 `Period`；视频取最高档、音频单独一轨，下载后 `-c copy` mux；点播里 `r=-1` 与**所有** DRM 仍**明确拒绝**（那些会下出「成功但没用」的文件） |
| 资源库标签与收藏 | 已实现：`resource_tags` 明细表（`COLLATE NOCASE`，级联删除）+ `resources.favorite`；`/library/tags`、`/library/favorite`、`GET /library?tag=&favorite=`；标签筛选用 `EXISTS` 而非 JOIN（JOIN 会让「一个资源 3 个标签」变成 3 行，分页口径全错） |
| 标签层级与颜色 | 已实现：层级 = 标签名里的 `/`（`系列/角色A`），**不建树表**；`all_tags()` 回 `parent`/`depth`/`n_tree` 并补齐中间层，`/library/tags` 下发调色板（界面不硬编码色值）；颜色存在 `tag_meta`，是**标签**的属性（资源删光也不丢）；`GET /library?tag=&tag_children=true` 做带分隔符的前缀匹配 |
| 分片缓存的跨任务复用 | 已实现：分片缓存也进暂存区（`{sha1(清单URL)}.seg/`），与单文件 `.part` 共用 TTL/预算/淘汰；**按清单指纹**（`fingerprint`，支持字节区间）判断能否复用，不符整份丢弃 |
| 媒体元数据 | 已实现：`resources.width/height/duration`，图片在落盘后读文件头量宽高、视频取下载层**已经测过**的容器时长（`info["probed_duration"]`，三条收尾路径统一回填）。⚠️ 没装 ffprobe 时 `duration` 留 **NULL**，不写 0 —— "没测量"与"0 秒"是两件事 |
| 下载顺序（优先级） | 已实现：`options.resource_order` ∈ `original` / `video_first` / `small_first`。线程池固定大小、空闲 worker 按**提交序**取任务，所以重排提交顺序就是事实上的优先级；`task_manager.order_resources` 只排序不过滤（输出长度与输入恒等） |
| 跨任务死信重放 | 已实现：`GET /library/failures`（按 `error_kind` 分组，每个原因给 `n` 与 `replayable` 两个数字）+ `POST /library/replay`（按 `refs`/`kinds`，同给取交集）。全部复用 `submit_resource` 这一条重下入口，跳过理由逐条回报 |
| `sidx` 嵌套索引（`reference_type=1`） | 已实现：`parse_sidx_refs` 把两种引用（`media` / `index`）分开，`video.py::_expand_sidx` **原地 DFS** 逐层取回再展开（`SIDX_MAX_DEPTH` / `SIDX_MAX_INDEXES`，到上限**报错不截断**）。⚠️ 顺序不能重排：子层分片与索引 box 在文件里是紧挨着的先后关系，先收父层媒体、事后再补子层会拼出「长度对得上、能播、内容错位」的文件 |
| 直播录制（DASH `type="dynamic"`） | 已实现：产出是**一段录制**而不是"下完整个流"。窗口 = `[now - timeShiftBufferDepth, now]`（`now` 取下载那一刻，退回 `publishTime`；两者都没有就**明确报错**）；清单**显式列出的**分片不按时钟裁（时钟偏一点就会裁掉该录的）；`_record_live` 按 `minimumUpdatePeriod` 反复取清单，只下**新出现的**分片（只往前不回头，多时段各自记窗口），时间上限（`UWC_LIVE_MAX_SECONDS`，默认 300s）/ 源站转 `static` / 取消时收工。⚠️ 取消时**仍把已录到的封成文件**（点播留的是能续传的半成品，直播窗口滚过去就补不回来）；漏录片数写进日志 |

## 后续可做

截至 V38，前面几轮列出的建议已**全部实施**，无遗留项。

- V33 是四条**缺陷修复**（跑通了但结果是错的）。
- V34 把 V33 列的八项 backlog 一次做完，并修掉两个"看不见"的问题（前端引用的
  `/files/raw` **从来不存在**；测试隔离的**窗口期** —— `monkeypatch.undo()` 把
  `DB_PATH` 还原成真实路径，那之后任何 DB 访问都落到用户的真库上）。
- V35 收掉最后三项（断点续传持久化 / DASH / 标签收藏），并复核出项目文档本身在
  **撒谎**：两处声称缺失的能力其实早就有了（见下）。
- V36 收掉 V35 结束时列出的最后三项（分片缓存跨任务复用 / 标签层级与颜色 /
  DASH 的 `SegmentBase` 与多 Period），并顺手修掉 HLS 路径**丢掉调用方 `info`**
  的那个静默缺陷。
- V37 做完了三条**在清单上消失的建议**（媒体元数据 / 下载顺序 / 跨任务死信重放）
  —— 它们写在 2026-09-22 的深度分析里，却既没进 backlog 也没实现，于是从所有
  清单上一起消失，是**逐条读代码**才发现的。
- V38 收掉最后两条标「低」的候选（嵌套 sidx / 直播录制）。它们当初被列成"暂不做"，
  理由是"明确报错已经足够"——**那个理由现在不成立**：嵌套索引只是"再解一层"，
  直播缺的是**结束条件**（时间上限），是产品决策而不是技术欠债。

若之后继续扩展，方向明确的有：

| 优先级 | 建议 | 价值与范围 |
| --- | --- | --- |
| 按需求 | 更多站点插件 | 契约已稳定（`GallerySite` + `@register`），新增站点只需声明 + `match_score` 把关 |
| 按需求 | HLS 直播（`#EXT-X-PLAYLIST-TYPE:EVENT` / 无 `ENDLIST`） | 与 DASH 直播同形的问题，可以复用 `_record_live` 的"反复取清单 + 只下新分片 + 时间上限"骨架；缺的是真站样本 |
| 低 | 直播断点续录 | 现在取消即收工（窗口滚过去补不回来）。要支持"接着录"得把已录分片进暂存区并按 `$Time$` 续接，收益取决于是否真有人长时间录 |

> **复核纠错记录（V35）**：曾有两条列在这里，去代码里核时发现**已经实现**，属文档
> 过期（声称缺失的能力其实在）：
> * 站点级并发配额 —— `DomainLimiter` 按 `site_key` 持有
>   `BoundedSemaphore(settings.domain_concurrency)`，默认 3，`slot()` 确实 acquire。
> * 巡检结果落库 —— `/library/verify` 会把 `error_kind` + `note` 写回 `resources`，
>   持久化从 V34 起就有。
>
> **复核纠错记录（V38）**：上面那两条「低」候选的**理由**是错的 —— 它们写的是
> "明确报错已经足够"，但那两条路径被拒绝的原因只是"当时没做"，不是"做不到"。
> 把"暂时不做"写成"不该做"，会让后来的人失去重新评估的机会。
>
> 这与「fixture 不诚实」「旧测试断言的前提失效」是**同一型**：声称某能力不在，其实早有。
> 区别只是这次说谎的是**文档**。**维护文档时要去代码里核，不能凭印象增删条目。**
>
> V36 的提交前照例做了关键词扫描（`暂不支持 / 还没有 / 只放内存 / 未实现 / 缺一个 /
> 明确拒绝`），逐条去代码里核。**这一轮没有发现新的"谎报能力缺失"** —— 上一轮列的
> 三项目标确实都还没做（这次才做完），V35 写的边界描述当时是准的。
> 唯一需要改的是**能力描述本身过期**（`SegmentBase` / 多 `Period` / 扁平标签 /
> 「暂存区只收单文件」四处），已在上面四处改掉。

> ⚠️ 新增站点时注意：纯 ID 样本存在跨站歧义，`match_score` 必须用自己的 `gid_shape`
> 对纯 ID 二次把关，否则会静默抢走别的站点的输入（详见 `AGENT_DEVELOPMENT_GUIDE.md` 第 11 节）。

## UI 改进（已实施）

首轮 UI 改版已完成，均基于源码实现并随本轮提交：

1. **创建区域收敛。** 单条主线为「输入网址 → 预览 → 开始」，高级设置折叠；批量模式独立切换。
2. **任务表格精简。** 名称与来源合并为两行主次结构，状态改中文，新增搜索、状态/采集器筛选与分页。
3. **详情改为侧边抽屉。** 资源、日志、信息分标签，标签记忆上次选择；资源区支持列表/网格切换。
4. **媒体预览增强。** 缩略图懒加载、统一占位；大图预览支持左右切换、缩放、旋转、复位与下载。
5. **视觉与反馈统一。** 保留深色基调，统一间距、字号、按钮与状态点；交互反馈改全局 toast，删除确认统一弹窗。
6. **空白与异常状态。** 列表区分"还没有任务"与"搜索无结果"，加载时显示骨架屏。

## 相关文档

- [文档索引](README.md)
- [项目 README](../README.md)：启动、配置、API 与详细能力。
- [Agent 开发说明](AGENT_DEVELOPMENT_GUIDE.md)：早期指引，具体实现以当前代码为准。
- [XChina 设计说明](XCHINA_ANALYSIS.md)：历史分析，部分策略已演进，以当前采集器代码为准。
