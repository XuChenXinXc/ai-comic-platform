from typing import Annotated

from fastapi import Depends

from app.db import Database, get_database
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository
from app.jobs.runner import job_runner
from app.services.asset_service import AssetService
from app.services.audio_service import AudioService
from app.services.chapter_service import ChapterService
from app.services.composition_service import CompositionService
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.shot_service import ShotService
from app.services.subtitle_service import SubtitleService
from app.services.writing_service import WritingService


def get_project_service(
    database: Annotated[Database, Depends(get_database)],
) -> ProjectService:
    return ProjectService(database)


def get_chapter_service(
    database: Annotated[Database, Depends(get_database)],
) -> ChapterService:
    return ChapterService(database)


def get_job_service(
    database: Annotated[Database, Depends(get_database)],
) -> JobService:
    return JobService(database)


def get_writing_service(
    database: Annotated[Database, Depends(get_database)],
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> WritingService:
    return WritingService(database, job_service)


def get_asset_service(
    database: Annotated[Database, Depends(get_database)],
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> AssetService:
    return AssetService(database, job_service)


def get_shot_service(
    database: Annotated[Database, Depends(get_database)],
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> ShotService:
    return ShotService(database, job_service)


def get_media_service(
    database: Annotated[Database, Depends(get_database)],
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> MediaService:
    return MediaService(database, job_service)


def get_provider_service(
    database: Annotated[Database, Depends(get_database)],
) -> ProviderService:
    return ProviderService(database)


def get_setting_service(
    database: Annotated[Database, Depends(get_database)],
) -> SettingService:
    return SettingService(database)


def get_audio_service(
    database: Annotated[Database, Depends(get_database)],
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> AudioService:
    return AudioService(database, job_service)


def get_subtitle_service(
    database: Annotated[Database, Depends(get_database)],
) -> SubtitleService:
    return SubtitleService(database)


def get_composition_service(
    database: Annotated[Database, Depends(get_database)],
    job_service: Annotated[JobService, Depends(get_job_service)],
) -> CompositionService:
    return CompositionService(database, job_service)


def get_dynamic_workflow_service(
    database: Annotated[Database, Depends(get_database)],
) -> DynamicWorkflowService:
    job_bridge = DynamicJobBridge(JobRepository(database), enqueue=job_runner.enqueue)
    return DynamicWorkflowService(database, job_bridge=job_bridge)
