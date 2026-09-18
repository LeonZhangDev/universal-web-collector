# Universal Web Collector — 项目长期笔记

## 项目定位
通用网页资源采集平台（非简单爬虫），目标是可扩展的 Resource Extraction Agent 基础设施。
目录名是 `universal_web_collector_v9`，但 README/文档已推进到 **V14** 阶段
（V10 任务系统/DB、V11 插件化、V12 过滤与自定义目录、V13 相册枚举、V14 视频链路/画质档/基类/健壮性）。

技术栈：uv + Makefile + FastAPI + Vue3/Vite + Playwright + SQLite(WAL)

## 核心架构（务必遵守）
```
URL → Browser → Extractor → Resource → Downloader → Storage
```
**开发铁律：不要把站点逻辑写进下载器。**
- Collector/Spider 只负责「发现资源」
- Downloader 只负责「下载资源」
反例：XChinaDownloader 同时做解析+发现+下载（错误做法）

## 目录结构
```
backend/
  main.py              FastAPI 入口
  api/tasks.py         HTTP 接口 + /events SSE
  core/
    task_manager.py    状态机/线程池/看门狗/资源级重试
    database.py        SQLite(WAL) → data/collector.db
    config.py          config.yaml + UWC_* 环境变量覆盖
    events.py          SSE 事件总线
    filters.py         资源过滤规则(类型/扩展名/关键词/大小)
    ffmpeg.py          ★ffmpeg 定位(不依赖 PATH; 成功永久缓存, 失败 30s TTL)
  collectors/
    __init__.py        @register("name") 注册表
    browser.py         共享 Playwright 层
    parsers.py         共享解析器
    gallery_base.py    ★序号枚举型图集站公共基类(SequenceGallerySpider + GallerySite 声明)
    xchina/spider.py   XChina 页面(浏览器)
    xchina/gallery.py  ★相册ID→全图枚举(纯HTTP, 继承 gallery_base)
    generic/spider.py  通用站点
  downloaders/
    base.py            stream_download / download_with_mirrors(镜像轮换)
    image/video/file/text.py
    ratelimit.py       站点级并发+间隔(支持随机区间)+代理
  models/schemas.py
frontend/              Vue3 + Vite + axios，构建产物 frontend/dist 由后端托管
scripts/probe.py       站点解析探针
scripts/verify_hls.py  ★HLS 双引擎验证台(ffmpeg 生成真实素材 + 解码级校验)
tests/                 11 个测试文件，78 个用例
```

## 关键机制备忘
- **状态机**：pending→running→extracting→downloading→success / **partial** / failed；failed/partial→retry(/tasks/{id}/retry)；支持 cancelled。迁移靠 TRANSITIONS 表 + 乐观锁（update where status=expected）
  - `partial`（2026-09-17 新增）：`_final_status()` 按资源分布判定 —— 全成功(或仅 filtered/skipped)→success，
    有成功也有失败→partial，全部失败→failed。此前 56 张全失败任务仍报 success，用户看不出问题
- **线程模型**：ThreadPoolExecutor(max_workers=2)，Playwright 同步 API 跑在工作线程
  - ⚠️ `delete()` **不能提前 pop `_active`**：`_cancelled()` 是查 `_active` 的，
    pop 掉就读不到取消标志，运行中的 worker 会把剩余资源全下完才释放槽位
    （实测占住 6 分钟，后续任务一直 pending）。正确做法：只在 `future.cancel()`
    返回 True（worker 尚未启动）时才手动回收，否则交给 `_run` 的 `finally`
- **四个解析器**（一次页面加载全跑）：APIDetector > NetworkParser > JSStateParser > DOMParser，merge_by_priority 去重
- **资源类型**：image/video/audio/doc/text，未知类型自动 skip
- **去重**：resources.hash(sha256)，跨任务复用已下载文件
- **m3u8 视频**（2026-09-17 重写分片链路；2026-09-18 接入 ffmpeg 双引擎）：
  - 分片走**独立配置** `segment_concurrency`(4) /
    `segment_min_interval`~`segment_max_interval`(0.15~0.35s)，
    **不再共用图片的 3~10 秒**（原实现把图片慢节奏套到分片上，一段 10 秒视频拉成 11 分钟）
  - 分片并发下载 + 分片级重试(`segment_retries`=3) + 断点续传(只补缺失分片)
  - 单片失败只坏那一片（原实现任一片失败整个视频报废）
  - **双引擎 `video_engine`**：`auto`(默认，有 ffmpeg 就用它拉流，失败降级内置器) /
    `ffmpeg`(强制，失败报错**不降级**) / `builtin`(强制内置器；仍可用 ffmpeg 做 remux)
  - ⚠️ **不要用 `shutil.which("ffmpeg")` 判断可用性**：PATH 是进程启动时的快照，
    运行期新装的读不到（实测 winget 装好后 which 仍返回 None）。
    统一走 `core/ffmpeg.py::find_ffmpeg()`：显式配置(`ffmpeg_path` / `UWC_FFMPEG`)
    → PATH → 已知安装位置(winget Links / scoop / choco / `C:\ffmpeg\bin` / 商店)
    → imageio_ffmpeg。**失败结果只缓存 30s**，故装好无需重启后端即可识别
  - ⚠️ ffmpeg 拉流时请求由 ffmpeg 自己发出：**DomainLimiter 不参与、mirrors 不轮换**、
    分片进度只在整文件完成时上报一次 → 对限速/镜像敏感的站点用 `builtin`
  - 本机 ffmpeg（2026-09-18 装）：winget 的 9.0.1-full_build，
    `%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`（同目录含 ffprobe）
- **看门狗**：30s 轮询，无 worker 的 active 任务→failed，心跳超时 900s→cancelled
- **存储**：downloads/{task_id}/，浏览器登录态 browser_state/{domain}.json
- **资源过滤**（backend/core/filters.py）：
  - URL 层（免请求）：类型白名单 / 扩展名黑白名单 / URL 包含·排除关键词
  - 大小层：min_size / max_size，HEAD 探测 Content-Length；**探测失败一律放行**
  - 无扩展名的 CDN URL 在扩展名白名单判定中放行（下载前无法判定真实类型）
  - 被过滤 → `status=filtered` + `note` 记原因；单资源重试＝强制下载（跳过过滤）
- **输出目录**：任务级 `download_dir`（须绝对路径、禁 `..`、禁盘符根目录），
  实际落在 `<dir>/<task_id>/`；文件服务 `/files/{task_id}/{path}`
  用 `is_relative_to` 锁在任务目录内，防跨任务读取

- **限速器语义（2026-09-17 离线实验纠正，之前判断反了）**：`DomainLimiter` 两个闸门**正交**
  - 间隔闸门 `_last`（在 `_ilock` 内，**全局串行**）→ 决定「每 N 秒发起一个请求」
  - 并发闸门 `_sem`（semaphore）→ 只限制「同时有多少请求在途」，**不摊薄间隔**
  - 实测（并发 3 / 间隔 0.5s / 每次耗时 2s）：6 个请求发起间隔恒为 0.53s，与并发 1 完全一致
  - 反向坑：当「单次下载耗时 > 间隔」时，`concurrency=1` 会让后续请求阻塞在闸门空等，
    实际节奏被拖长（视频/m3u8 分片场景致命）→ 故 `domain_concurrency` 定为 3
  - `_limiter()` 按 **站点** 分桶（`site_key()` 推断注册域：`img.xchina.io` 与
    `cdn.xchina.io` → 同为 `xchina.io`，共用限速器；正确处理 `co.uk` 二级后缀；
    可用 config `site_groups` 显式覆盖分组）：
    此前按 netloc 分桶，同站不同域名各持一个限速器，站点级限速形同虚设

- **相册枚举采集器** `xchina_gallery`：
  - 通用逻辑在 `collectors/gallery_base.py`（`SequenceGallerySpider` + `GallerySite` 声明），
    `xchina/gallery.py` 只提供站点声明 → 接第二个图集站不必复制整份文件
  - 输入相册 ID / 图片 URL / 相册页 URL，纯 HTTP 无需浏览器。随机 3~10 秒一张；
    探测走独立配置 `probe_interval: 0.3`（HEAD 是轻量请求，实测连续 60 次不被限流）
  - **画质档 `quality`**：`original` / `1200` / `800` / `600`
    - 选定档位作主 URL，其余按画质邻近度排序作 mirrors(`mirror_order`)
    - 该档在某张图上缺失时回退最高画质档，不中断整册
    - 实测 `quality=1200` 单张 68~119KB vs 原图 656KB（**省约 85%**），`RIFF` 魔数确认真 WebP
    - 站点 `options.quality` 平铺存储（`Filters` 只读自己认识的 key，互不干扰）

## ⚠️ img.xchina.io 站点特征（改动前必须重新验证）
1. **该站不返回 404**：越界序号返回 `200 + text/html`，存在的图返回 `200 + image/jpeg`。
   存在性判定**只能看 Content-Type**，看状态码会永不停止或第一张就误停。
   `gallery.probe` 返三态 ok/missing/error，5xx 与网络异常归 error（不与 missing 混同）
2. **WAF 校验 Accept 头**：无 Accept 或 `Accept: */*` → **403**；
   含 `image/*` → 200。Referer 无影响。
   常量统一定义在 `core/config.py`：`DEFAULT_ACCEPT` / `IMAGE_ACCEPT`，
   下载器与 `filters.probe_size` 都必须带上（这是项目级坑，不止这个站点）
3. 直连可用，无需登录/浏览器/代理
4. 一张图有 4 个下载点：`.jpg` 原图 + `_1200x0/_800x0/_600x0.webp` 尺寸变体
   → 天然就是"备用线路"，采集器把它们塞进 `resources.mirrors`

## 备用下载点（mirrors）机制
- 采集器输出 `mirrors` 列表 → `resources.mirrors` 列(JSON) → 下载器 `download_with_mirrors`
- 主 URL 失败依次切换；切换时清半成品且不续传（不同 URL 内容不能拼接）
- 切换后按新扩展名改名（`_swap_ext`），否则 webp 内容存成 .jpg 会误导类型判断
- **`require_image=True` 必开**：否则该站越界 URL(200+html) 会被当图存下来，
  且因"下载成功"永不触发镜像切换
- 开关 `mirror_fallback`

## 接入新站点（标准流程）
1. 新建 `backend/collectors/<site>/spider.py`
2. 用 `@register("<name>")` 装饰 Spider 类
3. 在 `collectors/__init__.py` 里 import
→ 无需改动 core/api

**若站点是「序号枚举型图集站」**（一个页面下 N 张按序号命名的图，
如 `00001.jpg ~ 00056.jpg`）：不要从零写，直接继承
`collectors/gallery_base.py::SequenceGallerySpider`，只声明 `GallerySite`
（URL 模板 / 变体列表 / 画质映射 / 探测规则）。
开工前先按 skill `gallery-site-probe` 做站点特征探测。

## API 缺口（已知，未处理）
- **没有 HTTP 取消端点**：`task_manager.cancel()` 存在，但没有路由暴露
  （路由只有 `/tasks/{id}/retry`、`DELETE /tasks/{id}`）。
  前端只能删任务，不能"取消但保留记录"。

## 常用命令
```bash
make install / make backend / make frontend / make build / make test / make docker
uv run python scripts/probe.py <url>
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY`

## ⚠️ 重要：Git 仓库结构异常（2026-09-17 发现）
**本项目目录不是独立 git 仓库。** `git rev-parse --show-toplevel` 返回 `C:/Users/admin`
——整个用户主目录被当作一个仓库，远程 `https://github.com/LeonZhangDev/Myproject.git`。

后果：
- `universal_web_collector_v9` 整个目录处于未跟踪状态（`??`）
- 仓库中混杂 Desktop 上大量无关文件（.obsidian、AI_interview、demo-python、
  fastapi-task-demo、个人文档/图片等），且当前有大量 `D` 删除标记
- 在该仓库根执行任何 commit/push/add 都可能误提交或误删大量个人文件

**做 git 操作前必须先跟用户确认，切勿在仓库根批量 add/commit。**
若要正规管理本项目，建议在 `universal_web_collector_v9` 内单独 `git init`。
