from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_WORKSPACE_DIR = PROJECT_ROOT / "workspace"
DEFAULT_DATABASE_URL = f"sqlite:///{(DEFAULT_WORKSPACE_DIR / 'platform.db').as_posix()}"
# 为项目 ID、最短目录名和素材文件层级预留 Windows MAX_PATH 空间。
_MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS = 137


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
        default=8012,
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
    project_storage_dir: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("APP_PROJECTS_DIR", "PROJECTS_DIR"),
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
    ffmpeg_path: str = Field(
        default="ffmpeg",
        validation_alias=AliasChoices("APP_FFMPEG_PATH", "FFMPEG_PATH"),
    )
    ffprobe_path: str = Field(
        default="ffprobe",
        validation_alias=AliasChoices("APP_FFPROBE_PATH", "FFPROBE_PATH"),
    )
    imgbb_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("APP_IMGBB_API_KEY", "IMGBB_API_KEY"),
    )

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("project_storage_dir", mode="before")
    @classmethod
    def empty_project_storage_dir_uses_default(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cors_origins", mode="after")
    @classmethod
    def reject_wildcard_cors(cls, value: str) -> str:
        for item in (part.strip() for part in value.split(",")):
            if item == "*":
                raise ValueError(
                    "APP_CORS_ORIGINS 不允许通配符 *。\n"
                    "多个来源用逗号分隔，例如：\n"
                    "  APP_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,https://app.example.com"
                )
        return value

    @model_validator(mode="after")
    def resolve_local_paths(self) -> Settings:
        if not self.workspace_dir.is_absolute():
            self.workspace_dir = (PROJECT_ROOT / self.workspace_dir).resolve()
        else:
            self.workspace_dir = self.workspace_dir.resolve()
        if self.project_storage_dir is not None:
            if not self.project_storage_dir.is_absolute():
                self.project_storage_dir = (PROJECT_ROOT / self.project_storage_dir).resolve()
            else:
                self.project_storage_dir = self.project_storage_dir.resolve()
            if self.project_storage_dir == Path(self.project_storage_dir.anchor):
                raise ValueError("APP_PROJECTS_DIR 不能是磁盘根目录")
            if (
                os.name == "nt"
                and len(str(self.project_storage_dir).encode("utf-16-le")) // 2
                > _MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS
            ):
                raise ValueError("APP_PROJECTS_DIR 路径过深，请改用更短的目录")
            for protected_dir in (PROJECT_ROOT.resolve(), self.workspace_dir):
                try:
                    protected_dir.relative_to(self.project_storage_dir)
                except ValueError:
                    continue
                raise ValueError("APP_PROJECTS_DIR 不能是平台源码、工作区或其上级目录")
            for source_dir in (
                BACKEND_DIR.resolve(),
                (PROJECT_ROOT / "frontend").resolve(),
                (PROJECT_ROOT / ".git").resolve(),
            ):
                try:
                    self.project_storage_dir.relative_to(source_dir)
                except ValueError:
                    continue
                raise ValueError("APP_PROJECTS_DIR 不能指向平台源码目录")
        if not self.frontend_dist_dir.is_absolute():
            self.frontend_dist_dir = (PROJECT_ROOT / self.frontend_dist_dir).resolve()
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.workspace_dir / 'platform.db').as_posix()}"
        return self

    @property
    def projects_dir(self) -> Path:
        return self.project_storage_dir or (self.workspace_dir / "projects").resolve()

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
