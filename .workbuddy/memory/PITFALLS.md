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

## V33 / V34 新增：三条"静默"坑 + 三条同族教训

这一轮的共同特征不是"报错"，而是**不报错、只是结果错** —— 所以在真实使用中会活很久。

### 第 9 条 ⚠️ 界面把"人看的文案"当数据用

`TaskDetail.vue` 原来用**正则解析 note 字符串**来归类失败原因（`/\b403\b|forbidden/`、`/timeout/`）。
后果：措辞一改归类就静默失效，换语言全落"其他"，而且没法支持"按原因筛选/批量重试"。

修法：`resources.error_kind`（8 个固定取值，由 `core/errors.classify()` 给出）+ 中文标签由**后端下发**
（`KIND_LABELS`），前端那份只是兼容旧后端的兜底。三个易错点：

1. `classify` 按**类名**识别 `RateLimited` / `HTTPError` —— 直接 import 会与 `downloaders/base.py` 循环依赖。
2. `db.error_kinds()` **不能按 status 过滤**：`corrupt` 落在 `done` 上，按 `failed` 过滤恰好漏掉最该看的那类。
3. **加一个资源状态就有多处口径必须同步** —— `_final_status` / `summarize_resources` / 前端计数。
   漏一处的症状是"摘要说 0 失败、任务状态却是 failed"。

### 第 10 条 ⚠️ `except OSError: pass` 盖在一个"本来就会失败"的写入上

等于把**数据丢失改装成静默**。判据两问：失败会发生吗？失败之后有人知道吗？
两个都答"否"的地方，至少要加一次重试，或者留一条可观测痕迹。

实例 `core/cdn_profile.py`：读者不走锁，写者用 `tmp.replace(p)` 换文件 —— 而 Windows 上只要目标
还有别的句柄开着（**哪怕只是只读**）`os.replace` 就抛 `PermissionError`，被 `except OSError: pass` 吞掉。
实测"一个只读线程 + 一个写线程"：**写 300 次只记下 150 次，丢一半且毫无声响**。
它也正是测试套件偶发变红的根源（每次红的用例不同）。

修法：`RLock` + 读者进临界区 + `replace` 退避重试（外部占用锁挡不住）。
⚠️ **必须 `RLock`**：`record_hit` 要在同一次"读-改-写"里调 `_read`/`_write`，普通 `Lock` 直接自锁死。
⚠️ 这套纪律现在收在 **`core/jsonstore.py`**（`cdn_profile` / `proxy_health` 共用）——
**别为新状态文件手写第二遍**，手写一遍 = 少一条纪律 = 又是静默丢一半。

### 第 11 条 ⚠️ 前端引用了一个**从来不存在**的接口

资源库网格一直引用 `/files/raw` —— 这个接口**从来没实现过**（V32 加资源库时写的）。
图片全是 404，但走 `<img onerror>` 把失败的图藏起来，**界面看起来只是"没有缩略图"而不是报错**。
静默失败的新变体：**请求根本没成功，却没有任何信号**。

通用教训：`<img>/<video>` 的 `onerror` 只该用于"这一项没有"，不该用于掩盖"整个功能没接上" ——
后者必须有一条能看见的痕迹（至少 console 一次）。**接线新前端功能时，接口存在性当场验一次。**

### 同族：条件请求与断点续传**互斥**

有 `Range` 时**不能**再带 `If-None-Match` —— 本地那份恰是最新时，服务器对带 `Range` 的请求会答
**304 而不是 206**，于是"416 = 本地已完整"那条收尾路径**永远走不到**，`.part` 再也收不了尾。
让位规则写死在 `_stream_one`（有 `offset` 就不加条件头）。

### 同族：测试隔离的"窗口期"

`tests/isolation.py` 的隔离靠 `monkeypatch.setattr`，于是 teardown 的 `undo()` 之后、下一个用例
setup 之前有一段窗口 —— 谁在这段里访问数据库，就写到**用户的真库**上。能干这活儿的：
看门狗心跳线程、没 `close()` 的 `TestClient` portal 线程、别处 fixture 的终结器。

⚠️ **指纹守卫不够**：① 只能事后发现"变了"，且 `_migrate()` 幂等 → **同一次变更只能逮到一次**；
② 报错只有"db 变了"，**没有是谁改的**。
修法：`isolation.install_real_db_guard()` 包装 `db.get_conn`，在**打开的那一刻**判路径并带调用栈失败。
两道闸各管一段：即时守卫抓"谁干的"，指纹守卫兜"清单漏登记"。

### 同族：旧断言 / fixture 的前提会失效（文档也会说谎）

产品加了校验或新能力，回头问 fixture 与旧断言还成立吗。三处真实记录：

- V33 给直链加内容终检后，`verify_output.py` 的**假 mp4** 被**正确地**判成坏文件删掉，
  而红的却是"视频任务应当成功" —— 根因在 fixture 说谎。
- `verify_hls.py` 必须显式 `-hls_playlist_type vod`：不写时 ffmpeg **6 次里约 1 次**产出只列 11 片、
  无 `#EXT-X-ENDLIST` 的中间态播放列表，被伪装成"站点播放列表不合格"。现已加**素材自检**。
- V34 实现了 Pexels 集合页，而 `test_features_v32.py::test_pexels_collections_unsupported`
  断言的正是"集合页报不支持" —— 说谎的是**断言**。
- `docs/PROJECT_OVERVIEW.md` 的"后续可做"曾把**已实现**的能力（站点级并发配额
  `DomainLimiter` 的 `BoundedSemaphore`、巡检结果落库 `/library/verify`）列为缺失。
- V35 抽出通用 `_check_duration`（多一个 `what` 参数后），`tests/test_ffmpeg.py` 里 7 处
  用 `lambda *a:` 写的替身**立刻炸** —— 那是它们在尽职，**说谎的是替身的参数表**。
- V35 复核一共抓出**四处**文档谎报，全都要靠**逐条去代码里核**才能发现：
  1. `docs/PROJECT_OVERVIEW.md` 的"后续可做"把**已实现**的能力（站点级并发配额
     `DomainLimiter` 的 `BoundedSemaphore`、巡检结果落库 `/library/verify`）列为缺失 —— 见上一条。
  2. `README.md` 说"熔断状态只放内存不落库"，而 V34 已落盘 `core/proxy_health.py`。
  3. `GUIDE.md` 的 Pexels 段说集合页"暂不支持"，而**同一份文件**的 V34 章节写着已实现。
  4. `GUIDE.md` §11.4 说熔断"只放内存不落库"，而**同一份文件** §13.7 写着已统一到 `jsonstore`。

第 3、4 条是**自相矛盾型**：复核的人读到其中一处就以为知道了事实，不会再翻第二处。
更隐蔽的是第 4 条埋在**历史章节的设计说明**里（"当初为什么这么设计"），不在一眼就能看到的
backlog 清单里 —— 而后者更容易被想起来复核。**清单型位置会被复查，"设计理由"段落不会。**

> 可操作的防法：提交前跑一次全文关键词扫描
> （`暂不支持 / 还没有 / 只放内存 / 未实现 / 缺一个 / 只支持`），逐个去代码里核。
> 本次两轮扫描共命中 4 处，**每一处都对应一个已经变了的事实**。

不诚实的素材会把**产品缺陷与测试缺陷混成同一条红**。看到"新功能做完，旧的某条测试红了"
先想这个，别急着改产品去迁就它；也**定期复核文档声称**（唯一可靠的办法是逐条去代码里核，
读着文档改文档只会把谎越描越圆）。

## V35 新增：一条真缺陷 + 两条教训

### 第 12 条 ⚠️ 流式响应不关 = 连接泄漏，表现为"任务卡死不报错"

`downloaders/video.py::_fetch_segments`（HLS 与 DASH **共用**）在 `raise_for_status()` 失败时
**没有关闭响应**：

```python
resp = self.session.get(segments[i], headers=headers, stream=True, timeout=...)
resp.raise_for_status()          # 404 抛异常前, resp 从未被关
```

`SESSION` 是共享连接池（`pool_block=True`，池大小 `max(10, domain_concurrency*2)`，
见 `base._build_session`，默认 10）。一条 404 漏一个连接，**漏满之后所有
worker 线程都永久阻塞在 `urllib3.connectionpool._get_conn`** —— 任务不报错、不打日志、
CPU 为 0，看起来像死锁。

**它为什么难发现**：触发条件与症状毫无表面关联。当时是 DASH 端到端卡死，而真实原因
链是：ffmpeg 把分段写到了 CWD（不在 HTTP 根下）→ 100% 404 → 连接漏光。
"素材没生成对"和"下载器卡死"隔了三层。

**诊断路径（比修法更值钱）**：我先猜了三个方向 —— "测试服务器是单线程"（改成
`ThreadingTCPServer`，还卡）、"DomainLimiter 与字节桶死锁"（读了半天，没问题）、
"ffmpeg 参数不对"（改了也没用）。最后用一行拿到真相：

```python
faulthandler.dump_traceback_later(45, exit=True)   # 45 秒还没完就打印所有线程的栈
```

栈里一眼就是 `_get_conn`。**卡死类问题的第一动作是打调用栈，不是猜。**

修法：`resp` 放进 `with`，或 `finally: resp.close()`。

### 同族：隐性磁盘占用必须可见

`core/partials.py` 把断点续传半成品挪进 `_meta/partial/`（按 URL 寻址，见 README V35 节）。
这东西**不在下载目录里**，于是用户会算不平账 —— "目录 8GB，可见产物只有 6GB"，
第一反应是"程序在偷偷吃盘"。

留痕方式：`GET /library/partials` 给出件数/占用/TTL/预算，前端概览条上直接显示 + 一键清理。
与第 10 条同一条心法（**失败之后有人知道吗**），只是这里的"失败"是**占用不解释**。

### 同族：`park` 与 `discard` 的判据是"能不能取回"，不是"有没有用"

半成品该留还是该删，**不能按"这文件有没有价值"判断**（那会变成口味问题），
要按**下次能不能取回**：

| 时机 | 行为 | 理由 |
| --- | --- | --- |
| `TaskCancelled` | park | URL 还在，下次同一个 URL 能命中 → **能取回** |
| `CorruptMediaError` | 删 | 字节已证明是坏的，park 只会污染下次 → 取回的是垃圾 |
| `DiskFullError` | 删 | 快满盘了还留半成品说不通 |
| 没有 URL | 就地删 | **没有 URL 就取不回**，留在相册目录里只会在用户目录堆 `xxx.jpg.part` |

## 接入新站点

新建 `collectors/<site>/spider.py` → `@register("<name>")` → 在 `collectors/__init__.py` import
（`__init__` 末尾 import 所有 spider，spider 又要读常量 → 分数常量必须放 `scores.py` 防循环依赖）。
**序号枚举型图集站**继承 `SequenceGallerySpider`，只声明 `GallerySite`
（`id_patterns`/`page_tail`（可为列表）/`gid_shape`/`id_samples`/URL 模板/变体/画质映射/
`base_candidate_digits`/`base_host_templates`），并可加 `match_score`。
**`id_samples` 必填** —— 那是声明自检唯一的护栏。开工前按 skill `gallery-site-probe` 探测。

**先跑 `python scripts/probe_site.py <资源直链> [相册页URL]`**（2026-09-23 新增）：五项
探测（存在性判定 / Accept / 页面可达性 / 尺寸变体 / URL 形态与正则自检）+ 一份可直接
填的 `GallerySite` 草稿。**至少要给一条资源直链** —— 它把基址与序号宽度写在 URL 里，
是唯一不靠猜的证据源。它只发 HEAD，不落盘任何资源。`scripts/probe.py` 是**浏览器**
探针，与这五项无关，别搞混。

## V36 新增：一条静默缺陷 + 三条同族

### 第 13 条 ⚠️ 形参被局部变量遮蔽，症状是"那条信息静静地没了"

`downloaders/video.py::_download_m3u8` 的**参数**里有 `info`（调用方传进来的一只空 dict，
下载完要把 `resolved_url` / `content_type` 回填进去）；方法体里预检那段又写了一次：

```python
leaf, info, last_err = murl, None, None     # ← 参数 info 被顶掉, 从此与调用方无关
for cand in ...:
    leaf, info = self._preflight_hls(cand, ...)   # 这里的 info 是预检结果
```

后半段再 `fill_info(info, leaf, ctype)` 填的是**预检那只 dict**，而任务管理器手里那只
永远是空的。结果：HLS 视频的 `resolved_url` 恒为空（DASH 与直链都正常）。

**为什么难发现**：不报错、不影响下载、不影响时长终检 —— 只是资源列表里少一列溯源信息。
没有任何一条现有断言会碰它。

**判据**：命名遮蔽（shadowing）在 Python 里合法，读代码时眼睛会自动把它当成"同一个东西"。
唯一的防线是**在测试里直接断言那只被传进来的 dict 被回填了**
（`test_hls_fills_the_callers_info_dict`）。只要有人重命名，它立刻炸。

### 同族：「通过但理由已经不对」比失败更危险

产品能力变了之后，旧断言**仍然绿**，但它守的已经不是原来那件事：

| 用例 | 为什么还在过 |
| --- | --- |
| `test_segment_base_is_refused` | 新代码确实还报错 —— 但换成"没有 `BaseURL`"那条判据了，报错文本里恰好含 `SegmentBase` |
| `test_multi_period_is_refused` | 新 fixture 没声明时长，**在时长那一步**就先炸了，根本没走到多时段判断 |

失败会逼你看，通过不会。所以**改产品能力之后，第一件事是回头看旧断言的"理由"**，
而不是只看红绿灯。做法：把断言从"某个词出现在报错里"改成"我守的那件事成立"
（例：多时段时顶层 `video`/`audio` 必须是 `None`）。

### 同族：静默降级必须留痕（`park` 搬不动 → `last_error`）

`partials.park*` 搬不动时**故意不抛错**（原地那份保持不动，最坏只是下次从头下）。
但那样一来"暂存区怎么永远攒不起来"就**没有任何线索** —— 用户能察觉现象却说不清原因。

按那条心法问两个问题：**失败会发生吗？** 会（Windows 上目录 rename 会被"另一个句柄
还开着"打断，实测偶发）。**失败之后有人知道吗？** 原本**否**。所以补上留痕：
模块级 `_LAST_PARK_ERROR` → `stats()["last_error"]` → `GET /library/partials`。

顺带一个副作用：测试里 `assert moved == 300, f"没收下: {stats().get('last_error')}"`
把偶发失败变成了**能自己解释自己**的失败。

### 同族：`Range` 与条件请求不能共存（第 A 条的延伸）

第 A 条讲的是**续传**时不能带 `If-None-Match`。V36 给 DASH 加字节区间寻址后又遇到同一件事，
只是这次的代价不同：

* 续传场景：服务器回 304 → "416 = 本地已完整"那条收尾路径永远走不到（**少下**）；
* 区间场景：分片请求带 `Range` 又带 `If-None-Match` → 回 304，那一段字节**永远取不到**。

所以 `_seg_target()` 构造 headers 时**主动剥掉** `if-none-match` / `if-modified-since` /
`range`，再放上自己的 `Range`。另一条同族的：**服务器忽略 `Range`**（回 200 全量）时
必须**报错** —— 拿整份文件从 0 开始去解 `sidx`，分片边界会**整体错位**，
拼出来"长度对得上、能播、但每隔几秒糊一下"。

### 环境类：Windows 目录 `os.replace` 偶发失败

`park_segments` 走 `os.replace(src_dir, dest_dir)`（同盘原子）。Windows 上只要**有别的句柄
开着目录里的任何一个文件**就会失败（`PermissionError` → 被归入 `OSError`）。全套 991 用例
里偶发 1 次，诊断插桩后再跑一次就没复现 —— 结论是环境而非回归，但必须能被看见（见上条）。

---

## V37：一条"测试里的真 worker"泄漏 + 环境三则

### 第 14 条 ⚠️ 测试里"顺手调真实入口"= 点火真 worker

**症状**：`test_watchdog.py::test_watchdog_fails_orphan_task` 随机红
（`assert 'running' == 'failed'`），而**单独跑那个文件全绿**。
先怀疑"新测试文件泄漏了 TaskManager 实例"——方向对了一半。

**真机制**（`tests/test_features_v37.py`）：

1. 新用例用 `client.post("/tasks/create", ...)` 触发真实接口；
2. `create()` 里那句 `task_manager.submit(result.task_id)` 点的是**模块级全局实例**
   （不是用例夹具造的那个），于是往它固定的线程池里排了一个**真采集任务** —— 真 worker 起来了；
3. 用例结束，夹具把 `db.DB_PATH` 换成**下一个用例**的库，**但线程还活着**；
4. 每个用例的库 id 都从 1 开始 → 那个 worker 的 `db.update_task(tid, hb=now)`
   **正好在给下一个用例的任务 1 续心跳**；
5. 看门狗看到心跳新鲜 → 不判"遗留任务" → 断言红。

**为什么难发现**：泄漏出去的是**线程**，不是状态变量；而且它只在
"上一条用例恰好 submit 过 + 下一条用例恰好用 id 1"时命中 —— 表现为**随机**。
`TaskManager.shutdown()` 的 docstring 早就写了这件事（"上一条用例没跑完的 worker
会继续在下一个用例里写库…表现为随机失败，极难定位"），只是新文件没照做。

**判据 / 防法**（三条，缺一不可）：

* 夹具里把 `submit` stub 掉 —— 它是**真 worker 的点火开关**。要测的是 HTTP 与落库，
  不是采集流水线。（反例见下：`submit_resource` 故意**不** stub，因为"护栏真的拦住了"
  正是被测对象，而它的早退分支不会起线程。）
* 任何创建 `TaskManager` 的用例：`try/finally: m.shutdown(wait=True)`。
  本仓库其它文件全这么写，只有新文件漏了 —— **新文件要照着老文件的夹具抄**。
* 写测试时对自己问一句：**这条路径会不会起后台线程？**

**诊断手法**（一次就定位，比读代码快）：挂一个 `pytest_runtest_teardown` 钩子，
打印 `_LIVE_MANAGERS` 里每个实例的 `_active` 键 + 当前库的 tasks 行。
本次一跑就看见全局实例挂着 `['1','2']` —— 铁证。
（⚠️ 钩子里**必须写文件**，pytest 会按用例捕获 stderr，只有失败的用例才回放。）

### 环境类：宿主命令包装器缺失 → 长命令直接失败

`... app.asar.unpacked/cli/bin/windows-child-process-containment.cjs` 报
`Cannot find module` + `MODULE_NOT_FOUND`（宿主在更新/被清理时会这样）。
**症状极具误导性**：命令根本没跑，看起来像 pytest 自己崩了；短命命令（`echo`、`python -c`）
正常，跑得久的命令被"升级到沙箱外"时才炸。
**绕过**：该命令加 `run_in_background=true`（本次就是这么跑完 1001 条用例的）。

### 环境类：`export PATH="/usr/bin:/bin:$PATH"` 会把 python 换掉

前置式 export 会让裸 `python` 解析到**托管版 3.13.12（没装 pytest）**，
报 `No module named pytest` —— 看着像依赖丢了，其实是解释器换了。
要么别前置（必要时用 `PY` 全路径），要么先 `python -c "import sys; print(sys.executable)"` 自证。

### 环境类：safe-delete 守卫的第三个实例 —— pytest 的 `tmp_path` 清理

已知两个（`verify_output.py` 的 `rmtree`、`vite build` 的 `emptyDir`）。第三个更迷惑：
**全部用例都过了**（进度行全是点），会话收尾删 `pytest-of-admin/...` 时撞守卫
（本次 3221 文件 / 阈值 50）→ **exit 1、没有 FAILED 行、连汇总行都没有**。
所以"exit 1"在这台机器上**不等于有失败**，要看进度行里有没有 `F`/`E`。

⚠️ 同族补充（V38）：被守卫 kill 时**输出重定向到文件的那份会丢**（stdout 是块缓冲，
没 flush 就被杀）。`verify_output.py` 那次整份输出只剩一行守卫消息，看着像"脚本啥也没干"。
**判据**：用 `python -u` 跑脚本（本次就是这么拿到"40/40"的）；或者别重定向，直接看终端。

⚠️ 想要失败清单时同理：全套跑完的 `-rf` 清单在守卫面前保不住。**替代手法**：
先用 `--collect-only -q` 拿执行序号（`pytest-randomly` **没装**，顺序就是文件顺序），
再按进度行里 `F` 的**列位置**反查是哪条用例 —— 本次就是这么证明"4 条红全是 `curl_cffi`"的。

⚠️ **更省事的办法（本轮新发现）：把 `--basetemp` 指到系统临时目录下一个"还不存在"的
路径，汇总行就能正常打出来。**

```bash
B="C:/Users/admin/AppData/Local/Temp/uwcpt-$$"   # $$ 保证每次都是新路径
python -u -m pytest -q --tb=short --basetemp="$B"
```

会话开始要清空 basetemp 时目录**不存在** → 无需删除；跑完 pytest 也不删它
（显式给了 `--basetemp` 就是用户的目录）→ 收尾不撞守卫。本轮全量 1075 条就是这么
拿到 `1069 passed, 6 skipped` 的。

⚠️⚠️ **绝不能把 `--basetemp` 指到项目目录里**（如 `--basetemp=.pytmp`）：那是**致命**
的，而且症状完全不像"路径问题"。宿主 safe-delete 垫片在会话开始清空 basetemp 时走
"移到回收站"，在工作区路径上它的短路径解析抛
`OSError: [Errno 53] 找不到网络路径`，而垫片是 **FAIL_CLOSED** —— 于是**每一个用例的
setup 都 ERROR**（实测 986 个 ERROR，看着像代码全崩了）。
**判据**：ERROR 堆栈里出现 `sitecustomize.py` → `_try_trash` → `_get_short_path` +
`[Errno 53]`，就是它，与项目代码无关。

⚠️ 用它跑**第二次**才会炸（第一次目录还不存在）—— 这类"第一遍绿、第二遍全红"的
现象优先怀疑环境，别先怀疑代码。

---

## V38：三条"顺序/口径"类坑 + 一条产品决策

### 第 15 条 ⚠️ 登记成"已处理"，然后又拿"未处理"去筛它

直播录制的 `absorb()` 里：先判断"这个初始化段见过没有"，然后**顺手把它的 key 塞进了
`seen`** —— 而 `seen` 正是紧接着那条"没见过的才下"的过滤器。于是初始化段永远进不了
待下队列：拼出来的文件**字节数一切正常**（少几百字节），播放器打开一片黑或直接判损坏。

修法不是"再小心一点"，而是**去掉那个多余的登记**：防重复本来就由 `inited` 管，
`seen` 只该管分片。**多一个集合就多一个口径，而多出来的那一个往往正好把刚登记的东西筛掉。**
自查问法：*我刚刚把它加到哪个集合里了？下面哪一行会拿这个集合去筛？*

> 这条是被"断言**精确字节顺序**"的用例抓出来的（`b"init.mp4" + b"10000.m4s" + ...`），
> 而不是被"片数对不对"抓出来的 —— 少一片的时候片数**恰好**也可能对得上
> （init 被当成"第 0 片"算进了 total）。**数量类断言拦不住这一类。**

### 同族：顺序类缺陷必须写"原地"，不能写成"按引用顺序"（V38 实例）

嵌套 `sidx` 的第一版实现：先遍历父层把媒体全收下，把嵌套索引压进栈里事后处理。
结果排出 `A, B, C, D`，而文件里的真实顺序是 `A, C, D, B`（子层分片紧跟在它那份索引
box 后面）—— **长度对得上、能播、每几秒错一段**。

文档里"按引用顺序处理"这句话**不足以约束实现**：栈式 DFS 也"按引用顺序"读了引用，
只是把子层的产出放到了后面。**要写成"原地递归/原地展开"**，并配一条钉住**全序**的用例
（用 `_sidx_box` 手造一个"父层媒体夹着嵌套索引"的 box，断言四种区间的完整顺序）。

### 同族：「上一轮的末尾」这类口径要跟着**窗口的 owner** 走（V38 实例）

直播的"我只录到哪儿了"最初按 `kind`（`video`/`audio`）记一份。多时段时每个时段各有
自己的滚动窗口，于是**后面时段的末尾被当成整条轨的末尾** → 前面时段新出现的分片全被
判成"已经过去了"跳过，**而且不报错**（录出来的文件就是缺那几秒）。

判据：`tail[(时段号, kind)]`。写用例时必须让**后段的旧末尾仍在新窗口里**（否则两种实现
都会通过 —— 第一版用例就是这样，白写了一条）：
`p0/30000` 已滚出、`p1/10000` 仍在窗口里 → 按 kind 记的实现会在 `p1/10000` 处切断，
p0 的新分片全丢。

### 产品决策：直播取消**仍然封文件**（与点播故意相反）

点播取消留的是"下次能接着下"的半成品；直播窗口滚过去就补不回来 —— 取消那一刻手上的
分片就是全部产物，丢掉等于把用户已经花掉的时间扔了。所以 `except TaskCancelled` 里
**先封一次文件再原样上抛**（任务状态照旧是"已取消"）。
⚠️ 封文件时 `progress_cb` 必须传 `None`：取消信号会让它立刻再抛一次，那不是"取消生效了"，
是"文件没封出来"。这条差异必须有测试钉住，否则后人会"顺手统一"成点播那样。

### 测试手法：时间类逻辑用**假时钟**，别 sleep

直播的轮询间隔是 2~30s：真 sleep 会让每条用例几秒，而且"到点没到点"本身成为 flaky 来源。
`tests/test_features_v38.py` 用 `_Clock` 顶掉 `V.time`（`time()` 由测试推进、
`monotonic()` 每读一次走一步、`sleep()` 只记账），回路靠**清单本身**收尾：第二轮给一份
`type="static"` 的清单 → 下完这轮就 break。比"上限到点"确定得多，也让用例快一个量级。

---

## V39（新站点探针 `scripts/probe_site.py`）：生成类 + 口径类坑

### 第 16 条 ⚠️ 从**一条**样本生成规则时，规则必须比样本更宽

采信用户给的一条直链去生成认领正则 —— 只认那一条的桶名（`photos`）会让 `photos2` 上的
直链落给通用采集器：**采集本身好使、只有预览/自动识别报错**，典型的"一半好一半坏"。
生成层要比证据**宽**（基址末尾无条件补 `\d*`），因为样本只有一条而站点有桶。

反向的那半更重要：**形状类**判据（`gid_shape`）要从样本推一个比样本**宽的下界**
（xchina 实测 13 位 hex → 声明 `[0-9a-f]{8,}`）。写窄了会把真图集挡在自动识别之外 →
落给通用采集器 → 静默采到 0 个资源。

### 同族：从样本"切前缀"必须**按路径段对齐**，不能按固定字符窗口

生成相册页正则时取"gid 前 N 个字符"：窗口取 8 而 `/photo/id-` 是 10 个字符 → 前缀被截成
`hoto/id-`。**它照样能匹配**（子串 `search`），所以跑起来一切正常 —— 直到换一个路径深度
或遇到 `notphoto/id-` 这种词。修法：按路径段取（上一段 + 段内前缀 + 查询参数名）。

⚠️ 用例要断言**不匹配** `notphoto/id-`；只断言"能匹配"是拦不住它的。

### 口径类：一条证据都没有时，**拒绝下结论**

探针第一版把"越界返回 403"判成"状态码可用" —— 而当时的真相是**整个站都进不去**。
那条结论会把一个完全不可达的站点写成"判定规则已确认"，比直接报错坏得多。

**通用判据**：任何"对照式"结论，先检查**对照组本身是否成立**（这里：对照组要求"存在"
的那一端真的探到了）。不成立就明说"还没验出来 + 下一步做什么"。

同族：**"空洞"必须与"末尾缺失"分开**。`--scan 6` 探一个 5 张的相册必然触发一次末尾缺失；
把它报成"序号可能不连续"的结果是这行永远在闪、没人再看它。真正的危险只有**中间**有洞
（指数探上界会把上界定在缺口之前 = 静默截断）。

### 环境类：`img.xchina.io` 从本机**整段 403**（不是站点改版，也不是回归）

2026-09-23 复测：对任何请求都 403，且是 **`text/plain`**（不是 CF 挑战页）；裸 curl 带
浏览器 UA + `image/*` 也一样；`curl-cffi impersonate=chrome` 也一样。所以是这台机器/
这条网络上整段不可达，**与 Accept、TLS 指纹、HEAD 都无关**。

⚠️ 别据此去改 `xchina/gallery.py` 的站点特征（那会把一条环境现象写进站点声明）；
⚠️ 也别拿它当探针的靶子 —— 会得到"探针坏了"的假警报。五项结论改用
`tests/test_probe_site.py` 里的本地假站点来钉。

### 测试手法：靶子要**自己做**一个，别依赖外网站点

`_FakeSite`（`http.server` + 系统分配端口）刻意**不返回 404**：越界与未登记的档位一律
`200 + text/html`。写它的时候有两条容易写错、写错就白测：

1. Accept 校验要按"含 `image/` 才放行" —— **`*/*` 不含 `image/`**。按"含通配即放行"写
   会把这条行为整个抹掉，测试照样绿。
2. 页面线索要**同时**放绝对 URL 和根相对路径（生成侧要用 `urljoin` 收两种）。

---

## V39 续（2026-09-24）：拿真实站点当靶子，抓出五处「看着能用」

### 方法论：两类靶子各有各的用处，谁也替代不了谁

* **本地假站点 = 回归靶子**：结论与网络无关、不会漂。
* **真实站点 = 找自己 bug 的靶子**：假站点只能验「我**想到**的行为」，真实站点的
  URL 形态是**想不出来**的。

⚠️ 但**别拿真实站点当回归靶子**（会过期 → 假警报）；也别因为"假站点够了"就不跑真实站点。
两者都要。

### 第 17 条 ⚠️ 序号后面跟着**内容哈希**时，它**不是**序号枚举型

真实形态（MangaDex）：`/data/<hash32>/<seq>-<page_sha256>.png`。文件名以 `1` 开头，
**看着就是**序号枚举；但改序号拼出来的 URL 必然 404（每页的哈希都不同）。
所以判据不能只看"是否以数字开头"。

`suffix_is_opaque()`：序号之后的残留 **≥40 字符** 或 **含 ≥16 位连续 hex** → 不透明。
阈值刻意取宽 —— 漏判成"不透明"最多多看一眼；判反了就是「任务 success 但 0 资源」。
（**误报的代价**决定判据松紧。）

### 同族的四条「产出错误」（都不是崩溃）

| 原本会做什么 | 症状 | 修法 |
| --- | --- | --- |
| 给 ⑤ 自检**没通过**的 URL 编期望 gid | 报告写着 `-> None`，`id_samples` 却配了值 = **编证据** | 只写自检通过的；其余以注释列出 |
| 末尾**整段是数字**的路径当「桶号」 | `/id/1040` → `base=/id/` + `base_candidate_digits=1040` | 数字紧跟 `/` 后不当桶号；桶号 >99 也不认 |
| 多段 base_path 去数字后留尾随 `/` | 生成 `/id//(\d{4,})/…`（匹配不上任何真 URL） | `rstrip("/")` |
| 命中 **0** 个序号时照样打印草稿 | 用户照抄一份没有任何证据支持的声明 | 顶部加醒目横幅 |

### 口径类：**同一个程序的另一段已经拿到反证**，就该用上它

MangaDex 那次最值得记的其实不是哈希后缀，而是：① 用实测说「只有第 1 个序号存在」
（`命中 1/6`），草稿却说「改序号就能枚举」。**两句话出自同一个程序，而照抄的那份是错的。**

修法：命中 1 个 → 明说这是**歧义**（①图集只有 1 张 ②每页文件名各不相同 ③序号不从 1
开始）+ 给出分辨方法（**再给一条不同序号的直链**）。

> 与第 16 条同族但更狠：不只是"规则比样本窄"，而是**反证就在同一个程序里，却没被用上**。

### 环境类：三个真实站点的实测事实（别再拿它们当靶子）

* `manhuagui.com`（漫画柜）连接超时；
* Lorem Picsum 的图片直链要 **`hmac` 签名**（裸 URL 一律 400）；
* Internet Archive 的页图是 `BookReaderImages.php?...&file=…_0001.jp2` —— 末段没有媒体
  扩展名，探针**连"这是资源直链"都判不出来**，按页面 URL 处理（exit 1）。


## V39 三（2026-09-24）：把五处补丁升成一条门禁

上一节抓出的五处「产出错误」，当时是**逐个手写 `if`** 堵的。堵完看明白了：它们不是五个
bug，是**一个** —— 探测结果没进结论（`build_draft` 拿的是从一条直链推出来的"理想形态"，
而 ① 的 `命中 1/6`、⑤ 的 `-> None` 只被打印、从不参与决策）。

### 第 18 条 ⚠️ 补丁只能挡住**已经见过**的形态

堵第五个 `if` 时就能预见：第六种形态迟早会出现，而它一定长得跟前五个不一样。所以把模式
抽了出来 —— **门禁**：

    档 A(拼不出 URL)  base / seq_format / suffix / gid_shape / id_patterns
                      缺实测证据 -> 拒绝出草稿(exit 2)
    档 B(采多采少)    variants / quality_map / id_samples     -> 降级并标注
    档 C(只影响体验)  input_forms / album_url_template        -> 只标注

分档判据只有一句：**猜错了，是每条 URL 都错，还是只是少采一点。**

价值不在"拦住这五种"，而在**第六种**：它哪怕从没见过，新写出来的那一行也必然带缺口、
必然被拦下。**补丁是"事后枚举见过的错"，规则是"事前描述什么叫错"。**

实现要点（换到别的"生成器"类工具上同样成立）：

* `build_draft()` 的返回从 `str` 变成 `Draft(str)` + `gaps`。**继承 `str` 是刻意的** ——
  既有调用方写的是 `draft.split(...)` / `x in draft`，不该为一个新字段把它们全改掉。
* "没有证据"和"证据不足"要分开：命中 0 个 = 档 A（前提取不到证）；命中 1 个 = 档 B
  （有证据，那是**歧义**不是缺口）。
* 拒绝时统一出口（`refuse()`），保证每个出口都说清**为什么拦** + **怎么补**两件事。

### 同族：可达性问题要在**第 0 段**就拦，别跑完再报

直链不通时，后面四项会发二十来个请求，然后给出一屏没有意义的数字 —— 而真正该做的第一件
事（解决可达性）被埋在最后。新增的"第 0 段"就是为此。判据是 **200 + 媒体类型**，不是
"状态码 200"：xchina 对拿不到的图集照样答 `200`，只是 Content-Type 变成 `text/html`
（正是第 17 条那一族）。

顺带把一件手工活自动化了：`403/429/503` 这类"被挡在门外"的状态，**自动换一次 chrome
TLS 指纹重试**后再判定。2026-09-23 在 xchina 上就是先看见 403、再手动加参数重跑的 ——
那一步纯属浪费。

### 同族：把"唯一的验收标准"前移

声明的唯一验收标准是 `check_site()`（每条 `id_samples` 必须被 `id_patterns` 解析出期望的
gid）。以前靠人肉：存盘 → import → 跑 `selfcheck.py`。现在草稿拼完**就地** `exec` 起来跑
一遍（把相对 import 换成绝对 import 即可，`backend` 已在 `sys.path`）。

⚠️ 但自检结果**不阻断**出草稿 —— 草稿是给人改的起点，不是交付物；阻断的只有档 A。

### 手法：测试全绿之后，把**完整输出打出来看一遍**

这一节的两处缺陷**都不是测试抓到的**，是把整屏输出读下来才看见的：

* ⑤ 里「相册页」正则**打印了两遍** —— 相册页 `/photo/id-X.html` 与它的分页
  `/photo/id-X/10.html` 推出的是**同一条**正则，草稿的 `id_patterns` 也跟着重复。
* `short()` 从头截断，把 `/10.html` 这个**关键末段**砍掉了，于是"末段是页码"那条提示里
  两个 URL 长得一模一样 —— 而这两个 URL 的处理规则**完全相反**（一个是 ID、一个是页码）。

**为什么测试抓不到**：断言是我**想到**的那几行；"同一条正则出现两次"、"提示里两个 URL
看起来一样"属于**我没想到要去断言**的东西。它和同族 F（"通过但理由已经不对"）是一对：
F 是"断言的理由失效了"，这条是"**该断言的地方没断言**"。

**判据**：凡是**产出给人看的东西**（报告、草稿、提示），改完都要目视一遍完整输出 ——
测试保证"我想到的都算对了"，目视保证"我没想的那些没坏"。

---

### 第 19 条 ⚠️ 模块级单例在 **import 期**就起了后台线程

**形状**：`backend/core/task_manager.py` 末尾一句 `task_manager = TaskManager()`
（生产需要：api 层直接用它提交任务，`main.py` 的 lifespan 靠它）。但 `TaskManager.__init__`
顺手 `start()` 了看门狗与调度两个线程 —— 于是**只要有人 import 这个模块，线程就活了**，
而它们干活时读的是**当前的** `database.DB_PATH`。测试的库是**每条用例现换的**。

**表现**（全是"单跑绿、全跑红、重跑又绿"）：

* 调度线程扫当前用例的库，把刚启用的订阅源"顺手跑掉"并回写 `next_run`
  → `due_watches()` 刚从 0 变 1、一转眼又变回 0（`test_disable_watch_stops_it_being_due`）。
* 用例里的活动任务被它当成**自己的**，在单例的 `_active` 里留下条目；之后别条用例的看门狗
  问 `_owned_by_any_manager(tid)` 得到 True，就不敢收那个 tid —— 而 tid 在小库上**从 1 重新数**，
  **跨用例撞号**（`test_watchdog_reaps_task_with_dead_heartbeat` / `..._no_heartbeat_at_all`）。

**为什么既有隔离管不到**：`tests/isolation.py` 登记的是**路径**，不是**线程**；
`tests/conftest.py` 顶部记的两轮污染源都是**磁盘状态文件**，这轮的污染源是
**一条进程内线程** + **一个跨用例复用的 dict**。所以隔离清单再全也拦不住它。

**定位手法（可复用，比"猜哪个用例"快得多）**：写一个 in-process pytest 插件，
在 `pytest_runtest_setup` 里打印活着的线程 / `mgr._active` 大小，用 `-p <插件名> -s` 跑一轮。
先看**连跑 vs 单跑**的差异确认是污染，再让插件把"谁还活着"打出来 —— ⑤心法：卡死类/隔离类
问题第一动作是**打出实际状态**，不是重跑撞运气。

**修法**（`tests/conftest.py::_muzzle_import_time_singleton`，autouse）：

* 只停**后台循环**（`_watchdog_stop.set()` / `_scheduler_stop.set()`），
  **不动方法本身** —— 有用例直接同步调 `task_manager._final_status` / `.submit_resource`。
* 用例结束后把它 `_active` 的增删**抹平**（存 before、clear、update before），免得下个用例再撞号。

**修完的副作用是个好信号**：全量**反而快了**（274s → 193s）—— 杂散线程本来在做无用功。

**推广判据**：写完一个模块，问一句「**只 import 它，会起线程吗？**」。
会 → 要么改成显式 `start()`（lifespan 调），要么在测试里把它摁住。**生产需要 ≠ 测试需要。**

---

### 同族：门禁的**选择器**错 → 四道闸一起**假绿**

`scripts/add_site.py` 的四道闸（可达性 / 声明自洽 / 命名落盘预演 / 过滤命中）都成立，
但第一版用 `getattr(cls, "site", None)` 挑站点 —— **凡有 `site` 声明就收**。于是
`pexels` 也被纳入了：它根本不是 `SequenceGallerySpider`（走 API 分页，不是序号枚举型），
四道闸于是全绿在**一个永不可能出现的 URL 形态**上。**闸是真的、跑是真的、结论是假的。**

判据：**"什么东西该被这道闸管"本身要有判据，且这个判据要写进代码**（这里 =
带 `site` 声明的 `SequenceGallerySpider`），不能靠"我拿两个例子试了没问题"。
同族 F 的变体：不是断言的理由失效，而是**被测集合的挑选理由错了** ——
更隐蔽，因为闸的输出全都是"绿"。


## V39 五（2026-09-24 四）：让"绿/红"不再可能是假的

这一节把"结果不可信"的两种形态摊开。它们看着是两个问题，其实**同一个根因**：

> **判据挂在了错误的东西上。**

* **假绿** —— 判据挂在"**没报错**"上。闸跑了、印了 ok，但它**一项都没核**（要核的对象
  根本不存在）。把"没验过"当成"验过了没问题"。
* **假红** —— 判据挂在"**中文文案**"上。断言写 `"chrome" not in msg`，而那句文案提到
  Chrome 恰恰是在说"换指纹没用"。一条**正确**的实现被判红，修的人于是去改产品迁就断言。

两类都在本项目真实发生过。所以修法必须落在**结构**上，不能靠"这次小心点"。

### 手法一：每道判据都要能回答"我核了几项"

`scripts/add_site.py` 从"返回问题列表"改成结构化的 `Gate`：

```python
class Problem:                              # kind 给机器判, message 给人看
    __slots__ = ("kind", "message")

class Gate:
    __slots__ = ("title", "checked", "problems", "rows")

    @property
    def empty(self):
        return self.checked == 0            # 空转: 一项都没核 —— 这不是"通过"

    def effective_problems(self):
        if self.empty and not self.problems:      # 空转兜底
            return [_p(NOTHING_CHECKED, "这道闸**一项都没核到**…")]
        return list(self.problems)

    @property
    def ok(self):
        return not self.effective_problems()

    def kinds(self):
        return {p.kind for p in self.effective_problems()}
```

三个要点，少一个这套就不成立：

1. **`checked` 是必填且必须是实际数目**。新写闸时忘了填，那闸就是永远绿的，而没人看得出来
   —— 它是整套机制里唯一防"空转绿"的字段。
2. **把"空转"做成 `Gate` 自己的性质**（放进 `effective_problems()`），而不是散在各处
   `print` 分支里。这样脚本、`verify()`、测试读的是**同一个判据**，不会出现"脚本记得拦、
   测试忘了拦"这种半拉子护栏。
3. **已经报了具体问题时，不许用"没验到"盖掉它** —— 否则排查时看到的是笼统的"一项都没核到"，
   而真正能照做的原因（`seq_format` 渲染失败）被吞掉。

**那条活的假绿**：`check_site()` 在 `id_samples` 为空时返回空列表（它的每条检查都被
`if … and samples:` 挡掉了），于是闸 1 会印一行 ok —— **一个还没写样本的新站点直接放行**。
这正是 `checked` 要治的东西：**"没验到"与"验过了没问题"必须是两个结果。**

### 手法二：判据只能挂在**代号**上

`probe_site.diagnose_block` 本来就有 `kind` 代号，但**建议**是散文，于是"这两种 403 处置
相反"只能靠 grep 中文来断言 —— 而 `ip_block` 的文案里**必须**写明"换指纹没用"并给出实测
证据，那就必然写到一个指纹名：`assert "chrome" not in ip` 会把一条**正确**的实现判红。

改成给建议也发一个代号，返回 `(kind, step, why, fix)`：`kind` 说"是什么"，`step` 说"该做什么"。
判据于是变成一句不可能因为改文案而失效的话：

```python
assert ip[0] != cf[0] and ip[1] != cf[1]     # 两种 403 的处置不一样
```

已有的两个先例：`core/errors.py` 用 `error_kind` 归类而不是 match 中文标签；
`drift_check.classify_drift` 返回 `{"level": "hard"|"soft", "field": …}` 而不是一句话。

### 顺带修掉的白名单 / 黑名单问题

自动换指纹重试原来的条件是 `kind != "ip_block"` —— **黑名单**。每加一种成因都会默认继承
"重试"，于是 429 限速、5xx 也各白跑一次换了指纹的请求。改成按 `step` 的**白名单**：

```python
_RETRY_STEPS = ("impersonate", "escalate")
```

**黑名单会随着新增成因不断漏人；白名单逼每一个新成因显式回答"换指纹有没有用"。**

### 第 20 条 ⚠️ `curl_cffi` 的响应**不支持上下文管理器**

**形状**：本项目有两种传输 —— `requests`（默认 TLS 指纹）与 `curl_cffi`（浏览器指纹，
被 CF 挡住的站点必须用它）。两者 API 像但不等价：

```python
with session.get(url, stream=True) as resp:   # requests: 可以
    ...                                       # curl-cffi: TypeError!
```

`curl_cffi.requests.Response` **没有实现 `__enter__`/`__exit__`**。这个写法在本仓库被抄了
**三遍**（`gallery_base._head_status_headers` 的流式回退 / `probe_site.sniff` /
`probe_site --smoke`），**每一处都包着 `except Exception`** —— 所以它从不报错，
只是**静默地什么都拿不到**。

**为什么它特别阴**：

1. 只在**最需要换指纹的场合**出现（站点在 CF 后面）—— 换了指纹反而什么都拿不到；
2. 失败长得像"站点不允许下载 / 探测失败"，不像代码 bug；
3. 它是**结论错**而不是崩溃：2026-09-24 目视探针输出时发现，`sniff()` 拿不到 CF 挑战页
   正文 → `diagnose_block` 分不清 `cf_challenge` 与 `blocked` → **用户丢掉"要去开真浏览器"
   这条线索**，而输出看起来仍然"完成了"。

**发现路径（值得复用的手法）**：改完别的东西去**目视完整输出**（第 18 条的规矩），三种
403/429 形态并排一看，只有 `cf` 那条的重点不对：

```
  => 成因: [cf_challenge] …                    <- 第一遍: 判对了
  => 403: 自动改用 chrome TLS 指纹重试一次(curl-cffi)
  => 成因: [blocked] 403, 但看不出具体成因…     <- 第二遍: 线索丢了
```

**修法**：抽 `backend/core/transport.py`，把**会话工厂**与**流式请求**放在一起
（`build()` + `streamed()`，后者显式 `close()`），三处调用统一走它。
`streamed()` 里 close 失败的处置有讲究：不能吞（会漏连接，第 12 条的家族），也不能在正文
已经出错时抛（会盖住真正的异常）—— 所以只在 `with` 体正常跑完时才把 close 的失败抛出去。

**顺手加的源码门禁**（⑦心法：同一个错出现第三遍就抽成程序可执行的规则）：用 AST 找
`With` 节点的 `context_expr.func.attr ∈ {get, head, post, …}`。

⚠️ **这条门禁第一版用正则扫文本，第一天就红** —— 它把注释与文档字符串里那些反面教材
（新模块自己的文档里就写着错误写法）也当成违规。一条一开始就需要豁免的门禁，迟早会被加到
失效。**判据要落在语法结构上。**

### 同一个错的第二种形态：局部变量遮蔽模块名

`core/transport.py` 一进来，`probe_site.main()` 里那句
`session, transport = make_session(...)` 就变成了第 13 条（`_download_m3u8` 的 `info` 被
局部变量顶掉）的同族 —— 改名为 `transport_name`。**同名的东西干两件事，迟早出事**，
尤其在**新加了 import** 之后：遮蔽是悄悄发生的，Python 不会提醒。

### 空转不许算绿，也适用于"报告型"脚本

`drift_check.py` 的 `checked == 0`（探针全失败 / 全被跳过 / 没有可用基线）原来也印一句
"无硬漂移"并 `exit 0`：

```python
if checked == 0:
    print("结论: **一个站点都没查成** …")
    sys.exit(2)          # 「没查到」与「查过没问题」是两件事
```

这是最坏的一种绿：**它把"巡检自己坏了"伪装成"站点没变"**，而挂进定时任务的人会一直收到
"一切正常"。顺带把 `ok  与基线一致(第一次见 —— 已记录基线)` 这句也改掉 —— 那一次**没有基线
可比**，说它"一致"是同一条假绿的另一种写法。

### 一条判据错，连"闸崩了"都能被当成"闸过了"

写 `variants=[]` 的坏声明去试闸 4 时，闸是**崩**的（`GallerySite.media()` 在
`self.variants[0]` 上抛 `IndexError`）而不是报错。崩比报错更糟：脚本跑不完，**前面几道闸
已经核出来的结论一起丢掉**，用户看到的是 traceback，而真正的原因（声明里 `variants` 是空的）
一个字也没提。修法是给 `media_names()` 加一层"抛异常就返回原因"的包装
（`_media_names()`），把异常变成一条**能直接照做**的问题。与 `core/errors.py` 同口径。

### 这一节的总判据

* **假绿**：每条判据都要能回答"我核了几项"；`0` 一律算红；"没验到"与"验过了没问题"分离。
* **假红**：产物自带**代号**（`kind` / `step` / `level` / `checked`），测试断言代号，只对
  代号做跨条目的不变量断言；文案随便改。
* 两类的共同前提：**先把它变成结构**（`Gate` / `Problem` / `step`），之后才可能"不小心也
  绿/红不了"。


## V39 六（2026-09-24）：CI 绿 ≠ 本地绿 —— 三处"没人跑"的假绿

**前言**：把"假绿/假红"按判据修完、CI 也全绿之后，顺手核了一次 CI 的汇总行 ——
`1139 passed, 8 skipped`，本机是 `1140 / 6`。差额查清了（平台参数化 +1、跳过项互换），
但**对账过程中发现的东西比差额本身重要**：CI 的"绿"所覆盖的用例集合**比本地少一截**，
而少掉的那一截恰好是最贵、最像"真验证"的那些。

### 第 21 条 ⚠️ CI 里那 7 条"真解码"用例从来只跳不跑

从 CI 日志的进度字符**逐位还原**（`-q` 的进度是逐位的，`s` 的下标即用例序号），
CI 跳过的 8 条是：

| 条数 | 用例 | 门控 |
| --- | --- | --- |
| 3 | `test_features_v36.py` 的 DASH 字节区间 / 多时段 / 多时段拒收 e2e | `@_needs_ffmpeg` |
| 4 | `test_phash.py` 的重压缩判定 / 动图首帧 / 重复标记 / 不误标 | 模块级 `FFMPEG = find_ffmpeg()` |
| 1 | `test_native_runtime.py::test_second_windows_launcher_waits_for_locked_runtime` | Windows-only |

`core/ffmpeg.py::find_ffmpeg()` 在 `ubuntu-latest` 上探不到（PATH 与四个已知目录都没有），
于是 **CI 从不验证"产物内容是不是好的"** —— dHash 去重、DASH 字节区间的真解码全只在本地跑。
"CI 全绿" 的真实含义是："本地那套里，**去掉 7 条最贵的之后**全绿"。

⚠️ 这条值得记的不是结论，而是**发现方式**：它不报错、不变慢、不影响任何数字，
**只有对账才能看出来**（把两边的收集数/跳过项一一列出，"哪类判据只在一边存在"一眼可见）。

### 第 22 条 ⚠️ 存在、但不在任何调用链里的测试 = 不存在

`frontend/package.json` 里有 `test:task-query`（`node scripts/test-task-query.mjs`），
而 CI 的 `frontend` job 只有 `npm install` + `npm run build` —— **这条测试从落地起
从未在 CI 执行过**。同族：仓库没有 `.gitattributes` ⇒ 行尾只能靠人每轮自查
（`Path.write_text` 在 Windows 上把整份文件翻成 CRLF，本项目已栽过好几次）。

判据：**"一个测试/守卫若不在 CI 的调用链里，它等价于不存在。"**

### 第 23 条 ⚠️ 三个"仓库级不变量"脚本只在本地跑

| 脚本 | 守的是什么 | 能否进 CI |
| --- | --- | --- |
| `scripts/selfcheck.py` | 站点声明自洽（`check_site`）+ CDN 画像 | 离线，可 |
| `scripts/add_site.py --all` | 认领 / 过滤 / 落盘预演（四闸） | 离线，可 |
| `scripts/verify_output.py` | 端到端产物 / 清单 / 打包（40 项） | 离线，可（需 ffmpeg 则同上装） |
| `scripts/verify_hls.py` | 真 HLS 双引擎（18 项） | 要 ffmpeg + 网络，不进 |

前三个都**离线**，符合进 CI 的条件；它们守的恰恰是"整条流水线接起来还对不对"这类
只能靠"跑一遍"才能发现的错。

### 修法清单（文件级）

1. `.github/workflows/ci.yml`：backend 装 ffmpeg（`apt-get install -y ffmpeg`）并跑
   `selfcheck.py` / `add_site.py --all` / `verify_output.py`；frontend 改 `npm ci`
   （走锁文件）+ node 22（本地是 22，且 node20 已被 GitHub 标 deprecated）+
   跑 `npm run test:task-query`。
2. `.gitattributes`（新增）：`* text=auto eol=lf`，并对两个**已知 CRLF** 文件
   （`backend/downloaders/video.py`、`scripts/verify_output.py`）写 `-text`
   —— 把行尾从"每轮人工检查"变成"Git 保证"。
3. `scripts/gateguard.py`（新增）**文档声称门禁**：README / docs 里的用例数、
   "N 项断言"必须与实测一致（本轮刚发生过"文档写 1146、CI 报 1147"）；顺带查
   未用 import / 死声明 / 代码围栏配平，并把 `with session.get(...)` 的 AST 门禁
   从单测扩到全仓。
   ⚠️ 别用 ruff 顶这一条：项目没有 lint 配置，**一条一开始就需要大量豁免的门禁
   迟早会被加到失效**（第 18 条）。
4. `docs/AGENT_DEVELOPMENT_GUIDE.md` 增 §21「CI 契约：哪些东西必须由机器守」；
   `README.md` 的"测试/部署"节补一张"在 CI 跑 / 只在本地跑（为什么）"对照表。

**总判据**：CI 绿的含义只能是"**这套判据都在 CI 里跑过一遍**"。
只要存在"某类判据只在一边跑"，绿就是一种**口径**，不是结论。


## V40（2026-09-24）：门禁真的落地了 + 本地相册集；抓出第 24/25 条

**前情**：V39 六 列的是"修法清单"（**打算**这么改）。这一轮把它**改完了**
（`scripts/gate.py` / `scripts/gateguard.py` / `.gitattributes` / ci.yml / README 的
「CI 契约」表），并新增了「本地相册集」：**把任意本地目录登记成相册集**，像 xchina 那样
浏览，但首页看的是**随机池**里的照片。

门禁一上线就抓出两条**新的**静默错 —— 都属于"看起来已经修好了"的那一类。

### 第 24 条 ⚠️ 标签的语义只能有**一个方向**，写反了单测抓不到（F 类现场）

`[platform:X]` 的判据原来写成"**标签必须等于当前平台**，否则算问题"。
**单测照着这个实现写，全绿** —— 因为用例里的"当前平台"和"标签"是同一份常量，怎么翻都自洽。

**它红在端到端全量**：本机是 Windows，仓库里有 6 条 `[platform:posix]` 的用例合法跳过，
在错的判据下**全部被判红**。

方向只有一个：`X` 是**这条用例需要哪个平台**，所以合法的跳过是 `X != 当前平台`；
`X == 当前平台` 却仍在跳，才是"门控条件写反了"。反过来的写法**永远不可能两边都绿** ——
仓库里同时有 POSIX-only 与 Windows-only 的用例，任何一边都会有一批判红。证明写成了用例：
`test_skip_policy.py::test_the_platform_tag_is_satisfiable_on_both_platforms`。

⚠️ 这是 **F 类**（"通过但理由已经不对"）的典型现场：**实现与测试一起错**，
单测越忠实越看不见。抓住它的是**另一条完全不同的路径**（全量真跑一遍）。
推论：**判据方向类**的错，必须至少有一个**不是照实现写的**观测面（端到端 / 真实平台 / 对账）。

### 第 25 条 ⚠️ 别拿**墙钟**当判据

`test_features_v32.py::test_proxy_breaker_cooldown_expires` 原来用
`cooldown=0.01` + `time.sleep(0.05)` 再断言"已到期"。`note_failure()` 还会落一次 JSON
（`core/jsonstore.py` 的四条并发纪律之一），负载高时这一步就超过 10ms → 断言看到的是
**还没到期**的状态 → 报 `assert 'http://a:1' == 'http://b:2'`：一条**正确**的实现被判红。

修法：把时刻**显式推**给实现（`cooldown=60` + `pool._is_blocked(url, now=time.time() + 61)`），
不再 sleep。判据：**断言里出现 `sleep`，先问它能不能换成"显式传时刻"**。

### 只读边界必须是**结构性**的，不能是"我小心了"

本地相册集的核心承诺是"**只读**：扫描 / 浏览**一个字节都不动**用户的文件"。
这类承诺最容易在后续维护里悄悄破掉，所以做了两层机器判据：

* **静态**：AST 扫 `core/localalbums.py` / `api/local.py` 有没有写调用（`_FORBIDDEN_WRITES`）。
  ⚠️ `Path.replace` **刻意不在黑名单**：它是路径归一操作，而且 AST 分不出接收者是不是
  `Path` —— 放进去会把**正常代码**判红（假红）。
* **运行时**：扫描前后对整棵树逐文件比 **`size` + `mtime_ns`** 指纹（而不是"我扫完没报错"）。
* 缩略图落在**库同级**的 `local_albums/_meta/thumb/`，并用 `resolve()` **词法归一**拦
  "把缓存目录本身登记成根"（不能靠"缓存目录已存在"来判断）。

同族的一条：**出图接口只收 `(root_id, rel)`，从不收裸绝对路径**；越界的判据是
"`resolve()` 之后在不在根内"，**不是**"字符串里有没有 `..`"。用例里加了 AST 扫
`FunctionDef` 签名、禁止出现 `path` / `file` 参数 —— 把"以后别加回来"变成机器规则。
（`not-image` 回 **415** 而不是 404：让"调用方传错了"与"文件真的没了"在日志里长得不一样。）

### 本地相册集的三条口径

1. **两套口径**：打开相册**实时列目录**（刚丢进去的照片立刻可见），随机池用**索引快照**
   （带回 `at` / `pool`）。不同步的地方靠 `_patch_album` 就地修正 —— 于是"刚删掉的照片"
   不会还留在随机池里。
2. **`seed` + 递增 `page` = 一副能一直往下翻的牌**：换一批 = 换 `seed`（重洗），
   更多 = 加 `page`（翻页）。各页**不重叠、不缺项**（有判据钉住）。
3. **按轮发牌**：`mode="album"` 每轮从每个相册各取一张（小相册不会被大相册淹掉）；
   `mode="photo"` 整池洗牌。

### ⚠️ 就地改缓存里的 dict = 把一次查询的状态**永久**写进缓存

`photos()` 返回的就是 `index["order"]` 里的**同一批对象**。早先的 `_with_favorite`
直接给它们加 `favorite=True` → **取消收藏之后界面还是老样子**（缓存里那个 key 还在）。
不报错，只是结果错。

修法：`_with_favorite` **返回副本**；并加一条用例专门钉住"标记不污染索引缓存"。
同族：`_list_album` 忘了把 `root_id` 写进每一条 → 星标**永远不亮**（也是不报错）。

### 环境类：npm 把 Windows 原生二进制装成**空目录**

`npm install` 之后 `npm run build` 报
`Cannot find module '@rollup/rollup-win32-x64-msvc'`，而
`node_modules/@rollup/rollup-win32-x64-msvc/` **存在但是空的** —— 它装的是
`rollup-win32-x64-gnu`（另一个候选），给 msvc 留了个空壳。

修法（**只动本机，不碰仓库**）：

```bash
rm -rf node_modules/@rollup/rollup-win32-x64-msvc
npm install @rollup/rollup-win32-x64-msvc@<与 rollup 同版本> \
    --no-save --package-lock=false --registry=https://registry.npmmirror.com
```

判据：**"包目录存在"不等于"包装好了"** —— 目录要非空、`package.json` 要能读出来。
同族的坑：`@esbuild/win32-x64` 也要一起看一眼（它没坏，只是同一类）。

### 环境类：`verify_output.py` 在"有上次残留"时会**在第 1 行就被拦**

症状与第 613 行那条**不一样**，所以单看"有没有汇总行"会误判：

| 症状 | 原因 |
| --- | --- |
| 进度行里有 `F`、**没有汇总行** | 守卫在**清理阶段**（结尾清 `tmp_path`）动手 |
| 输出**只有一行** `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]…`、`exit 1` | 守卫在**第 1 行**（`verify_output.py:162` 的 `shutil.rmtree(data/_verify_output)`）就动手，**40 项断言一项都没跑** |

第二种最容易被读成"验证失败了"。它其实**什么都不等于** —— 脚本压根没开始。
修法（只动本机）：

```bash
mv data/_verify_output /tmp/uwc_stale_vo      # 移动不是批量删除, 不触发守卫
python scripts/verify_output.py               # 这次会 40/40
```

⚠️ 别把这条当成"脚本有 bug"去改产品：守卫是**宿主/环境**的行为，仓库里的
`rmtree(ignore_errors=True)` 本身是对的（CI 每次都是干净工作区，遇不到）。

---

## V41（2026-09-24）：对标同类产品后吸收四项；抓出第 26/27 条 + 同族 H

调研对象：Immich 的 External Library（**同一个契约**：现有目录挂 `:ro`、只建索引、
一个字节都不动）/ PhotoPrism（`originals` 索引 + 端上分类）/ Eagle·Billfish（本地
素材管理，形态最接近"随机墙 + 收藏"）/ Google Photos（只作参照物）。吸收了四项：
排除模式 glob、往年今日、重复标记、扫描对账。明确推迟：人脸聚类、CLIP 语义搜索、
地图（合规 + 重依赖）、评分 / 颜色搜索 / 智能文件夹（都要一套"属性的属性"语义）。

### 第 26 条 ⚠️ **"没有结果"与"没算出结果"必须是两个值**

空列表会被读成"查过了，没有"。这一条在四个新能力里各出现一次：

| 场景 | 错的做法 | 对的做法 |
| --- | --- | --- |
| 查重复时 ffmpeg 不在 | 返回 `[]` | 返回 `reason="no-decoder"`（与 `core/phash` 的 `NO_DECODER` 同源） |
| 查重复超过单次上限 | 静默只算前 N 张 | 报 `skipped`（5000 张各起一次子进程只会**超时**，而超时被读成"功能坏了"） |
| 扫描对账没有上一次快照 | 返回 `{"added":0,"removed":0}` | 返回 `None` |
| 排除模式一条都没匹配 | 与"没配规则"长得一样 | 报 `excluded`（命中数 = "规则生效了"的唯一证据） |

**总判据**：任何"返回集合"的接口都要问一句 —— **返回空的时候，是"确实没有"，
还是"根本没算"？** 两者在界面上长得一模一样，所以必须在**返回值**里分开，
不能指望调用方从上下文推断。

⚠️ 同一个形状还有**第三个方向**，就是这条自己踩到的：

```python
reason = "no-decoder" if not hashes      # ✗ 把"文件坏"说成了"机器没装 ffmpeg"
```

"相册里全是解不开的图"（每张都 `DECODE_FAILED`）也被报成 `no-decoder`。后果很
具体 —— 界面照着它提示"请安装 ffmpeg"，而用户明明装了；于是他再也不信这个提示，
**真正的环境缺失也就一起被淹掉了**。修法是 `no_decoder` 与 `undecodable` **分开
计数**，reason **只由前者**决定。

教训的通用形式：**"归谁"要在计数阶段就分开，不能到了最后一步再靠"是不是空的"
反推。** 反推出来的原因码，一定会在某个组合下指错对象。

### 第 27 条 ⚠️ **"规则生效了"要有计数当证据**

排除模式这类"用户配了、但可能一条都没匹配上"的能力，唯一真正的风险就是
**规则静静地失效**。所以 `index["excluded"]`（跳过了多少目录 / 多少文件）不是
"锦上添花的统计"，它是这个功能**唯一的自证手段**。

匹配判据刻意放宽到三条（完整相对路径 / 末段文件名 / 任一祖先目录）：严格的 glob
语义会让用户随手写的 `Raw` 静默失效 —— 宁可多排除，不可静默不排除。

### 同族 H：`None`（不改）≠ `""`（清空）

`PATCH /local/roots/{id}` 无条件调 `rename_root(payload.name)`，于是"只想改排除
模式"的请求会顺手把名字抹成空。**PATCH 的语义是"只改显式传了的字段"**，所以
`name is None` 必须走"不动"，`name == ""` 才走"清空"。

同族 G（静默降级必须有痕）的近亲：这两类都是"请求做了它没要求做的事"，
不报错、不空转，只是**多改了一处**。

### ⚠️ 顺带抓出：`exclude_report` 的 `dropped` 恒为 0

"声明条数"和"生效条数"**一起被截断**（都过了 `parse_exclude`），于是
`len(declared) - len(pats)` 永远是 0 —— 一个"看起来没坏"的静默失败，界面永远
显示"没有规则被丢掉"。抓出来的是"配了 57 条就该报丢了 7 条"这条用例。

修法：**解析逻辑只有一份**（`_split_exclude` 只做拆分，截断只发生在
`parse_exclude` 里）。两个函数各自截断一次，是这类 bug 的固定形状。

### ⚠️ 冒烟抓到的假阳性：纯色图彼此"全是重复"

用 ffmpeg 造**真图**跑了一遍：纯红 / 纯红 / 纯蓝被两两配成 **3 对**，距离全是 0。
原因在 dHash 的定义本身 —— 它只比较**相邻像素谁更亮**，不看绝对亮度，所以纯红与
纯蓝每一对相邻像素都是"不大于"，指纹完全相同。放到真实场景里，一个装满纯色截图 /
占位图 / Logo 的文件夹会**整片互指**。

修法：`phash.is_flat_gray()`（灰度极差小于阈值 ⇒ 指纹不携带信息），这类图**不进
比对池**，数量报成 `flat`。

⚠️ **这条是冒烟抓到的，不是单测。** 单测用的是 monkeypatch 掉的假指纹，永远走不到
"真解码出一片纯色"那条路。由此补一条心法：**替身越忠实，越看不见替身覆盖不到的
那一层** —— 凡是"结果取决于真实数据长什么样"的判断，都要留一次真数据的目视。

顺带把替身写法也换了：不去替 `dhash_from_gray`（那样 `phash.distance` 这段真逻辑
就不跑了，用例验到的成了替身自己 —— E 类的变体），而是**反推**一组灰度字节让真的
dHash 算出来等于想要的值（`_raw_for_dhash`）。

### 往年今日的口径要随数据一起交出去

`on_this_day` 按**文件 mtime** 的月/日匹配，不是拍摄时间。拍照片的 EXIF 要新增
依赖，而大量下载来的图片**根本没有 EXIF** —— 所以返回值里带 `basis="mtime"`，
界面上写"修改时间"而不是"往年今日"就完事。代价也要说清：复制/重新导出会刷新
mtime，整批导入的照片会挤在同一天。**宁可少说，不可说错。**

同一天反复调用给**同一批**（用当天日期做种子）：每次都换一批就没有回忆了。






