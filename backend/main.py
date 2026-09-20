import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.sessions import router as sessions_router
from api.tasks import router
from core.config import settings
from core.task_manager import recover_orphans

logger = logging.getLogger("uwc")


@asynccontextmanager
async def lifespan(app):
    """启动补偿放在 lifespan 而不是模块导入时。

    ⚠️ 放模块级的话, 只要有人 `import main`(测试、脚本、文档工具)就会去扫库
    并改任务状态 —— 一个"只读地导入一下"的动作产生了写副作用, 而且用的还是
    被导入方当时的 DB_PATH。挂到 lifespan 上, 只有真正起服务才执行。
    """
    recover_orphans()
    yield


app = FastAPI(title="Universal Web Collector v10", lifespan=lifespan)


@app.get("/healthz")
def health():
    return {"status": "ok"}


def install_exception_handlers(app):
    """挂上兜底处理器。单独成函数是为了能被测试直接装配到裸 app 上 ——

    ⚠️ 不做成函数就只能 `import main` 来测, 而导入 main 会连带执行启动补偿、
    建目录、挂载前端, 测试也就跟着依赖真实磁盘了。
    """
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """兜住所有漏网的异常, 返回稳定结构而不是裸 500。

        ⚠️ 不注册的话, FastAPI/Starlette 的默认 500 响应结构随部署方式变化,
        而且在某些配置下会带上堆栈片段 —— 路径、文件名、依赖版本都泄漏给前端,
        而前端拿到一堆 HTML 也没法处理。这里统一成 `{detail, type}`:
        前端只需认识两个字段, 排查所必需的完整堆栈只进服务端日志。
        """
        logger.error(
            "unhandled error on %s %s: %s\n%s",
            request.method,
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "服务器内部错误", "type": type(exc).__name__},
        )
    return app


app = install_exception_handlers(app)

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
