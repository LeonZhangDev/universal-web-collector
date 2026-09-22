
# Agent继续开发说明

该文档用于后续 AI Agent 接手开发。


## 当前架构

backend/

api:
HTTP接口

collectors:
不同网站采集逻辑

downloaders:
资源下载逻辑

models:
数据模型

core:
任务、数据库、日志


## 开发原则

不要把网站逻辑写入下载器。

错误:

XChinaDownloader
同时负责:
- 页面解析
- URL发现
- 下载


正确:

XChinaSpider
负责:
发现资源


ImageDownloader
负责:
下载图片


VideoDownloader
负责:
下载视频


## 下一阶段任务


# V10 任务

## 1. 完善任务系统 ✅ 已完成(2026-09-17)

实现: backend/core/task_manager.py

状态机:

pending

↓

running

↓

extracting

↓

downloading

↓

success


失败:

failed

↓

retry (POST /tasks/{id}/retry, failed→pending, retry_count+1)

迁移校验: TRANSITIONS 表 + 乐观锁(update where status=expected)。

线程模型: ThreadPoolExecutor(max_workers=2)后台执行,
Playwright 同步 API 在工作线程中运行。


## 2. 数据库升级 ✅ 已完成(2026-09-17)

实现: backend/core/database.py, SQLite(WAL) 位于 data/collector.db


tasks:

- id
- url
- collector
- status
- progress
- retry_count
- error
- created_time


resources:

- id
- task_id
- type
- url
- headers(JSON)
- local_path
- hash(sha256, 去重)
- status(pending/downloading/done/failed/skipped)
- created_time

日志表 task_logs: 记录状态迁移与下载事件。


## 3. XChina解析增强 ✅ 已完成(2026-09-17)

实现: backend/collectors/xchina/parsers.py

四个解析器(一次页面加载全部运行):

- APIDetector: 监听 JSON 响应, 递归提取资源 URL
- NetworkParser: 监听媒体 Response, 捕获请求头(cookie/referer/UA)
- JSStateParser: 扫描内嵌 script 中的 JS 状态变量
- DOMParser: img[src]/data-src/video/source/a[href]

合并: merge_by_priority 按 API > network > js > dom 去重
质量: select_quality 按 base_identity 分组,
原图优先(缩略图模式识别), 同图取大图。


## 4. 下载系统 ✅ 已完成(2026-09-17)

实现: backend/downloaders/{base,image,video}.py

base: stream_download 统一重试+断点续传(Range)+流式sha256

ImageDownloader: 请求头注入, downloads/{task_id}/ 存储

VideoDownloader:
- mp4 直链流式下载
- m3u8: 优先 ffmpeg(-c copy), 失败降级手动分片合并
  (支持多码率子播放列表选择, 加密流需 ffmpeg)

并发: task_manager 线程池(max_download_workers=4)
hash去重: resources.hash 列, 跨任务复用已下载文件


## 5. 浏览器状态 ✅ 已完成(2026-09-17)

实现: backend/collectors/xchina/browser.py

browser_state/{domain}.json

Playwright storage_state 加载/保存, cookies+localStorage,
避免重复登录。


## 6. 前端 ✅ 已完成(2026-09-17)

实现: frontend/ (Vue3 + Vite + axios)

- 创建任务(POST /tasks/create JSON)
- 任务列表(2s 轮询)
- 实时进度条
- 图片预览(/files/{task_id}/xxx 静态服务)
- 视频资源状态
- 日志时间线(GET /tasks/{id}/logs)
- 失败任务重试按钮

构建: npm run build → frontend/dist, 后端生产模式直接托管


## 7. 工程化 ✅ 已完成(2026-09-17)

- Dockerfile: 多阶段构建(node 前端 + python3.13 + uv + chromium)
- docker-compose.yml: 数据卷持久化 /data
- CI: .github/workflows/ci.yml (pytest + 前端构建 + docker build)
- 配置管理: config.yaml + 环境变量 UWC_* 覆盖
  (backend/core/config.py)
- 测试: tests/ 17 个用例(状态机/数据库迁移/质量选择/下载器)


# V11 增强 ✅ 已完成(2026-09-17)


## 1. 采集器插件化

实现: backend/collectors/__init__.py (注册表模式)

- `@register("name")` 装饰器注册 Spider 类到 COLLECTORS 字典
- 共享层抽出: collectors/browser.py + collectors/parsers.py
  (xchina 与 generic 共用, 站点逻辑只留在各自 spider.py)
- 新增 generic 采集器: 任意 URL, 抓全部媒体资源 + 页面正文文本
- 新站点接入: 建 collectors/<site>/spider.py, @register 后
  在 __init__.py import 即可, 无需改动 core/api


## 2. 资源类型扩展

- image / video / audio / doc / text 五类
- FileDownloader(audio/doc): 复用 base stream_download
- TextDownloader: 抓页面正文存 .txt
- DOWNLOADERS 映射表在 task_manager.py, 未知类型自动 skip


## 3. 限速与代理

实现: backend/downloaders/ratelimit.py

- 单域名并发上限 domain_concurrency(默认3)
- 单域名最小请求间隔 domain_min_interval(默认0.5s)
- **令牌桶**: `domain_burst`(默认3)是突发容量 —— 长程平均速率不变, 只是允许
  把攒下来的配额一次花掉(下完一个大文件后的空档不必干等)
- **AIMD 自适应**: `adaptive_throttle` 开时, 间隔在 `domain_fast_interval`~
  `domain_slow_interval` 之间浮动: 连续成功缓慢收紧、429/非 2xx 立即翻倍放宽。
  ⚠️ **只按成功与否判定, 不看响应延迟** —— CDN 边缘缓存和 429 都能毫秒级返回,
  拿延迟当"服务器很闲"的信号会越错越快
- 代理: config.yaml proxy 或环境变量 UWC_PROXY
  (http/socks5, requests 与 Playwright 均走代理)

⚠️ **吞吐由间隔决定, 不由并发决定**: 并发只管"同时在飞几个"。想提速要动
`domain_min_interval`(或让 AIMD 自己收紧), 调大 `domain_concurrency` 不提速。
任务日志会打一行「站点节奏: 并发 3 / 间隔 0.50s / 突发 3 → 长程约 2.0 req/s」
并给出预计耗时 —— 看不见的阈值会被反复误调。


## 4. 任务健壮性

实现: backend/core/task_manager.py

- 看门狗线程(watchdog_interval=30s):
  - 无 worker 的 active 任务(服务重启遗留) → failed
  - 心跳超时(stale_task_timeout=900s) → cancelled
- 任务取消: cancel Event + 状态迁移, 下载中资源标记 skipped
- 任务删除: DELETE /tasks/{id}, best-effort 停止后删库记录(磁盘文件保留)
- 资源级重试: POST /tasks/{id}/resources/{rid}/retry,
  仅 failed/skipped 资源, 任务须已结束, 不影响任务状态


## 5. SSE 实时推送

实现: backend/core/events.py + api/tasks.py /events, 前端 EventSource

- 事件: task.updated / task.log / resource.updated
- 15s keepalive, 断线自动重连
- 前端列表不再轮询, 详情页轮询保留(兼容降级)


## 6. 前端适配

- 采集器下拉选择(GET /collectors)
- 任务删除按钮(行内 ✕)
- 资源重试按钮(失败/跳过资源, 任务结束后可见)
- 新类型图标: video ▶ / audio ♪ / doc 📄 / text 📝


## 7. 工具与修复

- scripts/probe.py: 站点解析探针, 输出各解析器发现的资源与质量选择
- config.py: yaml 读取改用 read_text(修复 WindowsPath 无 read 属性)
- 错误信息: task.error 只存首条消息(≤500字符), traceback 进 task_logs;
  资源失败不再把错误写入 local_path
- 测试: 37 个用例(新增 采集器注册/限速/看门狗/资源重试/SSE)


# V12 采集控制 ✅ 已完成(2026-09-17)


## 1. 自定义下载目录

- tasks.download_dir 存任务级输出目录, 为空则用全局 settings.download_dir
- 两种情况都再套一层 `<task_id>/` 子目录, 避免任务间文件混淆
  - ⚠️ **V28 已改**: 不再套 `<task_id>/`。媒体落在 `下载根/相册名/`,
    视频平铺在下载根目录, 清单落在 `下载根/_meta/<任务ID>/`;
    规则唯一定义在 `core/layout.py`。下面的"任务目录内"约束因此改为"下载根之内"。
- 校验(backend/api/tasks.py `_validate_download_dir`):
  必须绝对路径 / 不含 `..` / 不得是盘符根目录
- 文件服务改为 `/files/{task_id}/{file_path:path}` 路由(`serve_file`),
  用 `is_relative_to` 把访问范围锁在任务目录内,
  取代原先 mount 整个 downloads 目录(后者可被跨任务读取)


## 2. 资源过滤

实现: backend/core/filters.py

- URL 层(无需请求): 类型白名单 / 扩展名黑白名单 / URL 包含·排除关键词
- 大小层(HEAD 探测 Content-Length): min_size / max_size,
  支持 "10KB" / "2MB" / "1024" 三种写法
- 探测失败(405/超时/无 Content-Length)一律放行, 不因探针问题误杀资源
- 无扩展名的 CDN URL 在扩展名白名单判定中放行
  (下载前无从得知真实类型, 严格判定会误杀)
- 被过滤资源: status=filtered + note 记录原因, 不进入下载
- 单资源重试视为"强制下载", 跳过过滤规则(task_manager.submit_resource)


## 3. 数据库

- tasks: +download_dir, +options(JSON 存 filters)
- resources: +size, +note
- _migrate 改用 _ADD_COLUMNS 统一补列, 幂等, 旧库平滑升级


## 4. 前端

- FolderPicker.vue: 目录浏览弹窗(上级/进入/新建文件夹),
  双击进入目录、单击选中
- App.vue: 下载目录行 + 可折叠过滤面板, 选择记入 localStorage
- TaskDetail.vue: 资源显示体积与过滤原因、各状态计数、"强制下载"按钮


## 5. 新增接口

GET /config(默认目录+资源类型) / GET /fs/browse / POST /fs/mkdir;
/files/{task_id}/{path} 取代静态挂载


# V13 相册枚举采集 ✅ 已完成(2026-09-17)

目标: 输入相册 ID 拿到该相册全部图片, 每张间隔 3~10 秒随机下载,
主下载点失败自动切换。


## 1. 新采集器 xchina_gallery

实现: backend/collectors/xchina/gallery.py (纯 HTTP, 不启浏览器)

输入(三者皆可)::

    6aa113208a506
    https://img.xchina.io/photos/6aa113208a506/00001.jpg
    https://xchina.co/photoShow.html?id=6aa113208a506

URL 模板::

    https://img.xchina.io/photos/{gid}/{seq:05d}.jpg           原图
    https://img.xchina.io/photos/{gid}/{seq:05d}_{w}x0.webp   600/800/1200 三档

枚举从 00001 递增, 连续 3 次判定"不存在"则停止(实测相册 00001~00056 正确)。


## 2. ⚠️ 站点特征(改动前必须重新验证)

**(a) 该站不返回 404 —— 存在性只能看 Content-Type**

    00001.jpg -> 200 image/jpeg 656713B
    00056.jpg -> 200 image/jpeg 426153B
    00057.jpg -> 200 text/html              <- 越界, 状态码仍是 200

若按 404/403 判定, 枚举会永不停止或第一张就误停。
gallery.probe 因此返回三态: ok / missing / error,
5xx 与网络异常归 error(会重试), 绝不与 missing 混同, 否则相册会中途被截断。

**(b) WAF 校验 Accept 头 —— 缺了直接 403**

实测同一 URL 只改请求头::

    无 Accept                      -> 403
    Accept: */*                    -> 403
    Accept: image/avif,...,image/* -> 200
    带 Referer(无 Accept)          -> 403   (Referer 无影响)

这是项目级坑: 原先 `build_headers` 只设 UA, 对所有图片请求都会 403。
现统一在 core/config.py 定义 DEFAULT_ACCEPT / IMAGE_ACCEPT,
下载器与 filters.probe_size 都必须带上。

**(c) 直连可用, 无需登录/浏览器/代理**, Referer 有无均可。


## 3. 备用下载点(线路切换)

- 采集器为每张图输出 mirrors = 其余尺寸变体(1200/800/600 webp)
- resources 新增 mirrors 列(JSON 数组)
- downloaders/base.py `download_with_mirrors`: 主 URL 失败依次切换;
  切换时清掉半成品(不同 URL 内容不能拼接)且不做断点续传
- 切换后按新扩展名落盘(`_swap_ext`)—— 否则 .webp 内容存成 .jpg,
  后续按后缀判类型/预览都会错
- 开关: config.yaml `mirror_fallback`

**关键**: `require_image=True` 校验 Content-Type 必须是 image/*,
否则该站的越界 URL(200 + html)会被当图片存下来,
而且因为"下载成功", 备用下载点永远不会被触发。


## 4. 限速改为随机区间

- ratelimit.DomainLimiter 支持 max_interval > min_interval 时随机取值
- config: `domain_min_interval: 3` / `domain_max_interval: 10`
- **domain_concurrency 必须为 1**, 否则并发会把间隔摊薄成
  "每 3~10 秒 N 张" 而非 "每 3~10 秒 1 张"
- 探测(HEAD)走独立配置 `probe_interval: 0.3` —— 探测是轻量请求,
  套用下载级慢速节奏会让枚举 56 张耗时数分钟


## 5. 坑与修复记录

- `sqlite3.Row` 不支持 `.get()`, 只能用下标。
  db.get_mirrors 首版用了 `res.get("mirrors")`, 导致 56 张全部下载失败
  (报 'sqlite3.Row' object has no attribute 'get')
- 任务状态机不区分"全部资源失败": 56 张全失败时任务仍转 success
  (现有设计, 资源级状态在详情里可见)


## 6. 验证

- 测试: 47 个用例(新增 tests/test_gallery.py 10 个纯逻辑用例)
- 实测: 枚举 56 张且边界正确、min_size 过滤生效(56->54)、
  下载 6 个文件 3.85MB 且 JPEG 魔术字节正确、
  实测间隔 7.2s / 5.0s 落在 3~10s 区间、
  镜像降级(越界主 URL -> webp)落盘后缀正确改名


# V14 视频链路 + 画质档 + 采集器基类 + 健壮性 ✅ 已完成(2026-09-17)

承接 V13 复盘后用户确认的四项。核心洞察: **图片与视频的瓶颈恰好相反**
——图片是"下载太快, 间隔是瓶颈"; 视频是"下载太慢, 间隔在白白拖后腿"。


## 1. 视频 m3u8 分片链路重写

重写 backend/downloaders/video.py 的 m3u8 路径。原实现把**图片的慢节奏
套用到了分片**上(每片过 domain_slot 等 3~10 秒), 且串行、无重试、
任一片失败整个视频报废、无断点续传。

- 分片走独立配置 `segment_concurrency`(4) / `segment_min_interval`~`segment_max_interval`
  (0.15~0.35s), 不再共用图片的 3~10 秒
- `ThreadPoolExecutor` 并发下分片
- 分片级重试(`segment_retries`, 默认 3 次), **单片失败只坏那一片**
- 断点续传: 已完成分片落盘保留, 重跑只补缺失分片
- 分片流式写盘, 不再 `r.content` 整段读进内存
- ffmpeg 失败不再 `except Exception: pass` 静默吞掉 stderr

配套: `base.py` 的内容类型校验改为**黑名单挡 HTML**(而非白名单只放
`image/*`) —— 视频 CDN 常返回 `application/octet-stream`, 用白名单会误杀。

> ⚠️ 当时本机未安装 ffmpeg(`shutil.which("ffmpeg")` 为 None),
> 内置分片下载器(TS 手动合并, 输出 `.ts`)是**当时唯一在跑的路径**,
> 而非备用路径。
>
> 该状态已于 V15 改变 —— ffmpeg 现已安装, 且定位不再依赖 PATH。
> 本节的"有 ffmpeg 时优先"在当时是未执行的分支, 现在才真正跑起来。


## 2. 图片画质档

采集器新增 `quality` 参数: `original` / `1200` / `800` / `600`。

站点自带 4 档尺寸变体, 此前只被用作"故障备用"; 实际上 1200 宽 webp
与原图肉眼看几乎无差却省约 80% 空间, 应该作为**默认可选档**。

- 选定档位作为主 URL, 其余档位按"画质邻近度"排序作为 mirrors
  (`mirror_order`)
- 该档位在某张图上缺失时回退最高画质档, 不中断整册
- 任务 `options.quality` 透传; API 校验非法档返回 400
- 前端新增画质下拉

实测: `quality=1200` 落盘 68~119KB/张(原图 656KB), 魔术字节 `RIFF` 确认真 WebP。


## 3. 抽 SequenceGallerySpider 公共基类

新增 backend/collectors/gallery_base.py。站点差异收敛为**声明式配置**
(`GallerySite`: URL 模板、变体列表、画质映射、探测规则), 通用逻辑
(序号枚举、三态探测、连续缺失停止、镜像排序、随机间隔)在基类。

`xchina/gallery.py` 改为继承基类, 只提供站点声明。
接第二个图集站不再需要复制整份文件 —— 这是把 V13 的探测方法论
沉淀成资产, 而不是散在一个站点文件里。

注册方式不变(`@register`), 老采集器与任务流程零改动。


## 4. 任务 partial 状态 + 站点级限速

**(a) partial 状态**: 原设计下 56 张全失败任务仍转 `success`, 用户看不出
出了问题。现在 `_final_status()` 按资源分布判定:

- 全部成功(或仅 filtered/skipped) -> `success`
- 有成功也有失败 -> `partial`
- 全部失败 -> `failed`

`TRANSITIONS` 同步扩展(`downloading -> partial`, `partial -> pending` 支持重跑)。
前端 `TaskTable` / `TaskDetail` 增加 partial 徽标与"重试失败项"入口。

**(b) 站点级限速**: 限速器原本按 `netloc` 分桶, 导致 `img.x.com` 与
`cdn.x.com` 各持一个限速器, 站点级限速形同虚设。改为 `site_key()`
推断**注册域**(正确处理 `co.uk` 这类二级后缀, IPv4 原样保留),
可用 config `site_groups` 显式覆盖分组。


## 5. 坑与修复记录

- **`domain_concurrency` 与请求间隔是两个正交闸门**。
  此前误判为"并发必须为 1 否则节奏被摊薄", 已用离线实验证伪:
  并发闸门 3 / 间隔 0.5s / 每请求 2s 时, 6 个请求总耗时 5.07s,
  发起间隔实测 0.534 / 0.527 / 0.939 / 0.535 / 0.527 秒 —— **间隔未被摊薄**。
  配置改回 `domain_concurrency: 3`。
- **WAF 校验 Accept 头**(V13 同源问题): 无 `Accept` 或 `Accept: */*`
  直接 403, 必须显式含 `image/*`。常量统一放 config 层
  (`DEFAULT_ACCEPT` / `IMAGE_ACCEPT`), 下载器与大小预检 HEAD 都要带。
- **`delete()` 提前 pop `_active` 导致 worker 不退出**(本轮新发现)。
  原实现 `delete()` 先 `cancel()` 置标志、再无条件 pop 条目;
  但 `_cancelled()` 是查 `_active` 的, pop 之后就读不到标志了 ——
  运行中的 worker 会把剩余资源全部下完(实测 56 张约 6 分钟)才释放
  线程池槽位, 期间后续任务一直 `pending`。
  修复: 只在 `future.cancel()` 返回 True(**worker 尚未启动**)时才手动
  回收条目, 否则交给 `_run` 的 `finally`。已加 2 个回归测试。
- `sqlite3.Row` 不支持 `.get()`, 只能用下标(V13 遗留记录, 勿再犯)。


## 6. 验证

- 测试: **59 个用例**全通过(47 -> 59, 新增 partial / 站点级限速 / delete 回归)
- 视频: `python scripts/verify_hls.py`(本地 HLS 服务器, 分片响应故意加 0.4s 延迟,
  12 个分片) ——
  并发 4 耗时 3.42s vs 并发 1 耗时 5.47s;
  第 6 片注入 404 时只坏那一片(旧实现整个视频报废), 11/12 分片正常缓存;
  恢复后重跑 0.75s **只请求了 `/seg5.ts` 一个分片**(断点续传生效)
- 画质档: `quality=1200` 全量落盘 `.webp` + `RIFF` 魔术字节,
  单张 68~119KB vs 原图 656KB
- 队列释放: 删除运行中任务后 **2 秒**内新任务即进入 `extracting`
  (修复前会 pending 数分钟)
- 前端: vite 构建通过(69 modules)

## 7. 工具

- `scripts/probe.py <url>` —— 站点解析探针(通用采集器的四解析器)
- `scripts/verify_hls.py` —— HLS 验证台。V15 起用 ffmpeg 现场生成**真实**
  HLS 素材并加解码校验, 覆盖双引擎 / 强制策略 / 并发 / 续传, 不依赖外网


# V15 ffmpeg 接入: 定位策略 + 双引擎 ✅ 已完成(2026-09-18)

用户装好 ffmpeg(winget, 9.0.1-full_build)后暴露了两件事:
**装好了却用不上**, 以及**没有任何机制能确认到底用没上**。


## 1. 核心问题: `shutil.which()` 读不到新装的 ffmpeg

`which()` 查的是**进程 PATH**, 而那是进程启动时的快照。用户在后端运行期间
装了 ffmpeg, winget 已把 `%LOCALAPPDATA%\Microsoft\WinGet\Links` 写进注册表
PATH, 但已启动的进程读不到 —— `shutil.which("ffmpeg")` 持续返回 None。

若只让用户"重启一下", 这个坑在 Docker / CI / 长驻服务里还会再踩:
**任何运行期安装的可执行文件都识别不了**。

因此新增 backend/core/ffmpeg.py::`find_ffmpeg()`, 按序探测:

1. 显式配置 `settings.ffmpeg_path`(或环境变量 `UWC_FFMPEG`)
2. `shutil.which("ffmpeg")`
3. 已知安装位置: winget Links / scoop shims / choco bin / `C:\ffmpeg\bin`
   等手工解压落点 / 微软商店
4. `imageio_ffmpeg.get_ffmpeg_exe()`(可选依赖, 自带静态二进制)

成功结果进程内永久缓存; **失败结果只缓存 30 秒** —— 这样用户装好后
无需重启后端即可自动发现。配置值参与缓存键, 改配置会重新探测。
配置了但不存在的路径不直接报错, 而是继续往下探测(宽容)。


## 2. `video_engine`: 让"用没上 ffmpeg"可观测、可控

原实现是 `if shutil.which("ffmpeg"): ... else: 内置器`, 两个问题:
探测依赖 PATH(见上), 且**降级是静默的** —— 用户以为走了 ffmpeg,
实际产物是 .ts。

现在三态:

| 值 | 行为 |
|---|---|
| `auto`(默认) | 找到 ffmpeg 就用它拉流, 失败降级内置器 |
| `ffmpeg` | 强制 ffmpeg, 失败**直接报错不降级** |
| `builtin` | 强制内置分片器(仍可用 ffmpeg 做 remux) |

要点: `builtin` 只约束**下载**方式, 不阻止后续 remux —— 用户装 ffmpeg
就是为了拿通用容器, 没必要因强制内置下载而退回 .ts。

⚠️ 走 ffmpeg 拉流时请求由 ffmpeg 自己发出: `DomainLimiter` 不参与,
mirrors 不轮换, 分片进度也只在整个文件完成时上报一次。
对限速/镜像敏感的站点请用 `builtin`。

配套改动:
- subprocess 改用探测到的**绝对路径**, 不再靠命令名解析
- `-nostats` 抑制进度输出(我们用 capture_output 收 stderr,
  长视频的进度行会白白堆积在内存里)
- `_ffmpeg_merge` 更名 `_ffmpeg_pull`: 它做的是"拉流", 不是"合并本地分片",
  原名有误导性


## 3. 验证台重写: 假分片必须换成真实 HLS

旧 `scripts/verify_hls.py` 用 `bytes([i+1]) * n` 造假分片。装上 ffmpeg 后
`auto` 引擎会先走 ffmpeg 拉流, 而假分片不是合法 TS —— **脚本直接失效**。

重写为用 ffmpeg 现场生成真实 HLS(testsrc + sine, libx264 + aac,
关键帧对齐分片边界, 否则 hls_time 只是下限会合并成大分片)。
校验手段同步升级:

- **解码校验**: `ffmpeg -v error -i out -f null -` 完整解一遍 ——
  比只看魔术字节强得多, 后者只能证明文件头对
- **字节校验**: 内置器路径额外比对「按 m3u8 顺序拼接的 sha256」,
  能发现分片错序 / 丢块这类解码器不一定报错的问题


## 4. 启动自检

`scripts/start.py` 新增 `[5/5] 视频引擎 (ffmpeg)`: 直接显示探测到的
ffmpeg 路径与当前引擎行为; 未找到时提示产物为 .ts 及安装命令。
这里必须用 `find_ffmpeg()` 而非 `which()` —— 否则会**误报"未安装"**。


## 5. 验证

- 测试: **78 个用例**(59 -> 78, 新增 tests/test_ffmpeg.py 19 个)
- `python scripts/verify_hls.py`: **18 项断言全通过**
  - ffmpeg 引擎 -> `.mp4`, 完整解码通过, 时长 6.037s
  - builtin -> 内置器分片 + remux -> `.mp4`, 完整解码通过
  - builtin 无 ffmpeg -> `.ts`, **sha256 与原始分片拼接完全一致**
  - auto 无 ffmpeg -> 正确降级到内置器
  - ffmpeg 引擎无二进制 -> 正确报错, 未静默降级
  - 并发 4 = 3.19s vs 并发 1 = 4.26s
  - 缺片 -> 失败并保留 11/12 片 -> 恢复后**只请求 `/seg5.ts`**
- 环境: ffmpeg 9.0.1-full_build(winget), `http/https/crypto/tls` 协议、
  `hls/mpegts` demuxer、`libx264/aac` 编解码器齐全, 同目录带 ffprobe


## 6. 一键启动基础设施 (start.py 完整说明)

入口: `start.ps1`(Windows) / `start.sh`(Unix) / `make start` / `make start-dev`,
全部转发到 `scripts/start.py`, 参数: `--dev` `--build` `--port` `--front-port` `--no-open`。

自检五步(缺失自动安装, 均可跳过重):
1. `uv sync` 后端依赖
2. Playwright Chromium(检测 `%LOCALAPPDATA%/ms-playwright` 等, 缺则 `playwright install`)
3. node/npm(生产模式且 dist 已就绪时跳过)
4. 前端产物(缺则 `npm install` + `vite build`)
5. ffmpeg(见上文 §4)

端口对齐:
- 后端口 8000 被占 → 顺延至 8001... 通过环境变量 `UWC_PORT` 传给
  `backend/main.py`(config.py 已支持 UWC_HOST/UWC_PORT)
- dev 模式 vite 端口 5173 同样顺延, `vite.config.js` 从
  `UWC_PORT`/`UWC_FRONT_PORT` 读端口与代理目标, 代理路由含
  /tasks /collectors /events /files /healthz

坑记录:
- **不要用 `npm run dev` 启动子进程**: TRAE 的 npm.ps1 wrapper 丢
  node_modules/.bin PATH, vite 找不到。直接 `node vite/bin/vite.js`。
- **vite 默认只绑 IPv6 localhost**: 必须 `--host`, 否则 127.0.0.1 健康检查失败。
- **node_modules 被 rolldown 污染**(npm optional deps bug, npm/cli#4828):
  vite 5 依赖 rollup, 出现 rolldown 报错就删 node_modules + package-lock.json 重装。
- Python 子进程重定向输出是全缓冲: `sys.stdout.reconfigure(line_buffering=True)`。

验证记录(2026-09-18):
- prod: 8000 占用时第二个实例自动切 8001, /healthz 与静态页 200
- dev: 后端 8001 + vite 5173 自动对齐, /collectors 与 /events(SSE)
  经 vite 代理转发正常, Ctrl+C 双进程同停(taskkill /T)
- 78 测试全过


# V16 产出可交付: 命名/manifest + 停止 + 增量订阅 + 导出 + 登录态 ✅ 已完成(2026-09-18)

主题从"能爬下来"转到"爬下来的东西能直接用"。新增 4 个核心模块 + 1 组接口。

## 1. 产物组织: 命名模板 + manifest

- `core/naming.py`: `{site}/{host}/{album}/{seq}/{seq4}/{ext}/{type}/{id}` 占位符,
  支持 `/` 分层; `safe_relative()` 做安全校验。
  - ⚠️ 模板里出现 `..` 或绝对路径**整体拒绝**, 不做静默改写。第一版把 `..`
    擦成 `._` 被测试当场抓住 —— "替用户擦屁股"会让错误模板看起来能用。
- `core/layout.py`(**V28**): "文件落到哪一层"的唯一定义 —— 相册文件夹只有一层、
  视频平铺在下载根、清单进 `_meta/<任务ID>/`、重名在下载前消解(`claim`)。
  模板只决定**文件名与相册名**, 层数由这里说了算(所以自定义模板也绕不过规则)。
- `core/manifest.py`: 任务结束(成功/部分失败/**取消**都算)写 `manifest.json`,
  记录相对路径、sha256、size、resolved_url(实际生效的下载点)、content_type、
  note(过滤原因)。**它是面向用户的交付物, 不是内部状态**, 所以用宽松解析。
- 下载层回填: `download_with_mirrors(..., info={})` 里写入真正命中的 URL 与
  响应 Content-Type —— 老代码只知道自己"要下哪个", 不知道"实际下的哪个"。

## 2. 停止: 取消要穿透下载层

- `POST /tasks/{id}/cancel` 与 `DELETE` 分开: 前者保留记录与已下载文件。
- `core/cancel.py::TaskCancelled` 单独成模块, 是为了打破循环依赖
  (downloaders 要识别它, 但 task_manager 反过来依赖 downloaders)。
- ⚠️ **`except Exception` 会吞掉取消信号**: 取消被当成一次普通失败后,
  `_stream_one` 会退避重睡(2s/4s)再重试一个注定被放弃的请求, 期间资源卡在
  downloading、半成品留在磁盘上。所有 `except Exception` 前面必须加
  `except TaskCancelled: raise`(见 `downloaders/base.py`、`video.py`)。
  回归用例: `tests/test_cancel.py::test_cancel_not_swallowed_as_failure`
  断言的是**耗时 < 1s**, 不是"能抛出" —— 只断言类型的话这个 bug 照样过。
- ⚠️ `cancel()` 会**立刻**把 DB 状态写成 cancelled(界面秒响应), worker 还要
  收拾现场。因此"是否收拾干净"要等 `TaskManager.is_active()` 落下去,
  不能看 status。verify_output.py 起初就是在这里读早了。

## 3. 增量续采 + 订阅巡检

- `resources` 增加 `filename`/`resolved_url`/`content_type`; `add_resource`
  支持直接以 `status="done"` + 历史 `hash`/`local_path` 落库。
- `incremental: true` 时 `find_done_resource(url)` 命中即复用, 不发请求
  (`db.count_downloaded(task)` 可断言"本轮真实下载数为 0")。
- 新表 `watches`: url/collector/interval/next_run_time/enabled/hits/last_task_id。
  `due_watches()` + `claim_watch()`(先抢占再执行, 防同一到期点被跑两次)。
  调度线程随 TaskManager 启动, 间隔 `settings.watch_interval`(60s)。
- `_settle_watch()` 在任务 finally 里回填 last_task_id 与 hits。

## 4. 导出与图库视图

- `POST /tasks/{id}/archive`: 流式 ZIP。第一版写成了"先攒在 BytesIO 再吐",
  等于把整个包压进内存 —— 已重写为生成器逐条 `writestr`, 前端按需下载。
- 前端 `TaskDetail.vue`: 状态筛选页签(全部/成功/过滤/失败)、缩略图墙、
  manifest 表格、停止按钮、导出按钮。
  ⚠️ 前端是暗色主题, CSS 变量只有 `--panel-2/--border/--accent/--err/--muted`,
  写新样式前先 `grep "^\." style.css` 确认, 别用 `--surface` 这类不存在的。

## 5. 登录态管理 UI

- `core/sessions.py`: 起 headful Playwright, **周期快照** storage_state
  (用户关掉浏览器后 context 就没了, 只在收尾时读会拿不到);
  落 `browser_state/{domain}.json`, 与既有采集链路共用。
- `api/sessions.py`: `/sessions` 列表、`/sessions/login` 发起、进度查询、收尾、删除。
  注意: 服务端必须是桌面环境, 无头服务器上起 headful 会失败。

## 6. 独立 git 仓库

⚠️ 之前仓库根是 `C:\Users\admin`(整个用户目录), 项目处于未跟踪状态,
仓库里混着 Desktop 上大量无关文件。已在项目目录 `git init`(分支 main),
`.gitignore` 覆盖 downloads/data/node_modules/__pycache__/browser_state/dist。
**不要**在父目录执行 add/commit。

## 7. 验证(2026-09-18)

- `pytest` -> **127 用例**(新增 test_naming / test_manifest / test_incremental /
  test_export / test_cancel)
- `scripts/verify_output.py` -> **33 项断言**: 命名模板落子目录、manifest 溯源字段、
  ZIP 结构与内容、同 URL 二轮下载数为 0、订阅巡检数据闭环、停止在 0.01s 生效且
  无残留半成品
- 前端 `vite build` 通过(72 modules)
- 后端真实启动冒烟: /healthz /collectors /tasks /watches /sessions 均 200,
  不存在的任务返回 404(启动需 `--app-dir backend`, 见 §start.py 的 sys.path 注入)
- ⚠️ 本机有代理时 `curl 127.0.0.1` 会走代理返回 502, 冒烟要加 `--noproxy '*'`

---

## 8. 相册页 URL 解析事故 + 删除语义 (2026-09-18 晚)

### 事故现象
输入 `https://xchina.co/photo/id-6aa5136f606fe/10.html`, 日志显示

```
seq 00001 不存在(text/html; charset=utf-8), 连续缺失 1/3
...
extracted 0 resources
no resource to download (all filtered out)
task success          <- 最误导的一句
```

### 根因
`parse_gid` 的 `id_in_path` 只写了 `/photos/([0-9A-Za-z_-]{6,})`(图片直链形态),
相册页 URL 匹配不上 → 走"取路径末段"退路 → 拿到**页码 `10`** 当图集 ID
→ 去枚举 `https://img.xchina.io/photos/10/00001.jpg`。
该站对不存在的图集同样返回 `200 + text/html`, 于是 3 个序号全判"不存在",
枚举立即停止, **任务报 success 而 0 个资源**。

验证过的对照(HEAD + `Accept: image/*`):

```
photos/10/00001.jpg            -> 200 text/html; charset=UTF-8   <- 猜出来的假图集
photos/6aa5136f606fe/00001.jpg -> 200 image/jpeg  Content-Length: 382383
photos/6aa5136f606fe/00142.jpg -> 200 image/jpeg                 <- 最后一张
photos/6aa5136f606fe/00143.jpg -> 200 text/html                  <- 越界
```

密集扫描 1..200 确认: **1~142 连续无空洞**, 143 起全缺失。没有内部空洞,
所以 `miss_stop=3` 在这个站是安全的。

### 修法(三层)
1. `GallerySite.id_patterns` 支持多条正则并按顺序尝试(直链 / 相册页 / query);
   新增 `page_tail` 把"末段是纯数字"识别为页码, 直接放弃解析
2. 退路只在末段**看起来确实像 ID**(`[0-9A-Za-z_-]{6,}`)时才兜底。
   **宁可返回 None 让采集器报错, 也绝不猜** —— 猜错的表现("成功但 0 资源")
   比报错难查得多
3. `_run` 里采集器返回空列表即 `raise`, 任务判 `failed` 并给出可读原因

### 顺手修掉的两个 bug
- **文件名双后缀** `00001_.jpg.jpg`: 判断"是否最高画质档"时拿**变体后缀**
  (`".jpg"`)去和**画质档名**(`"original"`)比较, 永不相等, 原图也被贴标记,
  再拼上从 URL 取的扩展名就成了 `_.jpg.jpg`。改为 `_quality_tag(site, variant)`
- **看门狗误杀别的实例的任务**: `_watchdog_pass` 只看自己的 `self._active`,
  于是**只要进程里存在第二个 TaskManager**(测试、诊断脚本), 单例的看门狗就会
  把那个实例正在跑的任务判成"服务重启遗留"标 failed。已在模块级登记所有存活
  实例(`_LIVE_MANAGERS`), 判断前先问一遍。
  ⚠️ 只解决单进程内多实例; `uvicorn --workers N` 多进程部署仍需给 tasks 加
  worker_id 列来区分归属。

### 删除语义
- `DELETE /tasks/{id}` —— 只删记录, **磁盘文件保留**(默认)
- `DELETE /tasks/{id}?with_files=true` —— 连 `<下载根目录>/<task_id>/` 一起删;
  双重越界校验(必须是 base 直接子目录 + 解析后仍在 base 内), 只删这一层
  - ⚠️ **V28 已改**: 布局改了之后没有 `<task_id>/` 这一层可删, 改为**按库里的
    记录逐条删**(媒体散在下载根里, 与别的任务共用同一个根): 每条都做"解析后仍在
    base 内"校验, 别的任务仍在引用的文件跳过(`db.count_place_refs`),
    `_meta/<任务ID>/` 整棵清掉。原来那条"去重引用"的顾虑现在被显式处理掉了。
- `POST /tasks/bulk-delete` —— 按状态批量清理已结束任务, 运行中的不动
- 为什么默认不删文件: 内容 hash 去重下别的任务可能引用本任务目录里的文件,
  删了会让那些任务的 manifest 指向不存在的文件

### 验证
- `pytest` -> **149 用例**(新增 test_delete.py 11 项 + test_gallery.py 11 项)
- 真实站点端到端(单例 `tm.task_manager`, 枚举上限压到 3~5 张以控制时长):
  `task success` / 资源全 done / 魔数 `ffd8ffe0` 真 JPEG /
  original 落 `00001.jpg`(382KB)、`quality=1200` 落 `00001_1200.webp`(28KB)
- ⚠️ 验证脚本里 `gb.DEFAULT_MAX = 3` **不生效**: `discover(..., max_count=DEFAULT_MAX)`
  的默认值在函数定义时就绑定了, 改模块全局不影响。要限流得显式传参或包一层采集器


# V17 视频相册枚举 + 相册标题命名 ✅ 已完成(2026-09-18)

用户请求: "视频也是可以通过网址 `https://xchina.co/photo/id-6a3654854fd25.html`
或者相册 ID `6a3654854fd25` 反推到 `.../00001.mp4` 到 `.../00004.mp4`,
保存的文件名可以用相册名称吗, 就是网页的 `<title>` 标签里的文字"。

## 1. 站点实测: 图片与视频共用同一序号空间

```
photos/6a3654854fd25/00001.jpg -> 200 image/jpeg          <- 图片也有
photos/6a3654854fd25/00001.mp4 -> 206 video/mp4  64MB
photos/6a3654854fd25/00002.mp4 -> 206 video/mp4  77MB
photos/6a3654854fd25/00003.mp4 -> 206 video/mp4   5MB
photos/6a3654854fd25/00004.mp4 -> 206 video/mp4 105MB
photos/6a3654854fd25/00005.mp4 -> 200 text/html          <- 越界
```

相册页内嵌 `var videos = [...]` 列出 4 段视频, 网格里是 No.1~No.12 共 12 张图。
**结论: `00001.jpg` 与 `00001.mp4` 并存, 靠扩展名区分, 不会撞名;
只采图片会漏掉 260MB 视频, 只采视频会漏掉整套图。**

⚠️ 不要反推成"有视频的相册就没有图片"。

判存在性仍只能看 `Content-Type`(`video/mp4` vs `text/html`), 状态码越界时也是 200。

Accept 头这次**不构成门槛**: `.mp4` 对任何 Accept(含完全不带头)都返回 206。
`VIDEO_ACCEPT` 仍然保留 —— 这是别的站点可能需要的, 不是本站的实测结论。

## 2. `options.media`: 媒体类型可枚举

`GallerySite` 把媒体声明从"隐含只有图片"改成显式列表:

```python
video_variants = [".mp4"]              # 非空即声明"该站有视频"
video_base = ""                        # 视频在老 CDN 时填这里
site.media("image"|"video") -> MediaType(name, variants, quality_map,
                                          ctype_prefix, accept, seq_format, base)
```

`MediaType` 自带 `ctype_prefix`(`image/` vs `video/`), 所以 `probe()` 加了同名
参数 —— 判定"这是什么媒体"和"它存不存在"是同一件事, 不该分两处实现。

`options.media` 四态: `auto`(默认) / `image` / `video` / `both`。

`auto` 的判定两条线索**都问**:
1. 相册页里的 `var videos` 非空 -> 有视频
2. 否则探测 `00001.mp4` 是否存在(`_video_at_first_seq`, 该站视频必从 1 开始)

两个都说没有才认为没有。为什么要两条: 页面结构会变(不可依赖), 但多一次探测
能避免"站点悄悄改了页面导致静默漏采视频"。失败按"没有"处理, 宁可少采不搞挂任务。

## 3. 相册标题命名: `collectors/album_meta.py`

新增模块, 用 headless Chromium 打开相册页 → 解析 `<title>` 与 `var videos`。

`<title>` 形如 `未公开作品（下） - 国模套图 - 小黄书 xChina`,
按 `site.title_split`(默认 `\s+-\s+`)取第一段作目录名。三档 `options.album_title`:

| 值 | 目录名 |
| --- | --- |
| `clean`(默认) | `未公开作品（下）` |
| `full` | 完整 `<title>` |
| `id` | 图集 ID(不访问相册页) |

### ⚠️ 三个让这段代码白写的坑

**(a) Cloudflare 挑战页是"真实"页面, 不能只等 `goto` 返回。**
相册页先返回 `<title>Just a moment...</title>`, headless Chromium 几秒后自动解开。
若直接读标题就会把 `Just a moment...` 当相册名 —— **比拿不到更糟**(落盘成
`Just a moment.../00001.jpg`)。所以必须轮询到标题不再是拦截页特征串。

**(b) 不能用 `wait_for_function` 等, 只能轮询。**
挑战解开靠**一次导航**完成, 导航会销毁 JS 执行上下文, `wait_for_function`
的 Promise 永远不会 resolve(实测 25s 超时也等不到)。改成
`while _title_is_bad(page.title()): page.wait_for_timeout(1000)`。

**(c) 失效的 `cf_clearance` 比没有登录态更糟 —— 直接 403。**
本地 `browser_state/xchina.co.json` 里的 cookie 已过期, 带上它请求会被
Cloudflare 判为异常而**明确拒绝**; 同一时刻匿名访问反而能正常过挑战。
A/B 实测: 带登录态 → 标题始终 `Just a moment...`; 不带 → 拿到真实标题。
所以 `album_meta` 的策略是**先匿名, 失败再带登录态重试一遍**(而不是"有登录态就用")。
这条与常规直觉相反, 改动前请重新 A/B。

拿不到页面**从不**让任务失败 —— 它只影响目录名与"有无视频"的判定,
降级为"用图集 ID 命名 + 探测 `00001.mp4`"。

## 4. 命名链路: 采集器建议文件名 vs 任务级模板

`discover()` yield 的 `filename` 是 `{相册名}/{seq:05d}{画质标记}{ext}`
(如 `未公开作品（下）/00001.jpg`)。优先级仍是
**任务级 `name_template` > 采集器建议 > 全局默认**:
用户显式写的模板压过采集器习惯, 采集器的建议压过 `{name}`。
采集器建议同样过 `safe_relative()` 清洗, 防 URL 里的怪字符拼出 `../`。

`core/naming.py` 新增 `clean_segment()` 并导出 —— 标题里可能带 `/` `:` `?`,
不清洗就会多出一层目录或拼出非法路径; 清洗后为空则回退图集 ID。

## 5. 验证(2026-09-18)

- `pytest` -> **179 用例**(V16 的 149 + 新增 `tests/test_gallery_video.py` 30 项)
- 真实站点全链路(真下载): 含 mp4, 魔数 `ftyp` 正确, 任务 `success`
- 纯图片相册 `6aa5136f606fe` 回归无变化:
  ```
  图集 6aa5136f606fe -> 目录 '未公开作品（下）'; 采集媒体: image
  === ITEMS: 141 ===   images: 141  videos: 0
  [image] 未公开作品（下）/00001.jpg  size=382383
  ```
- 前端 `vite build` -> 72 modules, 新增 media / album_title 两个下拉

## 6. 顺手修掉的三个既有问题

1. **`tests/test_gallery.py` 里有重复定义的用例**: `test_filename_*` 三个函数
   被完整粘贴了两遍, **后定义覆盖前定义**, 于是 3 个用例名存实亡(一个都不跑)。
   已删掉重复块。教训: 同一文件的多处编辑别并行发, 改完立刻 grep 核对。
2. **`TaskManager.shutdown()` 不等 worker 退出**: 测试里留下的 2 秒慢 worker
   会在**下一个用例**里继续写库(DB 连接是模块级、被 monkeypatch 换过),
   把错误写进下一个用例的任务里, 表现为"看门狗用例随机失败"。
   新增 `shutdown(wait=True)` 并让测试 fixture 使用。
   ⚠️ 生产默认仍是 `wait=False`(不阻塞服务退出), 别把测试的用法当成默认语义。
3. **`album_title=id` 仍在开浏览器**: 文档说"不访问相册页", 代码却无条件抓取。
   已改为 `id` 模式整个跳过相册页(media=auto 退回 `00001.mp4` 探测, 不漏视频)。

## 7. 遗留

- `album_meta` 每开一个任务拉起一次 Chromium, 启动成本约 2~4s。
  做成常驻实例会更省, 但会引入生命周期/崩溃恢复问题, 暂不做
- 视频**没有**尺寸档位(只有 `.mp4` 一条), 所以 `mirrors` 为空、`quality` 对它无意义
- 相册页里 `var videos` 目前只用于"有没有视频"的布尔判断, **不**当权威资源清单
  —— 序号枚举才是稳定路径

# V18 相册页信息吃干榨净: 创建前预览 + 体积前置 + h1/标签命名 ✅ 已完成(2026-09-18 晚)

起因是"这个采集器还有什么建议"。动手前先复测了站点, 结果**推翻了既有结论中的两条** ——
按错的结论去优化, 做出来的东西一定错, 所以先记这两条。

## 1. 先纠错: 两条既有结论是假的

1. **"该站 HEAD 不返回任何响应头"是错的。** 实测 `requests` 的 HEAD 4/4 都正常:
   存在 → `200 image/jpeg` / `200 video/mp4`(带 `Content-Length`), 越界 → `200 text/html`;
   `probe_size('.../00001.mp4')` 当场拿到 67341834。
   原结论来自 **curl 经系统代理时 `-I` 只回一行 `200 Connection Established`** 的假象。
   危害: 会让人以为"每个序号要发两次请求"而去写无意义的优化。
   流式 GET 回退仍然保留(对真的不吐 `Content-Type` 的站点有用), 但**本站不需要**。
2. **六种输入形态解析全部正确**, 包括当初踩坑的 `/photo/id-XXX/10.html` ——
   因为 `id_patterns` 先命中拿到 ID, `page_tail` 只在**退路**生效。别因为它是事故现场就再改一遍。

## 2. 相册页白给的三样东西

页面里除了 `<title>`, 还有站点自己写的元数据(都在 `collectors/album_meta.py` 解析):

```
<i class="fas fa-image"></i></div><div class="text">12P + 4V</div>   <- 资源数量
<i class="fas fa-file"></i></div><div class="text">FENDSON</div>     <- 厂牌/制作方
<div class="item tags-line">…<div class="tag">丝袜</div>…            <- 标签
var videos = [{"url":"\\/photos\\/gid\\/00001.mp4","filesize":"64M"}, …]
```

- `filesize` 实测**精确**: `"64M"` ↔ `67341834`、`"105M"` ↔ `109900697`(MiB 取整, 误差 <1MiB)。
  而它**零请求** —— 比为了知道体积再发一次 HEAD 又快又稳。
- 定位一律靠**图标锚定**(`fa-image` / `fa-file`)或 class(`tags-line` / `tag`), 不靠 div 顺序。
- 数量/体积仍**只用于预告**, 不当资源清单(页面会改版、自报值可能滞后)。

## 3. `POST /tasks/preview`: 创建前预告

一个相册可能是"12 张图 + 4 段视频共 251MB", 而 `media=auto` 会照单全采 ——
用户应当在**点创建之前**就知道, 而不是等它慢慢拖完。

- 有页面数据 → **零序号枚举**一次返回: `目录名 / 12 图 / 4 视频 / 251.0MB / 标签`
- 页面拿不到(没浏览器 / Cloudflare 拦住 / `album_title=id`)→ 退回受限枚举,
  返回 `sampled: true`, 数量只能当**下限**读(前端显示成 `≥4 张图`)
- 什么都没枚举到时也会给出**预测的** `sample_files`(`相册名/00001.jpg`) ——
  命名是最容易出错的一环, 让用户先看一眼文件名
- `max_items`(默认 12, 上限 50)是必须的: 预览接口不能因为"想看全"被拖成几分钟
- 与创建**共用** `_gallery_options()`, 否则会出现"预览通过、创建却被拒"这种最难查的不一致
- ⚠️ `photos` / `videos` / `video_bytes` **只统计本次真要采的媒体**, 必须与 `media` 一致:
  `media=image` 时 `videos` 是 `null`、`video_bytes` 是 0。
  回归表现是"预告说会采 4 段视频、创建后一段没下" —— 预告与行为不一致比不预告更糟,
  用户会以为任务漏下了东西。相册页自报的总量另外用 `photos_declared` /
  `videos_declared` 带出, 界面据此提示"另有 4 段视频未采", 让用户能分清
  "站点没有"和"我没要"。

## 4. 体积前置: 把已拿到的 size 一路带下去

`discover()` 的 `probe()` 本来就要读 `Content-Length`(判断存在性顺带就拿到了),
相册页又直接给出每段视频体积。所以资源的 `size` 在采集阶段就是已知的, 一路带到下载层:

```python
size = r["size"]                    # 采集阶段已知 -> 零额外请求
if size is None:
    size = probe_size(r["url"], headers)   # 只有真未知才补一次 HEAD
```

⚠️ 两个易错点: `size` 经 JSON/DB 往返可能是**字符串**, 要 `isdigit()` 后再转 int,
不能因为类型不对就当成"未知"去重探; 反过来也不能把非数字字符串硬塞进比较。
回归表现是"每个资源平白多一次 HEAD", 所以 `tests/test_gallery_preview.py` 里
直接把 `probe_size` 换成 `pytest.fail` —— size 已知时一旦被调用就炸。

## 5. 命名: `h1` 与标签分层

- `album_title` 增加 `h1` 档: 页面 `<h1>` 往往比 `<title>` 更全
  (`…（FENDSON）` vs `…`), 取不到依次退让 `h1 → album → gid`。
- 新增 `album_tags_dir`: 在相册名外再套一层标签目录(`丝袜-情趣内衣/相册名/…`),
  标签逐段过 `clean_segment()` 且**只取前 3 个** —— 再多只是把路径撑长, 信息量递减。
  - ⚠️ **V28 已移除**: 布局规则变成"下载目录的下一级只有相册文件夹"之后, 套上去的
    分类层只会被 `layout.place()` 削掉, 于是"日志/预览显示的路径"与"实际落盘的"
    对不上(用户照预览去找文件找不到)。选项从前端到 schemas/API 全部删除 ——
    留着一个点了没反应的开关比删掉它更糟。标签仍完整写在 album.json / manifest 里。
- `discover(album=...)` 允许带 `/` 分层: 逐段清洗但保留层级; 含 `..` / 绝对路径时
  `safe_relative()` 返回 None, 退回整串当一段清洗, 所以标题里写什么字符都逃不出任务目录。

## 6. 有意识不做的三件事

1. 拿 `var videos` / `12P + 4V` 当资源清单 —— 序号枚举才是稳定路径。
2. 为视频做档位或 HLS —— 实测 `.m3u8`、`_600x0.mp4` 都不存在; 视频只有一条线路,
   `mirrors` 为空是事实而非缺陷。
3. `{date}` 占位符 —— 页面里的日期疑似来自"推荐位"(一次抓到 5 个不同日期),
   **归属未验证**, 当发布日用会命名错。

## 7. 验证(2026-09-18 晚)

- `pytest` -> **205 用例**(V17 的 200 + 预告口径与 `media` 一致性 5 项)
- 真实站点预览(相册页 URL): `12 图 / 4 视频 / 251.0MB`, `page=true sampled=false`, 零枚举
- 同一相册 `album_title=id`: `sampled=true`、`page=false`, 退回探测到 `00001.mp4`, 仍判出含视频
- 真实页面解析复核: `photos=12 videos_declared=4 maker=FENDSON`, 6 个标签, 4 段视频体积
- 前端 `vite build` -> 72 modules, 新增「预览」按钮与预告面板

---

# V19 采集器自动识别 + Cloudflare 长期对策 ✅ 已完成(2026-09-19)

用户不该记住"这个 URL 该配哪个采集器"; 而 Cloudflare 这件事的长期答案
也不是打赢指纹对抗, 而是**让采集不依赖那个被保护的 HTML 页**。

## 1. 采集器自动识别

约定(见 `collectors/__init__.py` 模块文档)::

    class SomeSpider:
        @classmethod
        def match_score(cls, url) -> Optional[int]: ...

返回 `None` = 不认领; 整数 = 能处理, **越大越优先**; 同分按名字升序 ——
可复现比"更聪明"重要, 否则同一输入今天走 A 明天走 B, 没法复盘。

分数阶梯放在 `collectors/scores.py`(单独一个模块是为了**打破循环依赖**:
`collectors/__init__` 末尾要导入所有 spider, spider 又要读这些常量)::

    SCORE_ALBUM_PAGE(100) > SCORE_RESOURCE_URL(50) > SCORE_BARE_ID(10)
    > SCORE_GENERIC(-1000)      # 通用采集器恒定垫底

### ⚠️ 两个必须守住的点

1. **认领必须有凭据, 而且要用 `strict` 模式解析 ID**
   第一版只要求"域名匹配 + `parse_gid` 成功", 结果 `/tag/some-tag` 被认领:
   `parse_gid` 的"路径末段退路"会从 `some-tag` 里读出一个看似合法的图集 ID,
   于是去枚举一个不存在的图集 —— 又是"任务成功但 0 个资源"。
   现在自动识别走 `parse_gid(..., strict=True)`, **跳过末段退路**, 只认
   正则配出来的 ID。真实采集保留退路(那时用户已手选采集器)。
   > 一句话: **替用户做决定的场合, 一律用最严的那条规则。**

2. **库里存真名, 不存 `auto`**
   存 `auto` 会让重放/订阅巡检时"同一个 auto 指向不同采集器", 任务行为
   不再可复现。`TaskCreateIn.collector` 默认 `"auto"`, `_pick_collector()`
   解析后立刻换成真名入库, 并把识别结论塞进响应的 `resolved` 字段回显。

没法识别时(输入连 URL 都不是)**明确报 400**, 不悄悄兜底给 generic ——
那会在 DNS 层失败, 错误信息对用户毫无帮助。

前端: 下拉框首项是「自动识别」, 输入框变化 300ms 后调 `/collectors/resolve`
显示"已识别为 X"; 认不出来 / 结果不唯一会换成对应颜色提示。
**不回显的自动识别是不合格的** —— 一旦认错, 用户连去哪里手选都找不到。

## 2. Cloudflare 长期对策

先说结论: **不投入指纹对抗**。那是永远升级的军备竞赛, 而且靠 headless 指纹
"打赢"之后, 下次失败往往更莫名其妙。真正的对策在架构上:

- 资源发现永远走纯 HTTP 序号枚举(`gallery_base`), 相册页只提供
  **锦上添花**的三样东西: 目录名、自报数量、视频体积线索。
- 拿不到页面 -> 降级用图集 ID 命名。**采集照常**, 只差一点美观。

在此之上补三件事(均在 `collectors/album_meta.py`):

| 机制 | 做法 | 为什么 |
| --- | --- | --- |
| 域级熔断 | 连续 3 次读不到 -> 该域 10 分钟内不再开 Chromium, 直接降级 | 每次读页要开一次 headless(几十秒), 明知会被拦还去开纯属浪费, 也更像扫描器 |
| 陈旧登录态隔离 | 带登录态被 `Attention Required!` 拒 -> 标记失效, 本进程不再使用; `/sessions` 的 `cf_stale` 列出 → 提示重新登录 | 陈旧 `cf_clearance` 是**永久拒绝**, 不会自愈; 必须第一次就认出来 |
| 降级可见 | 所有降级路径往 log 写人话, 并回答"接下来会怎样" | 用户至少要分清"我被拦了"和"站点改版了" |

⚠️ **被拦 ≠ 登录态失效**: 匿名同样会被拦。只有**带着登录态**被拒才把账记到
登录态头上 —— 否则会无端让用户去重新登录, 而真正的问题没解决。

同样的区分也用在熔断计数上: **页面结构不符**(比如站点改版、`objId` 对不上)
不计入熔断 —— 那是另一个问题, 继续开浏览器也救不回来, 但不该把采集器拖进冷却。

用户重新登录/删除登录态后, `finish_login` 与 `remove_session` 会调
`clear_domain_state()`, 立刻解掉记录(不需要重启后端)。

## 3. 验证(2026-09-19)

- `pytest` -> **245 用例**(205 + `test_collector_autoresolve` 28 项 +
  `test_cloudflare_guard` 12 项)
- 这两个文件都是**先写测试才抓到 bug** 的典型:
  - 自动识别 `/tag/some-tag` 被误认领 -> 引出 `parse_gid(strict=True)`
  - `loader.html = ""`(连正文都没拿到)原先不计入熔断 -> 改成 `not html`
- 真实站点冒烟: 三种输入形态 -> `xchina_gallery`; `example.com` -> `generic`;
  `随便打几个字` -> 400 "没有采集器能处理该输入"
- 真实预览走 auto: `12 图 / 4 视频 / 251.0MB`, `resolved.collector=xchina_gallery`
- 真实 create 走 auto: 库存 `xchina_gallery`(非 auto), 图集不存在时报 failed 并
  列出可接受的 URL 形态(失败是**响亮**的)
- `verify_output` 33 项 / `verify_hls` 18 项 / `vite build` 72 modules

---

# V20 XChina 视频页(签名 m3u8)支持 ✅ 已完成(2026-09-19)

用户给的真实样本: `https://video.xchina.download/m3u8/6aaa517d3f106/720.m3u8?expires=1789797019&md5=qKo4fsrJKvVIBp3tFwWVtA`
(迅雷抓到的播放列表)。实测结论(决定实现方案):

| 事实 | 影响 |
| --- | --- |
| 签名 `expires` 约 30 分钟有效 | 短命凭证, 不能像普通 URL 那样存库慢下; 但创建时若已过期必须**响亮失败** |
| 过期/错误签名 -> **200 + 语法合法的 m3u8**, 指向 `/fallback/placeholder.ts` | 最阴险的失败: 不报错 |
| `placeholder.ts` 是 **603KB 真实可播放 TS** | 会静默下成占位视频, 任务照报 success(本项目反复强调要杜绝的模式) |
| `#EXT-X-KEY:METHOD=AES-128,URI="/key/enc.key"` | 分片加密, 直接拼 .ts 得到密文(首字节 `a65d69f5`, 非 TS 同步字节 `0x47`) |
| `/key/enc.key` 无需 Referer/签名, 返回 16 字节 | 拿到 playlist 即拿到全片 |
| `cdn.xchina.download/ts/.../*.ts` 无需签名 | 只有 playlist 那一层设防 |
| 只有 720 一档; 1080/480/master 都返回那个 102 字节占位列表 | 无选档空间 |
| 视频页 `xchina.co/video/id-XXX.html` 是 **403 CF 挑战页**(首页正常) | 拿 playlist 只能靠 headless |

**不用 `<video>` 标签、不接迅雷**: 标签逐段拼是重复造轮子, 且拿不到签名 URL;
本工具已有 HLS 双引擎(`video_engine`), 缺的只是"识别出链接已死"的能力。

## 1. 播放列表健全性校验(`collectors/hls.py`, 新增)

`inspect_playlist(url, session, log)` 下载清单做三道校验, 返回 `(ok, kind, reason,
segments, duration, encrypted, key_url, ...)`:

- `expires` 解析自 URL query(支持秒/毫秒), **已过期 -> 拒绝**, 理由写明"凭据过期, 需重新获取"
  (数字先用 `time.time()` 兜底成合理范围, 避免 1970 之类脏值被误判)
- 分片清单为空(只指向 placeholder) -> 拒绝 `kind=placeholder`
- 有 `EXT-X-KEY` 但密钥 URI 不可达 -> 拒绝 `kind=no-key`
- 下钻 master playlist 取 leaf, 直到拿到真正分片; master 本身无分片不算失败

`estimate_size(segments, session, log)`: 用「单片 HEAD 体积 × 分片数」估整段大小,
让 `min_size/max_size` 过滤对 HLS 有效(否则 `.m3u8` 的 2.5KB Content-Length 会误杀整段视频)。

## 2. 下载器接线(`downloaders/video.py`)

- 新增 `_preflight_hls()`: 先 `inspect_playlist` 预检, 失败**抛清晰错误**(不进引擎);
  成功则向下钻到 leaf 播放列表。`_download_m3u8` 在引擎分发**之前**做预检。
- ffmpeg 路径改用预检得到的 leaf URL; builtin 路径复用预检的分片清单, 删掉重复的
  `_resolve_segments` 抓取, 并保留"加密流需 ffmpeg"守卫。
- ⚠️ `engine=ffmpeg` 无二进制时**先于预检** fail-fast(确定性本地配置错, 不该被网络错掩盖)。
- 端到端实测: ffmpeg 解密 AES-128 产出 **27.1MB / 5:05 真视频**(非 603KB 占位), 退出码 0。

## 3. 视频页采集器(`collectors/xchina/spider_video.py`, 新增 + `@register`)

- 输入 `https://xchina.co/video/id-{gid}.html` 或视频 gid, 或 m3u8 直链。
- `match_score`: `/video/` 路径=100, m3u8 直链=50; 复用 `scores.py` 阶梯。
- `crawl`/`preview`: `browser_runner(url, on_response)` 默认用 Playwright, **监听
  response 事件捕获 `.m3u8`**(播放器要播就必请求, 比解析 DOM/JS 可靠)。
  `on_response` 用 `"m3u8" in u`(真实 URL 带 `?expires=` 查询串, `endswith(".m3u8")`
  永远匹配不上 —— 这是测试立刻抓到的 bug)。
- 浏览器运行器做成**可注入依赖**, 单测用假运行器 + 假 session, 不真触网。
- 产出 video 资源: `url=m3u8, headers={referer: page}, size=estimate_size(...)`,
  `filename={clean_title}.mp4` 或退回 `{gid}.mp4`。
- 复用 `album_meta` 的 CF 熔断 + 匿名优先(视频页也是 CF 挑战页)。

## 4. 自动识别 / 前端

- `/collectors/resolve` 认领视频页与 m3u8 直链 -> `xchina_video`。
- 预览面板从 `isGallery` 扩成 `isGalleryLike`(视频也能看预览;`isVideo` 时隐藏图集专属
  的命名方式提示与"读取相册页"文案)。图集专属选项(画质/媒体/相册标题)仍只对 `isGallery` 生效。
- 中文名"XChina 视频页(自动过 Cloudflare 抓带签名 m3u8)"。

## 5. 验证(2026-09-19)

- `pytest` -> **266 用例**(`test_hls_guard` 12 + `test_xchina_video` 9, 其余继承)
- `verify_output` 33 项 / `verify_hls` 18 项 / `vite build` 72 modules
- 端到端 ffmpeg 解密实测真视频(27.1MB / 5:05), 非占位片
- 这两个文件同样是**先写测试才抓到 bug**: `endswith(".m3u8")` 匹配不上带 query 的
  真实 URL; `estimate_size` 的 FakeSession 漏 `head` 方法导致体积变 None


# V21~V23 任务列表增强 + CDN/正则加固 + "有效资源"三关 ✅ 已完成(2026-09-19)

## 1. 任务列表增强(V21)

- **任务名** `tasks.name`: 提取后由 `_infer_name()` 从首个资源推断(图集取目录名、
  视频取标题)回写。列表/详情优先显示它。
- **暂停/继续**: `TaskStatus.PAUSED` + `POST /tasks/{id}/pause|resume`。
  `pause()` 置取消标志让 worker 在**资源边界**退出; `resume()` 把
  `downloading/skipped/failed` 复位 pending、**不重新采集、不重下已 done 的文件** ——
  这是它相对"取消+重跑"的唯一价值, 测试要断言这一点, 不是断言状态字段变了。
- **打包下载**: 原本是裸 `<a href>`, 无 loading 态、与其他操作交互不一致 ——
  真实 UI bug。改成统一按钮。
- **重试抖动**: 指数退避 × 0.6~1.4。纯指数会让一批并发失败的请求在同一时刻
  集体重试(重试风暴), 把站点/WAF 瞬间打爆。
- ⚠️ `match_size` 收到的 size 经 JSON/DB 往返**是字符串**, `str < int` 抛
  `TypeError` 会把整个任务拖垮(资源全卡 pending)。`_coerce_size()` 统一转 int。

## 2. CDN 基址与序号宽度(V22/V23) —— 本版最值得记的一节

同一站点会把不同相册分到不同 CDN 子路径(`photos` / `photos2` / `photos3`),
序号补零位数也不同(`0001.jpg` vs `00001.jpg`)。写死基址的后果是: 相册明明存在,
枚举的却是另一条不存在的路径 → 全部判 missing → **0 资源 → failed**,
而用户只看到"失败", 完全看不出是路径不对。

**三条线索按可信度依次采信**(`_resolve_base`):

1. **相册页 HTML 里引用的图片地址** —— 页面白给的真实前缀, 最可信, 零探测。
   (`album_meta.extract_album_meta` 的 `resource_urls`, 用 `<img src>` 反推)
2. **用户输入的资源直链** —— 基址与宽度都写在 URL 里了。
3. **候选基址 × 相邻序号宽度试探** —— 默认基址先用, **不中才探**, 所以正常相册
   零额外请求。(初版无条件预探测, 让 happy path 平白多一次请求 —— 被计数型假
   session 当场逮住。这类"多花一次请求"的退化只有计数断言能发现。)

**候选不写死清单**: 站点声明 `base_candidate_digits=5` → 自动展开 `photos2..photos5`。
手写三个的话, 下次出现 `photos4` 就整批判空, 而用户看不出是路径变了。
(生成时从 **2** 起: 无后缀那个就是 base 本身, `photos1` 不是真实形态, 生成它
只会白探一次。)

⚠️ **一个资源根只能有一个出处**: `_base_candidates()` 同时供 `_resolve_base`(真去探)
与 `_match_score`(静态判前缀)使用。各写一份的话会出现"采集能探到、自动识别却不认领"
的半通状态 —— 直链被派给 generic、预览报 400, 而采集本身明明好使。**"一半好一半坏"
最难查**, 所以候选只允许有一个出处。

### 正则设计的四条

1. **锚定到"gid 后面紧跟一个带媒体扩展名的文件名"**, 不能只抓中间那一段:
   `/photos/featured/0001.jpg` 会把路径词 `featured` 当成 gid, 然后去枚举一个
   不存在的图集 —— 又是一次"成功但 0 资源"。
2. 序号用 `[^/?#]+` 而不是 `\d+`: 直链可能带变体后缀(`00046_600x0.webp`)。
3. `photos\d*` 而非 `photos`: 子路径带数字后缀是常态。
4. **形状校验 `site.gid_shape`**(如 `[0-9a-f]{8,}`)是 `strict=True` 专属:
   自动识别在"替用户做决定", 猜错是静默的; 手选采集器时不校验(输入形态各异)。
   形状不符**不再往下走末段退路** —— 退路比形状判据更不可靠, 放行更危险。

### 直链线索的解析要注意

`parse_resource_hint(site, raw, gid=...)` 从直链读出 `(base, seq_format)`。
⚠️ 必须传**用户原始输入**: 传 gid 的话直链里的 `photos2` / `0001` 信息早就丢了。
线索带 gid 校验(页面里有推荐位其他相册的图), 且只当**线索**不当结论(用户可能
粘了一条失效的旧直链), 仍会探一次, 探不通就退回候选探测。

### 采不到时要说人话

- 采集到 0 个资源抛 `_empty_hint()`: 列出试过的基址 + 下一步动作(如"用浏览器
  打开一张图, 把直链粘进来"), 而不是返回空列表让上层记一句"采集到 0 个资源"。
- 预览返回 `resource_roots`(实际生效的基址/序号格式/来源)与 `warning`;
  任务级落 `album.json` sidecar, 字段同名。**预告里看到的路径 = 创建后真正枚举的
  那一条**, 不再是黑箱。

## 3. "什么是有效资源"三关(`core/filters.py::match_resource` 唯一定义)

定义散落两处必然导致行为不一致, 所以两处接入点(提取阶段按 URL 预筛、下载前按
真实体积/像素复核)共用它。

1. **长得像不像(URL 层)**: 类型 / 扩展名黑白名单 / 关键词 / 广告位(`exclude_ad`)
2. **够不够格(体积层)**: min/max_size、`min_image_bytes`(挡 1x1 跟踪像素)
3. **是不是真的(内容层)**: 下载后按 Content-Type 复核(`require_image`) ——
   越界 URL 会返回 `200 + text/html`, 光看状态码会被骗。

本版新增 **像素层**: `min_width/max_width/min_height/max_height` —— 下载后读文件头
(`core/imageinfo.py`, 纯 Python 解 PNG/JPEG/GIF/WebP/BMP)拿真实尺寸, 不符则删文件
标 filtered。视频与文档不参与尺寸规则。

⚠️ 两条容易写错的:
- `exclude_ad` **按路径分段精确匹配**, 绝不做子串包含 —— 子串会把
  `downloads/badges/1.jpg` 这种正常文件误杀。
- **尺寸/去重复用文件时绝不删别人的文件**: 命中去重说明别处已有同一份内容,
  按本任务规则删它等于破坏别的任务的产出。

## 4. 429 与站点级冷却

- 站点说"慢一点"时按它给的 `Retry-After` 等(秒 / HTTP 日期), **不套**普通指数
  退避 —— 普通退避封顶 8 秒, 站点要求冷静几十秒时硬闯只会让封禁更久。
- 冷却**按站点共享**(`site_key`), 不是按 URL: 同一站点的一个 URL 被 429,
  其他 URL 硬闯同样会让封禁更久。

## 5. 本轮踩到的坑

- ⚠️ **Git Bash 给 Windows Python 传 `$PWD/...` 会造影子库**: `$PWD` 是
  `/c/Users/...`, Windows 解析成 `C:\c\Users\...`。冒烟时表现为"服务用的库
  和我查的不是同一个", 极易被误判成"多实例共用库"。传路径请用 `$(pwd -W)`
  或 Windows 风格绝对路径。
- ⚠️ **看门狗判活必须看库里的心跳**(`tasks.hb`), 不能只看进程内
  `_LIVE_MANAGERS`。两个实例共用一个 SQLite 时, A 的看门狗看不到 B 的 worker,
  会把一个正在枚举 114 张图的任务判成"重启遗留"标 failed —— 表面症状却是
  `invalid transition extracting -> downloading`(状态对不上), 根因在别处。
  长耗时枚举期间要靠采集日志回调节流刷新心跳(否则被"无进度"规则误伤)。
- 同一文件的多处 Edit **不要并行发**(会静默丢改动, 已踩多次), 改完立刻 grep 核对。

## 6. 验证(2026-09-19)

- `pytest` -> **347 用例**(新增 `test_cdn_base_discovery` / `test_ad_filter` /
  `test_image_filter` / `test_http_guard` / `test_sidecar` / `test_pause_resume`)
- `verify_output` 33 项 / `verify_hls` 18 项 / `vite build`
- 真实站点: 相册 `69ad45698f836`(photos2 + 4 位序号) 端到端 **114 张真图 / 56.1MB /
  全部 JPEG 魔术字节 `ffd8ff` / success**; `album.json` 正确记录
  `resource_roots.image = {base: photos2, seq_format: {seq:04d}, source: probe,
  site_default: photos}`


# V24 聚合页采集器(模特/演员/系列/索引) ✅ 已完成(2026-09-19)

## 1. 目标与设计

一次把"整个模特/整个系列"的相册与视频页收进队列, 再逐个**委派**给既有
`xchina_gallery` / `xchina_video`。采集器本身**不碰下载内核** —— 它只发现
子页面 URL, 完全复用现有两条采集链路。

**核心决策: URL 驱动抽取, 不是 DOM 选择器。** 从 HTML 里正则出所有 `<a href>`,
按 URL 模式(`/photo/id-*`、`/video/id-*`、`/model/id-*`…)分类。理由: 这是个
"以 URL 为主键"的站点, 类名会改、结构会变, 但 URL 语义稳定。用 DOM 选择器的话
站点一改版就静默采到 0 个 —— 本项目的头号忌讳。

## 2. 真站实测入口结构(2026-09, 必须复用这份而不是猜)

| 形态 | URL | HTTP 可达性 |
|---|---|---|
| 模特/演员落地页 | `/model/id-{id}.html`、`/actor/id-{id}.html` | **纯 HTTP 200** |
| 索引页 | `/models.html`、`/models/type-{n}.html` | 200 |
| 系列页 | `/photos/series-{id}.html`、`/videos/series-{id}.html` | **403 CF** |
| 全量列表页 | `/videos/model-{id}.html`、`/photos/model-{id}.html` | 403 CF(可降级 headless) |

⚠️ **落地/索引页开放, 全量列表页 CF 保护** 是本站的规律。我原本**猜**的
`/model/xxx`、`/tags/xxx`、`/series/xxx`、`/photos/xxx` 实测全是 **404** ——
再次印证"宁可先探也不猜"。

## 3. 三个关键坑(都由测试或真站验证暴露)

- ⚠️ **归一化只归内容页**: 相册的 `/10.html` 是**同一相册的第 10 页**, 必须
  归一化到主页(否则一个相册被当 N 个条目, 重复下载又白吃 `max_items`);
  但**列表页的分页是不同内容**, 归了会漏采。二者的判据是 URL 模式, 不是"有数字"。
- ⚠️ **闸门要在"追加时"生效**: 第一版 `max_items` 只在进入页面时检查, 追加时
  从不截断 → 闸门形同虚设。改成每追加一条就判一次合计上限。
- ⚠️ **截断必须说出来**: 到层数上限/条目上限时, 日志与 `album.json` 都要写明
  "还有 N 个未展开"。悄悄少采最容易被误当成"站点只有这些"。

## 4. 口径一致性(本项目的铁律)

`preview` 与 `crawl` 必须同源。本轮修掉两处不一致:
- `group`(目录名): `crawl` 用页面标题推出的模特名, `preview` 却回退 gid →
  **预告的目录名与最终落盘目录名不一样**。改成共用同一套。
- `max_items`: `crawl` 读 `options["max_items"]`, `preview` 只读参数 →
  用户设 5、预览却按 12 展开。改成取两者较小值, 并以 options 为准。

## 5. 日志回调签名(踩过两次的坑)

采集器会用 `log(msg, "warn")` 标出"这条要显眼"。而 task_manager 注入的
`crawl_log(m)`、API 预览的 `logs.append` 都**只收一个参数** —— 两参调用会抛
`TypeError`, **把所有采集工作做完后、写汇总那一刻崩掉整个任务**。
两处适配器都改成接受可选 `level` 参数。原则: **日志绝不该有能力让功能失败。**

## 6. 验证(2026-09-19)

- `pytest` -> **399 用例**(新增 `test_xchina_aggregate` 46 项 + API 选项校验)
- `verify_output` 33 / `verify_hls` 18 / `vite build` 全过
- 真站有界预览(单请求, 不激怒被收紧的 CF): `group=艾玛`(模特名而非 gid)、
  `max_items=2` 生效、截断如实标注, 0.9 秒返回
- 真站端到端 crawl: **119 个资源**, 三层命名空间 `艾玛/阁楼监禁…/00001.jpg`,
  页面自报 119 张与枚举结果**完全一致**
- 自动识别 12 形态复核: 6 种聚合形态 → `xchina_aggregate`, 相册/视频页/直链/
  裸 ID 各归其位, `/tag/some-tag` 与非本站域落 `generic`, 全部无歧义

## 7. 正则加固 / CDN 画像 / 感知去重 / 形状软提示(2026-09-19 续)

### 7.1 声明自检 `check_site`: 把"改一行正则的副作用"变成测试红灯

`parse_gid` 取的是**首个命中**的 pattern, 所以新加一条正则时若不慎也能匹配旧
URL, 行为就**静默**变了 —— 某个相册突然采空, 日志里一切正常。

`GallerySite.id_samples = [(url, 期望gid), ...]` + `check_site(site)` 断言两件事:

1. 每条样本经 `parse_gid(strict=True)` 得到期望的 gid;
2. **没有任何一条 `id_patterns` 单独**就能对某条样本配出不同结果 ——
   这是"谁先谁赢"的隐式依赖, 顺序一变行为就变, 应该修正则而不是靠顺序。

第三层自洽检查: 声明了 `gid_shape` 却没有任何样本能通过 = 形状写窄了
(自动识别会把真图集也挡在外面)。

分工: 运行期只记 warn(采集器不该因一次自检失败就拒绝干活), 测试里
`selfcheck_all() == {}` 是硬断言, 运维另跑 `scripts/selfcheck.py`。

### 7.2 手选采集器的形状软提示

`gid_shape` 在自动识别时是**硬**判据(不符直接不认领), 在手选时只给提示。
理由是不对称: 自动识别认错是静默的(去枚举不存在的图集 -> "成功但 0 资源");
手选是用户已表过的态, 而站点可能刚换 ID 格式、我们比用户知道得晚。

实现上**刻意不塞进 `resolved`** —— 那个字段专指自动识别的结论, 界面靠 `auto`
区分显示方式。所以 `_pick_collector` 返回三元组 `(名字, 识别结论, 软提示)`,
创建响应里是 `TaskCreateOut.warning`(独立字段), 预告面板顶层也是 `warning`。

### 7.3 CDN 画像: 消费 `seq_formats` 才是真正省钱的那一步

`cdn_profile.py` 原本只被用来重排**基址**。实测发现问题: 探测循环是
"格式外层、基址内层", 所以**宽度不对时会把每个候选基址都白试一遍**才轮到正确
宽度 —— 用户那个相册因此花掉 6 次探测。把画像里的 `preferred_seq_format` 提到
首位后:

```
冷启动  6 次探测(photos/photos2..5 × {seq:05d} 全灭, 才试到 photos2+{seq:04d})
有画像  1 次探测(photos2 + {seq:04d})
```

⚠️ 只是**排序提示**: 全部候选组合仍真探一遍。反向断言
(`test_profiled_format_does_not_hide_other_widths`) 专门锁住这一点, 防的
是未来有人把 `fmts` 直接替换成 `[fav_fmt]`。

### 7.4 ⚠️ `tests/conftest.py` 为什么必须隔离画像文件

画像落在真实数据库旁边时, 测试会**借助磁盘文件偷偷互相通信**: 一个用例探到
photos2 并记一笔, 之后每个用 XCHINA 的用例候选顺序都被改掉,
`test_discover_no_extra_request_on_happy_path` 于是失败, 而报错只有
"请求数 4 != 3", 完全看不出跟上个用例有关。单跑绿、全跑红、重跑又绿。

解法: `UWC_CDN_PROFILE` 环境变量(生产上也是有用的运维开关), conftest 里
autouse fixture 把它指到每个用例自己的 `tmp_path`。**隔离靠机制, 不靠自觉。**

### 7.5 感知去重(dHash): 只标记, 绝不删除

已有的是 sha256(字节级), 认不出"换尺寸 / 重新压缩 / 重复收录"——
而图集站上最常见的重复恰恰是这种。`core/phash.py` 用 ffmpeg 解成 9x8 灰度再算
64 位 dHash, **零新增依赖**。

三条约束(写在模块 docstring 里, 改之前必读):

1. **只标记不删除** —— dHash 会误判(纯色图、连拍), 删文件不可逆, 出错的代价
   由用户承担。命中写 `resources.duplicate_of` + manifest 的 `duplicate_of`。
2. **失败即放行** —— ffmpeg 不在 / 解码失败一律当"没算出指纹", 绝不因此让
   下载失败。`_mark_perceptual_dup` 自己兜住所有分支。
3. **不动 sha256 那条路径** —— 两条独立判据并行。

实测判别力(ffmpeg 现场生成真图): 同图换尺寸 **距离 0**、同图重压 jpg **距离 0**、
异图(testsrc2 vs smptebars) **距离 40**。阈值 4/64。

几个易错点:

- `-frames:v 1` 不能省: 动图会让 ffmpeg 一路吐帧, 输出超过 9x8 字节时旧实现会
  把后续帧当同一张图的像素接着算 —— 得到一个"稳定但错误"的指纹, 比报错难查。
- `distance(a, b)` 返回 `None`(没法比)与 `0`(完全相同)含义必须分开。
  把"没法比"当"相同", 就是凭空冤枉用户的文件。
- `find_duplicate` 要返回**最近**的那个而不是第一个命中的: 提示语里会写
  "与 #N 疑似同一张, 距离 d 位", 报一个更不像的会让用户觉得功能不准,
  于是整个标记都不看了。
- **只在同一任务内比对**: 跨任务的"重复"用户点不过去也删不掉, 是不可行动的信息。
- 默认开、可关(`filters.dedup_perceptual`), 但它**不算过滤条件**(不进
  `Filters.active`)—— 否则每个任务都会打印一行"filter [无过滤条件]"。

### 7.6 ⚠️ `FilterIn` 静默丢弃字段(界面开关空转的元凶)

`models/schemas.py::FilterIn` 曾只声明 8 个字段, 而前端 `buildFilters()` 会发
`min_width` / `min_height` / `exclude_ad` / `min_image_bytes`。pydantic 默认
**忽略未知字段**, 于是这些参数在进入 `Filters` 之前就被丢掉了 ——
**界面上的「尺寸下限」「排除广告位」开关按了没有任何效果**, 无报错、无日志,
用户只会以为"这个功能没用"。

实测: `FilterIn(min_width='300').model_dump()` 返回 `{}`。

两条一起做: ① 已知键全部声明; ② `extra="allow"` 兜底(新前端 + 老后端混跑时
不再吞参数, `Filters` 只读它认识的键, 多余键无害)。回归断言
`test_filter_in_keeps_every_dimension_it_declares` 拿
`Filters` 认识的键集合做对照 —— 以后新增维度忘了声明, 这里立刻红。

### 7.7 验证(本轮)

- `pytest` -> **458 用例**(新增 `test_phash` 19 项 + `test_site_declaration` 40 项)
- `verify_output` 33 / `verify_hls` 18 / `vite build` 全过
- 真站回归(同一相册): 冷启动 6 次探测 -> 有画像 **1 次**; `data/cdn_profile.json`
  记录 `{photos2: n, {seq:04d}: n, last: photos2}`
- 感知去重真实图片判别力: 同图 0 / 0 / 异图 40(阈值 4)

## 8. 新增/改动的文件清单(本轮)

```
backend/core/phash.py           新   dHash 指纹(ffmpeg 解码, 零新依赖)
backend/core/cdn_profile.py     新   站点 CDN 画像(含 UWC_CDN_PROFILE 开关)
scripts/selfcheck.py            新   声明自检 + 画像快照
tests/conftest.py               新   把 CDN 画像隔离到 tmp_path
tests/test_phash.py             新   19 项
tests/test_site_declaration.py  新   40 项
backend/collectors/gallery_base.py   check_site / assert_site / selfcheck_all /
                                     shape_warning / base_host_templates /
                                     page_tail 列表化 / % 解码 / ±2 宽度 /
                                     _hit() 记画像 / 画像排序
backend/collectors/xchina/gallery.py id_samples(8 条, 覆盖全部输入形态)
backend/core/database.py             resources.phash / duplicate_of + task_phashes()
backend/core/manifest.py             manifest 带 phash / duplicate_of
backend/core/filters.py              dedup_perceptual / dedup_threshold
backend/models/schemas.py            FilterIn 补全 + extra="allow";
                                     TaskCreateOut.warning; ResourceOut 补字段
backend/core/task_manager.py         _mark_perceptual_dup()
backend/api/tasks.py                 _pick_collector 三元组 / _manual_warning
frontend/src/App.vue                 重复检测开关 / 手选提示行 / 偏好持久化
frontend/src/components/TaskDetail.vue  疑似重复标记(清单 + 资源网格)
frontend/src/style.css               .dup-hint
```


## 8. V26 — 下载速度优化（令牌桶 / 自适应节流 / 枚举快路径 / 连接池）

起因：按实际代码逐段测算 300 张图的耗时，结论反直觉 —— **真正传数据只占 5%，
其余 95% 都在等**。所以这一轮不是"把并发调大"，而是把等待本身拆掉。

### 8.1 ⚠️ 最重要的诊断：吞吐由间隔决定，不由并发决定

旧实现在每个请求前持锁按 `_last + gap` 排队 —— 这正是**容量=1 的退化令牌桶**：
长程平均速率被限死的同时，连一点突发都不允许。于是 `domain_concurrency` 调多大
都不提速（用户最常问的一句"我把并发调到 16 怎么还是这么慢"）。

修法不是降 `domain_min_interval`（那是用礼貌性换速度），而是放大桶的容量：
**平均速率不变，只是允许把攒下来的配额一次花掉**。同样的礼貌、更少的干等。

### 8.2 AIMD：补上"只减不增"的缺口

冷却原本是**单向阀** —— 429 之后间隔会放宽，但到期后只回到配置值，没有"增"
那一半。站点被限过一次就永久卡在最慢档，只能人工改配置恢复。

- 加性增：连续成功 `_AI_EVERY`(8) 次收紧一档，地板 `domain_fast_interval`
- 乘性减：任一失败/被限立即翻倍放宽，天花板 `domain_slow_interval`，并清零计数
- ⚠️ **不看响应延迟**（Scrapy AutoThrottle 的默认套路在本项目是帮倒忙）：
  CDN 边缘缓存亚毫秒返回 → 判定"服务器很闲"→ 疯狂加速；429 同样毫秒级返回 →
  被当成健康。本项目两个条件都命中，所以**只按成功与否判定**。
- ⚠️ 只有"传输层/服务端"失败才计入放宽；Content-Type 不合预期是这条 URL 的问题，
  拿它去拖慢整个相册是把"URL 不对"误判成"站点限流"。

### 8.3 枚举快路径：抽样校验过的区间替代逐张探测

三级阶梯：**L0** 页面自报数量 → 直接取 N，抽样校验后跳过逐张探测；
**L1** 指数探上界 + 二分（**默认关**）；**L2** 线性扫描兜底（原行为）。

⚠️ **L1 默认关是刻意的**：二分假定序号连续，中间恰好缺一张就会把上界定在缺口
之前 —— 那是"300 张只采到 4 张"的**静默截断**，比慢得多更糟。要开先确认站点
序号确实连续（`enumeration_search`）。

⚠️ 快路径的本质是"用抽样推断全体"，所以抽样点**必须包含首尾**：数量错一个、
起点错一位是最常见的两种不一致，只抽中间点会同时放过它们。任一不中即退回逐张
扫描 —— 最坏情况只是多花几次探测，不会采错。

真站实测（`69ad45698f836`，同一相册各跑一遍）：

| | 资源数 | 探测次数 | 耗时 |
|---|---|---|---|
| 逐张扫描 | 114 | 117 | 61.7s |
| 快路径 | **114（完全一致）** | **6** | **2.6s** |

### 8.4 连接池与传输

- `SESSION` 挂 `HTTPAdapter(pool_maxsize = max(10, 并发×2), pool_block=True)`：
  默认 `pool_maxsize=10` 且池满时**建了又丢**，池化收益全丢还白付握手
- ⚠️ `pool_block=True` 有个前提：响应**必须关闭**。416/429/内容校验这些提前退出
  的路径都没读过响应体，连接不会自动归还；泄漏到池满就是永久阻塞（卡死而非报错）。
  已用 `try/finally: resp.close()` 兜住。
- `filters.probe_size` 改用共享 `SESSION.head`：裸 `requests.head` 每次新建连接，
  N 个资源就是 N 次 TCP+TLS 握手 —— 而探测本身就是为了省请求
- `CHUNK` 64KB → 256KB；超时拆成 `(connect 10, read 120)` 元组，单值会同时约束
  两者，把大视频的读取掐断

### 8.5 感知去重降开销 + 自检扩字段

- sha256 命中**别的任务**时不再解码：感知比对只在任务内进行，那次 ffmpeg 一定
  比不出东西，300 张图就是 300 次白白创建进程且占着下载 worker
- `check_site` 纳入 `seq_format` / `base_candidate_digits`：这两项写错的表现是
  "每张都判 MISSING → 0 资源 → failed"，而用户只看到失败（V22 的根因）
- 任务日志新增「站点节奏 …→ 长程约 N req/s」与预计耗时

### 8.6 踩坑记录

1. ⚠️⚠️ **可重入死锁**：`DomainLimiter` 的 `interval` / `rate` 属性各自取锁，
   而 `_wait_token` / `describe` 持锁后调用它们 —— 普通 `Lock` 不可重入，
   **当场死锁**，表现为"调了一次就整个进程卡住"，从堆栈完全看不出所以然。
   修法：锁内**就地计算**，并把锁换成 `RLock` 兜底（可重入只掩盖问题，不解决）。
2. ⚠️ **`sqlite3.Row` 没有 `.get()`**：误用会抛 AttributeError，而这个位置在
   `try` 里 —— 异常被当成"下载失败"，报得离真相很远（测试里表现为
   `assert 'failed' == 'filtered'`）。
3. ⚠️ **`finally` 里的异常会覆盖原异常**：给响应加 `close()` 时，测试替身没有该
   方法 → AttributeError 在 `finally` 中抛出，掩盖了真实原因，重试循环继续跑，
   最终报成"pop from empty list"这种毫不相干的错。替身应当**模拟真实对象的完整
   契约**。
4. ⚠️ **写测试期望值前先确认数据形态**：序号是 5 位补零，`"/0030.jpg" in url`
   永远匹配不上，于是"中间缺一张"的场景根本没生效，断言却在别处先红。

### 8.7 验证

- `pytest` → **477 用例**（新增 `test_ratelimit` 9 项令牌桶/AIMD、
  `test_enum_fast` 9 项、`test_phash` 1 项）
- `verify_output` 33 / `verify_hls` 18 / `selfcheck` / `vite build` 全过
- 真站对比：探测 117 → 6，耗时 61.7s → 2.6s，资源数一致

### 8.8 新增/改动的文件清单（本轮）

```
backend/downloaders/ratelimit.py    DomainLimiter 令牌桶 + AIMD + describe()
                                    note_success / note_failure
backend/downloaders/base.py         连接池(HTTPAdapter) / CHUNK / 超时元组 /
                                    try-finally 关闭响应 / 成功失败信号
backend/core/config.py              domain_burst / adaptive_throttle /
                                    fast|slow_interval / enumeration_* /
                                    connect|read_timeout
backend/core/filters.py             probe_size 走共享 SESSION
backend/collectors/gallery_base.py  discover 快路径(declared_count) /
                                    _sample_points / _search_upper /
                                    check_site 扩 seq_format
backend/core/task_manager.py        跨任务字节命中跳过 dHash / 节奏与 ETA 日志
tests/test_ratelimit.py             +9
tests/test_enum_fast.py             新 9 项
tests/test_phash.py                 +1
tests/test_cancel.py                替身补 close()
tests/test_http_guard.py            替身补 close()
```


## 9. V27 — 健壮性（从"记得别踩"到"忘了就红"）

起因：把后端一万行按失效模式扫了一遍，结论不是"又发现几个坑"，而是——
**已有的 30 多个坑都靠人记才不踩**。PITFALLS.md 写得再全，也不会有人在下一次
加 `try` 之前先去读一遍。所以这一轮一半在补缺口，一半在把缺口变成门禁。

### 9.1 P0：四个"用户一定会遇到、且看起来像程序坏了"的缺口

1. **孤儿任务**：后端重启后，库里 `running/extracting/downloading` 的任务没有任何
   manager 持有，看门狗只在活着的实例里跑 —— 它们**永远停在"运行中"，进度条不动**。
   → `recover_orphans()`：扫心跳超时的活动任务，有资源清单的复位成 `paused`
   （已下好的文件还能接着用），没有清单的判 `failed` 并给出"重试"提示
   （续跑一个空清单只会得到"成功但 0 资源"）。
   ⚠️ 挂在 `lifespan` 上而**不是模块导入时**：放模块级的话，任何人 `import main`
   都会去扫库改状态 —— 一个"只读地导入一下"的动作产生写副作用。
2. **SQLite 零锁处理**：`sqlite3.connect` 没有 `timeout`、没有 `busy_timeout`、
   没有任何 `OperationalError` 重试。WAL 只解决读写并发，**写-写仍单写者**，
   多个 worker 同时写心跳/日志/资源状态撞上就是 `database is locked` ——
   而它落在业务 `try` 里会被当成"这个资源下载失败"，任务莫名 failed。
   → `DB_TIMEOUT` / `busy_timeout` + `_retry_write()` 带退避，**只对 locked/busy
   重试**（语法错误重试只是把真 bug 藏起来），且有上限（一直撞锁要变成可诊断错误）。
3. **非原子写**：直接写最终路径 → 中断留半成品，被去重/manifest/预览当成成果；
   更隐蔽的是**续传不校验**：无条件 `open(path,"ab")` 接着写，残片若来自另一个
   URL 或本身就是坏的，产出坏文件，而 sha256 把"坏的全文"算得毫无破绽 ——
   **文件在、大小对、内容是坏的**，最难查的一类。
   → 写 `.part` + `os.replace` 原子改名；`.part.src` 记下来源 URL，不匹配即
   丢弃重下；落盘后比对实际字节数与声明大小（有 `Content-Encoding` 时不比，
   那是压缩后的长度）。
4. **磁盘满**：`ENOSPC` 会走完整重试链空转几百次。
   → 任务前 `ensure_free()` 预检；下载中捕获即置 `abort` 事件让同批 worker 直接
   跳过，终态交 `_final_status` 判 partial/failed（已下好的文件是真实成果）。

### 9.2 P1/P2：把"记得加"变成机器门禁

| 机制 | 落点 | 挡住的坑 |
|---|---|---|
| **静态门禁** `tests/test_cancel_guard.py` | AST 扫核心模块：凡 `try` 块内有取消源调用，前面必须有 `except TaskCancelled: raise`，漏了 pytest 红 | 85 处 `except Exception` 对 8 处取消捕获 —— 漏一处就是"点了停止没反应" |
| **异常分类** `core/errors.py` | `CollectorError`（给用户看、不打堆栈）/ `TransientError`（重试）/ 未分类 → 记 traceback | 报错离真相很远（`Row.get()` 那次） |
| **全局异常处理器** | `install_exception_handlers(app)` → 统一 `{detail, type}`，堆栈只进服务端日志 | 默认 500 的结构随部署方式变化，且可能带堆栈片段 |
| **日志裁剪** | 每任务保留 `LOG_KEEP_PER_TASK` 条，超出删最旧 | 长期运行数据库单调膨胀 |
| **采集取消检查点** | 借 `crawl_log` 检查取消 | 逐张枚举几十分钟，点了停止界面变灰后台还在跑 |
| **同路径独占写** | 新建用 `xb`，冲突即报错退出 | 两个任务写同一 `.part`，字节交错且双方都报成功 |

⚠️ 门禁第一次跑就**抓到一个真违规**：`video.py` 的 ffmpeg 拉流被 `except Exception`
包着 —— 取消会被当成"ffmpeg 失败"，进而**降级到内置分片下载，继续下一个已被叫停的
视频**。这种"取消后还在干活"的 bug 靠读代码很难发现，靠 AST 扫一遍就有了。

⚠️ 采集取消检查点有两处细节：`crawl_log` 必须收第二个可选参数（采集器用
`log(msg,"warn")`），且**只在没有异常正在传播时才检查取消** —— 采集器常在 `except`
块里调 log 报告错误，那时抛 TaskCancelled 会把真因顶掉。

### 9.3 验证

- `pytest` → **502 用例**（新增 `test_robustness` 23 项、`test_cancel_guard` 门禁）
- `verify_output` 33 / `verify_hls` 18 / `selfcheck` / `vite build` 全过
- 真实下载冒烟：`.part` 与 `.part.src` 落盘后均已清理，二次下载 sha 一致

### 9.4 新增/改动的文件清单（本轮）

```
backend/core/errors.py              新: CollectorError / TransientError /
                                    DiskFullError / is_retryable / describe
backend/core/disk.py                新: ensure_free / free_bytes
backend/core/database.py            DB_TIMEOUT / busy_timeout / _retry_write /
                                    日志裁剪(LOG_KEEP_PER_TASK)
backend/core/task_manager.py        recover_orphans / 磁盘预检与 abort /
                                    crawl_log 取消检查点 / 异常分类消费
backend/downloaders/base.py         .part 原子写 / _prepare_resume 来源校验 /
                                    长度校验 / ENOSPC → DiskFullError / xb 独占
backend/downloaders/video.py        补 except TaskCancelled(门禁抓到的真违规)
backend/main.py                     install_exception_handlers / lifespan
tests/test_cancel_guard.py          新: 取消穿透 AST 门禁
tests/test_robustness.py            新 23 项
tests/isolation.py                  新: 共享磁盘状态隔离清单(唯一入口)
tests/test_isolation.py             新 7 项: 隔离机制本身失效即红
```

### 9.5 共享状态显式化：DB 与下载目录隔离（V27 收尾）

PITFALLS「跨边界耦合」类坑的根因从来不是逻辑错，而是**两个用例隔着磁盘互相说话**。
上一轮只隔离了 CDN 画像（`UWC_CDN_PROFILE`），DB 与下载目录这一轮补上 —— 它比画像
危险得多，因为里面装的是**用户的真实任务记录**：一个忘了加夹具的新用例调一次
`db.create_task`，用户的任务列表里就多出一条假记录，界面上看不出是谁写的。

是怎么堵住的，以及为什么不能只靠夹具：

1. **根因 1 — 模块级常量**：`core.database.DB_PATH`、`core.task_manager.DOWNLOADS_DIR`
   是导入时就从 `settings` 拷出来的常量。`monkeypatch.setattr(settings, ...)` **打不到**
   它们 —— 夹具改了 settings，测试仍在写真库。所以 `isolation.py` 的 `MODULE_TARGETS`
   直接 `setattr` 到模块上。
2. **根因 2 — 连接单例**：`_conn` 一旦建过就永远指向那个文件，光改 `DB_PATH` 没用，
   后面所有写操作仍落旧库，症状是"打印 DB_PATH 正确、数据却进了上一个用例的库"。
   → 换库同时 `setattr(db, "_conn", None)` 复位。
3. **机制而非自觉**：`tests/isolation.py` 是**唯一登记入口**。新增一处共享状态，只改
   这一个文件；漏登记也不再静默 —— 一道 session 级守卫 `_no_writes_to_real_dirs`
   在整轮前后给真实目录拍指纹（size/mtime），变了就红，并提示"去 isolation.py 登记"。
4. **守卫本身也要可证伪**：`tests/test_isolation.py` 里 `test_changed_keys_detects_writes` /
   `test_new_task_lands_in_temp_db_not_the_users` 等 7 项，保证"失效就红"而不是"看起来在隔离"。
   验证过：故意往真实 `downloads/` 写一个文件，pytest 立即变红；拆掉 conftest 的隔离，
   全量测试也立即变红。

⚠️ 这套只覆盖"运行期攒状态、测试会读"的模块。将来新模块若在导入时把别的配置也拷成常量，
必须同步加进 `MODULE_TARGETS`，否则隔离对它失效而测试照样绿 —— 这正是守卫要兜住的漏登记。

### 9.6 产物先于终态：次序本身就是契约

**"任务到了终态"这句话，对消费者意味着"输出目录已经完整"。** 一旦终态先于产物可见，
中间就有一个窗口：轮询到终态的消费者（打包、预览、`verify_output`）去读目录，而
`manifest.json` 还没写出来。

原来的 `_run` 正是反的 —— 先把任务 `transition` 成 `success`，manifest 到 `finally`
才补写。所以 `verify_output` 会**偶发**在"manifest exists"这条上失败（实测 3 次挂 1 次）。
它也解释了为什么这类 bug 特别难查：单跑常常绿，CI 上偶尔红，重跑又绿。

修法是把次序倒过来，并让"记进 manifest 的状态"永远等于"任务最终的状态"：

```python
finally:
    # 停止态(取消/暂停)会先于收尾落库, 以库里的为准; 否则用本次算出的终态。
    stopped = cur["status"] if cur and cur["status"] in (CANCELLED, PAUSED) else None
    effective = stopped or final
    self._write_manifest(task_id, status=effective)   # 先落产物
    self._settle_status(task_id, effective)           # 再置终态
```

两处细节，缺一个都会留坑：

- **`_settle_status` 不能直接用 `_transition`**：`_transition` 开头会 `_check_cancel`，
  而收尾跑在 `finally` 里 —— 在那里抛取消异常会顶掉真正的收尾动作（manifest、watch 结算）。
  所以另设一个**绝不抛异常**的置态函数，迁移失败只记 warn。
- **`status` 要覆盖着传进 `_write_manifest`**：写清单时库里还是 `downloading`，而清单该记的
  是终态。同时用 `stopped or final` 兜住"取消恰好落在收尾窗口里"的情况，否则会出现
  「清单说 success、任务列表显示 cancelled」。
- **`resume` 分支也要走 `finally`**：它原先在分支里自己 `_transition(DOWNLOADING, final)`
  再 `return` —— 同样把终态摆在了 manifest 前面。现在两个分支都只负责算 `final`，
  置态统一交给 `finally`。

回归用例不等时序碰运气：拦住"写终态"这个动作，在它发生的**那一刻**查 manifest 在不在
（`test_sidecar.py::test_manifest_lands_before_task_is_marked_done/_failed`）。次序一反过来必红，
与调度快慢无关 —— 验证过。
