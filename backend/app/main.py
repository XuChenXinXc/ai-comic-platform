from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePath

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api import (
    assets,
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
from app.services.errors import ServiceError
from app.services.file_service import is_reparse_point
from app.services.task_handlers import register_handlers


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
        if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
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
        storage_root = storage_root.resolve()
        layout = str(record["storage_layout"] or "prefixed")
        if layout == "categorized":
            storage_name = str(record["storage_name"] or "")
        elif layout == "prefixed":
            storage_name = project_id
        else:
            raise StarletteHTTPException(status_code=404)
        project_candidate = storage_root / storage_name
        if is_reparse_point(project_candidate):
            raise StarletteHTTPException(status_code=404)
        project_root = project_candidate.resolve()
        if not storage_name or project_root.parent != storage_root:
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
    register_handlers()
    job_runner.start(recover=True)
    try:
        yield
    finally:
        job_runner.shutdown()
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
def health() -> dict[str, str]:
    return {"status": "ok"}


for api_router in (
    projects.router,
    writing.router,
    assets.router,
    shots.router,
    media.router,
    providers.router,
    jobs.router,
    settings_api.router,
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
