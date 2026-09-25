# Universal Web Collector — 长期笔记（索引）

栈：uv + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
**正文以 `README.md` / `docs/AGENT_DEVELOPMENT_GUIDE.md`（§20–§22 最新）为准；坑的完整清单见 `PITFALLS.md`。**
只做三件事：**索引 + 本机契约 + 一眼能认出的坑名**；命令与环境变量**都在 README**。
> 维护：**新坑先写 `PITFALLS.md`，这里只加一行索引**（上限约 16.5KB，已溢两次）。

## 铁律 / 落盘
`URL → Browser/HTTP → Extractor → Resource → Downloader → Storage`；站点逻辑不进下载器。
布局（唯一入口 `core/layout.py`）：`downloads/相册名/图片`；**视频平铺到 downloads 根**（网站原名，m3u8 用 `gid.mp4`）。
重名：异源→`name(2).mp4`；相册视频撞名→`0001_相册名.mp4`；同源复用不重下。清单在 `downloads/_meta/任务ID/`。
⚠️ 采集器的 `album_tags_dir`（标签层）已废弃，别引回来（留着就是"按了没反应"）。
本地相册集的缩略图落在 `<库同级>/local_albums/_meta/thumb/`，**不在用户登记的根目录里**（只读承诺）。

## 关键文件
`api/tasks.py`(HTTP+SSE) · `core/task_manager.py`(状态机/看门狗/下载顺序) · `core/filters.py`(`match_resource` = "有效资源"唯一定义) · `core/errors.py`(`classify`/`KIND_*`) · `core/layout.py` · `core/disk.py` · `core/manifest.py`(manifest/album 两级 sidecar)
`core/database.py`：加列**必须同时**进 SCHEMA 与 `_ADD_COLUMNS`；⚠️ **新表时刻列 = `created_at` + REAL(epoch 秒)**，老表是 `created_time` 字符串
`core/phash.py`(只标记不删) · `core/mediacheck.py`(完整性**算术**判据) · **`core/filekind.py`(扩展名 vs 文件头；宁可漏报不可误报)** · `core/thumbs.py`(按 **sha**) · **`core/colors.py`(主色→色系, 走 ffmpeg, 不引 Pillow)** · `core/exporter.py`(打包导出+manifest) · `core/webhooks.py`(投递+HMAC, 零新依赖) · `downloaders/dash.py`(MPD 解析，纯函数) · `downloaders/video.py`(**HEAD 是 CRLF**) · `downloaders/ratelimit.py`(请求桶 + **全局字节桶**)
**三个"唯一实现"**：`core/jsonstore.py`(JSON 状态；四条并发纪律) · `core/transport.py`(HTTP `streamed()`；`curl_cffi` 响应**不支持 `with`**，AST 门禁盯着) · `core/localalbums.py`(本地相册集，只读)
`collectors/gallery_base.py`(声明式契约；`check_site` = 验收标准) · `collectors/hls.py` · `core/partials.py`(**按 URL 寻址**) · `core/sessions.py`(浏览器登录态 storage_state) · `api/local.py`(出图只收 `(root_id, rel)`，**不收裸绝对路径**) · `main.py` · `tests/isolation.py` · `tests/conftest.py`(R-CI-3)
`LIBRARY_SORTS`/`library_order_by` = 排序代号**唯一翻译点**（白名单；未知 400）· `library_searches` 表（存**具名参数**不是 SQL；坏条件保存时就拒绝）
**`scripts/gate.py`**(`Gate`/`Problem` 唯一定义，`checked` 必填、`==0` 算红) · `scripts/gateguard.py`(六道闸，逐闸报"核了几项") · **`.gitattributes` = 行尾唯一来源**
`scripts/probe_site.py`(**五项探针**+声明草稿+证据门禁；回归靶子=假站、找 bug 靶子=真站) · `scripts/probe.py`(浏览器探针) · `scripts/add_site.py`(`--list`/`--verify`) · `scripts/drift_check.py`(一个站点都没查成则退出 2) · `scripts/probe_feed.py`

## ⚠️ 静默坑索引（1–28 + 同族 A–J；细节见 `PITFALLS.md`）
**共同特征：不报错，只是结果错 —— 所以会在真实使用中活很久。**

| # | 一句话 | 判据 / 触发器 |
| --- | --- | --- |
| 1 | 次序即契约 | 先写 manifest 再落状态；`_settle_status` **不得抛** |
| 2 | 原子落盘 | `.part`+`.partsrc`+`os.replace`；**`.partsrc` 记来源 URL** |
| 3 | 单写者 | 孤儿恢复挂 **lifespan**（别放模块级）；SQLite → `busy_timeout`+`_retry_write` |
| 4 | 取消穿透 | 每个 `except Exception` 前必须 `except TaskCancelled: raise`（**AST 门禁**） |
| 5 | pydantic 吞字段 | 新增字段必须**显式声明**（+ `extra="allow"`） |
| 6 | 重名须先消解 | `layout.claim`；"目标已存在"当**半成品**续传 |
| 7 | 前端传"代号/意图" | 取值不在库内必须**显式映射**，不能直接拼 SQL |
| 8 | 认领 URL 是共享资源 | 复用 `gallery_base._match_score`；同分按采集器名字字典序 |
| 9 | 把"人看的文案"当数据 | 归类改用 `error_kind`；中文标签由后端下发 |
| 10 | `except OSError: pass` | 盖在"本来就会失败"的写入上 = 把丢数据改装成静默 |
| 11 | 引用不存在的接口 | 前端引用 `/files/raw` 而它**从没实现过** → 404 被 `onerror` 藏掉 |
| 12 | 流式响应不关 = 连接泄漏 | 池满后所有 worker 阻塞，**任务卡死不报错** |
| 13 | 形参被局部变量遮蔽 | `_download_m3u8` 的 `info` 被预检结果顶掉 → HLS `resolved_url` 恒缺 |
| 14 | 测试里"顺手调真实入口" | `POST /tasks/create` 起真 worker，给下个用例的库写心跳 |
| 15 | 登记成"已处理"又拿"未处理"去筛它 | 直播 `absorb()` 把 init 塞进 `seen`，而 `seen` 正是过滤器 → init 永不下 |
| 16 | 从**一条**样本生成规则 | 生成层要比样本**宽**（`photos` → `photos\d*`）；形状判据取下界 |
| 17 | 序号后跟**内容哈希** ≠ 序号枚举型 | `/data/<hash>/1-<sha256>.png` 改序号必 404。看残留熵，**不看**"是否以数字开头" |
| 18 | 测试断言的是"**我想到的行**" | 产出类改动的最后一步是**目视完整输出**（正则重复、截断方向只有看才发现） |
| 19 | **import 期单例**会替你起后台线程 | `task_manager = TaskManager()` 一 import 就活，抢当前用例的 `DB_PATH` |
| 20 | **`curl_cffi` 的响应不支持 `with`** | 只在"最需要换指纹"时发作（调用点包着 `except` → 静默拿不到正文） |
| 21 | CI 里 7 条"真解码"用例**只跳不跑** | 装不上 ffmpeg → **CI 从不验"产物内容好不好"**（V40 已修） |
| 22 | 存在、但不在任何调用链里的测试 = **不存在** | `test:task-query` 从落地起没在 CI 跑过 |
| 23 | 三个"仓库级不变量"脚本只在本地跑 | `selfcheck.py` / `add_site.py --all` / `verify_output.py` |
| 24 | **标签的语义只能有一个方向** | `[platform:X]` 的 `X` = **用例需要哪个平台** → 合法跳过是 `X != 当前平台`；写反了只有端到端全量能抓 |
| 25 | 别拿**墙钟**当判据 | 断言里出现 `sleep` → 改成显式传时刻 |
| 26 | **"没有结果" ≠ "没算出结果"** | 无解码器报 `no-decoder`、超限报 `skipped`、无快照报 `None`，**不许一律 `[]`** |
| 27 | **"规则生效了"要有计数当证据** | "配了但可能一条都没匹配"的必须报命中数，否则"没配"和"配错了"一样 |
| A | 条件请求 ⊥ 续传 | 有 `Range` 不能带 `If-None-Match`（回 304 而非 206） |
| B | 隔离的窗口期 | `monkeypatch.undo()` 还原 `DB_PATH` → 谁在那时碰库就写**用户真库** |
| C | 素材/断言的前提会失效 | 产品加了校验/能力 → 回头问 fixture、旧断言、**文档声称** |
| D | 测试基线会漂 | 会话间"自动提交"会合并别处分支 → 别拿旧用例数当事实 |
| E | 测试替身写死签名 | 抽通用函数时旧的 `lambda *a:` 立刻炸 —— **替身不诚实**，不是回归 |
| F | **通过但理由已经不对** | 新代码在**另一条**判据上报错、或更早一步就炸。比失败更危险 |
| G | 静默降级必须有痕 | `park*` 搬不动时不抛错，但得让人知道 → `stats()['last_error']` |
| H | `None`(不改) ≠ `""`(清空) | PATCH 无条件写全部字段 → 提交一个字段会顺手抹掉别的 |
| I | 替身覆盖不到**真数据**那一层 | 纯色图互指成"全是重复"(dHash 只看梯度) —— **只有真图冒烟能抓** |
| J | **不该数的时候不要数** | 条件读不出来时空条件是"全库条数" → 报 `null`+`broken`，**不许报 0** |
| 28 | **静默回退成默认值** | 未知排序档位回退 = 用户读成"这软件排序坏了"而日志安静 → 一律 400；`ORDER BY` 拼串是**能跑通的注入** |

**十条心法**：① 加一个状态就有多处**口径**要同步（`_final_status`/`summarize_resources`/前端计数）。② 凡"静默"处都问：**失败会发生吗？之后有人知道吗？** 都否 → 加重试或留痕。③ **误报的代价**决定判据松紧：只下"能被证明"的结论。④ **新功能接完回头验旧前提**（fixture/断言/文档声称），别改产品迁就旧的。⑤ **卡死类第一动作是打调用栈**（`faulthandler.dump_traceback_later`）。⑥ 写测试先问：**这条路径会起后台线程吗？**（第 14/19 条）。⑦ **补丁 vs 规则**：同一个错出现第三遍就抽成可执行规则（第 18/20 条都有 AST 门禁）。⑧ **连跑红、单跑绿 ⇒ 有东西跨用例活着**，先打出"谁还活着"。⑨ **判据只能挂"代号"不能挂"文案"**：假绿＝挂"没报错"（要能报"核了几项"，`0` 算红）、假红＝挂中文子串；"没验到"与"验过了没问题"**必须是两个结果**。⑩ **判据"方向"类错必须有非"照实现写"的观测面**（第 24 条）→ 端到端/真实平台/两边对账至少留一个。

## 状态机 / 速度 / 去重
`pending→running→extracting→downloading→success/partial/failed`，可 paused/cancelled；采集器返回空 → failed。pause 在资源边界退出；resume 不重采不重下。看门狗判活**必须看库里的 `tasks.hb`**（`_LIVE_MANAGERS` 只是进程内列表）；`delete()` 不能提前 pop `_active`。资源级终态 `gone`（源站 404/410）**自动不重试**；规格不匹配报 400。
吞吐由 **`domain_min_interval`** 决定，不由并发决定；令牌桶只改突发。AIMD 只按成功与否，**不看延迟**。站点级并发 `DomainLimiter` 按 `site_key` 分组（默认 3）。枚举快路径：页面给数量 → 抽样校验 → 跳过逐张探测（117→6 次）。去重两层都不删文件：sha256 复用；dHash ≤ 4 写 `duplicate_of`。

## 本机验证
`~/.workbuddy/binaries/python/envs/uwc-verify`（用户 `.venv` 是 WSL 的）。后端要 `--app-dir backend`；curl 对 127.0.0.1 加 `--noproxy '*'`。别把 Git Bash 的 `$PWD/...` 传给 Windows Python（造影子库）。
⚠️ **口径 = 收集总数**。本机 Windows **1398**（1392 passed + 6 skipped）；CI(Linux) **1399**（差额恒为 `POSIX_TERMINATION_SIGNALS` 的 SIGHUP 参数×1）。**改文档前先跑数；差分不闭合先怀疑自己。**
⚠️ **`img.xchina.io` 从本机整段 403**（`text/plain`，不是 CF 挑战页）→ 是**网络**问题，不是改版、不是回归。别拿它当靶子。
⚠️ 别前置 `export PATH="/usr/bin:/bin:$PATH"`（裸 `python` 会变成没 pytest 的 3.13.12）。coreutils 全无，`echo`/`git`/`python`/`date` 可用。
⚠️ **`exit 1` ≠ 有失败**：safe-delete 守卫拦"清空大目录"（阈值 50）→ 被杀时**无 `FAILED`、连汇总行都没有**。判据：进度行有没有 `F`。脚本要 `python -u`。
⚠️ 想拿 pytest 汇总行：`--basetemp` 指系统临时目录下**不存在**的路径，**绝不进项目目录**。长命令 `MODULE_NOT_FOUND` = 宿主包装器偶发缺失（**没跑**）→ 加 `run_in_background=true`。
⚠️ **行尾**：`Path.write_text()` 会把 `\n` 翻成 `\r\n`（用 `write_bytes` 或 `newline=""`）；`.gitattributes` 是唯一来源，`downloaders/video.py`、`scripts/verify_output.py` 的 HEAD 本是 CRLF，别"统一"。
⚠️ npm 的"包目录存在"≠"包装好了"：`@rollup/rollup-win32-x64-msvc` 可能是空目录 → `vite build` 报 `Cannot find module`。

## Git / 远端
独立建仓（toplevel = 项目目录），分支 `main`。**上级 `C:\Users\admin` 那个仓库绝不能碰**。远端 <https://github.com/LeonZhangDev/universal-web-collector>；本机**无 `gh` CLI** → `printf "protocol=https\nhost=github.com\n\n" | git credential fill` 取 `gho_` token。
⚠️ 本机 Bash 极简：`git -C <绝对路径>` 解析失败 → 用默认 cwd；提交用多个 `-m` 或 `git commit -F <文件>`。**会话间"自动提交"会移动 HEAD** → 每轮先 `git rev-parse HEAD` + `git ls-remote`，别拿上轮 SHA 当事实。**CI 跑的是 `origin/main`**；"本地全绿 / CI 全红"第一条命令是 `git log --oneline origin/main..HEAD`。
- 先试 `git push origin main`；只在真报 `502`/`000`/`CONNECT tunnel failed` 时降级用 Git Data API。别在没核对时 `--force`；推完与本地 HEAD 对一次。
- CI `.github/workflows/ci.yml`：`backend`(`uv sync --group dev`+ffmpeg+`pytest`+`gateguard.py`+三个离线守卫) / `frontend`(`npm ci`+`build`+`test:task-query`) / `docker`。日志 `GET /repos/{o}/{r}/actions/jobs/{id}/logs`。
- ⚠️⚠️ **CI 绿 ≠ 本地绿**：差额清单见 `PITFALLS.md` V39 六（修法是 V40 落地的）。依赖别漏：`curl-cffi`（主依赖）、`httpx2`（dev 组）—— 漏了是 collection 阶段整片红。
- 相关 skill：**`verification-red-triage`（红了怎么分类定位）与 `verification-gate-design`（判据/门禁怎么设计才不假绿假红）＝ 2026-09-25 从原单一 skill 拆出**、`github-ci-failure-triage`、`git-push-via-rest-api`、`competitor-feature-absorption`（对标同类产品前**先回代码核**）、`downloader-output-layout`、`graceful-cancel-worker`、`gallery-site-probe`。

## 版本史（细节见各版 commit / README / `PITFALLS.md`）

**V28–V38** 见 `PITFALLS.md` 各节（587→819→880→942→997→1063）。
- **V39** 站点探针（五项探测 + 真站当靶子 + 三档证据门禁；`drift_check`/`probe_feed`/`add_site`）；抓出第 16–19 条
- **V39.5** 让"绿/红"不再可能是假的（`Gate.checked`/`Problem.kind`）；抓出第 20 条（1105）
- **V40** **门禁落地**（`gateguard` 六闸+`.gitattributes`+CI 补 ffmpeg+R-CI-1/2/3）· **本地相册集**（只读、随机池、收藏）；抓出第 24/25 条（**1290**）
- **V41** 对标 Immich/PhotoPrism/Eagle：**排除模式 glob**（命中要计数）/ **往年今日**（`basis="mtime"`）/ **重复标记**（只标记不删）/ **扫描对账**（`None` ≠ `{"added":0}`）；抓出第 26/27 条 + 同族 H/I（**1309**）
- **V42** 五方向各对标：gallery-dl/yt-dlp → **URL 归档**；Eagle/digiKam → **评分**（上限后端硬拦）·**体检视图**（九维，0 就是 0）；Czkawka/dupeGuru → **重复分组**·**文件头 sniff**（`core/filekind.py`）（**1341**）
- **V43** 回代码核"V42 声称推迟的"→ 周期巡检/sidecar/会话登录态**早就有**；真缺的两个入口：**资源库排序**（`LIBRARY_SORTS` 白名单 + 次级键 `r.id`，未知 400）·**保存的搜索**（`library_searches`；`count` **0 就是 0**，读不出来给 `null`+`broken`，应用时**整体替换**）；抓出第 28 条 + 同族 J（**1367**）
- **V44** 落地上一轮清单里"能纯代码完成"的六项：**文件头规范化**(默认 dry_run + isobmff 不改)·**主色检索**(存色系代号)·**打包导出**(超限报错不截断)·**虚拟相册**(实时非快照)·**Webhook**(不返 secret/失败留痕)·**自检面板**(复用 gateguard)；+ 采集器插件 SDK 文档（**1398**）
  ⚠️ 两个决策点：**图像能力一律走 ffmpeg（项目刻意无 Pillow，加依赖=整片 collection 红）**；**加筛选维度时所有按位置转发 `library_filters` 的调用点都要同步**（不然"筛了没生效"且不报错 —— 本轮真发生过）
  ⚠️ 门禁**第三次**在同一处救场（V42/V43/V44 都是 README 用例数）
- **三句话**：加站的成本在探测不在写声明（V39）；假绿＝判据挂"没报错"、假红＝判据挂中文子串，修法是**先变成结构**，结构也有方向（第 24 条）；**"没有结果"与"没算出结果"必须两个值**，**规则生效要有计数**（V41）。
- **V42 一句话**：**先分清"谁跟我们是同一件事"**（Immich External Library = 同一契约，Google Photos = 托管模型）；同类产品的**默认动作**不能照抄（dupeGuru 默认删，我们只标记）。
- **下一步候选**（V44 后）：更多站点插件（先跑 `probe_site.py`）/ HLS 直播（缺真站样本）/ 人脸·语义搜索·地图（另一档投入）/ 智能文件夹**嵌套规则版** —— 全是**"暂时不做"，不是"不该做"**。
- ⚠️ **推迟理由会过期，同一错法犯过两次**（V41 评分 / V42 智能文件夹，都被"回代码看要多少活"戳破）→ **推迟理由要写在"最小可做版本"旁边**。
