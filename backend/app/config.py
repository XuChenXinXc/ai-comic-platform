from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_WORKSPACE_DIR = PROJECT_ROOT / "workspace"
DEFAULT_DATABASE_URL = f"sqlite:///{(DEFAULT_WORKSPACE_DIR / 'platform.db').as_posix()}"


def load_project_environment(env_file: Path = PROJECT_ROOT / ".env") -> None:
    load_dotenv(dotenv_path=env_file, override=False)


load_project_environment()


class Settings(BaseSettings):
    app_name: str = "AI 漫剧本地平台"
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("APP_HOST", "HOST"),
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("APP_PORT", "PORT"),
    )
    debug: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_DEBUG", "DEBUG"),
    )
    database_url: str = Field(
        default="",
        validation_alias=AliasChoices("APP_DATABASE_URL", "DATABASE_URL"),
    )
    workspace_dir: Path = Field(
        default=DEFAULT_WORKSPACE_DIR,
        validation_alias=AliasChoices(
            "APP_WORKSPACE",
            "APP_WORKSPACE_DIR",
            "WORKSPACE_DIR",
        ),
    )
    frontend_dist_dir: Path = PROJECT_ROOT / "frontend" / "dist"
    cors_origins: str = Field(
        default="http://127.0.0.1:3000,http://localhost:3000",
        validation_alias=AliasChoices("APP_CORS_ORIGINS", "CORS_ORIGINS"),
    )
    text_workers: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("TEXT_WORKERS", "APP_TEXT_WORKERS"),
    )
    image_workers: int = Field(
        default=2,
        ge=1,
        validation_alias=AliasChoices("IMAGE_WORKERS", "APP_IMAGE_WORKERS"),
    )
    video_workers: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("VIDEO_WORKERS", "APP_VIDEO_WORKERS"),
    )
    provider_poll_interval_sec: float = Field(default=0.25, gt=0)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def resolve_local_paths(self) -> Settings:
        if not self.workspace_dir.is_absolute():
            self.workspace_dir = (PROJECT_ROOT / self.workspace_dir).resolve()
        else:
            self.workspace_dir = self.workspace_dir.resolve()
        if not self.frontend_dist_dir.is_absolute():
            self.frontend_dist_dir = (PROJECT_ROOT / self.frontend_dist_dir).resolve()
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.workspace_dir / 'platform.db').as_posix()}"
        return self

    @property
    def projects_dir(self) -> Path:
        return self.workspace_dir / "projects"

    @property
    def worker_limits(self) -> dict[str, int]:
        return {
            "text": self.text_workers,
            "image": self.image_workers,
            "video": self.video_workers,
        }

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
