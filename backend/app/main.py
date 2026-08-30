import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePath

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api import (
    assets,
    audio,
    chapters,
    composition,
    dynamic_runs,
    jobs,
    media,
    projects,
    providers,
    shots,
    writing,
)
from app.api import settings as settings_api
from app.config import get_settings
from app.db import Database, db, init_db
from app.jobs.recovery_all import recover_all_once
from app.jobs.repository import JobRepository
from app.jobs.runner import job_runner
from app.models.tables import project
from app.services.errors import ServiceError
from app.services.ffmpeg_utils import shutdown_ffmpeg_executor
from app.services.file_service import file_service, is_reparse_point
from app.services.setting_service import get_imagbb_api_key_override
from app.services.task_handlers import register_handlers
from app.services.utils import utc_now


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        normalized_path = PurePath(path).as_posix().strip("/")
        # /assets 同时是前端页面与 Vite 静态目录，精确路径应进入 SPA。
        if scope["method"] in {"GET", "HEAD"} and normalized_path == "assets":
            return await super().get_response("index.html", scope)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (
                exc.status_code != 404
                or scope["method"] not in {"GET", "HEAD"}
                or normalized_path.split("/", 1)[0] in {"api", "assets", "media"}
                or PurePath(normalized_path).suffix
            ):
                raise
            return await super().get_response("index.html", scope)


class ProjectMediaStaticFiles(StaticFiles):
    def __init__(
        self,
        directory: str | Path,
        *,
        database: Database | None = None,
        check_dir: bool = True,
    ) -> None:
        self.database = database or db
        super().__init__(directory=directory, check_dir=check_dir)

    async def get_response(self, path: str, scope: Scope) -> Response:
        normalized_path = PurePath(path).as_posix().strip("/")
        parts = normalized_path.split("/") if normalized_path else []
        # 加强路径验证：检查是否包含 ".." 或其他可疑模式
        if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
            raise StarletteHTTPException(status_code=404)
        # 额外检查：确保原始路径中不包含 ".."（防止编码绕过）
        if ".." in path:
            raise StarletteHTTPException(status_code=404)
        project_id = parts[0]
        relative_path = Path(*parts[1:])
        if relative_path.is_absolute():
            raise StarletteHTTPException(status_code=404)
        with self.database.engine.connect() as connection:
            record = (
                connection.execute(
                    select(
                        project.c.storage_root,
                        project.c.storage_name,
                        project.c.storage_layout,
                    ).where(project.c.id == project_id)
                )
                .mappings()
                .first()
            )
        if record is None:
            raise StarletteHTTPException(status_code=404)

        stored_root = str(record["storage_root"] or "")
        storage_root = Path(stored_root) if stored_root else Path(self.directory)
        if not storage_root.is_absolute():
            raise StarletteHTTPException(status_code=404)
        # 存储根目录本身不能是符号链接/目录联接（resolve 后再查会查不到）
        if is_reparse_point(Path(stored_root)):
            raise StarletteHTTPException(status_code=404)
        storage_root = storage_root.resolve()
        layout = str(record["storage_layout"] or "prefixed")
        if layout == "categorized":
            storage_name = str(record["storage_name"] or "")
        elif layout == "prefixed":
            storage_name = project_id
        else:
            raise StarletteHTTPException(status_code=404)
        project_candidate = storage_root / storage_name
        # 项目目录本身不能是符号链接/目录联接
        if is_reparse_point(project_candidate):
            raise StarletteHTTPException(status_code=404)
        project_root = project_candidate.resolve()
        # 使用 is_relative_to 检查路径包含关系，防止符号链接穿越
        if not storage_name or not project_root.is_relative_to(storage_root):
            raise StarletteHTTPException(status_code=404)
        requested_path = (project_root / relative_path).resolve()
        try:
            requested_path.relative_to(project_root)
        except ValueError as exc:
            raise StarletteHTTPException(status_code=404) from exc
        project_files = StaticFiles(directory=project_root, check_dir=False)
        return await project_files.get_response(relative_path.as_posix(), scope)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db(seed_mock_providers=True)
    # ImgBB 是平台级辅助密钥，优先从加密的 platform_setting 读取，
    # 避免明文密钥在 .env 中长期存在。空值时回退到 settings.imgbb_api_key（环境变量）。
    override_key = get_imagbb_api_key_override(db)
    if override_key:
        settings.imgbb_api_key = override_key
    file_service.migrate_categorized_media_layouts(strict=False)
    register_handlers()
    job_runner.start(recover=False)
    repository = JobRepository(db)
    for job_id in recover_all_once(repository):
        job_runner.enqueue(job_id)
    try:
        yield
    finally:
        job_runner.shutdown()
        # 等待在途 ffmpeg/ffprobe 任务结束后再释放数据库连接。
        shutdown_ffmpeg_executor()
        db.dispose()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ServiceError)
async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/api/health", tags=["system"])
def health() -> dict[str, object]:
    """系统健康检查：数据库 / FFmpeg / 磁盘 / 活跃 Job 状态。

    任一关键依赖异常时返回 ``status: unhealthy``，FFmpeg 失败时为 ``degraded``。
    任何异常仅记录在 checks 字段内，HTTP 状态码统一 200，由调用方根据 status 判断。
    """
    checks: dict[str, dict[str, object]] = {}
    overall = "healthy"

    # 1) 数据库
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = {"status": "healthy"}
    except Exception as exc:  # noqa: BLE001 — 健康检查吞所有异常
        checks["database"] = {"status": "unhealthy", "error": str(exc)}
        overall = "unhealthy"

    # 2) FFmpeg（degraded，不影响整体健康）
    try:
        result = subprocess.run(
            [settings.ffmpeg_path, "-version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exit={result.returncode}: {result.stderr.decode(errors='replace')[:200]}"
            )
        first_line = (result.stdout or b"").decode(errors="replace").splitlines()[0]
        checks["ffmpeg"] = {"status": "healthy", "version": first_line}
    except Exception as exc:  # noqa: BLE001
        checks["ffmpeg"] = {"status": "unhealthy", "error": str(exc)}
        if overall == "healthy":
            overall = "degraded"

    # 3) 磁盘空间（工作区所在盘）
    try:
        usage = shutil.disk_usage(settings.workspace_dir)
        free_gb = usage.free / (1024**3)
        if free_gb < 0.5:
            disk_status = "unhealthy"
            overall = "unhealthy"
        elif free_gb < 1.0:
            disk_status = "warning"
            if overall == "healthy":
                overall = "degraded"
        else:
            disk_status = "healthy"
        # 不返回 workspace 绝对路径：/api/health 无鉴权，路径属于不必要的信息泄露。
        checks["disk"] = {
            "status": disk_status,
            "free_gb": round(free_gb, 2),
        }
    except Exception as exc:  # noqa: BLE001
        checks["disk"] = {"status": "unknown", "error": str(exc)}

    # 4) Worker 状态
    try:
        active_jobs = job_runner.active_job_ids()
        checks["workers"] = {
            "status": "healthy",
            "active_jobs": len(active_jobs),
            "limits": job_runner.worker_limits,
        }
    except Exception as exc:  # noqa: BLE001
        checks["workers"] = {"status": "unhealthy", "error": str(exc)}
        overall = "unhealthy"

    return {
        "status": overall,
        "timestamp": utc_now(),
        "checks": checks,
    }


for api_router in (
    projects.router,
    chapters.router,
    writing.router,
    assets.router,
    shots.router,
    media.router,
    providers.router,
    jobs.router,
    settings_api.router,
    audio.router,
    composition.router,
    dynamic_runs.router,
):
    app.include_router(api_router, prefix="/api")

app.mount(
    "/media",
    ProjectMediaStaticFiles(
        directory=settings.projects_dir,
        database=db,
        check_dir=False,
    ),
    name="media",
)

if settings.frontend_dist_dir.is_dir():
    app.mount(
        "/",
        SPAStaticFiles(directory=settings.frontend_dist_dir, html=True),
        name="frontend",
    )
