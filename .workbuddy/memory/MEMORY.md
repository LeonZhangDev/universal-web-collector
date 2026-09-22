# Universal Web Collector — 长期笔记（索引）

栈：uv + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
**正文以 `README.md` / `docs/AGENT_DEVELOPMENT_GUIDE.md` 为准；完整踩坑清单见同目录 `PITFALLS.md`。**

## 铁律
`URL → Browser/HTTP → Extractor → Resource → Downloader → Storage`。
不把站点逻辑写进下载器：Collector 只发现资源，Downloader 只下载资源。

## 落盘布局（V28，唯一入口 `backend/core/layout.py`）
`downloads/相册名/图片`；**视频一律平铺到 downloads 根**，文件名取网站原名（m3u8 用 `gid.mp4`）。
重名：异源 → `name(2).mp4`；相册视频平铺撞名 → `0001_相册名.mp4`；同源复用不重下。
清单统一放 `downloads/_meta/{任务ID}/`。
⚠️ 采集器的 `album_tags_dir`（标签层）已废弃 —— 别再引回来，留着就是"按了没反应"。

## 关键文件
`api/tasks.py`(HTTP+SSE) · `core/task_manager.py`(状态机/看门狗) · `core/database.py`(加列必须进 `_MIGRATIONS`)
`core/filters.py`(有效资源唯一定义 `match_resource`) · `core/errors.py` · `core/disk.py` · `core/layout.py`
`core/phash.py`(只标记不删) · `core/cdn_profile.py` · `core/manifest.py`
`collectors/gallery_base.py`(SequenceGallerySpider/GallerySite/check_site) · `collectors/hls.py` · `collectors/scores.py`
`downloaders/base.py` · `downloaders/ratelimit.py`(令牌桶+AIMD) · `main.py`(lifespan)

## ⚠️ 六条"静默"坑（细节见 PITFALLS）
1. **次序即契约**：先 `_write_manifest` 再 `_settle_status`；`_settle_status` 不得抛。
2. **原子落盘** `.part` + `.part.src` + `os.replace`，落盘比字节数（有 `Content-Encoding` 不比）。
3. **孤儿恢复**挂 lifespan（别放模块级）；**SQLite 写-写仍单写者** → `busy_timeout` + `_retry_write`。
4. **取消穿透**：所有 `except Exception` 前必须 `except TaskCancelled: raise`（AST 门禁把关）。
5. **`FilterIn` 必须声明全部键 + `extra="allow"`**，否则前端开关被静默丢弃。
6. **重名必须在下载前消解**（`layout.claim`）：下载器见"目标已存在"就当**半成品**续传 → 异源同名被拼成一份、字节数还常恰好对上 → 报成功。只查库不够，要加本次采集内的占位表。

## 状态机
`pending→running→extracting→downloading→success/partial/failed`，可 paused/cancelled。
采集器返回空列表 → failed。cancel 留文件；pause 在资源边界退出；resume 不重采不重下。
看门狗判活**必须看库里的 `tasks.hb`**；`_LIVE_MANAGERS` 是进程内列表。`delete()` 不能提前 pop `_active`。

## 速度 / 去重
吞吐由 **`domain_min_interval`** 决定，不由并发决定；令牌桶只改突发。AIMD 只按成功与否判定，**不看延迟**。
枚举快路径：页面给数量 → 抽样校验 → 跳过逐张探测（实测 117→6 次）。L1 二分默认**关**。
去重两层都不删文件：sha256 一致复用；dHash ≤ 4 写 `duplicate_of` 并保留文件。

## 本机验证
用 `~/.workbuddy/binaries/python/envs/uwc-verify`（用户 `.venv` 是 WSL 的）。后端要 `--app-dir backend`；
curl 对 127.0.0.1 加 `--noproxy '*'`；起服务用 `run_in_background`；命令前 `export PATH="/usr/bin:/bin:$PATH"`。
⚠️ 别把 Git Bash 的 `$PWD/...` 传给 Windows Python（会造影子库）；同一文件多处 Edit 不要并行发。

```bash
make install|backend|frontend|build|test|docker
python scripts/verify_output.py            # 40 项断言
python scripts/verify_hls.py               # 18 项断言
python scripts/selfcheck.py                # 站点声明自检 + CDN 画像快照
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY` / `UWC_FFMPEG` / `UWC_CDN_PROFILE`

## Git
项目内独立建仓（toplevel 即项目目录），分支 `main`。**上级 `C:\Users\admin` 那个仓库绝不能碰**。

⚠️ **本机 Bash 环境限制（2026-09-21 实测）**：`cat`/heredoc 不可用，`git -C <绝对路径>` 解析失败（报 not a git repository）。
git 命令直接用 Bash 工具的默认 cwd（已是项目根 `C:/Users/admin/Desktop/universal_web_collector_v9`），提交用多个 `-m` 而非 heredoc。
`uv` 在 PATH 上（0.12.15），`uwc-verify` 环境**仍在**
（`C:/Users/admin/.workbuddy/binaries/python/envs/uwc-verify/Scripts/python.exe`，里面是旧的 `httpx 0.28.1`）；
模拟 CI 环境用 `UV_PROJECT_ENVIRONMENT=<临时目录> uv sync --group dev`。

⚠️ **本仓库有"自动提交"机制，会在会话之间提交并移动 HEAD**（V32 期间 HEAD 从 `2492fe3` 变成 `f389338`）。
所以**每轮开始必须先 `git rev-parse HEAD` + `git ls-remote origin refs/heads/main`**，
不要拿上一轮记的 SHA 当事实。

⚠️ **CI 跑的是 `origin/main`，不是本地 HEAD**。"本地全绿 / CI 全红"的第一个诊断命令是
`git log --oneline origin/main..HEAD` —— 本地可能有几十个没推的提交（V32 时是 29 个）。

## ⚠️ 第 7 条静默坑：前端传"代号/别名/意图"，后端必须先翻译再查库
V29 的 `active`（状态组代号）、V31 的 `auto`（采集器意图，非落库名）两次同型缺陷 —— 多传一个词就静默返回 0 条。
凡筛选参数取值不在库内，后端必须显式映射或排除，不能直接拼 SQL。

## ⚠️ 第 8 条静默坑：认领 URL 的逻辑是全局共享资源，任何站点不得自写宽松版
V32 加 Pexels 时，自写 `parse_gid(strict=False)` 把 xchina 的 `/photo/id-6aa5136f606fe.html`
解析成 `id-6aa5136f606fe` 抢走 → **任务不报错但采不到东西**。修法：必须复用
`gallery_base._match_score(site, raw)`。另：纯 ID 样本存在跨站歧义（通用 `[0-9A-Za-z_-]{6,}` 什么都能装），
两站同分时胜负由**采集器名字字典序**决定（`pexels` < `xchina_gallery`）——
所以每个站点都要用自己的 `gid_shape` 对纯 ID 再复判一次，不能只靠通用正则。

## 落盘/远端
- GitHub 远端：**https://github.com/LeonZhangDev/universal-web-collector**（分支 `main`）。
  本机**无 `gh` CLI**；用 `git credential fill` 取 GCM 里的 `gho_` token 调 REST API 建仓。
  `git credential fill` 需要 `printf "protocol=https\nhost=github.com\n\n" | ...`。
- **CI**：`.github/workflows/ci.yml`，三个 job — `backend`(`uv sync --group dev` + `pytest`) /
  `frontend`(`npm install` + `build`) / `docker`(`docker build`，`needs: [backend, frontend]`)。
  首次在 `f389338` 上跑绿（run 35717962425）。
- 看 job 日志：`GET /repos/{o}/{r}/actions/jobs/{job_id}/logs` —— **带 token 时返回纯文本不是 zip**。
- ⚠️ 依赖声明别漏：`curl-cffi`（xchina TLS 伪装，主依赖）与 `httpx2`（`starlette.testclient` 必需，dev 组）。
  漏了不会在导入期报错，而是**测试 collection 阶段整片红**（5 个 import TestClient 的文件）。

## 最近大版本
- V28（c14d6c3/d2573cb）：落盘目录重构 — 相册单层 + 视频平铺 + 清单与媒体分离。
- V29（c300198）：批量创建 / 搜索筛选分页 / 环境诊断三接口 + 前端全面改版（创建区/表格/详情抽屉/诊断面板/样式统一+大图预览）。
- V30（3fd1baf）：批量操作 / 失败重试 / 统计面板 / 代理字段（仅落库）。
- V31（609f2c7）：代理池轮换**真正联动下载层**（`ProxyPool`/`make_proxy_session`，任务独占 Session）+
  任务通知中心（`_settle_status` 钩子 + SMTP，cancelled 静默）+ 统计纯 SVG 图表 + 采集器筛选 +
  Lightbox 缩放旋转 + tab 记忆 + 空状态插画；`.gitignore` 扩展；三份文档同步。测试 557 passed。
- V32（2edbc62 + d410551）：**四条 P2 全部实施** — ① 代理熔断（3 次失败 / 冷却 300s / 到期半开 /
  全池熔断退化按序号；`GET /tasks/{id}/proxy`）② 字节级进度（`progress_cb(nbytes)` + `0.4s` 节流 +
  只走内存不落库 + 前端 `byterate.js` 差分类 + 相对峰值归一化）③ 跨任务资源库
  （`library_filters` 单一 WHERE 源 + 相册精确匹配 + `refs` 引用计数 + `/library`、`/library/albums`）
  ④ Pexels 采集器（验证声明式契约可撑第二站）。测试 587 passed，前端 89 modules / 200.07 kB。
  首次配远端并推送成功。**新增第 8 条静默坑（见上）。**
