# Universal Web Collector v10

uv + Makefile + FastAPI + Vue3 + Playwright 资源采集平台。

## 快速开始

```powershell
# Windows 一键启动(推荐): 自检环境 -> 缺失自动安装 -> 端口占用自动对齐 -> 打开浏览器
.\start.ps1              # 生产模式(后端 8000 托管前端)
.\start.ps1 --dev        # 开发模式(后端 + vite 热更新)
```

```bash
# 通用入口 / Makefile 等价命令
./start.sh               # Linux/macOS
make start               # 生产模式
make start-dev           # 开发模式
```

启动脚本会依次: `uv sync` 装后端依赖 → 检测/安装 Playwright Chromium →
按需 `npm install` + 构建前端 → 检测 8000/5173 端口占用并自动顺延对齐 →
启动后自动打开浏览器。常用参数: `--port 9000` 指定起始端口、
`--build` 强制重建前端、`--no-open` 不开浏览器。

手动方式(不依赖脚本):

```bash
make install        # 安装后端依赖 + chromium + 前端依赖
make backend        # 启动后端 (http://127.0.0.1:8000)
make frontend       # 前端开发模式 (http://127.0.0.1:5173)
```

生产模式: `make build` 构建前端后, 后端直接托管 `frontend/dist`,
访问 http://127.0.0.1:8000 即完整应用。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /tasks/create | 创建任务 `{"url": "...", "collector": "auto"}` |
| POST | /tasks/batch-create | **批量创建**: 一次粘贴多行 URL, 创建前逐行预检重复/无效并返回逐条结论 |
| POST | /tasks/preview | **创建前预告**: 只发现不下载不写库, 返回目录名/张数/视频体积/标签 |
| GET | /collectors | 已注册采集器列表 |
| GET | /collectors/resolve | **URL -> 采集器**(纯字符串判定, 不打网络请求), 用于"已识别为 X"回显 |
| GET | /tasks | 任务列表, 支持 `q`(搜索) / `status`(可重复, 兼容组代号) / `collector` / `page` / `page_size`, 返回 `{items,total,page,page_size,pages}` |
| GET | /tasks/stats | 采集统计: 总量 / 按状态 / 按采集器 / 近 30 天日期序列 / 失败原因聚合 / 去重报表 |
| GET | /library | **跨任务资源库**: `q`(搜索) / `kind`(image,text) / `album`(相册名精确匹配) / `task_id` / `page` / `page_size`; 默认只列 `done` 资源, 每条带 `refs`(被多少任务共用) |
| GET | /library/albums | 资源库内出现过的相册名(仅含有已完成资源的相册) |
| GET | /tasks/{id}/proxy | 该任务代理池状态: 脱敏 spec + 每线路 `{proxy,fails,blocked,blocked_for}` |
| GET | /tasks/storage | 任务/产出占用概览, 供"清理"界面预检 |
| GET | /env/diagnose | 环境诊断: Python / 浏览器 / ffmpeg / 磁盘 / 下载目录 五项 ok/warn/fail + 修复提示 |
| GET | /tasks/{id} | 任务详情 + 资源 |
| GET | /tasks/{id}/logs | 任务日志 |
| POST | /tasks/{id}/resources/{rid}/retry | 资源级重试(强制下载, 跳过过滤规则) |
| POST | /tasks/{id}/retry-failed | **失败资源选择性重试**: 只重下 `failed`/`skipped`, 保留已成功部分 |
| POST | /tasks/bulk-action | **批量操作**: `{"action":"pause\|resume\|cancel\|retry\|delete","task_ids":[...]}` 逐任务归类 ok/skipped/not_found, 单个失败不阻断其他 |
| POST | /tasks/{id}/cancel | 停止任务: 保留已下载文件, 未完成资源置 skipped |
| DELETE | /tasks/{id}?with_files=true | 删除任务。**默认只删记录, 磁盘文件保留**; `with_files=true` 连任务目录一起删 |
| POST | /tasks/bulk-delete | 批量清理 `{"statuses":[...], "with_files":false}`; 运行中的任务不会被删 |
| POST | /tasks/{id}/archive | 打包导出该任务的产出(ZIP 流式, 可按状态过滤) |
| GET | /notifications | 任务结算通知列表 + 未读数 |
| POST | /notifications/read | 标记通知已读(不传 `ids` 表示全部) |
| GET | /files/{task_id}/manifest | 产出清单 manifest.json(JSON) |
| GET | /watches | 订阅源列表 |
| POST | /watches | 新建订阅源 `{"url","collector","interval_minutes"}` |
| POST | /watches/{id}/run | 立即巡检一次 |
| POST | /watches/{id}/toggle | 启用/停用 |
| DELETE | /watches/{id} | 删除订阅源 |
| GET | /sessions | 已保存的登录态 + 正在进行的登录任务 |
| POST | /sessions/login | 打开 headful 浏览器手动登录(服务端必须是桌面环境) |
| GET | /sessions/login/{id} | 查询登录进度 |
| POST | /sessions/login/{id}/stop | 收尾并保存 storage_state |
| DELETE | /sessions/{domain} | 删除该站登录态 |
| GET | /events | SSE 实时推送(task.updated/task.log/resource.updated/task.bytes) |
| GET | /files/{task_id}/{file} | 下载文件(限定在该任务输出目录内) |
| GET | /config | 默认下载目录 + 可选资源类型 |
| GET | /fs/browse | 浏览本机目录, 供前端目录选择器使用 |
| POST | /fs/mkdir | 新建文件夹 |

创建任务时可额外传:

```json
{
  "url": "...", "collector": "auto",
  "download_dir": "D:\\my-out",
  "name_template": "{site}/{album}/{seq4}.{ext}",
  "quality": "original",
  "media": "auto",
  "album_title": "clean",
  "incremental": true,
  "write_manifest": true,
  "proxy": "http://127.0.0.1:7890",
  "filters": {
    "types": ["image", "video"],
    "exts": ["jpg", "png"],
    "exclude_exts": ["gif"],
    "keywords": ["original"],
    "exclude_keywords": ["thumb", "icon"],
    "min_size": "10KB", "max_size": "50MB"
  }
}
```

`download_dir` 必须是绝对路径、不含 `..`、不能是盘符根目录;
文件落在该目录下的**相册文件夹**里(视频平铺在该目录根, 见下文「落盘目录结构」)。
被过滤的资源标记 `filtered` 并把原因写入 `note`, 可在详情页"强制下载"单独补下。

`proxy` 为**任务级代理**, 与全局环境变量 `UWC_PROXY` 相互独立: 写单个地址即该任务
全程走它; 写多个(英文逗号分隔)则按资源序号**轮换**, 用来摊薄单 IP 的请求量。
每个任务独占一个会话(不共享全局连接池), 所以任务之间不会互相污染代理设置。
不填时下载器继续用全局 `SESSION`(含 `UWC_PROXY`) —— 不会把环境变量里的代理清掉。

线路不是平均轮换就算完: 同一条线路**连续失败 3 次即熔断**, 冷却 300 秒, 期间 `pick()`
会跳过它选下一条; 冷却到期自动半开放出(保留失败计数, 再失败一次立刻重新熔断)。
成功的下载会清零该线路的失败计数。冷却中**不再需要健康检查线程** —— 到期即放行,
下一次真实请求本身就是探针。全池都在冷却时退化为按序号返回(故障多半不在代理上,
直接返回 `None` 会让任务彻底停摆)。熔断状态只放内存不落库: 进程重启即重算, 更符合直觉。
运行时可用 `GET /tasks/{id}/proxy` 看到每条线路的 `fails` / `blocked` / `blocked_for`。

### 采集器自动识别

`collector` 缺省就是 `"auto"`: 后端按 URL 形态挑一个采集器, **不需要用户
记住"这个链接该用哪个采集器"**。每个采集器用类方法 `match_score(url)` 声明
自己能处理什么(返回 `None` = 不认领), 分数越高越优先, 通用采集器恒定垫底。

| 输入 | 识别结果 |
| --- | --- |
| `https://xchina.co/photo/id-6aa513208a506.html` | `xchina_gallery` (分数 100, 相册页) |
| `https://img.xchina.io/photos/6aa513208a506/00001.jpg` | `xchina_gallery` (分数 50, 资源直链) |
| `6aa513208a506`(纯图集 ID) | `xchina_gallery` (分数 10) |
| `https://xchina.co/video/id-6aaa517d3f106.html` | `xchina_video` (分数 100, 视频页) |
| `https://video.xchina.download/m3u8/abc/720.m3u8?expires=...&md5=...` | `xchina_video` (分数 50, 带签名 m3u8) |
| `https://xchina.co/model/id-601190f157fe7.html` | `xchina_aggregate` (分数 100, 模特/演员落地页) |
| `https://xchina.co/models.html` / `/models/type-7.html` | `xchina_aggregate` (分数 100, 索引页) |
| `https://xchina.co/videos/model-601190f157fe7.html` | `xchina_aggregate` (分数 100, 全量列表页) |
| `https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg` | `pexels` (分数 50, 资源直链) |
| `https://www.pexels.com/photo/xxx-1234567/` | `pexels` (分数 100, 单图页) |
| `1234567`(纯数字 ID) | `pexels` (分数 10) |
| `https://example.com/a/b` | `generic`(没有专用采集器认领) |
| `随便打几个字` | 无法识别 -> 400 "请手动选择采集器" |

三条设计约束:

1. **认领必须有凭据** —— 只凭域名不够, 必须先 `parse_gid` 成功, 而且用的是
   `strict` 模式(不用"路径末段"那条退路)。`/tag/some-tag` 这种列表页的末段
   同样过得了 ID 字符集校验, 被认领就会去枚举一个不存在的图集 —— 后果是
   "任务成功但 0 个资源", 比报错难查得多。
2. **库里存解析后的真名, 不存 `auto`** —— 否则重放/订阅巡检时同一个 `auto`
   可能指向不同采集器, 任务行为不再可复现。
3. **结论必须回显且可覆盖** —— 界面在输入框失焦时调 `/collectors/resolve`
   显示"已识别为 X"; 认不出来或结果不唯一会明确提示。手选永远优先。

> ⚠️ **多个采集器抢同一个输入时, `match_score` 必须各退一步。** 纯 ID 样本是歧义重灾区:
> 通用判据 `[0-9A-Za-z_-]{6,}` 什么都能装, xchina 的 `6aa5136f606fe` 和 Pexels 的
> `1234567` 都满足。如果两站同分, 最终按采集器名字字典序决胜 —— `pexels` 排在
> `xchina_gallery` 前面, 用户粘一个 xchina 图集 ID 会被静默送去 Pexels。
> 所以新采集器在 `match_score` 里必须用自己的 `gid_shape` 再对纯 ID 把关一次
> (`if not _BARE_ID.fullmatch(raw): return None`), 而不是只依赖通用正则。

`name_template` 可用占位符: `{site}` `{host}` `{album}` `{seq}` `{seq4}`
`{ext}` `{type}` `{id}`, 支持 `/` 分层; 含 `..` 或绝对路径分隔符的模板
整体拒绝(不静默改写), 由 `core/naming.py` 处理。
`incremental: true` 时, 同一 URL 历史上已成功下载的资源直接复用磁盘文件,
不再产生网络请求。

只对图集类采集器(`xchina_gallery`)生效的选项:

| 选项 | 取值 | 说明 |
| --- | --- | --- |
| `quality` | `original` / `1200` / `800` / `600` | 图片主 URL 用哪一档; 未选中的档位自动作 mirrors |
| `media` | `auto` / `image` / `video` / `both` | 采哪些媒体; `auto` = 相册里有什么采什么 |
| `album_title` | `clean` / `full` / `h1` / `id` | 输出目录名取法: 相册页 `<title>` 去站名尾巴 / 完整标题 / 页面 `<h1>` / 图集 ID |

### 落盘目录结构

```
<download_dir>/
  葡萄一番街/00001.jpg        ← 图片等非视频: 相册文件夹 + 文件名(只有一层)
  6aaa517d3f106.mp4          ← 视频: 平铺在下载根目录, 文件名取站点原名
  _meta/101/manifest.json    ← 每个任务的清单(与媒体分开, 见下)
  _meta/101/album.json
```

规则唯一定义在 `backend/core/layout.py`, 采集器与下载器都来问它, 不各自拼路径:

- **下载目录的下一级只允许是相册文件夹**(图片/音频/文档等)。采集器/聚合页套了
  多层(标签层、模特层)时只保留最深那一段 —— 完整信息在 `album.json` 里。
- **视频一律平铺在下载根目录**, 文件名取站点原名: mp4 直链用 URL 末段,
  m3u8 用 gid(`6aaa517d3f106.mp4` —— 站点自己的播放列表就叫 `{gid}.m3u8`)。
  不用页面标题: 那一层没有相册文件夹可依赖, 名字必须自带唯一性, 而标题会重名。
- **重名在下载前消解**(`layout.claim`): 同一条 URL 复用原名交给下载层去校验;
  不同来源撞名则相册内的加序号(`00001(2).jpg`)、平铺的加相册名
  (`0001_葡萄一番街.mp4`)。不这么做的话, 下载器会把已有的同名文件当成
  「同一个文件的半成品」接管续传, 拼出一份长度正确、内容错误的文件还报成功。
- **清单落在 `<download_dir>/_meta/<任务ID>/`**, 不与媒体混放 —— 视频平铺在根
  目录, 清单要是也在根目录, 每跑完一个视频任务就会盖掉上一个的清单。
  清单里 `file` 字段相对的是**下载根**(不是清单自己所在的目录)。

`album_title=id` 不只是"不用标题", 而是**完全不开浏览器** —— 选它往往正因
Cloudflare 或浏览器不可用, 此时 `media=auto` 会退回一次 HTTP 探测, 不会漏视频。

⚠️ 同一 gid 下图片与视频**可以同时存在**(实测 xchina `6a3654854fd25` 是
12 张图 + 4 段 mp4, `00001.jpg` 与 `00001.mp4` 并存, 靠扩展名区分)。
`auto` 会两者都采; 只要图片请显式传 `media=image`。

### XChina 视频页(签名 m3u8)

视频不是 `.mp4` 直链, 而是 `https://video.xchina.download/m3u8/{gid}/720.m3u8?expires=...&md5=...`
这种**带签名、有有效期**的 HLS 播放列表; 迅雷/浏览器抓到的就是它。本工具
原生支持, 不用 `<video>` 标签逐段拼、也不依赖外部下载器:

- 粘贴 **视频页 URL** `https://xchina.co/video/id-{gid}.html` 即可: 采集器用
  headless 浏览器过 Cloudflare, 监听播放器的网络请求**捕获**带签名 m3u8
  (比解析 DOM/JS 可靠 —— 播放器要播就一定会请求它)。
- 也可以直接粘贴**迅雷那种带签名 m3u8 直链**。
- 自动识别会把它认成 `xchina_video`; 预览面板显示「1 段视频 + 估算体积」。

⚠️ **最阴险的失败模式 —— 静默下成占位视频**: 签名过期或错误时, 站点**不报错**,
而是返回 200 + 语法完全合法的 m3u8, 指向 `/fallback/placeholder.ts`(一个 603KB
真实可播放 TS)。直接丢给 ffmpeg 会"成功"产出一个坏视频, 任务报 success。
所以下载前强制做**播放列表健全性校验**(`collectors/hls.py`):

```
inspect_playlist(m3u8):
  - 解析 expires: 已过期 -> 明确失败, 提示"凭据过期, 请重新获取"
  - 分片清单: 没有任何分片(指向占位) -> 拒绝
  - EXT-X-KEY(AES-128): 有加密但密钥不可达 -> 拒绝
  - 通过才交给下载器
```

实测 `6aaa517d3f106` 的流: `#EXT-X-KEY:METHOD=AES-128` 加密, 但 `/key/enc.key`
**无需 Referer/签名**, 分片也**无需签名**(只有 playlist 那一层设了防)。
ffmpeg 原生处理拉钥+解密+remux, 端到端实测产出 27.1MB / 5:05 的真视频, 退出码 0
(而非那个 603KB 占位片)。所以**解密走 ffmpeg, 零新增依赖**。

### 聚合页采集(整模特 / 整系列一次下完)

粘贴模特页或索引页, 采集器**先枚举出下面的相册与视频页, 再逐个委派给对应的
子采集器** —— 采集器只负责"发现有哪些内容页", 资源的枚举与下载仍由各站原有
采集器完成, 不重复实现:

```bash
# 模特落地页 / 索引页 / 全量列表页 都行
curl -X POST localhost:8000/tasks/create -H 'Content-Type: application/json' \
  -d '{"url":"https://xchina.co/model/id-601190f157fe7.html","max_items":50}'
```

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `aggregate_depth` | `1` | 向下展开几层; 索引页挂的是落地页, 要拿全量得 ≥2 |
| `max_items` | `50` | **条目总数**上限(相册 + 视频页合计), 先截断再委派 —— 避免为一堆用不到的子页面去开浏览器 |

落盘目录会多套一层聚合层父目录(取页面标题, 拿不到回退 URL 里的 ID):

```
艾玛/阁楼监禁 把美乳艾玛锁在家中阁楼的小房间里/00001.jpg
└─ 模特名(聚合层)  └─ 相册名(子采集器)              └─ 序号
```

三条设计要点:

1. **URL 驱动抽取, 不是 DOM 选择器** —— 只按链接路径形态(`/photo/id-*` 是相册、
   `/video/id-*` 是视频、`/photos/model-*` 是更深的列表)归类。站点改版换 div
   结构不影响; 真改了 URL 形态也会**采到 0 个并报错**, 而不是静默采空。
2. **单个子页面失败不拖垮整任务** —— 60 个相册里坏 1 个是常态, 失败的跳过并
   在日志里点名, 其余照常下载。
3. **截断必须说出来** —— 触顶时预告标 `sampled`(数量只是下限)并给可行动提示;
   到层数上限时写明"还有 N 个更深入口未展开"。悄悄少采最容易被误当成"站点只有这些"。

⚠️ 实测该站的分工是: **落地/索引页开放(纯 HTTP 可读), 全量列表页受 Cloudflare
保护**(headless 能过, 但代价是每次几十秒)。采集器按此自动选择纯 HTTP 或浏览器,
并共用同一套域级熔断。

### 创建前预告(推荐先看一眼)

```bash
curl -X POST localhost:8000/tasks/preview \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://xchina.co/photo/id-6a3654854fd25.html","collector":"xchina_gallery"}'
```

相册页本身**自报**了资源数量与每段视频的体积(`12P + 4V`, `filesize` 实测精确),
所以预告通常**一次页面读取即可, 零序号枚举**:

```
目录名 '约啪172cm车模 完美肉体 主动求操' | 12 图 / 4 视频 / 视频合计 251.0MB
```

页面拿不到(未装浏览器 / Cloudflare 失败 / `album_title=id`)时退回受限枚举,
此时返回 `sampled: true`, 数量只能当**下限**读(界面显示为 `≥`)。

自报数量**只用于预告**, 不当资源清单 —— 页面会改版、可能滞后, 序号枚举才是权威。

预告还会带出 `resource_roots`(本次实际生效的 CDN 基址与序号格式)与 `warning`
(枚举到 0 个时的可行动提示)。前者对齐 `album.json` 的同名字段: 用户在预告里
看到的路径, 就是创建后真正会去枚举的那一条 —— 不再是黑箱。

预告里的 `photos` / `videos` / `video_bytes` **只统计本次实际要采的媒体**,
与 `media` 严格一致: `media=image` 时 `videos` 为 `null`、`video_bytes` 为 0,
不会出现"预告说会采 4 段视频、创建后一段没下"这种口径不一致。
相册页自报的总量另用 `photos_declared` / `videos_declared` 原样带出,
界面据此提示"另有 4 段视频未采", 用户能分清"站点没有"与"我没要"。

体积前置还有一个隐性收益: 图集枚举的 `probe()` 本来就要读 `Content-Length`,
把已拿到的值带进下载层后, 大小过滤(`max_size`)在**图集任务上是零额外请求**的,
只有 size 未知的采集器才回退一次 HEAD。

## 去重与"什么是有效资源"

两层去重, 职责不同, **都不删用户的文件**(除显式判定为广告的那一档):

| 层 | 判据 | 命中后 | 默认 |
|---|---|---|---|
| 字节级 | sha256 完全一致 | 复用已有文件, 不重复传输 | 开 |
| 感知级 | dHash 指纹海明距离 ≤ 4/64 | 写 `duplicate_of`, **文件保留** | 开 |

字节级认不出"换尺寸 / 重新压缩 / 重复收录"的同一张图 —— 而这恰恰是图集站上
最常见的重复形态。感知级用 ffmpeg 把图解成 9x8 灰度再算 64 位 dHash
(`backend/core/phash.py`), **零新增依赖**(复用已装好的 ffmpeg)。

三条硬约束(想改这个功能请先读 `phash.py` 的模块注释):

1. **只标记, 绝不自动删除** —— dHash 会误判(纯色图、连拍), 删文件不可逆,
   出错的代价由用户承担。标记同时写进 `manifest.json`:
   `jq '.resources[] | select(.duplicate_of) | .file'`
2. **失败即放行** —— ffmpeg 不在 / 解码失败一律当"没算出指纹", 绝不让下载失败
3. **不动 sha256 那条路径** —— 两条并行的独立判据

关掉它: `filters.dedup_perceptual=false`(界面上的「重复检测」开关, 默认开)。
唯一的理由是省掉每张图一次本地解码; 阈值可调 `filters.dedup_threshold`(默认 4)。

体积/尺寸这些**过滤**判据见 `backend/core/filters.py::match_resource`:
① URL 层(类型/扩展名/关键词/**广告位按路径分段精确匹配**) ② 体积层
③ 内容层(下载后复核 Content-Type) ④ 像素层(下载后读文件头, 只作用于 image)。
探测失败一律放行 —— 宁可漏判, 不误杀。

## 站点声明自检与 CDN 画像

图集站点的一切都写在一份**声明**里(`collectors/<站点>/gallery.py`), 两套机制
保证这份声明不会悄悄失真:

```bash
python scripts/selfcheck.py              # 声明自检 + CDN 画像快照
python scripts/selfcheck.py --reset-profile
```

**声明自检** (`check_site`): 用 `id_samples` 把每种输入形态钉成断言 ——
每条样本必须解析出同一个 gid, 且不得有某条 `id_patterns` **单独**就能配出
不同结果(那是"谁先谁赢"的隐式依赖, 改一次顺序行为就变)。`parse_gid` 取的是
首个命中, 新加一条正则时若不慎也能匹配旧 URL, 行为就**静默**变了 —— 某个相册
突然采空, 而日志里一切正常。测试里 `selfcheck_all()` 对全部已注册站点断言为空。

**CDN 画像** (`backend/core/cdn_profile.py` → `data/cdn_profile.json`): 记住每条
基址的命中次数与序号格式, 用来给候选探测排序。实测价值:

```
冷启动  6 次探测  photos/photos2..5 × {seq:05d} 全灭, 才试到 photos2+{seq:04d}
有画像  1 次探测  photos2+{seq:04d} 直接命中
```

画像只是**排序提示**: 全部候选组合仍然真探一遍, 画像错了最多多花一次探测,
绝不会让本来能采的相册采不到。没有它也能工作 —— 缺失/损坏/写不进去一律退回
站点的固定顺序。另一层用处是排查: 某条基址的占比**突然**从主跌到 0, 基本就是
站点换了 CDN 子路径, 这比用户报"某天开始全失败"要早得多。

`UWC_CDN_PROFILE` 可覆盖画像文件位置, 或设成 `off` 关掉。

**手选采集器的形状软提示**: `gid_shape` 在**自动识别**时是硬判据(形状不符直接
不认领 —— 认错是静默的); 在**手选**时只给一条 `warning`(不做 400)。理由是不
对称: 手选是用户已表过的态, 站点可能刚换 ID 格式而我们比用户知道得晚。提示会
出现在创建响应与预告面板里, **任务照常创建**。

## 跨任务资源库 / 实时速率 / 代理熔断(V32)

### 跨任务资源库

任务列表回答"我下过什么", 资源库回答"**我现在手上有什么**" —— 同一个文件可能被多个
任务引用(增量续采、同一图集重复创建), 资源库按**内容**聚合, 不按任务。

- 入口: 顶部「任务列表 / 资源库」切换(`App.vue` 的 `view`, 记忆在 `localStorage`)。
- 筛选: `q` 搜索(文件名/路径)、`kind`(图片/文本)、`album`(相册名)、`task_id`。
  **默认只列 `status="done"`** —— 资源库是"手上有什么", 不是"所有见过的 URL"。
- 相册名是**精确匹配**而非 `LIKE`: 找 `ABP-123` 不该命中 `ABP-1234`。
- 每条资源带 `refs`(被多少个任务指向), 卡片上以「共用 ×N」角标显示。
- `library_filters()` 把筛选条件抽成单一函数, `library_count` 与 `library_list`
  共用同一份 WHERE —— 分页错位几乎都源于两条查询条件不一致。
- 删除语义**未变**: `task_manager._purge_files` 早已用
  `db.count_place_refs(rel, local, exclude_task=...)` 做引用计数 —— 删任务连文件删时,
  只要还有别的任务指向该文件就跳过。所以资源库不需要新的删除规则。

### 字节级实时速率曲线

后端 `GET /events` 新增 `task.bytes` 事件, 前端据此画真实 sparkline。

- 下载层 `progress_cb(nbytes)` 传**本 chunk 字节数**, 用 `try/except TypeError`
  兼容无参回调(老测试替身写的是 `def cb(): ...`)。
- **推送按 0.4 秒节流**(`PROGRESS_PUSH_INTERVAL`), 否则每个 256KB chunk 都广播一次
  会把 SSE 淹掉。
- `task.bytes` 事件**只走内存不长库** —— 高频写 SQLite 会撞上单写者瓶颈。
- 前端 `byterate.js` 用**差分**(本次累计 − 上次累计)/dt 得瞬时速率;
  `dt < 0.05s` 的样本忽略但**仍然更新累计值**, 否则下一次差分会把两段量算到一起。
- 曲线按**相对峰值**归一化: 图片 2MB 与视频 200MB 差两个数量级, 按绝对值画
  图片曲线永远贴地。

### 代理熔断

见上文「任务级代理」段。核心是: 连续失败 3 次熔断 300 秒, 到期半开放出,
全池熔断时退化为按序号返回(而非返回 `None` 让任务停摆)。
`GET /tasks/{id}/proxy` 可实时查看池状态。

## 失败分类 / 内容终检 / 坏文件标记(V33)

这一版修的是三个**"跑通了但结果是错的"**的缺陷, 不是加功能。

### 失败分类: 4xx 不再拖慢整个站点

下载层原来的重试循环里只有一句 `isinstance(e, requests.RequestException) → note_failure(url)`。
而 `raise_for_status()` 抛的 `HTTPError` **正是** `RequestException` 的子类 —— 于是:

- **一张已被删除的图(404)会让该域名后续所有请求的间隔翻倍。** `note_failure` 是
  "这个站点吃不消了"的信号(驱动 AIMD), 而 404 是**这一个 URL** 的问题。症状是
  "加了几张失效图之后全站都变慢", 没有任何报错。
- **404 是永久失败, 却要走满 3 次指数退避** —— 每张约 20 秒空转。

现在按状态码分流(`downloaders/base.py` 的 `PERMANENT_STATUS`):

| 状态 | 处理 |
| --- | --- |
| 408 / 429 | 照旧重试; 429 走站点级冷却(它**是**在说"慢一点") |
| 5xx | 照旧重试 + `note_failure`(站点自己出错, 惩罚是对的) |
| 400/401/402/403/404/405/406/410/451 | `GoneError` → **不重试、不退避、不记站点失败** |

资源状态新增 `gone`(源站已无), 与 `failed` 分开:

- `submit_resource` / `/tasks/{id}/retry-failed` **仍然允许**手动重试 `gone` ——
  自动流程克制(重试是空转), 用户手动点则放行(403 可能换了代理就好了)。自动的
  克制与手动的自由不矛盾。
- 任务终态把 `gone` **计入失败**(要 56 张拿到 0 张, 不该显示成绿灯)。

### 内容终检: 长度对得上 ≠ 内容可用

`Content-Length` 校验只能证明字节数没少。能通过它却仍是坏文件的三种真实形态:
中间设备截断响应体但保留正确长度、CDN 回了长度正确的错误页、源站上的文件本身就是
转码失败的半成品。这三种都会以"下载成功"落盘, 之后 sha256 去重 / manifest / 缩略图
全建在坏字节上。

- **`core/mediacheck.py`(新)**: 只读头尾各 32 字节的**算术级**判据, 零子进程,
  开销与文件大小无关。RIFF/BMP 的长度字段必须与文件大小**恰好相等**; ISOBMFF
  (mp4/avif) 的 box 链必须能走通且不越出文件末尾; JPEG 必须有 EOI、PNG 必须有
  IEND、GIF 必须有 `0x3B`。
- **视频直链**补上 ffprobe 时长校验(`_validate_direct_media`)。HLS 路径早就有,
  偏偏直链这条绕过 ffmpeg 自己写字节的路没有。mp4 的 `moov` 通常在尾部, 被截断的
  文件连元数据都读不到 → ffprobe 直接失败。
- **判据只取"能被证明"的部分。** 认不出的容器一律返回 `None` 放行; BMP 长度字段为 0
  视作"未填写"而非"截断"; JPEG 的 EOI 是在尾部 64 字节**里找**而非要求正好在最后两位
  —— 误报的代价是删掉一个好文件。

### 坏文件信号不再被丢弃

`core/phash.py` 用 ffmpeg 解码算指纹, 原来失败就返回 `None`, 于是
**ffmpeg 在、却解不开这个文件**这条最强证据(比长度校验强)唯一的后果是"这次没算指纹"。

`decode_gray_ex()` / `dhash_ex()` 现在把原因分成两个码:

- `NO_DECODER` —— 本机没有 ffmpeg(或环境异常), **与文件无关**, 放行;
- `DECODE_FAILED` —— ffmpeg 解不开, **文件可疑**。

合并这两者是原实现的根本问题: 要么在缺 ffmpeg 的机器上把所有文件都判成坏的, 要么把
真损坏的文件当成"环境问题"放过去。`DECODE_FAILED` 写进 `resources.error_kind='corrupt'`,
**仍只标记不删除**(与 dHash 同一条约束: 解码器也会认错冷门格式, 而删文件不可逆),
详情页以「可能已损坏」标出。

### `error_kind`: 拿数据当数据用

界面原来靠**正则解析 note 文案**(`/403|forbidden/`、`/timeout/`)来归类失败原因 ——
那是拿人看的字当数据用: 措辞一改归类就静默失效, 换个语言全落进"其他", 也没法支持
"按原因筛选/批量重试"。

`resources` 新增 `error_kind` 列, 取值固定:

| kind | 含义 | 该做什么 |
| --- | --- | --- |
| `gone` | 404/410 源站已无 | 不用重试 |
| `forbidden` | 401/403/451 拒绝访问 | 换代理 / 改 Referer |
| `corrupt` | 字节完整但内容不可用 | 人工确认(文件已保留) |
| `disk` | 磁盘满 / 不可写 | 清理空间 |
| `ratelimit` | 429 | 降速或换线 |
| `server` | 5xx | 稍后重试 |
| `network` | 连接/超时/TLS | 换代理 |
| `unknown` | 分类之外 | 多半是我们自己的 bug |

- `db.error_kinds()` 按 kind 聚合(与 `failure_reasons()` 按 note **全文**分组互补 ——
  后者每条 note 都带各自的 URL, 真实数据上几乎每组只有 1 条)。
- 聚合**不按 status 过滤**: `corrupt` 会落在 `done` 上(文件保留只标记), 按 status
  过滤恰好会漏掉最该被看见的那一类。
- 中文标签由后端下发(`error_kind_labels`, 来源 `core/errors.KIND_LABELS`), 前端只用
  不另写一套 —— 两处各写一套的措辞迟早对不上, 而失败措辞正是用户判断"要不要重试"的依据。
- 旧任务没有这一列, 前端用 note 关键词兜底, 免得历史任务的分布图突然变成空白。

### 附带修掉: CDN 画像的命中被静默丢弃

`core/cdn_profile.py` 记的是"哪条基址真的命中过", 用来给候选探测排序。它的读者
(`preferred_bases` / `summarize`)原本**不走锁**, 而写者靠 `tmp.replace(p)` 换文件 ——
在 Windows 上只要目标文件还有别的句柄开着(哪怕只是只读), 替换就会以 `PermissionError`
失败, 而它落在 `except OSError: pass` 里被吞掉。实测「一个只读线程 + 一个写线程」并发:

```
写入 300 次, 画像记到 150 次  ->  丢 150 次
```

也就是并发采集时**命中记录丢一半**, 且毫无声响 —— 与上面三条同一类"静默失败", 只是
这次丢的不是资源而是线索。修法两层:

- 锁换成 `RLock` 且**读者也进临界区**: 进程内的占用彻底消除(必须可重入 ——
  `record_hit` 要在同一次"读-改-写"里同时调 `_read` 与 `_write`);
- `tmp.replace` 加**重试**(5 次线性退避): 编辑器/杀毒软件这类**外部**占用是锁挡不住
  的, 而窗口是微秒级。

契约未变 —— 画像只是线索, 写不进去仍然不该让采集失败。区别是"写不进去"从常态变回了
罕见。它同时也正是本仓库测试套件偶发变红的原因, 所以这一版顺手收掉。

## 测试 / 部署

```bash
make test           # pytest (819 用例: 含 hls 校验 / 视频采集器 / 自动识别 / CDN 探测 / 有效资源 / 聚合页 / 感知去重 / 声明自检 / 令牌桶与 AIMD / 枚举快路径 / 健壮性与取消门禁 / 测试隔离守卫 / 产物-终态次序 / 批量操作与通知 / 代理池与熔断 / 统计增强 / 跨任务资源库 / 字节速率 / 失败分类与内容终检 / 画像并发)
make docker         # docker compose 构建并启动
python scripts/verify_output.py   # 端到端: 命名/manifest/打包/增量/订阅/停止 (40 项断言)
python scripts/verify_hls.py      # 真实 HLS 双引擎验证 (18 项断言)
python scripts/preview_probe.py <相册页URL或图集ID>   # 真实站点创建前预告
python scripts/selfcheck.py       # 站点声明自检 + CDN 画像快照
```

配置: `config.yaml`, 环境变量 `UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` /
`UWC_BROWSER_STATE_DIR` / `UWC_PROXY` / `UWC_CDN_PROFILE` / `UWC_FFMPEG` /
`PEXELS_API_KEY`(仅 Pexels 关键词/集合采集需要) 优先。
站点解析探针: `uv run python scripts/probe.py <url>`。
感知去重依赖 ffmpeg(与视频 remux 共用同一套探测, 见 `core/ffmpeg.py`) ——
探测不到时自动降级为"不算指纹", 不影响任何下载。

### 任务通知(可选)

任务结算(`success` / `partial` / `failed`)时会写一条通知, 界面右上角铃铛可查看。
`cancelled` 不发 —— 那是用户自己点的停止, 推一条"已取消"只是噪声。

如需邮件推送, 额外配置以下环境变量(缺 `UWC_SMTP_HOST` 或 `UWC_SMTP_TO` 时不发送):

| 变量 | 说明 |
| --- | --- |
| `UWC_SMTP_HOST` | SMTP 服务器地址 |
| `UWC_SMTP_TO` | 收件人 |
| `UWC_SMTP_PORT` | 端口, 默认 `465` |
| `UWC_SMTP_USER` / `UWC_SMTP_PASS` | 账号与密码(可选) |
| `UWC_SMTP_TLS` | 设为 `0`/`false`/`no` 关闭 TLS(默认开) |

邮件发送失败只记日志, 不影响任务本身 —— 通知系统不该因为邮件服务器抖动向用户报错。

## 键盘快捷键

| 按键 | 作用 |
| --- | --- |
| `/` | 聚焦任务搜索框 |
| `j` / `k`(或上下方向键) | 上下移动高亮任务 |
| `Enter` | 打开高亮任务的详情 |
| `Space` | 暂停 / 继续高亮任务 |
| `Esc` | 关闭任务详情抽屉 |
| 灯箱内 `+` / `-` / `0` / `R` | 放大、缩小、复位、旋转 |

## 健壮性(V27)

这一轮的目标不是"再修几个坑", 而是**让坑不再靠人记才不踩**:

- **孤儿任务恢复**: 后端重启后心跳超时的活动任务会被复位(有资源清单 → `paused`
  可续跑; 无清单 → `failed` 并提示重试), 不再永远卡在"运行中"
- **SQLite 写重试**: `busy_timeout` + 只对 `locked/busy` 的退避重试, 避免写冲突
  被误判成"这个资源下载失败"
- **原子落盘**: 写 `.part` 后原子改名; 续传前校验残片确实来自同一 URL; 落盘后
  比对字节数 —— 杜绝"文件在、大小对、内容是坏的"
- **磁盘满 fail-fast**: 预检 + 下载中捕获即中止整任务, 不走完重试链空转
- **取消穿透门禁**: `tests/test_cancel_guard.py` 用 AST 扫核心模块, 凡 `try` 块内
  有取消源却没写 `except TaskCancelled: raise`, **pytest 直接红**
- **异常分类**: `core/errors.py` 区分"站点/环境的问题(给用户看)"、"可重试"、
  "我们的 bug(记堆栈)"; API 全局兜底只返回 `{detail, type}`, 堆栈不泄漏

## 架构

URL → Browser(4解析器: API>Network>JS>DOM) → Resource → Downloader(并发/重试/断点/hash去重) → Storage

- 任务状态机: pending→running→extracting→downloading→success / partial / failed→retry / cancelled
  - `partial` = 有成功也有失败资源(全成功才是 success); 全部失败为 failed
- 采集器插件化: `@register("name")` 注册
  - `generic` 任意站点(浏览器四解析器)
  - `xchina` XChina 页面(浏览器抓取)
  - `xchina_gallery` XChina 相册: 纯 HTTP 序号枚举, 无需浏览器。以下输入**都**可用:
    - 相册页 URL `https://xchina.co/photo/id-6aa5136f606fe/10.html`
    - 图片直链 `https://img.xchina.io/photos/6aa5136f606fe/00001.jpg`
    - 图集 ID 本身 `6aa5136f606fe`
    - 支持画质档 `quality`: original / 1200 / 800 / 600
    - 支持媒体类型 `media`: 同一 gid 下图片与视频并存(见上文)
    - 支持创建前预告 `POST /tasks/preview`: 零枚举给出目录名/张数/视频体积
    - **CDN 基址与序号宽度自动探测**: 同一站点可能把不同相册分到
      `photos` / `photos2` / `photos3`, 序号也可能是 4 位(`0001.jpg`)而非 5 位。
      写死基址会让"相册在另一个 CDN 子路径"变成 0 资源 -> failed, 而用户只看
      到失败、看不出是路径不对。三条线索按可信度依次采信:
      ① **相册页 HTML 里引用的图片地址**(页面白给的真实前缀, 零探测)
      ② 用户输入的资源直链(基址与宽度都写在 URL 里)
      ③ 候选基址 × 相邻序号宽度逐个试探 —— 默认基址先用, **不中才探**,
      所以正常相册零额外请求
    - 候选不写死清单: 站点声明 `base_candidate_digits=5` 即自动展开
      `photos2..photos5`(手写三个的话, 下次出现 `photos4` 就整批判空);
      换 host 的迁移另用 `base_host_templates` 显式声明(数字后缀表达不了)
    - 序号宽度试**相邻 ±2**(站点写 5 位而相册实际 3 位时, ±1 恰好漏掉)
    - 探测顺序由 **CDN 画像**排序(实际命中过的基址/宽度优先), 见下节
    - `gid_shape` 声明 ID 形状(如 `[0-9a-f]{8,}`), 自动识别时用它当
      "这真的是本站 ID"的判据 —— `/photos/featured/0001.jpg` 这类路径词因此
      不会被误认领。手选采集器时只给软提示, 不做校验
    - ⚠️ 一个资源根**只能有一个出处**(`_base_candidates`): 探测与自动识别共用,
      各写一份会出现"采集能探到、识别不认领"的半通状态
    - 任务级 `proxy` 选项覆盖全局代理
    - 序号枚举型站点可继承 `collectors/gallery_base.py::SequenceGallerySpider`,
      站点只声明 URL 模板、ID 正则与探测规则; 媒体类型由 `MediaType` 声明
      (自带 `ctype_prefix`: 图片看 `image/`、视频看 `video/`)
    - ⚠️ ID 解析不出来时**直接报错**, 绝不猜: 猜错会去枚举一个不存在的图集,
      表现为"任务成功但 0 个资源"(曾把相册页 URL 里的页码 `10` 当成 ID)
  - `xchina_video` XChina 视频页: headless 浏览器过 Cloudflare, 监听网络请求捕获
    带签名 m3u8(无需解析 DOM/JS); 也认迅雷那样的 m3u8 直链。下载前强制做播放列表
    健全性校验(过期/占位/无密钥一律拒绝), AES-128 解密走 ffmpeg
    (零新增依赖; 密钥与分片均无需签名, 只有 playlist 那一层设防)
  - `xchina_aggregate` XChina 聚合页(模特/演员/系列/分类索引): 一次把**整个模特**
    或**整个系列**的相册与视频页收进队列, 再逐个**委派**给 `xchina_gallery` /
    `xchina_video`。可识别的入口(2026-09 实测):
    - `/model/id-{id}.html`、`/actor/id-{id}.html` 落地页(纯 HTTP 可读)
    - `/models.html`、`/models/type-{n}.html` 索引页
    - `/photos/series-{id}.html`、`/videos/series-{id}.html`
    - `/videos/model-{id}.html`、`/photos/model-{id}.html` 全量列表页
    - ⚠️ **URL 驱动抽取, 不是 DOM 选择器**: 靠 URL 模式认内容链接, 站点改版
      (class 改名)不会让采集器静默采到 0 个
    - `aggregate_depth` 控制展开层数(默认 1), `max_items` 是**合计**上限;
      被截断时日志与 `album.json` 都会明说"还有 N 个未展开", 绝不悄悄少采
    - 落地页多为**纯 HTTP 可达**, 更深的系列/全量列表页受 Cloudflare 保护,
      复用 `album_meta.load_page_html` 的"纯 HTTP 优先 + 失败降级 headless"
      与整套域级熔断/登录态隔离
    - ⚠️ 只归内容页: 相册的 `/10.html` 分页是**同一相册的不同页**, 归一化到
      主页; 列表页的分页是**不同内容**, 不做归一化(归了会漏采)
- 资源类型: image / video / audio / doc / text, 对应下载器
- 资源过滤("什么是有效资源"的唯一定义在 `core/filters.py::match_resource`):
  类型/扩展名黑白名单/URL关键词/大小区间/图片体积下限/广告位识别,
  命中者标记 filtered 并记原因。
  - 大小过滤优先复用**采集阶段已探测到的** size(图集任务因此零额外请求), 未知才补 HEAD
  - `min_image_bytes`: 挡 1x1 跟踪像素这类体积极小的图片
  - `min_width` / `max_width` / `min_height` / `max_height`: 按**图片真实像素**
    过滤, 专治横幅广告(728x90 之类)。图片可下载后读文件头拿尺寸再删,
    不会误判; 视频与文档不参与(尺寸规则只作用于 image)
  - `exclude_ad`: 按**路径分段精确匹配**识别广告位/站点装饰图(`/ad/`、`banner_*.jpg`
    `/assets/logo.png`…)。绝不做子串包含 —— 子串会把 `downloads/badges/1.jpg`
    这种正常文件误杀
  - ⚠️ 尺寸/去重复用文件时**绝不删别人的文件**: 命中去重说明别处已有同一份内容,
    按本任务规则删掉它等于破坏别的任务的产出
- 429 退避: 站点说"慢一点"时按它给的 `Retry-After` 等(支持秒与 HTTP 日期),
  而不是套用普通指数退避 —— 普通退避最长 8 秒, 站点要求冷静几十秒时硬闯只会
  让封禁更久。普通失败则是指数退避 × 0.6~1.4 抖动, 避免重试风暴
- 输出组织: 任务级命名模板(默认保留 URL 原名), 落 `<download_dir>/相册名/` 下的
  任意层级(**视频平铺在下载根目录**, 见上文「落盘目录结构」); 任务结束(含
  取消/部分失败)都会产出 `manifest.json`, 记录来源 URL、实际生效的下载点、
  Content-Type、sha256、字节数、本地相对路径与过滤原因
- 相册元数据 sidecar: 图集任务在**下载开始前**落 `_meta/<task_id>/album.json`, 记录
  相册名/标签/厂牌/自报张数与 `resource_roots`(本次实际生效的 CDN 基址、序号格式、
  来源是"页面线索/输入直链/探测")。它回答的是"为什么这个相册采不到/sidecar 里
  写的是哪条路径"—— 写死基址的旧版在这里静默采到 0 个, 用户拿不到任何线索
- 采集到 0 个资源时抛的是**可操作错误**(列出试过的基址 + 下一步动作),
  而不是返回空列表让上层记一句"采集到 0 个资源"
- 增量续采: `incremental: true` 时按 URL 复用历史已下载资源, 重跑只补新增与失败;
  订阅源(watches)在此基础上按间隔自动巡检并创建任务
- 导出: `/tasks/{id}/archive` 流式打包 ZIP(不把整包攒进内存), 前端有图库视图
  与状态筛选(成功/过滤/失败)、清单表格
- 任务名: 提取阶段从首个资源推断相册名/视频标题回写 `tasks.name`,
  列表与详情页优先显示它(不用在一排 URL 里认任务)
- 暂停/继续: `POST /tasks/{id}/pause` 停下 worker 但**保留文件与资源记录**并落
  `paused`; `/resume` 把被打断的资源复位 pending 后从断点补下, 不重新采集、
  不重下已 done 的文件 —— 这是它相对"取消+重跑"的核心价值
- 停止: `POST /tasks/{id}/cancel` 立刻落 cancelled 状态, 下载循环在每个数据块
  检查取消标志(不是资源边界), 半成品即时清理, 已下载文件保留
- 看门狗: 心跳**同时写库**(`tasks.hb`)。只靠进程内字典判断时, 两个实例共用
  一个 SQLite 库会互相把对方正在跑的任务判成"重启遗留"并标 failed
  (实测: 一个正在枚举 114 张图的任务就这么被杀掉了)
- 删除: **记录与文件分开决定**。默认 `DELETE /tasks/{id}` 只删记录, 磁盘文件
  保留(去重机制下别的任务可能正引用它们); 要连文件删需显式 `?with_files=true`,
  此时**按库里的记录逐条删**(媒体文件散在下载根里, 与别的任务共用同一个根)并做
  越界校验 —— 别的任务还在引用的文件会跳过, `_meta/<任务ID>/` 整棵清掉。
  `POST /tasks/bulk-delete` 一键清理已结束任务(按状态筛选, 运行中的不动)
- 空结果即失败: 采集器一个资源都没发现时任务判 `failed` 并给出原因,
  不再出现"任务成功但什么都没下到"
- 登录态: `POST /sessions/login` 起 headful 浏览器手动登录, 定期快照
  storage_state, 落 `browser_state/{domain}.json`
  - ⚠️ 带着**失效**的 `cf_clearance` 访问会得到 `Attention Required!`(永久拒绝,
    不会自愈)。一旦在使用登录态时被这样拒过, 该域登录态会被标记失效并停用,
    `GET /sessions` 的 `cf_stale` 字段会列出了来说明"请重新登录"(详见下节)
- Cloudflare 长期对策(见 `collectors/album_meta.py` 模块文档):
  - **不投入指纹对抗** —— 军备竞赛打不赢, 真正的对策是让采集**不依赖**那个
    HTML 页: 资源发现永远走纯 HTTP 序号枚举, 相册页只提供目录名/自报数量/
    视频体积这些锦上添花的信息, 拿不到就降级用图集 ID 命名
  - **域级熔断**: 连续 3 次读不到(被拦)就在 10 分钟内**不再开 Chromium**,
    直接降级并说明原因 —— 每次读页都要开一个 headless 浏览器(几十秒),
    明知道会被拦还去开纯粹是浪费, 也更像扫描器
  - **降级原因可见**: 所有降级路径都往日志写人话(还要回答"接下来会怎样")
  - 被拦 ≠ 登录态失效: 只有**带着登录态**被拒才把账记到登录态头上
- 输出目录: 支持任务级自定义目录, 文件服务按任务目录做越权校验
- 下载限速: 站点级**令牌桶**(突发容量 `domain_burst`, 长程平均速率不变)+ 并发闸门
  + 请求间隔(支持随机区间, 如 3~10 秒模拟人工节奏); AIMD 自适应: 连续成功缓慢收紧
  间隔, 429/非 2xx 立即翻倍放宽(`adaptive_throttle`)。支持 HTTP/SOCKS5 代理
  - ⚠️ **吞吐由间隔决定, 不由并发决定**。并发只管"同时在飞几个", 调大它不提速
- 枚举快路径: 页面给了数量时用**抽样校验过的区间**替代逐张探测
  (`enumeration_fast`)。真站实测 114 张: 探测 **117 → 6 次**, 耗时 **61.7s → 2.6s**,
  资源数与逐张扫描完全一致。抽样不中即退回逐张扫描
- m3u8 视频双引擎 (`video_engine`): auto 优先 ffmpeg 拉流、失败降级内置器 /
  ffmpeg 强制且不降级 / builtin 强制内置分片器
  - ffmpeg: 一步 `-c copy` 输出 .mp4(也能处理 AES-128 加密流)
  - builtin: 分片独立快间隔 + 并发下片 + 分片级重试 + 断点续传(只补缺失分片),
    单片失败不废整个视频; 产物 .ts, ffmpeg 可用时自动 remux 成 .mp4
- ffmpeg 免配置: 按「显式配置 -> PATH -> 常见安装位置」自动探测,
  绕开「进程 PATH 是启动快照」问题(服务运行期间新装也能识别)
- 备用下载点: 采集器可为资源附带 mirrors, 主 URL 失败自动切换, 落盘后缀随实际内容变化
- 健壮性: 看门狗处理僵死任务, 任务取消/删除, 资源级重试
- 数据库: SQLite(WAL), tasks/resources/task_logs/watches
- 浏览器状态: browser_state/{domain}.json 持久化登录态
