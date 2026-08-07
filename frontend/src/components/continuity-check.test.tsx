import { describe, expect, it } from "vitest";
import type { MediaVersion, Shot } from "../types/api";
import { orderedShots, playableShots } from "./continuity-check";

function shot(partial: Partial<Shot> & { id: string; shot_no: number }): Shot {
  return {
    project_id: "test-project",
    chapter_id: "test-chapter",
    shot_order: partial.shot_no,
    source_type: "screenplay",
    source_ref: "",
    source_signature: "",
    start_state: "",
    action: "",
    narration: "",
    shot_type: "",
    camera_angle: "",
    camera_move: "",
    duration_sec: 6,
    end_state: "",
    next_hook: "",
    transition_from_prev: "scene_change",
    frame_description: "",
    image_prompt: "",
    compiled_image_prompt: "",
    video_prompt: "",
    compiled_video_prompt: "",
    negative_prompt: "",
    character_ids_json: [],
    prop_ids_json: [],
    positions_json: [],
    expressions_json: [],
    dialogue_json: [],
    reference_bindings_json: [],
    status: "ready",
    own_signature: "",
    revision: 1,
    created_at: "",
    updated_at: "",
    ...partial,
  };
}

function video(partial: Partial<MediaVersion> = {}): MediaVersion {
  return {
    id: "video-1",
    version_no: 1,
    prompt_snapshot: "",
    negative_prompt_snapshot: "",
    model: "model-1",
    provider_task_id: "",
    status: "completed",
    is_current: true,
    input_signature: "",
    source: "generated",
    media_url: "/media/video-1.mp4",
    created_at: "",
    ...partial,
  };
}

describe("playableShots", () => {
  it("只保留有已完成视频的分镜，并按 shot_no 升序排列", () => {
    const shots = [
      shot({ id: "b", shot_no: 2, current_video: video({ id: "v2" }) }),
      shot({ id: "a", shot_no: 1, current_video: video({ id: "v1" }) }),
      shot({ id: "c", shot_no: 3, current_video: video({ id: "v3", status: "running" }) }),
      shot({ id: "d", shot_no: 4, current_video: null }),
    ];
    const result = playableShots(shots);
    expect(result.map((item) => item.id)).toEqual(["a", "b"]);
  });
});

describe("orderedShots", () => {
  it("按 shot_no 升序排列且保留全部镜头", () => {
    const shots = [
      shot({ id: "b", shot_no: 2 }),
      shot({ id: "a", shot_no: 1 }),
      shot({ id: "c", shot_no: 3 }),
    ];
    expect(orderedShots(shots).map((item) => item.id)).toEqual(["a", "b", "c"]);
  });
});
