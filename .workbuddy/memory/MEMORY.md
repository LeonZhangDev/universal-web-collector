# Universal Web Collector — 长期笔记（索引）

栈：uv + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
**正文以 `README.md` / `docs/AGENT_DEVELOPMENT_GUIDE.md`（§20–§22 最新）为准；坑的完整清单见 `PITFALLS.md`。**
本文件只做三件事：**索引 + 本机操作契约 + 一眼能认出的坑名**。命令清单与环境变量**都在 README**。
> 维护规则：**新坑先写进 `PITFALLS.md`，这里只加一行索引**；否则会重新长到被截断（09-23 / 09-24 各溢过一次，**这份文件上限约 16.5KB**）。

## 铁律 / 落盘
`URL → Browser/HTTP → Extractor → Resource → Downloader → Storage`；站点逻辑不进下载器。
布局（唯一入口 `core/layout.py`）：`downloads/相册名/图片`；**视频平铺到 downloads 根**（网站原名，m3u8 用 `gid.mp4`）。
重名：异源→`name(2).mp4`；相册视频撞名→`0001_相册名.mp4`；同源复用不重下。清单在 `downloads/_meta/任务ID/`。
⚠️ 采集器的 `album_tags_dir`（标签层）已废弃 —— 别引回来，留着就是"按了没反应"。
本地相册集的缩略图落在 `<库同级>/local_albums/_meta/thumb/`，**不在用户登记的根目录里**（只读承诺）。

## 关键文件
`api/tasks.py`(HTTP+SSE) · `core/task_manager.py`(状态机/看门狗/下载顺序) · `core/filters.py`(`match_resource` = "有效资源"唯一定义) · `core/errors.py`(`classify`/`KIND_*`) · `core/layout.py` · `core/disk.py` · `core/manifest.py`
`core/database.py`：加列**必须同时**进 SCHEMA 与 `_ADD_COLUMNS`；⚠️ **新表的时刻列 = `created_at` + REAL(epoch 秒)**，老表是 `created_time` 字符串
`core/phash.py`(只标记不删) · `core/mediacheck.py`(完整性**算术**判据) · `core/thumbs.py`(按 **sha** 不按路径) · `downloaders/dash.py`(MPD 解析，纯函数) · `downloaders/video.py`(**HEAD 是 CRLF**) · `downloaders/ratelimit.py`(请求桶 + **全局字节桶**)
**`core/jsonstore.py` = JSON 状态存储唯一实现**（读-改-写必须过它的四条并发纪律，**别手写第二遍**）
**`core/transport.py` = HTTP 传输唯一实现**（`streamed()`；`curl_cffi` 的响应**不支持 `with`**，有 AST 门禁盯着）
**`core/localalbums.py` = 本地相册集唯一实现**（只读；登记/扫描/浏览/随机池/收藏 + V41 的排除模式/往年今日/重复标记/扫描对账）· `api/local.py`(出图只收 `(root_id, rel)`，**从不收裸绝对路径**)
`collectors/gallery_base.py`(声明式契约；`check_site` = 声明唯一验收标准) · `collectors/hls.py` · `core/partials.py`(**按 URL 寻址**) · `main.py`(lifespan) · `tests/isolation.py` · `tests/conftest.py`(R-CI-3 跳过策略)
**`scripts/gate.py` = `Gate`/`Problem` 唯一定义**（`checked` 必填；`checked==0` **一律算红**）· `scripts/gateguard.py`(仓库门禁：六道闸，逐闸报"核了几项") · **`.gitattributes` = 行尾的唯一来源**
`scripts/probe_site.py`(**五项探针** + 声明草稿 + 证据门禁 + `--json`/`--snapshot`/`--smoke`；**回归**靶子=本地假站点、**找 bug** 靶子=真实站点) · `scripts/probe.py`(浏览器探针，两码事)
`scripts/add_site.py`(**加站门禁**：`--list` 只挑带 `site` 声明的 `SequenceGallerySpider`；`--verify <名>` 跑四闸) · `scripts/drift_check.py`(**一个站点都没查成则退出 2**) · `scripts/probe_feed.py`

## ⚠️ 静默坑索引（1–27 + 同族 A–H；细节见 `PITFALLS.md`）
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
| 8 | 认领 URL 是共享资源 | 复用 `gallery_base._match_score`；同分按**采集器名字字典序** |
| 9 | 把"人看的文案"当数据 | 归类改用 `error_kind`；中文标签由后端下发 |
| 10 | `except OSError: pass` | 盖在"本来就会失败"的写入上 = 把丢数据改装成静默 |
| 11 | 引用不存在的接口 | 前端引用 `/files/raw` 而它**从没实现过** → 404 被 `onerror` 藏掉 |
| 12 | 流式响应不关 = 连接泄漏 | 池满后所有 worker 阻塞，**任务卡死不报错** |
| 13 | 形参被局部变量遮蔽 | `_download_m3u8` 的 `info` 被预检结果顶掉 → HLS `resolved_url` 恒缺 |
| 14 | 测试里"顺手调真实入口" | `POST /tasks/create` 起真 worker，给下一个用例的库写心跳 |
| 15 | 登记成"已处理"又拿"未处理"去筛它 | 直播 `absorb()` 把 init 塞进 `seen`，而 `seen` 正是过滤器 → init 永不下 |
| 16 | 从**一条**样本生成规则 | 生成层要比样本**宽**（`photos` → `photos\d*`）；形状判据取更宽的**下界** |
| 17 | 序号后跟**内容哈希** ≠ 序号枚举型 | `/data/<hash>/1-<sha256>.png` 改序号必然 404。看残留熵，**不看**"是否以数字开头" |
| 18 | 测试断言的是"**我想到的行**" | 产出类改动的最后一步是**目视完整输出**：正则重复、截断方向只有看才发现 |
| 19 | **import 期单例**会替你起后台线程 | `core/task_manager.py` 末尾 `task_manager = TaskManager()` → 一 import 就活，抢当前用例的 `DB_PATH` |
| 20 | **`curl_cffi` 的响应不支持 `with`** | 只在"最需要换指纹"时发作（调用点都包着 `except` → 静默拿不到正文） |
| 21 | CI 里 7 条"真解码"用例**只跳不跑** | 装不上 ffmpeg → **CI 从不验"产物内容好不好"**（phash×4 + DASH e2e×3） |
| 22 | 存在、但不在任何调用链里的测试 = **不存在** | `frontend` 的 `test:task-query` 从落地起没在 CI 跑过 |
| 23 | 三个"仓库级不变量"脚本只在本地跑 | `selfcheck.py` / `add_site.py --all` / `verify_output.py` |
| 24 | **标签的语义只能有一个方向** | `[platform:X]` 的 `X` = **这条用例需要哪个平台** → 合法跳过是 `X != 当前平台`；`X == 当前平台` 却还在跳 = 写反了。**写反了单测抓不到**（实现与测试一起错），只有**端到端全量**能抓|

| 25 | 别拿**墙钟**当判据 | 断言里出现 `sleep`，先问能不能换成"**显式传时刻**"（`_is_blocked(url, now=t+61)`） |
| 26 | **"没有结果" ≠ "没算出结果"** | 空的时候是"确实没有"还是"没算"？无解码器报 `no-decoder`、超上限报 `skipped`、无快照报 `None`，**不许一律 `[]`**；且"归谁"要在**计数阶段**分开（别用"是不是空的"反推原因码） |
| 27 | **"规则生效了"要有计数当证据** | 排除模式这类"配了但可能一条都没匹配"的，必须报命中数 —— 否则"没配"和"配错了"长得一样 |
| A | 条件请求 ⊥ 续传 | 有 `Range` 不能带 `If-None-Match`（回 304 而非 206） |
| B | 隔离的窗口期 | `monkeypatch.undo()` 还原 `DB_PATH` → 谁在那时碰库就写**用户真库** |
| C | 素材/断言的前提会失效 | 产品加了校验或能力 → 回头问 fixture 与旧断言（**也包括文档声称**） |
| D | 测试基线会漂 | 会话间"自动提交"会合并别处分支 → 别拿旧用例数当事实 |
| E | 测试替身写死签名 | 抽出通用函数时旧的 `lambda *a:` 会立刻炸 —— **替身不诚实**，不是回归 |
| F | **通过但理由已经不对** | 新代码在**另一条**判据上报错、或在更早一步就炸。比失败更危险 |
| G | 静默降级必须有痕 | `park*` 搬不动时不抛错，但"怎么永远攒不起来"得有人知道 → `stats()['last_error']` |
| H | `None`(不改) ≠ `""`(清空) | PATCH 无条件写全部字段 → "只提交一个字段"会顺手抹掉别的字段 |
| I | 替身覆盖不到**真数据**那一层 | 纯色图互指成"全是重复"(dHash 只看梯度) —— 假指纹永远走不到，**只有真图冒烟能抓** |

**十条心法**：
① 加一个状态，就有多处**口径**要同步（`_final_status`/`summarize_resources`/前端计数）。
② 凡"静默"处都问：**失败会发生吗？失败之后有人知道吗？** 两个都否 → 加重试或留痕。
③ **误报的代价**决定判据松紧：只下"能被证明"的结论（`mediacheck` 认不出的容器一律放行）。
④ **新功能接完，回头验旧的前提**（fixture / 断言 / 文档声称），别改产品去迁就旧的。
⑤ **卡死类问题第一动作是打调用栈** —— `faulthandler.dump_traceback_later(N, exit=True)`。
⑥ 写测试时问：**这条路径会不会起后台线程？**（第 14 条；也包括只 import 就起线程的模块级单例，第 19 条）。
⑦ **补丁 vs 规则**：同一个错出现第三遍，就把它抽成程序可执行的规则（第 18/20 条都有 AST 门禁）。
⑧ **连跑红、单跑绿 ⇒ 有东西跨用例活着**。别急着重跑撞运气，先把"谁还活着"打出来。
⑨ **判据只能挂在"代号"上，不能挂在"文案"上**。假绿＝判据挂在"没报错"（要能报"我核了几项"，`0` 一律算红）；假红＝判据挂在中文子串（产物自带 `kind`/`step`/`level`）。"没验到"与"验过了没问题"**必须是两个结果**。
⑩ **判据"方向"类错误必须有非"照实现写"的观测面**：实现与测试一起错时，单测越忠实越看不见（第 24 条）→ 端到端 / 真实平台 / 两边对账，至少留一个。

## 状态机 / 速度 / 去重
`pending→running→extracting→downloading→success/partial/failed`，可 paused/cancelled；采集器返回空 → failed。pause 在资源边界退出；resume 不重采不重下。
看门狗判活**必须看库里的 `tasks.hb`**（`_LIVE_MANAGERS` 只是进程内列表）；`delete()` 不能提前 pop `_active`。资源级终态 `gone`（源站 404/410）**自动不重试**。规格不匹配报 400 而不是静默忽略。
吞吐由 **`domain_min_interval`** 决定，不由并发决定；令牌桶只改突发。AIMD 只按成功与否，**不看延迟**。站点级并发 `DomainLimiter` 按 `site_key` 分组（默认 3）。
枚举快路径：页面给数量 → 抽样校验 → 跳过逐张探测（117→6 次）。去重两层都不删文件：sha256 一致复用；dHash ≤ 4 写 `duplicate_of` 保留。

## 本机验证
`~/.workbuddy/binaries/python/envs/uwc-verify`（用户 `.venv` 是 WSL 的）。后端要 `--app-dir backend`；curl 对 127.0.0.1 加 `--noproxy '*'`。别把 Git Bash 的 `$PWD/...` 传给 Windows Python（造影子库）。
⚠️ **口径 = 收集总数**。本机 Windows **1309 收集**（1303 passed + 6 skipped）；CI(Linux) **1310**（CI 实测 1308+2，与 README 逐位对账过）—— 差额恒为 `POSIX_TERMINATION_SIGNALS` 的 SIGHUP 参数×1。**V40 起 CI 装了 ffmpeg，那 7 条"真解码"用例真的跑了**（第 21 条已修，剩下 2 条是 Windows-only）。**改文档前先跑数；差分不闭合先怀疑自己。**
⚠️ **`img.xchina.io` 从本机整段 403**（`text/plain`，不是 CF 挑战页）→ 这条**网络**的问题，不是站点改版、不是回归。别拿它当靶子。
⚠️ 别前置 `export PATH="/usr/bin:/bin:$PATH"`（会把裸 `python` 换成没 pytest 的 3.13.12）。coreutils 全无，但 `echo`/`git`/`python`/`date` 可用。
⚠️ **`exit 1` ≠ 有失败**：safe-delete 守卫拦"清空大目录"（阈值 50）→ 被杀时**无 `FAILED`、连汇总行都没有**。判据：看进度行有没有 `F`；若输出**只有一行** `[safe-delete]…`，那是上次留下的 `data/_verify_output` 触发的 —— **`mv` 走它**再跑。被 kill 时重定向的输出去丢 → 脚本要 `python -u`。
⚠️ 想拿 pytest 汇总行：`--basetemp` 指系统临时目录下一个**不存在**的路径；**绝不指进项目目录**。宿主包装器偶发缺失 → 长命令 `MODULE_NOT_FOUND`（**压根没跑**），加 `run_in_background=true` 绕过。
⚠️ **行尾**：`Path.write_text()` 在 Windows 上把 `\n` 翻成 `\r\n`。`.gitattributes` 是唯一来源；`downloaders/video.py` 与 `scripts/verify_output.py` 的 HEAD 本是 CRLF，别去"统一"。
⚠️ **npm 的"包目录存在"≠"包装好了"**：`@rollup/rollup-win32-x64-msvc` 可能是**空目录** → `vite build` 报 `Cannot find module`（`--no-save` 重装即可）。

## Git / 远端
独立建仓（toplevel = 项目目录），分支 `main`。**上级 `C:\Users\admin` 那个仓库绝不能碰**。远端 <https://github.com/LeonZhangDev/universal-web-collector>；本机**无 `gh` CLI** → `printf "protocol=https\nhost=github.com\n\n" | git credential fill` 取 `gho_` token。
⚠️ 本机 Bash 极简：`git -C <绝对路径>` 解析失败 → 用默认 cwd；提交用多个 `-m` 或 `git commit -F <文件>`（`\n` 不会被展开）。**会话间"自动提交"会移动 HEAD** → 每轮先 `git rev-parse HEAD` + `git ls-remote origin refs/heads/main`，别拿上轮记的 SHA 当事实。**CI 跑的是 `origin/main`，不是本地 HEAD**；"本地全绿 / CI 全红"第一条命令是 `git log --oneline origin/main..HEAD`。
- 先试 `git push origin main`；只在真报 `502`/`000`/`CONNECT tunnel failed` 时降级用 Git Data API（skill `git-push-via-rest-api`）。都**别在没核对时 `--force`**；推完与本地 `rev-parse HEAD` 对一次。
- CI `.github/workflows/ci.yml`：`backend`(`uv sync --group dev` + 装 ffmpeg + `pytest` + `gateguard.py` + 三个离线守卫) / `frontend`(`npm ci` + `build` + `test:task-query`) / `docker`。日志 `GET /repos/{o}/{r}/actions/jobs/{id}/logs`。
- ⚠️⚠️ **CI 绿 ≠ 本地绿**：差额清单见 `PITFALLS.md` V39 六（**修法是 V40 落地的**）。依赖别漏：`curl-cffi`（主依赖）、`httpx2`（dev 组）—— 漏了是**测试 collection 阶段整片红**。
- 相关 skill：`verification-red-triage`、`github-ci-failure-triage`、`git-push-via-rest-api`、`downloader-output-layout`、`graceful-cancel-worker`、`gallery-site-probe`。

## 版本史（细节见各版 commit / README / `PITFALLS.md`）

**V28–V38** 见 `PITFALLS.md` 各节（587→819→880→942→997→1063）。
- **V39** 新站点探针：五项探测 → **真实站点当靶子** → **三档证据门禁**；续 `--json`/`--snapshot`/`--smoke`·`drift_check.py`·`probe_feed.py`·`add_site.py`；抓出第 16/17/18/19 条
- **V39.5** 让"绿/红"不再可能是假的（`Gate.checked` / `Problem.kind` / `drift_check` 空转非 0 退出）；抓出第 20 条（1105）
- **V40** **门禁落地**（`gate.py` + `gateguard.py` 六闸 + `.gitattributes` + CI 补 ffmpeg/离线脚本 + R-CI-1/2/3）· **本地相册集**（只读、随机池、收藏）；抓出第 24/25 条（**1290**）

- **V38 复核要点**：当初写的拒绝理由**站不住** —— **"暂时不做"不许写成"不该做"。**
- **V39 / V39.5 / V40 一句话**：**加站的成本在探测不在写声明**；假绿＝判据挂在"没报错"（要 `checked`，`0` 算红）；假红＝判据挂在中文子串（要 `kind`/`step`/`level` 代号）。修法是**先把它变成结构**；结构也有方向 —— 见第 24 条。
- **V41** 对标 Immich/PhotoPrism/Eagle 后吸收四项：**排除模式 glob**（命中要计数）/ **往年今日**（`basis="mtime"`，不用 EXIF）/ **重复标记**（复用 `core/phash`，无 ffmpeg 报 `no-decoder` 不报空列表）/ **扫描对账**（`None` ≠ `{"added":0}`）；抓出第 26/27 条 + 同族 H/I（**1309**）
- **V41 一句话**：**"没有结果"与"没算出结果"必须是两个值**（空列表会被读成"没有重复"）；**"规则生效了"要有计数当证据**（写了却一条没排除，长得和"没配规则"一模一样）。
- **下一步候选**：更多站点插件（先跑 `probe_site.py`）/ HLS 直播（`EVENT` 或无 `ENDLIST`，缺真站样本）/ 直播断点续录（低）/ 人脸·语义搜索·地图（另一档投入）/ 评分·颜色搜索·智能文件夹（要"属性的属性"语义）—— 全是**"暂时不做"，不是"不该做"**。
