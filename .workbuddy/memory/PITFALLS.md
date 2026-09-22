# PITFALLS.md — 完整踩坑清单

`MEMORY.md` 只放每次注入必需的索引与最锋利的约束，本文件放完整细节。
**改相册 / CDN / HLS / 限速 / headless / 预览 / 新站点接入前，先读对应小节。**

---

## 状态机与生命周期（补充）

`_final_status()`：全成功(或仅 filtered/skipped)→success；有成功也有失败→partial；全失败→failed。
推断"收拾干净没有"要看 `is_active()`，不是看 DB 状态——`cancel()` 立刻写 DB（界面秒响应），
worker 之后才收拾现场。

## 产出 / 命名 / 增量

- `name_template`：`{site}{host}{album}{seq}{seq4}{ext}{type}{id}`，支持 `/`，含 `..` 整体拒绝。
- 任务结束（含取消/部分失败）写 `<task_id>/manifest.json`：rel path / sha256 / size /
  **resolved_url = 实际生效的下载点** / content_type / note。
- 相册任务在**下载前**写 `<task_id>/album.json`（**不是** `<group>/_meta.json`）：
  相册名/标签/厂牌 + **`resource_roots`**（实际生效的 CDN 基址、序号格式、
  `source` = 页面线索/直链/探测、`site_default`）。任务全失败时它就是唯一的排查线索。
- 去重靠 `resources.hash`(sha256) 跨任务复用；`incremental` 命中 `find_done_resource(url)`
  即复用不发请求。⚠️ 尺寸/去重复用文件时**绝不删别人的文件**（那是别任务的产出）。
- `/tasks/{id}/archive` **流式** ZIP（攒 BytesIO 等于整包压内存）。
- 输出目录须绝对路径、禁 `..`、禁盘符根，实落 `<dir>/<task_id>/`；
  `/files/{task_id}/{p}` 用 `is_relative_to` 锁目录。

## 限速与重试（补充）

两闸门**正交**：间隔闸门决定"每 N 秒发一个请求"；并发闸门只限在途数，**不摊薄间隔**。
单次耗时 > 间隔时 `concurrency=1` 会让后续请求空等（分片场景致命）→ `domain_concurrency=3`。
`_limiter()` 按**站点**（注册域）分桶而非 netloc；可用 config `site_groups` 覆盖。

- ⚠️ 429 是"慢一点"不是"文件坏了"：按 `Retry-After`（秒 / HTTP 日期）等 + **站点级冷却**
  （按 `site_key` 共享），别套 8 秒封顶的普通退避——站点要求冷静几十秒时硬闯只会封更久。
- 普通失败才是 2^n × 0.6~1.4 抖动 —— 纯指数会让并发失败**集体重试**把站点/WAF 瞬间打爆。

## 过滤 / 类型 / 去重（补充）

四个解析器一次加载全跑：APIDetector > NetworkParser > JSStateParser > DOMParser。
被过滤 → `status=filtered` + `note`；单资源重试＝强制下载（跳过过滤）。
`min_image_bytes` 专治 1×1 跟踪像素/广告占位图（43~200B），真实缩略图通常 > 1KB。

## m3u8 与 ffmpeg（补充）

分片走**独立配置** `segment_concurrency`(4) / 间隔 0.15~0.35s，**不共用图片的 3~10 秒**
（曾把 10 秒的视频拉成 11 分钟）。分片级重试 + 断点续传，单片失败只坏那一片。
`video_engine`：`auto`（有 ffmpeg 就用，失败降级）/ `ffmpeg`（强制，失败不降级）/ `builtin`。

- ⚠️ ffmpeg 拉流时请求由 ffmpeg 发出：**DomainLimiter 不参与、mirrors 不轮换**、
  进度只整文件完成上报。
- `find_ffmpeg()` 失败结果**只缓存 30s**（装好无需重启后端）。
  本机路径：`%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`。

## ⚠️ img.xchina.io 站点特征（改动前必须重新验证）

1. **不返回 404**：存在 → `200 image/jpeg`；越界 → `200 text/html`。存在性**只能看 Content-Type**。
   `probe()` 返三态 ok/missing/error，5xx 与网络异常归 error（不与 missing 混同，否则图集被截断）。
2. **HEAD 是可靠的**（2026-09-18 复测 4/4 返回 `Content-Type`+`Content-Length`）。曾误记成
   "HEAD 无响应头"，那是 **curl 走系统代理时 `-I` 只回 `200 Connection Established`** 的假象。
3. **WAF 校验 Accept**：无 Accept 或 `*/*` → **403**；含 `image/*` → 200。常量见 `core/config.py`。
4. `xchina.co` 的 HTML 页对普通请求 **403**（Cloudflare）→ 资源发现不能依赖页面 HTML。
5. 一图 4 个下载点（`.jpg` + `_1200x0/_800x0/_600x0.webp`）→ 天然 mirrors。
   相册 `6aa5136f606fe` 实测 **1~142 连续无空洞**，143 起越界。
6. `var videos[].filesize` 实测精确（"64M" ↔ 67341834B，**零请求**）；另有 h1 / tag 可用。
   页面里的 `…date…` 可能是推荐位的，**归属未验证，别当发布日**。
7. 视频无档位/预览变体（`.m3u8`、`_600x0.mp4` 均不存在）→ `mirrors` 为空；单视频可达 105MB。

## 相册采集器 `xchina_gallery`（补充）

三种输入**都必须能解析**：相册页 `https://xchina.co/photo/id-{id}[/10].html`、
直链 `https://img.xchina.io/photos{N}/{id}/0001.jpg|.mp4`、图集 ID 本身。

- 画质档 `original/1200/800/600`：选定档作主 URL，其余按邻近度作 mirrors；缺该档回退最高档。
  ⚠️ 判断是否最高画质档要拿**变体后缀**跟 `site.variants[0]` 比，不能拿档名比
  （曾落盘成 `00001_.jpg.jpg`）。
- ⚠️ `base_candidate_digits=5` 自动展开 `photos2..photos5`（**从 2 起**，`photos1` 非真实形态）。
- ⚠️ `parse_resource_hint` 的 hint 必须传**用户原始输入**；传 gid 的话直链信息早丢了。
- 采到 0 个抛可操作错误（列出试过的基址 + 下一步动作）；预览也带 `resource_roots`/`warning`。
- 任务级 `options.proxy` 覆盖全局 `settings.proxy`（透传到 `discover(proxy=)`）。

## mirrors / 多媒体 / 命名选项（补充）

- `mirrors`：主 URL 失败依次切换；切换时清半成品且**不续传**（不同 URL 内容不能拼接），
  并按新扩展名改名。⚠️ **`require_image=True` 必开**，否则越界 URL(200+html) 会被当图存下来
  且永不触发切换。
- 同一 gid 下**图片与视频并存**（`6a3654854fd25` = 12 图 + 4 mp4，同名不同后缀）。
  `options.media` = `auto`/`image`/`video`/`both`；auto **两条线索都问**
  （相册页 `var videos` + 探一次 `00001.mp4`）——只信页面会静默漏采。
  ⚠️ `.mp4` 对任何 Accept 都返回 206，别据此推断别的路径。
- `options.album_title` = `clean`/`full`/`h1`/`id`（**`id` = 完全不开浏览器**）；
  `album_tags_dir` 再套标签目录（只取前 3 个标签，`-` 连接）。
  目录名取不到一律回退 gid（**绝不让任务失败**）。

## 相册页做 headless（Cloudflare）

三个坑：
1. 首次是挑战页 `<title>Just a moment...</title>`，须轮询；
2. **不能** `wait_for_function`（挑战靠一次导航完成，导航销毁上下文）、也**不能**按标题判就绪
   （8s 时标题是 `Loading <url>`，`content()` 紧接着抛异常）→ 只能轮询 `page.content()`，
   看有无 `photo-items`/`hero-title-item`/`var videos`/`objId` 标记；
3. **先匿名、失败再带登录态**（陈旧 `cf_clearance` 会招来 `Attention Required!` 永久拒绝），
   与 `collectors/browser.py` 相反。

### 长期对策（写在 `album_meta.py` 模块文档里）
**不投入指纹对抗**。真正的对策是让采集**不依赖那个 HTML 页**：资源发现走纯 HTTP 序号枚举，
相册页只提供目录名/自报数量/视频体积；拿不到就降级用 gid 命名（**采集照常**）。在此之上：

1. **域级熔断**：连续 3 次读不到 → 该域 10 分钟内不再开 Chromium（每次读页几十秒，
   明知被拦还开纯属浪费，也更像扫描器）；
2. **陈旧登录态隔离**：带登录态被拒即标记失效，`/sessions` 的 `cf_stale` 列出 → 提示重新登录；
3. **降级可见**：日志写人话并回答"接下来会怎样"。

⚠️ **被拦 ≠ 登录态失效**（匿名一样被拦），只有**带登录态**被拒才记到登录态头上。
⚠️ **页面结构不符**（改版/objId 对不上）不计入熔断 —— 那是另一个问题。
重登/删态后 `finish_login`、`remove_session` 会 `clear_domain_state()`，无需重启后端。

## 相册页自报数据 → 创建前预览（补充）

页面白给三样：`12P + 4V`、`filesize`、标签与厂牌。定位靠**图标/class 锚定**
（`fa-image`/`fa-file`/`tags-line`），不靠 div 顺序；`_TAGLIST_RE` 对换行缩进敏感，改前用真实页面复核。
`preview()` 有页面数据时**零序号枚举**；拿不到才受限枚举 → `sampled=true`，数量只是**下限**
（前端显示 `≥`）。

- ⚠️ 自报数量**不当资源清单**，序号枚举才是权威。
- ⚠️ 预览与创建**共用** `_gallery_options()`，否则"预览通过、创建被拒"。
- ⚠️ `photos`/`videos`/`video_bytes` **只统计本次真要采的媒体**，必须与 `media` 一致 ——
  曾直接回自报总量，预告写"12 图+4 视频 251MiB"而创建后一段视频没下。
  **预告与行为不一致比不预告更糟**。自报总量另用 `photos_declared`/`videos_declared` 带出。
- 体积前置：`probe()` 本就读 Content-Length → 下载层过滤**优先用 `r["size"]`**，
  真未知才 `probe_size()` ⇒ 图集任务零额外请求。⚠️ 回归表现是"每个资源平白多一次 HEAD"
  （用例里把 `probe_size` 换成 `pytest.fail` 就能逮住）。

## 视频页采集器 `xchina_video`（补充）

输入 `/video/id-{gid}.html`、视频 gid、或迅雷式带签名 m3u8 直链。
- 支持 `#EXT-X-KEY` 多密钥轮换与**相对密钥 URI**（按 playlist 路径拼绝对）。
- ⚠️ `browser_runner` 做成**可注入依赖**，单测用假运行器 + 假 session，绝不真触网。
- `video.py`：引擎分发**前**先 `_preflight_hls()`（挡过期/占位/无密钥）；
  `engine=ffmpeg` 无二进制时先于预检 fail-fast（确定性本地错不该被网络错掩盖）。

## 聚合页采集器 `xchina_aggregate`（补充）

一次收"整个模特/系列"的子页面 URL，再委派给 `xchina_gallery`/`xchina_video`。
- **URL 驱动抽取，绝不用 DOM 选择器**：正则 `<a href>` 后按 URL 模式分类。类名会改、
  URL 语义稳定；DOM 选择器改版即静默采 0 个。
- 真站结构（2026-09，**别猜**）：`/model/id-*`、`/actor/id-*` 落地页 **纯 HTTP 可读**；
  `/models.html`、`/models/type-*` 索引 200；`/photos|videos/series-*`、`/videos|photos/model-*`
  全量列表页 **403 CF**（可降级 headless）。我猜的 `/model/xxx`、`/tags/`、`/series/` 全 404。
- ⚠️ **归一化只归内容页**：相册 `/10.html` 是同一相册的分页 → 归主页；列表页的分页是
  **不同内容** → 不归（归了会漏采）。
- ⚠️ **`max_items` 闸门必须在"追加时"判**（只在进页面时判 → 形同虚设）。
- ⚠️ **截断必须说出来**（"还有 N 个未展开"写进日志与 `album.json`）。
- ⚠️ `preview` 与 `crawl` **同源**：`group`（目录名）与 `max_items` 必须共用同一套，
  否则预告的目录名/数量与实际落盘不符。
- ⚠️ 日志回调：`log(msg, "warn")` 两参调用会给只收一参的 `crawl_log`/`logs.append` 抛
  `TypeError`，**在采集全做完后写汇总那刻崩掉整个任务**。适配器必须收可选 `level`。

## 测试隔离（补充）

- `TaskManager.shutdown(wait=True)`：测试必须等 worker 真退出，否则随机失败（生产 `wait=False`）。
- ⚠️ 断言失败要 `check_eq` 打印期望/实际值 —— 只打标签的门禁排不了偶发失败。
- ⚠️⚠️ **测试会借磁盘文件偷偷互相通信**：CDN 画像落在真实库旁时，一个用例探到
  photos2 记一笔 → 之后每个用 XCHINA 的用例候选顺序被改 → "happy path 不该多花请求"
  失败，而报错只有"请求数 4 != 3"，看不出跟上个用例有关。**单跑绿、全跑红、重跑又绿。**
  解法：`tests/conftest.py` 里 autouse fixture 把 `UWC_CDN_PROFILE` 指到各用例的 `tmp_path`。
  凡"运行期会攒数据、测试会读它"的模块，都要有一个这样的开关 + 隔离夹具。

## 站点声明自检 / 形状软提示（本轮新增）

- `GallerySite.id_samples = [(url, 期望gid), ...]` + `check_site(site)`：`parse_gid` 取**首个命中**，
  新加正则若不慎也匹配旧 URL，行为**静默**变（某相册突然采空，日志全正常）。
  自检还断言"没有哪条 pattern **单独**就能配出不同结果"（谁先谁赢的隐式依赖）。
- 分工：运行期只 warn（不能因自检失败拒绝干活）＋ 测试里 `selfcheck_all() == {}` 硬断言。
- `gid_shape`：自动识别 **硬** 判据（不符不认领）；**手选只给软提示，绝不 400** ——
  手选是用户已表过的态，站点可能刚换 ID 格式而我们比用户知道得晚。
  ⚠️ 软提示**不塞进 `resolved`**（那个字段专指自动识别结论）→ `_pick_collector` 返回三元组
  `(名字, 识别结论, 软提示)`；`TaskCreateOut.warning` 是独立字段。

## CDN 画像（本轮新增）

- `data/cdn_profile.json`，`UWC_CDN_PROFILE` 可改路径或设 `off`。
- **消费 `seq_formats` 才是省钱的那一步**：探测循环是"格式外层、基址内层"，宽度不对时
  会把每个候选基址都白试一遍 → 实测 6 次探测。把上次命中的宽度提首位后 **1 次命中**。
- ⚠️ 画像只是**排序提示**，全部组合仍真探一遍；反向断言锁住"画像指错宽度时仍能回到正确宽度"。
- 另有用处：某基址占比**突然**从主跌到 0 = 站点在迁 CDN，比用户报障早得多。

## 感知去重 dHash（本轮新增，`core/phash.py`）

已有 sha256 认不出"换尺寸/重压缩/重复收录"，而这是图集站最常见的重复形态。
ffmpeg 解成 9x8 灰度 → 64 位 dHash，**零新增依赖**。

- ⚠️ **只标记不删除**（写 `resources.duplicate_of` + manifest），dHash 会误判（纯色/连拍），
  删文件不可逆且代价由用户承担。
- ⚠️ **失败即放行**：ffmpeg 不在/解码失败一律返回 None，绝不因此让下载失败。
- ⚠️ `-frames:v 1` 不能省：动图会让 ffmpeg 一路吐帧，输出超 9x8 时会把后续帧当同一张图的
  像素接着算 → "稳定但错误"的指纹，比报错难查。
- ⚠️ `distance()` 的 `None`（没法比）与 `0`（完全相同）**必须分开**；把前者当后者就是凭空冤枉文件。
- `find_duplicate` 返回**最近**的而非第一个命中的（提示语里要写距离，报个更不像的会毁掉可信度）。
- ⚠️ **只在同一任务内比对**：跨任务的"重复"用户点不过去也删不掉，属不可行动信息。
- 默认开、可关（`filters.dedup_perceptual`），但**不算过滤条件**（不进 `Filters.active`）——
  否则每个任务都打印一行"filter [无过滤条件]"。
- 实测判别力：同图换尺寸 **0**、同图重压 jpg **0**、异图(testsrc2 vs smptebars) **40**；阈值 4/64。

## ⚠️ `FilterIn` 静默丢弃字段（曾让界面开关空转）

`models/schemas.py::FilterIn` 曾只声明 8 个字段，而前端会发 `min_width`/`min_height`/
`exclude_ad`/`min_image_bytes`。pydantic 默认**忽略未知字段** → 参数在进入 `Filters` 前被丢掉，
**界面「尺寸下限」「排除广告位」开关按了没反应，无报错无日志**。
实测 `FilterIn(min_width='300').model_dump()` 返回 `{}`。

修法：① 已知键全部声明 ② `extra="allow"` 兜底（新前端+老后端混跑不再吞参数）。
回归断言拿 `Filters` 认识的键集合做对照 —— 以后新增维度忘了声明立刻红。
**教训：凡 pydantic 模型转发到"只读自己认识的键"的组件，必须显式声明 + 允许额外键。**

## 速度优化（本轮新增，`ratelimit.py` / `base.py` / `gallery_base.discover`）

- ⚠️⚠️ **吞吐由间隔决定，不由并发决定**：站点长程上限 = `1 / domain_min_interval`。
  `domain_concurrency` 只管"同时在飞几个"，调大不提速。
- 令牌桶 `domain_burst` 放大容量 = 平均速率不变、可花掉攒下来的配额。**不是**提速手段；
  降 `domain_min_interval` 才是（那是用礼貌性换速度）。
- AIMD 只按**成功与否**判定，**不看响应延迟**：CDN 边缘缓存与 429 都毫秒级返回，
  会被当成"服务器很闲"（Scrapy AutoThrottle 那套在此帮倒忙）。
- ⚠️ 只有传输层/服务端失败计入放宽；Content-Type 不合预期是这条 URL 的问题，
  拿它拖慢整个相册 = 把"URL 不对"误判成"站点限流"。
- 枚举快路径：`declared_count` + 抽样校验（**点必含首尾**），任一不中即退回逐张扫描。
  L1（指数+二分）默认关 —— 二分假定序号连续，中间缺一张就是**静默截断**。
- ⚠️ `pool_block=True` 的前提是**响应必须关闭**：416/429/内容校验这些提前退出的路径
  都没读响应体，连接不归还 → 泄漏到池满 = 永久阻塞（卡死而非报错）。

## 本轮踩到的四个坑（通用性很强）

1. ⚠️⚠️ **可重入死锁**：属性（如 `interval`/`rate`）各自取锁，而调用方持锁后再调它们 ——
   普通 `Lock` 不可重入，**当场死锁**，表现为"调一次就整个进程卡住"，堆栈看不出所以然。
   修法：锁内**就地计算**，锁换 `RLock` 兜底（可重入只掩盖问题，不解决）。
   排查手段：`timeout 120 pytest ... > log 2>&1` 再读文件 —— 挂死时 stdout 是被截断的。
2. ⚠️ **`sqlite3.Row` 没有 `.get()`**：误用抛 AttributeError；若该位置在 `try` 里，
   异常会被当成业务失败，报错离真相很远（表现为 `assert 'failed' == 'filtered'`）。
3. ⚠️ **`finally` 里的异常会覆盖原异常**：给真实对象加了一个方法调用后，测试替身没有
   该方法 → 掩盖真实原因、重试循环继续 → 最终报成毫不相干的错（"pop from empty list"）。
   **替身要模拟真实对象的完整契约**。
4. ⚠️ **写测试期望值前先确认数据形态**：序号是 5 位补零，`"/0030.jpg" in url` 永远
   匹配不上 —— 场景没生效，断言却在别处先红，很容易误判成实现有问题。

## 健壮性一轮（本轮新增：`errors.py` / `disk.py` / 原子写 / 孤儿恢复 / 门禁）

1. ⚠️⚠️ **PITFALLS 写得再全也没人会在加 `try` 之前读一遍** —— 所以真正的护栏是
   **`tests/test_cancel_guard.py` 的 AST 门禁**：凡 `try` 块内有取消源调用却没写
   `except TaskCancelled: raise`，pytest 直接红。首次运行就抓到 `video.py` 的真违规
   （取消被当成 ffmpeg 失败 → 降级继续下一个**已被叫停**的视频）。
   **新增下载/采集调用时要同步更新 `CANCEL_SOURCES`**，否则门禁静默失效。
2. ⚠️ **重启后任务永远卡在"运行中"**：库里 `running/extracting/downloading` 的任务
   没有任何 manager 持有，看门狗只在活着的实例里跑。→ `recover_orphans()` 挂在
   **`lifespan`** 上；放模块级的话 `import main` 就会扫库改状态（导入产生写副作用）。
3. ⚠️ **WAL 不等于并发写**：`sqlite3.connect` 默认 `timeout=5` 但项目里没设，且
   WAL 只解决读写并发，写-写仍单写者 → `database is locked` 落在业务 `try` 里被
   当成"资源下载失败"。→ `busy_timeout` + `_retry_write`（只对 locked/busy 重试、
   有上限）。**非锁错误不重试** —— 重试只是把真 bug 藏起来。
4. ⚠️⚠️ **非原子写 + 续传不校验 = 最难查的一类坏文件**：直接写最终路径，中断留
   半成品被当成成果；无条件 `open(path,"ab")` 续传，残片来自另一个 URL 也能拼出
   "文件在、大小对、内容是坏的"，而 sha256 算的是**坏的全文**，全部校验都会放行。
   → `.part` + `os.replace`；`.part.src` 记来源 URL（不匹配即丢弃重下）；落盘比对
   字节数（**有 `Content-Encoding` 时不比**，那是压缩后的长度）。
5. ⚠️ **磁盘满会走完整重试链空转**：几百个资源 × 完整退避，用户只看到一堆
   `No space left on device`。→ 预检 + 捕获即置 `abort` 让同批 worker 跳过。
   ⚠️ 不标 `cancelled` —— 已下好的文件是真实成果，终态交 `_final_status`。
6. ⚠️ **`finally`/`except` 里调回调时抛异常会顶掉真因**：采集器常在 `except` 块里
   `log(msg)` 报告错误，而 `crawl_log` 又是取消检查点 → 会把"基址全 MISSING"变成
   "任务已取消"。→ 只在 `sys.exc_info()[0] is None` 时检查取消。
7. ⚠️ **`monkeypatch.setattr(module, "open", ...)` 需要 `raising=False`**：`open` 是
   内置函数，模块里本来没这个属性。设进模块全局即可生效（模块全局优先于 builtins）。
8. ⚠️ **`"wb" + "x"` = `wbx` 不是合法模式**：独占新建写作 `"xb"`。
9. ⚠️ **把启动副作用放进模块级, 会让"导入"变成"写入"**：任何 `import main`
   （测试/脚本/文档工具）都会执行。挂 `lifespan`。
10. ⚠️⚠️ **共享磁盘状态必须显式隔离, 且隔离本身要可证伪**：DB / 下载目录 / 浏览器态
    是用户的真实数据，一个漏夹具的用例调 `db.create_task` 就往用户库里塞假记录。
    隔离集中到 `tests/isolation.py`（清单式：加一处只改一个文件）；另设 session 守卫
    整轮前后比对真实目录指纹，脏写即红。守卫逻辑本身有单测（`test_changed_keys_detects_writes`
    等），并验证过"拆掉 conftest 隔离 → 全量测试立即变红"—— 否则"看起来在隔离"比不隔离更糟。
11. ⚠️⚠️ **次序即契约：产物必须先于终态**。"任务到了终态"对消费者意味着"输出目录已完整"。
    原来 `_run` 先把任务 `transition` 成 success、manifest 到 `finally` 才补写 —— 中间那个窗口里，
    轮询到终态的消费者去读目录会扑空。症状是**偶发**："任务成功但 manifest 不存在"
    （verify_output 实测 3 次挂 1 次），单跑常绿、CI 偶红、重跑又绿。
    → 收尾次序倒过来：先 `_write_manifest(status=effective)` 再 `_settle_status`。
    ⚠️ ① 置态不能用 `_transition`（它开头 `_check_cancel`，在 finally 抛取消会顶掉收尾）
    → 另设绝不抛异常、迁移失败只 warn 的 `_settle_status`。
    ⚠️ ② `status` 要覆盖着传进 `_write_manifest`（写清单时库里还是 downloading），
    并用 `stopped or final` 兜住"取消恰好落在收尾窗口"→ 否则「清单 success、任务 cancelled」。
    ⚠️ ③ 回归用例不等时序碰运气：拦住"写终态"动作，在那一刻查 manifest 在不在。
    ⚠️ ④ 正常与 resume **两条分支都要走 finally** 收尾（resume 原先自己 transition+return）。

## 落盘布局 V28（本轮新增：`core/layout.py`）

12. ⚠️⚠️ **"文件落到哪一层"必须只有一个定义**。原先散在三处：采集器给 `filename`、
    `task_manager` 拼 `<task_id>/`、下载器 `resolve_target` 兜底。三处各改一点就会出现
    "能采到、但用户按预想去处找不到"的半通状态 —— 与 `filters.match_resource` 是同一种病，
    解法也一样。→ `core/layout.py`：`place()` 归位、`claim()` 消解重名、`meta_dir()` 定清单位置；
    采集器与下载器都来问它，不各自拼路径。
    ⚠️ 顺带：`place()` 里曾有一对**表达式完全相同**的分支（`album_of` 已经把多层折叠成最深
    一段）—— 死分支，删掉；留着以后只改一处就会漂移。

13. ⚠️⚠️ **重名必须在下载前消解，不能指望下载层兜底**。`downloaders/base.py::_prepare_resume`
    见到"目标路径已有文件"时会把它当**半成品**搬进 `.part` 续传 —— 它本来就是按"这是同一个
    文件的下一次续传"设计的。于是两个**不同来源**的同名文件被**拼接**成一份产物，而且字节数
    可能恰好对得上，最终以"成功"落盘。又是"文件在、大小对、内容是坏的"那一类（sha256 算的是
    坏的全文，所有校验都放行，用户拿到手才发现）。
    ⚠️ 归位查询要**两步**：`db.find_place_owner(计划名, 绝对路径)`（先看盘上真有的、再看计划名，
    覆盖还没下完的占位与下载时被换了扩展名的行）+ **本次采集内的内存占位表** —— 库查询只看得到
    已经写进去的行，同一个相册里两个同名资源会一起漏过。
    ⚠️ 区分词只给**平铺**文件（`0001_葡萄一番街.mp4`）：相册文件夹里的同名文件已经在该相册
    下面了，再缀一遍相册名只是噪音 → 纯序号 `00001(2).jpg`。

14. ⚠️⚠️ **别用 shell 判断换行符，也别信 `git show`**。`git show HEAD:<path>` 会应用 smudge
    过滤（输出 CRLF），`git cat-file -p HEAD:<path>` 才是原始 blob —— 两者对"HEAD 是 LF 还是
    CRLF"给出**相反**的结论，判断仓库换行只用后者。Git Bash 里 `grep -c $'\r'` 在 `for` 循环中
    结果不可信（同一文件循环里报 1399 行、单独跑报 0）→ 用 Python `bytes.count(b'\r')`。
    （本仓库 `autocrlf=false`、无 `.gitattributes`，HEAD 全是 LF；唯一例外是
    `scripts/verify_output.py`，它**一致地**是 CRLF，改它时保持 CRLF。编辑工具会把文件写成
    CRLF → 事后必须核对，否则整文件 diff。）

15. ⚠️ **清单不能跟媒体放一起**。视频平铺在下载根目录，清单要是也落根目录，每跑完一个视频任务
    就盖掉上一个的 `manifest.json`（用户拿到的溯源信息是串的）。统一进 `下载根/_meta/<任务ID>/`。
    ⚠️ 清单里 `file` 字段要相对**下载根**、而不是相对清单自己所在的目录，否则会写满
    `../../相册名/0001.jpg` → `build_manifest/write_manifest` 的 `out_dir`（写在哪）与
    `rel_base`（相对谁）必须分开。

16. ⚠️ **布局改了，删除语义也得跟着改**。`with_files=true` 不能再 rmtree `<下载根>/<task_id>/`
    （那层已不存在了），而且媒体现在与其它任务**共用一个根** → 改为按库里记录**逐条删**：
    越界校验 + `db.count_place_refs()` 跳过仍被别的任务引用的文件（内容去重会让它们复用同一份）
    + 清 `.part` 半成品 + `_meta/<任务ID>/` 整棵删。旧注释里把这件事写成"去重的固有代价、
    所以默认不删文件"，现在是显式处理。

## 接入新站点

新建 `collectors/<site>/spider.py` → `@register("<name>")` → 在 `collectors/__init__.py` import
（`__init__` 末尾 import 所有 spider，spider 又要读常量 → 分数常量必须放 `scores.py` 防循环依赖）。
**序号枚举型图集站**继承 `SequenceGallerySpider`，只声明 `GallerySite`
（`id_patterns`/`page_tail`（可为列表）/`gid_shape`/`id_samples`/URL 模板/变体/画质映射/
`base_candidate_digits`/`base_host_templates`），并可加 `match_score`。
**`id_samples` 必填** —— 那是声明自检唯一的护栏。开工前按 skill `gallery-site-probe` 探测。
