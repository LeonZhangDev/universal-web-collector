
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
- 代理: config.yaml proxy 或环境变量 UWC_PROXY
  (http/socks5, requests 与 Playwright 均走代理)


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
