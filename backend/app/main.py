from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePath

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api import assets, jobs, media, projects, providers, shots, writing
from app.config import get_settings
from app.db import db, init_db
from app.jobs.runner import job_runner
from app.services.errors import ServiceError
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
):
    app.include_router(api_router, prefix="/api")

app.mount(
    "/media",
    StaticFiles(directory=settings.projects_dir, check_dir=False),
    name="media",
)

if settings.frontend_dist_dir.is_dir():
    app.mount(
        "/",
        SPAStaticFiles(directory=settings.frontend_dist_dir, html=True),
        name="frontend",
    )
