"""登录态管理接口。

付费/会员站点没有登录态就只能采到登录页, 这组路由让用户在本机浏览器里
登录一次, 之后采集器复用这份 cookies。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import sessions

router = APIRouter(prefix="/sessions", tags=["sessions"])


class LoginIn(BaseModel):
    url: str
    timeout: int = 900  # 最长等待时间(秒), 超时自动收尾


@router.get("")
def list_sessions():
    """已保存的登录态 + 正在进行的登录任务(前端据此显示进度)。

    `cf_stale`: 哪些域的登录态**在这一次访问中被判定失效**(带着它去访问会
    被 Cloudflare 直接拒绝)。它不是"文件过期"这类能静态判断的事, 只有真被
    拒过一次才知道, 所以必须由采集端上报 —— 界面据此提示用户重新登录。
    """
    try:
        from collectors import album_meta

        stale = album_meta.stale_state_domains()
    except Exception:
        stale = []
    return {
        "sessions": sessions.list_sessions(),
        "jobs": sessions.list_jobs(),
        "cf_stale": stale,
    }


@router.post("/login")
def start_login(payload: LoginIn):
    """打开一个可见浏览器让用户手动登录。立即返回, 不阻塞请求。"""
    if not payload.url.strip():
        raise HTTPException(status_code=400, detail="url 不能为空")
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url 必须以 http:// 或 https:// 开头")
    return sessions.start_login(payload.url.strip(), timeout=payload.timeout)


@router.get("/login/{job_id}")
def login_status(job_id: str):
    job = sessions.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/login/{job_id}/stop")
def finish_login(job_id: str):
    """提前收尾: 存下当前快照并关闭浏览器。

    完成后会解掉该域的 CF 熔断/登录态隔离记录 —— 用户刚换了一份新的登录
    态, 继续记着"它失效过"只会让人以为重登也没用。
    """
    job = sessions.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not sessions.stop_login(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    _forget_cf_state(job.get("url") or "")
    return sessions.get_job(job_id)


@router.delete("/{domain}")
def remove_session(domain: str):
    """删除登录态文件。同时解掉该域的隔离记录, 让下一次还能用匿名或新态重试。"""
    if not sessions.delete_session(domain):
        raise HTTPException(status_code=404, detail="session not found")
    _forget_cf_state(f"https://{domain}/")
    return {"deleted": domain}


def _forget_cf_state(url):
    try:
        from collectors import album_meta

        album_meta.clear_domain_state(url)
    except Exception:
        pass  # 采集模块不可用时不影响登录态管理本身
