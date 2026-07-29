from typing import Annotated

from fastapi import Depends

from app.db import Database, get_database
from app.services.asset_service import AssetService
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.provider_service import ProviderService
from app.services.shot_service import ShotService
from app.services.writing_service import WritingService


def get_project_service(
    database: Annotated[Database, Depends(get_database)],
) -> ProjectService:
    return ProjectService(database)


def get_job_service(
    database: Annotated[Database, Depends(get_database)],
) -> JobService:
    return JobService(database)


def get_writing_service(
    database: Annotated[Database, Depends(get_database)],
) -> WritingService:
    return WritingService(database, JobService(database))


def get_asset_service(
    database: Annotated[Database, Depends(get_database)],
) -> AssetService:
    return AssetService(database, JobService(database))


def get_shot_service(
    database: Annotated[Database, Depends(get_database)],
) -> ShotService:
    return ShotService(database, JobService(database))


def get_media_service(
    database: Annotated[Database, Depends(get_database)],
) -> MediaService:
    return MediaService(database, JobService(database))


def get_provider_service(
    database: Annotated[Database, Depends(get_database)],
) -> ProviderService:
    return ProviderService(database)
