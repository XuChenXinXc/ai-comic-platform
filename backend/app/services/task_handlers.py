from app.db import Database, db
from app.jobs.runner import JobRunner, job_runner
from app.services.asset_service import AssetService
from app.services.composition_service import CompositionService
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.shot_service import ShotService
from app.services.writing_service import WritingService


def register_handlers(
    *,
    database: Database | None = None,
    runner: JobRunner | None = None,
) -> None:
    current_database = database or db
    current_runner = runner or job_runner
    jobs = JobService(
        current_database,
        repository=current_runner.repository,
        runner=current_runner,
    )
    writing = WritingService(current_database, jobs)
    assets = AssetService(current_database, jobs)
    shots = ShotService(current_database, jobs)
    media = MediaService(current_database, jobs)
    composition = CompositionService(current_database, jobs)

    handlers = {
        "analyze_novel": writing.handle_analysis,
        "generate_screenplay": writing.handle_screenplay,
        "generate_screenplay_scene": writing.handle_screenplay_scene,
        "extract_assets": assets.handle_extract,
        "generate_asset_prompt": assets.handle_prompt,
        "extract_shots": shots.handle_extract,
        "generate_shot_image_prompt": shots.handle_image_prompt,
        "generate_shot_video_prompt": shots.handle_video_prompt,
        "generate_shot_director_plan": shots.handle_director_plan,
        "optimize_shot_video_prompts": shots.handle_optimize_video_prompts,
        "check_shot_continuity": shots.handle_continuity_check,
        "fix_shot_quality": shots.handle_quality_fix,
        "refine_prompts_continuity": shots.handle_refine_prompts,
        "generate_asset_image": media.handle_asset_image,
        "generate_keyframe": media.handle_keyframe,
        "generate_video": media.handle_video,
        "check_keyframe_quality": media.handle_keyframe_qa,
        "run_shot_pipeline": media.handle_shot_pipeline,
        "run_shot_pipeline_media": media.handle_shot_pipeline_media,
        "run_shot_pipeline_videos": media.handle_shot_pipeline_videos,
        "compose_chapter": composition.handle_compose,
    }
    for job_type, handler in handlers.items():
        current_runner.register_handler(job_type, handler)
