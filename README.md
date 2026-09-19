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
| POST | /tasks/preview | **创建前预告**: 只发现不下载不写库, 返回目录名/张数/视频体积/标签 |
| GET | /collectors | 已注册采集器列表 |
| GET | /collectors/resolve | **URL -> 采集器**(纯字符串判定, 不打网络请求), 用于"已识别为 X"回显 |
| GET | /tasks | 任务列表 |
| GET | /tasks/storage | 任务/产出占用概览, 供"清理"界面预检 |
| GET | /tasks/{id} | 任务详情 + 资源 |
| GET | /tasks/{id}/logs | 任务日志 |
| POST | /tasks/{id}/resources/{rid}/retry | 资源级重试(强制下载, 跳过过滤规则) |
| POST | /tasks/{id}/cancel | 停止任务: 保留已下载文件, 未完成资源置 skipped |
| DELETE | /tasks/{id}?with_files=true | 删除任务。**默认只删记录, 磁盘文件保留**; `with_files=true` 连任务目录一起删 |
| POST | /tasks/bulk-delete | 批量清理 `{"statuses":[...], "with_files":false}`; 运行中的任务不会被删 |
| POST | /tasks/{id}/archive | 打包导出该任务的产出(ZIP 流式, 可按状态过滤) |
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
| GET | /events | SSE 实时推送(task.updated/task.log/resource.updated) |
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
文件始终落在该目录下的 `<task_id>/` 子目录。被过滤的资源标记
`filtered` 并把原因写入 `note`, 可在详情页"强制下载"单独补下。

### 采集器自动识别

`collector` 缺省就是 `"auto"`: 后端按 URL 形态挑一个采集器, **不需要用户
记住"这个链接该用哪个采集器"**。每个采集器用类方法 `match_score(url)` 声明
自己能处理什么(返回 `None` = 不认领), 分数越高越优先, 通用采集器恒定垫底。

| 输入 | 识别结果 |
| --- | --- |
| `https://xchina.co/photo/id-6aa513208a506.html` | `xchina_gallery` (分数 100, 相册页) |
| `https://img.xchina.io/photos/6aa513208a506/00001.jpg` | `xchina_gallery` (分数 50, 资源直链) |
| `6aa513208a506`(纯图集 ID) | `xchina_gallery` (分数 10) |
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
| `album_tags_dir` | `true` / `false` | 在相册名外再套一层站点标签目录(`丝袜-情趣内衣/相册名/...`, 取前 3 个标签) |

`album_title=id` 不只是"不用标题", 而是**完全不开浏览器** —— 选它往往正因
Cloudflare 或浏览器不可用, 此时 `media=auto` 会退回一次 HTTP 探测, 不会漏视频。

⚠️ 同一 gid 下图片与视频**可以同时存在**(实测 xchina `6a3654854fd25` 是
12 张图 + 4 段 mp4, `00001.jpg` 与 `00001.mp4` 并存, 靠扩展名区分)。
`auto` 会两者都采; 只要图片请显式传 `media=image`。

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

预告里的 `photos` / `videos` / `video_bytes` **只统计本次实际要采的媒体**,
与 `media` 严格一致: `media=image` 时 `videos` 为 `null`、`video_bytes` 为 0,
不会出现"预告说会采 4 段视频、创建后一段没下"这种口径不一致。
相册页自报的总量另用 `photos_declared` / `videos_declared` 原样带出,
界面据此提示"另有 4 段视频未采", 用户能分清"站点没有"与"我没要"。

体积前置还有一个隐性收益: 图集枚举的 `probe()` 本来就要读 `Content-Length`,
把已拿到的值带进下载层后, 大小过滤(`max_size`)在**图集任务上是零额外请求**的,
只有 size 未知的采集器才回退一次 HEAD。

## 测试 / 部署

```bash
make test           # pytest (245 用例)
make docker         # docker compose 构建并启动
python scripts/verify_output.py   # 端到端: 命名/manifest/打包/增量/订阅/停止 (33 项断言)
python scripts/verify_hls.py      # 真实 HLS 双引擎验证 (18 项断言)
python scripts/preview_probe.py <相册页URL或图集ID>   # 真实站点创建前预告
```

配置: `config.yaml`, 环境变量 `UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` /
`UWC_BROWSER_STATE_DIR` / `UWC_PROXY` 优先。
站点解析探针: `uv run python scripts/probe.py <url>`。

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
    - 序号枚举型站点可继承 `collectors/gallery_base.py::SequenceGallerySpider`,
      站点只声明 URL 模板、ID 正则与探测规则; 媒体类型由 `MediaType` 声明
      (自带 `ctype_prefix`: 图片看 `image/`、视频看 `video/`)
    - ⚠️ ID 解析不出来时**直接报错**, 绝不猜: 猜错会去枚举一个不存在的图集,
      表现为"任务成功但 0 个资源"(曾把相册页 URL 里的页码 `10` 当成 ID)
- 资源类型: image / video / audio / doc / text, 对应下载器
- 资源过滤: 类型/扩展名黑白名单/URL关键词/大小区间, 命中者标记 filtered 并记原因。
  大小过滤优先复用**采集阶段已探测到的** size(图集任务因此零额外请求), 未知才补 HEAD
- 输出组织: 任务级命名模板(默认保留 URL 原名), 落 `<download_dir>/<task_id>/` 下的
  任意层级; 任务结束(含取消/部分失败)都会产出 `manifest.json`, 记录来源 URL、
  实际生效的下载点、Content-Type、sha256、字节数、本地相对路径与过滤原因
- 增量续采: `incremental: true` 时按 URL 复用历史已下载资源, 重跑只补新增与失败;
  订阅源(watches)在此基础上按间隔自动巡检并创建任务
- 导出: `/tasks/{id}/archive` 流式打包 ZIP(不把整包攒进内存), 前端有图库视图
  与状态筛选(成功/过滤/失败)、清单表格
- 停止: `POST /tasks/{id}/cancel` 立刻落 cancelled 状态, 下载循环在每个数据块
  检查取消标志(不是资源边界), 半成品即时清理, 已下载文件保留
- 删除: **记录与文件分开决定**。默认 `DELETE /tasks/{id}` 只删记录, 磁盘文件
  保留(去重机制下别的任务可能正引用它们); 要连文件删需显式
  `?with_files=true`, 只会删 `<下载根目录>/<task_id>/` 这一层并做双重越界校验。
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
- 下载限速: 站点级并发 + 请求间隔(支持随机区间, 如 3~10 秒模拟人工节奏);
  支持 HTTP/SOCKS5 代理。并发与间隔是两个**正交**闸门, 互不影响
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
