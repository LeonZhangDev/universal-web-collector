# Universal Web Collector — 项目长期笔记

通用网页资源采集平台（Resource Extraction Agent 基础设施）。目录 `universal_web_collector_v9`。
栈：uv + Makefile + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
详细设计见 `README.md` 与 `docs/AGENT_DEVELOPMENT_GUIDE.md`（已到 V19）；
本文件只记**会导致踩坑的约束**，重复的正文一律以那两份为准。

## 铁律
```
URL → Browser/HTTP → Extractor → Resource → Downloader → Storage
```
**不要把站点逻辑写进下载器**：Collector 只发现资源，Downloader 只下载资源。

## 关键文件
```
api/tasks.py, api/sessions.py   HTTP + SSE（create/preview/resolve/watches/manifest/storage/fs）
core/task_manager.py            状态机/线程池/看门狗/资源级重试/is_active
core/database.py                SQLite(WAL) → data/collector.db
core/cancel.py                  TaskCancelled（打破 downloaders↔task_manager 循环依赖）
core/naming.py / manifest.py    命名模板（含 .. 整体拒绝）/ 产出清单（取消也写）
core/sessions.py                headful 登录 + 周期快照 storage_state
core/ffmpeg.py                  ffmpeg 定位（不依赖 PATH）
collectors/__init__.py          注册表 + 自动识别（match_score / resolve_collector）
collectors/scores.py            识别分数阶梯（单独模块，为打破循环依赖）
collectors/gallery_base.py      SequenceGallerySpider + GallerySite + MediaType（序号枚举站基类）
collectors/album_meta.py        相册页元信息（headless）+ Cloudflare 熔断/登录态隔离
downloaders/ratelimit.py        站点级并发+间隔
scripts/verify_output.py / verify_hls.py   两个验证台（33 / 18 项断言）
tests/                          21 个文件，245 个用例
```

## 状态机
`pending→running→extracting→downloading→success / partial / failed`，可 `cancelled`，可 retry。
TRANSITIONS 表 + 乐观锁（update where status=expected）。
`_final_status()`：全成功(或仅 filtered/skipped)→success；有成功也有失败→partial；全失败→failed。
**采集器返回空列表 → 直接 failed**（旧行为报 success，用户看不出"什么都没下到"）。

## 取消 / 停止 / 删除
- `POST /tasks/{id}/cancel`（保留记录与文件）与 `DELETE`（删记录）语义分开。
- ⚠️ **`except Exception` 会吞掉取消信号**：被当失败后还退避重睡再重试 → 资源卡 downloading、
  半成品留盘。**所有 `except Exception` 前必须加 `except TaskCancelled: raise`**（base.py 两处、video.py 三处）。
- ⚠️ 取消类回归要断言**耗时 < 1s**，不是"能抛出"——只断言异常类型，这个 bug 照样过。
- ⚠️ `cancel()` 立刻写 DB（界面秒响应），worker 之后才收拾现场；断言"收拾干净"要看 `is_active()`。
- ⚠️ `delete()` **不能提前 pop `_active`**（`_cancelled()` 查的就是它）→ worker 会全下完。
- `DELETE` 默认只删记录；`?with_files=true` 才连 `<下载根>/<task_id>/` 删，双重越界校验。
  ⚠️ 删文件前先等 worker 退出（≤5s），否则它会把文件写回来。

## 产出 / 导出 / 增量
- `options.name_template`：`{site}{host}{album}{seq}{seq4}{ext}{type}{id}`，支持 `/`；含 `..` 整体拒绝。
- 任务结束(含取消/部分失败)写 `manifest.json`：rel path / sha256 / size /
  **resolved_url（实际生效下载点）** / content_type / note。
- `POST /tasks/{id}/archive` **流式** ZIP（攒 BytesIO 等于整包压内存）。
- `incremental: true` → `find_done_resource(url)` 命中即复用不发请求。
  `watches` 表：`due_watches()` + `claim_watch()` **先抢占再执行**。

## 线程与看门狗
- ThreadPoolExecutor(max_workers=2)；看门狗 30s 轮询：无 worker 的 active 任务→failed；心跳超时 900s→cancelled。
- ⚠️ 看门狗必须**问过进程内所有存活 TaskManager**（`_LIVE_MANAGERS`），只看自己 `_active` 会把别的
  实例正在跑的任务判成"重启遗留"标 failed。仍不覆盖 `uvicorn --workers N`（需 tasks 加 worker_id 列）。

## 限速器（2026-09-17 离线实验纠正，别反向推断）
两闸门**正交**：间隔闸门 `_last` 决定「每 N 秒发一个请求」；并发闸门 `_sem` 只限在途数，**不摊薄间隔**。
单次耗时 > 间隔时 `concurrency=1` 会让后续请求空等（分片场景致命）→ `domain_concurrency=3`。
`_limiter()` 按**站点**（注册域）分桶，非 netloc；可用 config `site_groups` 覆盖。

## 过滤 / 类型 / 去重
URL 层免请求：类型白名单 / 扩展名黑白名单 / URL 关键词包含·排除。
大小层：min/max_size，HEAD 探 Content-Length，**探测失败一律放行**。
被过滤 → `status=filtered` + `note`；单资源重试＝强制下载（跳过过滤）。
四个解析器一次加载全跑：APIDetector > NetworkParser > JSStateParser > DOMParser。
去重靠 `resources.hash`(sha256)，跨任务复用文件。
输出目录须绝对路径、禁 `..`、禁盘符根，实落 `<dir>/<task_id>/`；`/files/{task_id}/{p}` 用 `is_relative_to` 锁目录。

## m3u8 与 ffmpeg
- 分片走**独立配置** `segment_concurrency`(4)/间隔 0.15~0.35s，**不共用图片的 3~10 秒**
  （曾把 10 秒视频拉成 11 分钟）。分片级重试 + 断点续传，单片失败只坏那一片。
- `video_engine`：`auto`（有 ffmpeg 就用，失败降级）/ `ffmpeg`（强制，失败不降级）/ `builtin`。
- ⚠️ **不要用 `shutil.which("ffmpeg")`**：PATH 是进程启动快照。走 `core/ffmpeg.py::find_ffmpeg()`，
  失败结果**只缓存 30s**，装好无需重启后端。本机 ffmpeg：`%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`。
- ⚠️ ffmpeg 拉流时请求由 ffmpeg 发出：**DomainLimiter 不参与、mirrors 不轮换**、进度只整文件完成上报。

## ⚠️ img.xchina.io 站点特征（改动前必须重新验证）
1. **不返回 404**：存在 → `200 image/jpeg`；越界 → `200 text/html`。存在性**只能看 Content-Type**。
   `probe()` 返三态 ok/missing/error，5xx 与网络异常归 error（不与 missing 混同，否则图集被截断）。
2. ⚠️ **HEAD 是可靠的**（2026-09-18 复测：4/4 返回 `Content-Type`+`Content-Length`）。
   曾记成"HEAD 无响应头"，那是 **curl 走系统代理时 `-I` 只回 `200 Connection Established`** 的假象。
3. **WAF 校验 Accept**：无 Accept 或 `*/*` → **403**；含 `image/*` → 200。常量见 `core/config.py`。
4. `xchina.co` 的 HTML 页对普通请求 **403**（Cloudflare）→ 资源发现不能依赖页面 HTML。
5. 一图 4 个下载点（`.jpg` + `_1200x0/_800x0/_600x0.webp`）→ 天然 mirrors。
6. 相册 `6aa5136f606fe` 实测：**1~142 连续无空洞**，143 起越界。
7. `var videos[].filesize` 实测精确（"64M"↔67341834B，**零请求**）；另有 h1 / tag 可用。
   页面里的 `…date…` 可能是推荐位的，**归属未验证，别当发布日**。
8. 视频无档位/预览变体（`.m3u8`、`_600x0.mp4` 均不存在）→ `mirrors` 为空；单视频可达 105MB。

## 相册采集器 `xchina_gallery`
- 三种输入**都必须能解析**：相册页 `https://xchina.co/photo/id-{id}[/10].html`、
  直链 `https://img.xchina.io/photos/{id}/00001.jpg|.mp4`、图集 ID 本身。
- ⚠️ **相册页末段 `10.html` 是页码不是 ID**（曾用页码去枚举 → 0 资源却报 success）。
  现在 `page_tail=r"^\d+$"` 直接放弃。**宁可报错也绝不猜**——"成功但 0 资源"比报错难查得多。
- 画质档 `original/1200/800/600`：选定档作主 URL，其余按邻近度作 mirrors；缺该档回退最高档。
- ⚠️ 判断是否最高画质档要拿**变体后缀**跟 `site.variants[0]` 比，不能拿档名比（曾落盘成 `00001_.jpg.jpg`）。
- ⚠️ 自动识别时用 `parse_gid(strict=True)`（不用末段退路），见下"自动识别"。

## mirrors 机制
采集器给 `mirrors` → `resources.mirrors`(JSON) → `download_with_mirrors`。
主 URL 失败依次切换；切换时清半成品且**不续传**（不同 URL 内容不能拼接），并按新扩展名改名。
**`require_image=True` 必开**，否则越界 URL(200+html) 会被当图存下来且永不触发切换。

## 多媒体枚举（图 + 视频）与相册命名
同一 gid 下**图片与视频并存**（`6a3654854fd25` = 12 图 + 4 mp4，同名不同后缀）。
`options.media`：`auto`/`image`/`video`/`both`；auto **两条线索都问**（相册页 `var videos` + 探一次 `00001.mp4`），
只信页面会静默漏采。⚠️ `.mp4` 对任何 Accept 都返回 206，别据此推断别的路径。
`options.album_title`：`clean`/`full`/`h1`/`id`，**`id` = 完全不开浏览器**；`album_tags_dir` 再套标签目录
（`丝袜-情趣内衣/相册名/…`，只取前 3 个标签）。目录名取不到一律回退 gid（**绝不让任务失败**）。

## 相册页做 headless（Cloudflare）
三个坑：①首次是挑战页 `<title>Just a moment...</title>`，须轮询；
②**不能** `wait_for_function`（挑战靠一次导航完成，导航销毁上下文）、也**不能**按标题判就绪
（8s 时标题是 `Loading <url>`，`content()` 紧接着抛异常）→ 只能轮询 `page.content()`，看有无
`photo-items`/`hero-title-item`/`var videos`/`objId` 标记；③**先匿名、失败再带登录态**
（陈旧 `cf_clearance` 会招来 `Attention Required!` 永久拒绝），与 `collectors/browser.py` 相反。

### 长期对策（2026-09-19，写在 `album_meta.py` 模块文档里）
**不投入指纹对抗**。真正的对策是让采集**不依赖那个 HTML 页**：资源发现走纯 HTTP 序号枚举，
相册页只提供目录名/自报数量/视频体积；拿不到就降级用 gid 命名（**采集照常**）。
在此之上三件事：①**域级熔断**：连续 3 次读不到 → 该域 10 分钟内不再开 Chromium（每次读页
几十秒，明知被拦还开纯属浪费，也更像扫描器）；②**陈旧登录态隔离**：带登录态被拒即标记失效，
`/sessions` 的 `cf_stale` 列出 → 提示重新登录；③**降级可见**：日志写人话并回答"接下来会怎样"。
⚠️ **被拦 ≠ 登录态失效**（匿名一样被拦），只有**带登录态**被拒才记到登录态头上。
⚠️ **页面结构不符**（改版/objId 对不上）不计入熔断 —— 是另一个问题。
重登/删态后 `finish_login`、`remove_session` 会 `clear_domain_state()`，无需重启后端。

## 相册页自报数据 → 创建前预览（`POST /tasks/preview`）
页面白给三样：`12P + 4V`、`filesize`、标签与厂牌。定位靠**图标/class 锚定**
（`fa-image`/`fa-file`/`tags-line`），不靠 div 顺序；`_TAGLIST_RE` 对换行缩进敏感，改前用真实页面复核。
`preview()` 有页面数据时**零序号枚举**；拿不到才受限枚举 → `sampled=true`，数量只是**下限**（前端显示 `≥`）。
⚠️ 自报数量**不当资源清单**，序号枚举才是权威。
⚠️ 预览与创建**共用** `_gallery_options()`，否则"预览通过、创建被拒"。
⚠️ `photos`/`videos`/`video_bytes` **只统计本次真要采的媒体**，必须与 `media` 一致 ——
曾直接回自报总量，导致预告写"12 图+4 视频 251MiB"而创建后一段视频没下。**预告与行为不一致比不预告更糟**。
自报总量另用 `photos_declared`/`videos_declared` 带出。

## 体积前置
`probe()` 本就要读 Content-Length，页面又直接给视频体积 → `size` 在采集阶段已知。
下载层大小过滤**优先用 `r["size"]`**，真未知才 `probe_size()` ⇒ 图集任务零额外请求。
⚠️ `size` 经 JSON/DB 往返可能是**字符串**，要 `isdigit()` 再转 int；类型不对就当未知重探是错的。
回归表现是"每个资源平白多一次 HEAD" → 用例里把 `probe_size` 换成 `pytest.fail` 兜着。

## 采集器自动识别（2026-09-19）
每个采集器可选实现类方法 `match_score(url) -> Optional[int]`（None = 不认领，越大越优先，
同分按名字升序保证**可复现**）。分数在 `collectors/scores.py`：
`SCORE_ALBUM_PAGE(100) > SCORE_RESOURCE_URL(50) > SCORE_BARE_ID(10) > SCORE_GENERIC(-1000)`。
三条约束：
1. **认领必须有凭据**：域名匹配还不够，必须 `parse_gid(strict=True)` 成功。第一版没加 strict，
   `/tag/some-tag` 的末段被末段退路当成图集 ID 认领 → 又是"成功但 0 资源"。
   **替用户做决定的场合一律用最严规则。**
2. **库里存真名不存 `auto`**（否则重放/巡检时同一 auto 指向不同采集器，行为不可复现）；
   识别结论塞进响应 `resolved` 字段回显。
3. **结论必须回显且可覆盖**：`/collectors/resolve` 是纯 CPU 判定，前端失焦 300ms 后问一次。
   无法识别的输入直接 400（不悄悄兜底给 generic —— 那会在 DNS 层失败，错误信息没用）。

## 视频页采集器 `xchina_video`（V20, 2026-09-19）
输入 `https://xchina.co/video/id-{gid}.html`、视频 gid、或迅雷式带签名 m3u8 直链。
`match_score`：`/video/` 路径=100、m3u8 直链=50，复用 `scores.py` 阶梯。
核心模块 `collectors/hls.py`：
- `inspect_playlist()` 下载清单做**健全性校验**，防最阴险的失败：签名过期/错 → 站点回
  **200 + 语法合法的 m3u8** 指向 `/fallback/placeholder.ts`（603KB 真实可播放 TS），会静默下成
  占位视频且任务报 success。`expires` 解析自 URL query（秒/毫秒都认），过期即明确失败。
- `estimate_size()` 用「单片 HEAD 体积 × 分片数」估整段大小，让 `min_size/max_size` 对 HLS 有效
  （`.m3u8` 的 Content-Length 只有几 KB，不估会误杀整段视频）。
实测 `6aaa517d3f106`：`#EXT-X-KEY:AES-128` 加密，但 `/key/enc.key` **无需签名**、
分片也无需签名（只有 playlist 设防）；ffmpeg 原生拉钥+解密+remux，端到端产出 27.1MB/5:05 真视频。
**解密走 ffmpeg，零新增依赖**。
⚠️ `crawl`/`preview` 监听 `.m3u8` 网络响应用 `"m3u8" in u` 而非 `endswith(".m3u8")` ——
真实 URL 带 `?expires=...` 查询串，endswith 永远匹配不上（测试立刻抓到）。
⚠️ `browser_runner` 做成**可注入依赖**，单测用假运行器+假 session，绝不真触网。
`video.py`：引擎分发**前**先 `_preflight_hls()` 校验，失败抛清晰错误；`engine=ffmpeg` 无二进制时
先于预检 fail-fast（确定性本地错不该被网络错掩盖）。

## 测试隔离 / 冒烟 / 常用命令
- `TaskManager.shutdown(wait=True)`：测试必须等 worker 真退出，否则上用例的 worker 在下一个
  用例里继续写库 → 随机失败。生产仍 `wait=False`。
- ⚠️ 沙箱 safe-delete shim 同 turn 删 >50 次后会拒绝删除 → 删除类用例假失败 `SystemExit`；
  绕过：`PYTHONPATH= python -m pytest ...`。
- 后端启动需 `--app-dir backend`。本机 curl 必须对 127.0.0.1 加 `--noproxy '*'`（否则 502）。
- 用户项目 `.venv` 是 **WSL 里的 Linux venv**，Windows 侧跑不了；本机验证用
  `~/.workbuddy/binaries/python/envs/uwc-verify`。
- Bash 的后台进程随该次调用结束被回收 → 起服务必须 `run_in_background: true`。
- 本机 bash PATH 偶发失效（`ls`/`head`: command not found）→ 命令前 `export PATH="/usr/bin:/bin:$PATH"`。
- ⚠️ **同一文件的多处 Edit 别并行发**：会静默丢改动（已踩多次，曾把 3 个用例粘贴两遍）
  → 改完立刻 grep 核对。Edit 时还要注意别把 docstring 的 `"""` 提前写进去（写成两句文档 + 语法错误）。
- ⚠️ `verify_output.py` 起点要**整个清掉 `data/_verify_output`**（只删 DB 会留下产物给续传逻辑用 →
  偶发失败无法复现）；断言失败要打印期望/实际值（`check_eq`），只打标签的门禁排不了错。

```bash
make install / backend / frontend / build / test / docker
python scripts/verify_output.py           # 33 项断言
python scripts/verify_hls.py              # 18 项断言
python scripts/preview_probe.py <url|gid> # 真实站点创建前预告
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY` / `UWC_FFMPEG`

## 接入新站点
新建 `collectors/<site>/spider.py` → `@register("<name>")` → 在 `collectors/__init__.py` import。
**序号枚举型图集站**继承 `SequenceGallerySpider`，只声明 `GallerySite`
（`id_patterns`/`page_tail`/URL 模板/变体/画质映射），并可加 `match_score` 参与自动识别。
开工前按 skill `gallery-site-probe` 探测。

## ⚠️ Git：已独立建仓
在 `universal_web_collector_v9/` 内执行 git（toplevel 已是项目目录），分支 `main`，产物全被 `.gitignore` 挡。
**上级 `C:\Users\admin` 那个仓库绝不能碰**（根是整个用户主目录，远程 `LeonZhangDev/Myproject.git`）。
