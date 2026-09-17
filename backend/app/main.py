import logging
import shutil
import subprocess
import time
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
from app.jobs.runner import job_runner
from app.models.tables import project
from app.providers.base import set_imgbb_api_key_override
from app.services.errors import ServiceError
from app.services.ffmpeg_utils import shutdown_ffmpeg_executor
from app.services.file_service import file_service, is_reparse_point
from app.services.setting_service import get_imgbb_api_key_override
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


logger = logging.getLogger(__name__)

# B-L10：project_id → 项目媒体根目录的进程内缓存（TTL + 容量上限）。
# /media 每个请求都要按 project_id 查询数据库解析存储根目录，热门项目的
# 图片/视频加载会放大为等量 DB 查询；缓存命中时跳过查询，命中后仍校验
# 目录存在，项目删除（目录被移除）后自动失效回源重查。
_MEDIA_ROOT_CACHE_TTL_SEC = 30.0
_MEDIA_ROOT_CACHE_MAX_ENTRIES = 256


class ProjectMediaStaticFiles(StaticFiles):
    def __init__(
        self,
        directory: str | Path,
        *,
        database: Database | None = None,
        check_dir: bool = True,
    ) -> None:
        self.database = database or db
        # project_id -> (缓存时间戳, 已解析的项目根目录绝对路径)
        self._root_cache: dict[str, tuple[float, Path]] = {}
        # 已解析项目根目录 -> StaticFiles 实例：避免每个 /media 请求都新建实例。
        self._files_cache: dict[Path, StaticFiles] = {}
        super().__init__(directory=directory, check_dir=check_dir)

    def _files_app(self, project_root: Path) -> StaticFiles:
        """按项目根目录复用 StaticFiles 实例（随根目录缓存容量一并清空）。"""
        cached = self._files_cache.get(project_root)
        if cached is not None:
            return cached
        if len(self._files_cache) >= _MEDIA_ROOT_CACHE_MAX_ENTRIES:
            self._files_cache.clear()
        app = StaticFiles(directory=project_root, check_dir=False)
        self._files_cache[project_root] = app
        return app

    def _resolve_project_root(self, project_id: str) -> Path | None:
        """解析并缓存项目媒体根目录；失败返回 ``None``（按 404 处理）。"""
        now = time.monotonic()
        cached = self._root_cache.get(project_id)
        if cached is not None:
            cached_at, cached_root = cached
            # TTL 内且目录仍存在（项目删除会移除目录）才信任缓存。
            if now - cached_at < _MEDIA_ROOT_CACHE_TTL_SEC and cached_root.is_dir():
                return cached_root
            self._root_cache.pop(project_id, None)

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
            return None

        stored_root = str(record["storage_root"] or "")
        storage_root = Path(stored_root) if stored_root else Path(self.directory)
        if not storage_root.is_absolute():
            return None
        # 存储根目录本身不能是符号链接/目录联接（resolve 后再查会查不到）
        if is_reparse_point(Path(stored_root)):
            return None
        storage_root = storage_root.resolve()
        layout = str(record["storage_layout"] or "prefixed")
        if layout == "categorized":
            storage_name = str(record["storage_name"] or "")
        elif layout == "prefixed":
            storage_name = project_id
        else:
            return None
        project_candidate = storage_root / storage_name
        # 项目目录本身不能是符号链接/目录联接
        if is_reparse_point(project_candidate):
            return None
        resolved_root = project_candidate.resolve()
        # 使用 is_relative_to 检查路径包含关系，防止符号链接穿越
        if not storage_name or not resolved_root.is_relative_to(storage_root):
            return None

        # 简单容量上限：超过容量时整体清空（构建成本远低于 LRU 复杂度）。
        if len(self._root_cache) >= _MEDIA_ROOT_CACHE_MAX_ENTRIES:
            self._root_cache.clear()
        self._root_cache[project_id] = (now, resolved_root)
        return resolved_root

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

        project_root = self._resolve_project_root(project_id)
        if project_root is None:
            raise StarletteHTTPException(status_code=404)
        requested_path = (project_root / relative_path).resolve()
        try:
            requested_path.relative_to(project_root)
        except ValueError as exc:
            raise StarletteHTTPException(status_code=404) from exc
        project_files = self._files_app(project_root)
        response = await project_files.get_response(relative_path.as_posix(), scope)
        # 当前版本媒体文件复用固定存储路径（URL 不变，仅文件内容被替换）：
        # 浏览器对相同 URL 的启发式缓存会让上传/生成新图后仍显示旧内容。
        # 强制协商缓存：保留 ETag/Last-Modified（未变化时 304 零内容传输），
        # 但每次请求都必须重新验证，确保新内容及时生效。
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db(seed_mock_providers=True)
    # ImgBB 是平台级辅助密钥，优先从加密的 platform_setting 读取，
    # 避免明文密钥在 .env 中长期存在。空值时回退到 settings.imgbb_api_key（环境变量）。
    # B-L10：写入独立的运行时覆盖变量，不再直接改写 lru_cache 单例 settings。
    override_key = get_imgbb_api_key_override(db)
    if override_key:
        set_imgbb_api_key_override(override_key)
    file_service.migrate_categorized_media_layouts(strict=False)
    register_handlers()
    # 启动期恢复失败不应让 lifespan 失败，避免服务反复启动失败形成死循环。
    try:
        job_runner.start(recover=True)
    except Exception:
        logger.exception("启动期任务恢复失败，已跳过；不影响服务启动")
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
