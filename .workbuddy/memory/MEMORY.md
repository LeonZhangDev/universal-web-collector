# Universal Web Collector — 项目长期笔记

通用网页资源采集平台（可扩展的 Resource Extraction Agent 基础设施）。
目录名 `universal_web_collector_v9`，文档已到 **V16**（产出可交付化）。
栈：uv + Makefile + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
详细设计见 `README.md` 与 `docs/AGENT_DEVELOPMENT_GUIDE.md`；本文件只记**会导致踩坑的约束**。

## 铁律
```
URL → Browser → Extractor → Resource → Downloader → Storage
```
**不要把站点逻辑写进下载器**：Collector 只发现资源，Downloader 只下载资源。

## 关键文件
```
api/tasks.py 与 api/sessions.py    HTTP + SSE（cancel/archive/watches/manifest/storage/fs）
core/task_manager.py               状态机/线程池/看门狗/资源级重试/is_active
core/database.py                   SQLite(WAL) → data/collector.db
core/cancel.py                     TaskCancelled（单独模块，打破 downloaders↔task_manager 循环依赖）
core/naming.py                    命名模板（含 .. 整体拒绝）
core/manifest.py                  产出清单（取消/部分失败也写）
core/sessions.py                  headful 登录 + 周期快照 storage_state
core/ffmpeg.py                    ffmpeg 定位（不依赖 PATH）
collectors/gallery_base.py        SequenceGallerySpider + GallerySite + MediaType（序号枚举型图集站基类）
collectors/album_meta.py          相册页 <title> / var videos（headless Chromium，带 TTL 缓存）
downloaders/ratelimit.py          站点级并发+间隔
scripts/verify_output.py / verify_hls.py   两个验证台（33 / 18 项断言）
tests/                            18 个文件，179 个用例
```

## 状态机
`pending→running→extracting→downloading→success / partial / failed`，可 `cancelled`，可 retry。
TRANSITIONS 表 + 乐观锁（update where status=expected）。
`_final_status()`：全成功(或仅 filtered/skipped)→success；有成功也有失败→partial；全失败→failed。
**采集器返回空列表 → 直接 failed**（旧行为报 success，用户看不出"什么都没下到"）。

## 取消 / 停止
- `POST /tasks/{id}/cancel`（保留记录与文件）与 `DELETE`（删记录）语义分开。
- ⚠️ **`except Exception` 会吞掉取消信号**：被当失败后还退避重睡(2s/4s)再重试注定被放弃的请求，
  资源卡 `downloading`、半成品留盘。**所有 `except Exception` 前必须加 `except TaskCancelled: raise`**
  （base.py 两处、video.py 三处）。
- ⚠️ 回归用例断言**耗时 < 1s**，不是"能抛出"——只断言异常类型这个 bug 照样过。
- ⚠️ `cancel()` **立刻**写 DB cancelled（界面秒响应），worker 之后才收拾现场；
  断言"收拾干净"要等 `is_active()` 落下去，不能看 status。

## 删除
- `DELETE /tasks/{id}` **默认只删记录，磁盘文件保留**（去重下别的任务可能引用它们）；
  `?with_files=true` 才连 `<下载根目录>/<task_id>/` 一起删，双重越界校验。
- `POST /tasks/bulk-delete` 按状态批量清理已结束任务（运行中的不动）；`GET /tasks/storage` 占用概览。
- ⚠️ 删文件前要等 worker 退出（≤5s），否则它会把文件写回来。

## 产出与导出
- `options.name_template` 占位符 `{site}{host}{album}{seq}{seq4}{ext}{type}{id}`，支持 `/`；
  含 `..`/绝对路径**整体拒绝**（不静默改写）。
- 任务结束(含取消/部分失败)写 `manifest.json`：rel path / sha256 / size /
  **resolved_url（实际生效下载点）** / content_type / note。
- `POST /tasks/{id}/archive` **流式** ZIP（生成器逐条 writestr；攒 BytesIO 等于整包压内存）。

## 增量与订阅
`incremental: true` → `find_done_resource(url)` 命中即复用不发请求。
表 `watches`，`due_watches()` + `claim_watch()` **先抢占再执行**；调度线程随 TaskManager 起。

## 线程与看门狗
- ThreadPoolExecutor(max_workers=2)，Playwright 同步 API 跑在工作线程。
- ⚠️ `delete()` **不能提前 pop `_active`**：`_cancelled()` 查的就是它，pop 掉就读不到取消标志，
  worker 会把剩余资源全下完（实测占住 6 分钟，后续任务一直 pending）。
  只在 `future.cancel()` 返回 True（worker 未启动）时手动回收。
- 看门狗 30s 轮询：无 worker 的 active 任务→failed；心跳超时 900s→cancelled。
  ⚠️ 必须**问过进程内所有存活 TaskManager**（模块级 `_LIVE_MANAGERS`），只看自己 `_active`
  会把第二个实例（测试/脚本）正在跑的任务判成"服务重启遗留"标 failed。
  仍不覆盖 `uvicorn --workers N` 多进程（那需要 tasks 加 worker_id 列）。

## 限速器（2026-09-17 离线实验纠正，别反向推断）
`DomainLimiter` 两个闸门**正交**：间隔闸门 `_last`（`_ilock` 内全局串行）决定「每 N 秒发一个请求」；
并发闸门 `_sem` 只限制在途数量，**不摊薄间隔**。
反向坑：单次耗时 > 间隔时 `concurrency=1` 会让后续请求空等被拖长（分片场景致命）→ `domain_concurrency=3`。
`_limiter()` 按 **站点**（注册域）分桶，非 netloc；`img.xchina.io` 与 `cdn.xchina.io` 共用；
可用 config `site_groups` 覆盖。

## 资源过滤 / 类型 / 去重
URL 层免请求：类型白名单 / 扩展名黑白名单 / URL 关键词包含·排除。
大小层：min_size/max_size，HEAD 探测 Content-Length，**探测失败一律放行**。
无扩展名的 CDN URL 在扩展名白名单里放行。被过滤 → `status=filtered` + `note`；
单资源重试＝强制下载（跳过过滤）。
四个解析器一次加载全跑：APIDetector > NetworkParser > JSStateParser > DOMParser。
类型 image/video/audio/doc/text，未知自动 skip。去重靠 `resources.hash`(sha256)，跨任务复用文件。
输出目录 `download_dir` 须绝对路径、禁 `..`、禁盘符根，实际落 `<dir>/<task_id>/`；
`/files/{task_id}/{path}` 用 `is_relative_to` 锁在任务目录内。

## m3u8 与 ffmpeg
- 分片走**独立配置** `segment_concurrency`(4) / `segment_min~max_interval`(0.15~0.35s)，
  **不共用图片的 3~10 秒**（曾把一段 10 秒视频拉成 11 分钟）。分片级重试 + 断点续传，单片失败只坏那一片。
- `video_engine`：`auto`(有 ffmpeg 就用，失败降级) / `ffmpeg`(强制，失败**不降级**) / `builtin`。
- ⚠️ **不要用 `shutil.which("ffmpeg")` 判断可用性**：PATH 是进程启动快照，运行期新装的读不到。
  走 `core/ffmpeg.py::find_ffmpeg()`，**失败结果只缓存 30s**，所以装好无需重启后端。
- ⚠️ ffmpeg 拉流时请求由 ffmpeg 发出：**DomainLimiter 不参与、mirrors 不轮换**、
  分片进度只在整文件完成时上报一次 → 对限速/镜像敏感的站点用 `builtin`。
- 本机 ffmpeg：winget 9.0.1-full_build，`%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`。

## ⚠️ img.xchina.io 站点特征（改动前必须重新验证）
1. **不返回 404**：存在 → `200 image/jpeg`；越界 → `200 text/html`。存在性**只能看 Content-Type**。
   `gallery.probe` 返三态 ok/missing/error，5xx 与网络异常归 error（不与 missing 混同）。
2. **HEAD 可能不返回 Content-Type** → 退回流式 GET 只读响应头再断开（`_head_status_headers`）。
   否则会得出"整个图集是空的"。
3. **WAF 校验 Accept**：无 Accept 或 `*/*` → **403**；含 `image/*` → 200。
   常量 `core/config.py::DEFAULT_ACCEPT` / `IMAGE_ACCEPT`，下载器与 `filters.probe_size` 都要带。
4. 直连可用。`xchina.co` 的 HTML 页对普通请求 **403**（Cloudflare），
   **不要**指望"抓相册页 HTML 提取图片列表"。
5. 一图 4 个下载点：`.jpg` 原图 + `_1200x0/_800x0/_600x0.webp` → 天然备用线路，塞进 `mirrors`。
6. 相册 `6aa5136f606fe` 实测：**1~142 连续无空洞**，143 起越界（`miss_stop=3` 安全）。

## 相册采集器 `xchina_gallery`
- 三种输入**都必须能解析**：相册页 `https://xchina.co/photo/id-{id}/10.html`、
  图片直链 `https://img.xchina.io/photos/{id}/00001.jpg`、图集 ID 本身。
- ⚠️ **相册页末段 `10.html` 是页码不是 ID**。曾因 `id_in_path` 只写 `/photos/`，退路取到页码 `10`
  去枚举 `photos/10/*` → 全 text/html → 0 资源 → **报 success**。
  现在 `page_tail=r"^\d+$"` 识别页码直接放弃；退路只在末段形如 ID 时才兜底。
  **宁可报错也绝不猜**——猜错的表现("成功但 0 资源")比报错难查得多。
- 画质档 `original`/`1200`/`800`/`600`：选定档作主 URL，其余按邻近度作 mirrors；
  某张缺该档则回退最高画质档。实测 1200 档 28~119KB vs 原图 382~656KB（省约 85%）。
- ⚠️ "是否最高画质档"要拿**变体后缀**跟 `site.variants[0]` 比，不能拿画质档名比
  ——曾落盘成 `00001_.jpg.jpg`。见 `_quality_tag()`。

## mirrors 机制
采集器给 `mirrors` → `resources.mirrors`(JSON) → `download_with_mirrors`。
主 URL 失败依次切换；切换时清半成品且**不续传**（不同 URL 内容不能拼接），并按新扩展名改名
（`_swap_ext`）。**`require_image=True` 必开**，否则越界 URL(200+html) 会被当图存下来且永不触发切换。

## 多媒体枚举（图片 + 视频）与相册标题命名
同一个 gid 下**图片与视频并存**（xchina `6a3654854fd25` = 12 图 + 4 mp4，
`00001.jpg` 与 `00001.mp4` 同名不同后缀）。`GallerySite.video_variants` 非空即声明有视频；
`MediaType.ctype_prefix`（`image/` vs `video/`）决定存在性判定。
`options.media`：`auto`(默认)/`image`/`video`/`both`；auto **两条线索都问**
（相册页 `var videos` + 探一次 `00001.mp4`），只信页面会静默漏采。
⚠️ `.mp4` 对任何 Accept（含不带头）都返回 206，别据此推断别的路径。
`options.album_title`：`clean`/`full`/`id`，**`id` = 完全不开浏览器**。
目录名 = `<title>` 截掉 `" - 分类 - 站名"`，取不到回退 gid（**绝不让任务失败**）。

⚠️ 相册页 `xchina.co` 是 Cloudflare 挑战页，三个坑：
1. 首次返回 `<title>Just a moment...</title>`，几秒后自动跳真实页 → 必须轮询；
2. **不能** `wait_for_function`（挑战靠一次导航完成，导航销毁执行上下文，Promise 永不
   resolve），也**不能**按标题判就绪（8s 时标题是 `Loading <url>`，`content()` 紧接着抛
   异常）→ 只能轮询 `page.content()`，看正文有无 `photo-items`/`hero-title-item`/
   `var videos`/`objId` 标记；
3. **先匿名、失败再带登录态**：陈旧的 `cf_clearance` 会让 CF 直接回
   `Attention Required!`（永久拒绝），匿名反而能过 —— 与 `collectors/browser.py` 相反。

## 测试隔离
`TaskManager.shutdown(wait=True)`：测试/脚本必须等 worker 真退出，否则上个用例没跑完的
worker 会在**下一个用例**里继续写库（DB 连接是模块级、被 monkeypatch 换过），把错误写进
下一个用例的任务 → 表现为随机失败。生产默认仍 `wait=False`。
⚠️ 同一文件多处 Edit **别并行发**，改完立刻 grep 核对：真的会静默丢改动，且曾造成
`tests/test_gallery.py` 里 3 个用例被完整粘贴两遍、后定义覆盖前定义而"名存实亡"。
⚠️ 沙箱 safe-delete shim 在同一 turn 累计删除 >50 次后拒绝删除，会让删除类用例报
`SystemExit` **假失败**；绕过：`PYTHONPATH= python -m pytest ...`。

## 接入新站点
新建 `collectors/<site>/spider.py` → `@register("<name>")` → 在 `collectors/__init__.py` import。
**"序号枚举型图集站"**：继承 `SequenceGallerySpider`，只声明 `GallerySite`
（`id_patterns` / `page_tail` / URL 模板 / 变体 / 画质映射）。开工前按 skill `gallery-site-probe` 探测。

## 冒烟要点
后端启动需 `--app-dir backend`（`main.py` 用 `from api.x import y`）。
⚠️ 本机有代理时 `curl 127.0.0.1` 会走代理返回 **502** → 必须 `--noproxy '*'`。
⚠️ 用户的项目 `.venv` 是 **WSL 里的 Linux venv**（输出目录形如 `/mnt/c/...`），Windows 侧跑不了；
本机验证用隔离环境 `~/.workbuddy/binaries/python/envs/uwc-verify`。
⚠️ 同一文件的多处 Edit **别并行发**：会静默丢改动（已踩 2 次），改完立刻 grep 核对。

## 常用命令
```bash
make install / backend / frontend / build / test / docker
python scripts/verify_output.py   # 33 项断言
python scripts/verify_hls.py      # 18 项断言
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY` / `UWC_FFMPEG`

## ⚠️ Git：已独立建仓（2026-09-18 处理完毕）
在 `universal_web_collector_v9/` 内执行 git（`--show-toplevel` 已返回项目目录），分支 `main`。
`.gitignore` 覆盖 downloads/data/node_modules/`__pycache__`/browser_state/dist，已核对 0 条产物入库。
**上级 `C:\Users\admin` 那个仓库仍然存在且绝不能碰**：根是整个用户主目录，混着 Desktop 个人文件，
远程 `github.com/LeonZhangDev/Myproject.git`。**绝不要在 `C:\Users\admin` 下 add/commit/push。**
