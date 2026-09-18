from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.sessions import router as sessions_router
from api.tasks import router
from core.config import settings

app = FastAPI(title="Universal Web Collector v10")


@app.get("/healthz")
def health():
    return {"status": "ok"}


app.include_router(router)
app.include_router(sessions_router)

# 文件访问走 /files/{task_id}/... 路由(见 api/tasks.py serve_file),
# 按任务下载目录做越权校验, 不再整体 mount downloads 目录
settings.download_dir.mkdir(parents=True, exist_ok=True)

# 生产模式: 若前端已构建, 直接托管(必须最后挂载, 让 API 路由优先)
_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
