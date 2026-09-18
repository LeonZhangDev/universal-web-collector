"""浏览器登录态管理。

为什么要这个功能
================
会员/付费/需要登录的站点, 采集器看到的页面永远是登录框。解决办法是让用户在
真实浏览器里登录一次, 把 cookies/localStorage 存成 Playwright 的
storage_state, 之后采集器用这份状态发起请求。

难点与取舍
==========
用户操作完该怎么告诉服务器"好了"?两条路都有坑:

  * 等页面关闭 —— 用户往往是直接点浏览器右上角 X, 整个 context 一起没了,
    此时再调 `storage_state()` 会抛 "Target closed", 什么也存不下来。
  * 加个"完成"按钮 —— 要求在任意第三方页面上注入 UI, 脆弱且侵入。

这里选**周期快照**: 轮询期间每 snapshot_interval 秒把当前状态写一次,
最后主线程退出时留下的就是最近一次成功的快照。用户想怎么关窗口都行 ——
哪怕是任务管理器里杀进程, 也已经存到了两秒前的状态。

代价是最后一两秒的 cookie 变动可能丢(通常登录跳转早就完成了), 换来的是
完全不用在第三方页面上做任何事。

线程
====
开浏览器是分钟级的阻塞操作, 不能占着 HTTP 请求线程。每个登录任务跑在独立
daemon 线程里, 前端用 job id 轮询状态。
"""

import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core.config import settings

# 每次快照间隔: 用户再怎么突然关窗口, 损失的窗口 <= 这个数值
SNAPSHOT_INTERVAL = 2
# 单次登录任务的最长等待时间(秒), 超时自动收尾, 避免浏览器永远挂着
DEFAULT_TIMEOUT = 900

_SAFE_DOMAIN = re.compile(r"[^A-Za-z0-9._-]+")

_jobs = {}
_jobs_lock = threading.Lock()


def state_file(domain):
    """登录态文件路径。

    domain 会经过清洗 —— 它可能来自 URL(netloc 可以含 `:` 端口、空格等),
    直接拼进文件名既可能非法(Windows 上 `localhost:8080.json` 写不了),
    也可能被构造成 `../` 跑到目录外面去。
    """
    # strip 只去掉下划线: 去掉点会让 "../../evil" 变成 "evil", 恰好和真正的
    # evil 域名撞车 —— 保留原样虽然看着怪, 但至少所见即所得。
    safe = _SAFE_DOMAIN.sub("_", domain or "").strip("_") or "default"
    settings.browser_state_dir.mkdir(parents=True, exist_ok=True)
    return settings.browser_state_dir / f"{safe}.json"


def domain_of(url):
    """由 URL 推出登录态的 domain key。"""
    host = urlparse(url).netloc
    return _SAFE_DOMAIN.sub("_", host).strip("._") or "default"


def list_sessions():
    """列出已保存的登录态, 最近修改的排前面。"""
    d = settings.browser_state_dir
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.json"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append(
            {
                "domain": p.stem,
                "path": str(p),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    out.sort(key=lambda x: x["modified"], reverse=True)
    return out


def delete_session(domain):
    """删除登录态文件。返回是否真的删掉了。

    domain 只用来在同目录里定位 `domain.json`, 且已过清洗 —— 不接受路径。
    """
    p = state_file(domain)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _update(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)
        return job


def _set(job_id, status, message):
    _update(job_id, status=status, message=message)


def start_login(url, timeout=DEFAULT_TIMEOUT):
    """启动一次登录流程, 立即返回 job(不等完成)。"""
    job_id = uuid.uuid4().hex[:12]
    domain = domain_of(url)
    job = {
        "id": job_id,
        "url": url,
        "domain": domain,
        "status": "starting",
        "message": "正在启动浏览器",
        "file": str(state_file(domain)),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cancel": False,
        "browser": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    t = threading.Thread(
        target=_login_worker, args=(job_id, url, timeout), daemon=True,
        name=f"login-{job_id}",
    )
    t.start()
    return dict(job)


def get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return {k: v for k, v in job.items() if k != "browser"} if job else None


def list_jobs():
    with _jobs_lock:
        jobs = list(_jobs.values())
    return [{k: v for k, v in j.items() if k != "browser"} for j in jobs]


def stop_login(job_id):
    """提前结束登录: 快照已经存好了, 这里只是把浏览器收掉。"""
    job = _update(job_id, cancel=True, status="stopping",
                  message="正在关闭浏览器并保存登录态")
    if not job:
        return False
    try:
        browser = job.get("browser")
        if browser:
            browser.close()
    except Exception:
        pass
    return True


def _login_worker(job_id, url, timeout):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set(job_id, "error", "未安装 playwright, 无法启动登录浏览器")
        return

    target = None
    try:
        with sync_playwright() as p:
            launch_kwargs = {"headless": False}
            if settings.proxy:
                launch_kwargs["proxy"] = {"server": settings.proxy}
            browser = p.chromium.launch(**launch_kwargs)
            _update(job_id, browser=browser)

            ctx_kwargs = {"user_agent": settings.user_agent}
            existing = state_file(domain_of(url))
            if existing.exists():
                ctx_kwargs["storage_state"] = str(existing)
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                # 打开失败也要给个机会: 站点可能很慢或需要重试, 页面仍可能可用
                _set(job_id, "running",
                     f"页面加载提示: {type(e).__name__} —— 若窗口已打开请继续操作")

            _set(job_id, "running",
                 "浏览器已打开: 请完成登录后直接关闭窗口, 登录态会自动保存")
            target = existing
            deadline = time.time() + timeout

            while time.time() < deadline:
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    cancelled = bool(job and job.get("cancel"))
                if cancelled or not browser.is_connected():
                    break
                _snapshot(context, target)
                time.sleep(SNAPSHOT_INTERVAL)

            # 收尾: 浏览器可能刚被关闭, 这次快照失败不算错误 —— 之前的快照还在
            _snapshot(context, target)

        with _jobs_lock:
            job = _jobs.get(job_id)
            cancelled = bool(job and job.get("cancel"))
        if target and target.exists():
            _set(job_id, "done",
                 f"登录态已保存: {target.name}"
                 + (" (手动结束)" if cancelled else ""))
        else:
            _set(job_id, "error", "未能保存登录态: 浏览器关闭过早或被外部终止")
    except Exception as e:
        _set(job_id, "error", f"登录失败: {type(e).__name__}: {e}")


def _snapshot(context, target):
    """把当前 context 的状态写到目标文件。

    先写临时文件再原子替换: 轮询到一半被中断时, 不会留下被截断的半成品
    JSON(那会让下一次采集读到一份损坏的登录态)。
    """
    if not isinstance(target, Path):
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        context.storage_state(path=str(tmp))
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(target)
            return True
        tmp.unlink(missing_ok=True)
    except Exception:
        # context 已随浏览器关闭 —— 属预期情况, 保留上一次快照
        return False
    return False
