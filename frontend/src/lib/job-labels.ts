export const JOB_TYPE_LABELS: Record<string, string> = {
  analyze_novel: "小说分析",
  generate_screenplay: "剧本生成",
  generate_screenplay_scene: "剧本场次生成",
  extract_assets: "素材提取",
  generate_asset_prompt: "素材提示词",
  generate_asset_image: "素材图片",
  extract_shots: "分镜提取",
  generate_shot_image_prompt: "分镜图片提示词",
  generate_shot_video_prompt: "分镜视频提示词",
  generate_shot_director_plan: "分镜导演设计",
  generate_keyframe: "关键帧生成",
  generate_video: "分镜视频生成",
  optimize_shot_video_prompts: "视频提示词优化",
  refine_prompts_continuity: "提示词连续性精修",
  check_shot_continuity: "连续性审校",
  fix_shot_quality: "分镜质量修复",
  generate_dialogue_audio: "对白配音",
  generate_dynamic_subtitle: "字幕生成",
  dynamic_stage_generate: "动态工作流生成",
  compose_chapter: "章节合成",
};

export function jobTypeLabel(jobType: string) {
  return JOB_TYPE_LABELS[jobType] || jobType;
}
