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
    """已保存的登录态 + 正在进行的登录任务(前端据此显示进度)。"""
    return {"sessions": sessions.list_sessions(), "jobs": sessions.list_jobs()}


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
    """提前收尾: 存下当前快照并关闭浏览器。"""
    if not sessions.stop_login(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return sessions.get_job(job_id)


@router.delete("/{domain}")
def remove_session(domain: str):
    if not sessions.delete_session(domain):
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": domain}
