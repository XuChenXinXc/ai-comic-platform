import { beforeEach, describe, expect, test, vi } from "vitest";
import type { ApiProvider, Asset, Shot } from "../types/api";
import {
  customShotPayload,
  draftFromFreeShot,
  applyFreeSceneDefault,
  emptyFreeShotDraft,
  isTianyueVideoProvider,
  firstSceneAssetId,
  isSlotDurationValid,
  nextFreeProjectName,
  nextShotTransition,
  runFreeProjectBootstrap,
  selectedAssetContext,
} from "./CustomShotsPage";

// 仅 mock 自由项目迁移单飞用到的接口；其余测试不触发 api 调用
const freeBootstrapMocks = vi.hoisted(() => ({
  projectsList: vi.fn(),
  projectsUpdate: vi.fn(),
  projectsCreate: vi.fn(),
  chaptersList: vi.fn(),
  chaptersCreate: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    projects: {
      list: freeBootstrapMocks.projectsList,
      update: freeBootstrapMocks.projectsUpdate,
      create: freeBootstrapMocks.projectsCreate,
    },
    chapters: {
      list: freeBootstrapMocks.chaptersList,
      create: freeBootstrapMocks.chaptersCreate,
    },
  },
  mediaUrl: vi.fn(),
  queryKeys: {
    shots: (id: string) => ["shots", id],
    assets: (id: string) => ["assets", id],
  },
}));

function makeAsset(
  id: string,
  assetType: Asset["asset_type"],
  name: string,
  prompt: string,
): Asset {
  return {
    id,
    asset_type: assetType,
    name,
    prompt,
  } as Asset;
}

function makeVideoProvider(baseUrl: string): ApiProvider {
  return {
    id: "video-provider",
    kind: "video",
    name: "视频模型",
    adapter: "http_video",
    base_url: baseUrl,
    api_key_env: "",
    api_key_configured: true,
    api_key_source: "direct",
    model: "C-sd2-video-mini-15s",
    params: {},
    timeout_sec: 900,
    max_retries: 0,
    enabled: true,
    is_default: false,
    created_at: "",
    updated_at: "",
  };
}

test("test_free_create_detects_tianyue_fixed_15s_video_provider", () => {
  expect(
    isTianyueVideoProvider(
      makeVideoProvider("https://api.tianyue.xyz/v1"),
    ),
  ).toBe(true);
  expect(
    isTianyueVideoProvider(
      makeVideoProvider("https://api.apib.ai/v1"),
    ),
  ).toBe(false);
});

test("test_free_create_context_orders_scene_character_prop", () => {
  const scene = makeAsset("scene-1", "scene", "山道", "晨雾笼罩");
  const character = makeAsset("character-1", "character", "林昭", "黑色短发");
  const prop = makeAsset("prop-1", "prop", "匕首", "银色短柄");

  const result = selectedAssetContext(
    [character, prop, scene],
    "scene-1",
    ["character-1"],
    ["prop-1"],
  );

  expect(result).toBe(
    [
      "场景「山道」设定：晨雾笼罩",
      "角色「林昭」设定：黑色短发",
      "物品「匕首」设定：银色短柄",
    ].join("\n"),
  );
});

test("test_free_create_context_uses_name_fallback_when_prompt_is_empty", () => {
  const character = makeAsset("character-1", "character", "林昭", "  ");

  const result = selectedAssetContext([character], "", ["character-1"], []);

  expect(result).toBe("角色「林昭」设定：以名称为准");
});

test("test_free_create_context_ignores_unselected_assets", () => {
  const scene = makeAsset("scene-1", "scene", "山道", "晨雾笼罩");
  const character = makeAsset("character-1", "character", "林昭", "黑色短发");

  const result = selectedAssetContext([scene, character], "", [], []);

  expect(result).toBe("");
});

test("test_free_create_default_scene_is_empty_without_scene_assets", () => {
  expect(firstSceneAssetId([])).toBe("");
  expect(applyFreeSceneDefault(emptyFreeShotDraft(), "", false).sceneAssetId).toBe(
    "",
  );
});

test("test_free_create_default_scene_uses_the_only_scene_asset", () => {
  const scene = makeAsset("scene-1", "scene", "山道", "晨雾笼罩");

  expect(firstSceneAssetId([scene])).toBe("scene-1");
  expect(applyFreeSceneDefault(emptyFreeShotDraft(), "scene-1", false).sceneAssetId).toBe(
    "scene-1",
  );
});

test("test_free_create_default_scene_uses_the_first_scene_asset", () => {
  const firstScene = makeAsset("scene-1", "scene", "山道", "晨雾笼罩");
  const secondScene = makeAsset("scene-2", "scene", "庭院", "落日余晖");

  expect(firstSceneAssetId([firstScene, secondScene])).toBe("scene-1");
  expect(
    applyFreeSceneDefault(emptyFreeShotDraft(), "scene-1", false).sceneAssetId,
  ).toBe("scene-1");
});

test("test_free_create_default_scene_preserves_existing_or_explicit_state", () => {
  const selected = { ...emptyFreeShotDraft(), sceneAssetId: "scene-2" };
  const filled = { ...emptyFreeShotDraft(), imagePrompt: "主角走向大门" };

  expect(applyFreeSceneDefault(selected, "scene-1", false)).toBe(selected);
  expect(applyFreeSceneDefault(emptyFreeShotDraft(), "scene-1", true)).toEqual(
    emptyFreeShotDraft(),
  );
  expect(applyFreeSceneDefault(filled, "scene-1", false)).toBe(filled);
});

test("test_free_create_shot_payload_binds_selected_asset_ids", () => {
  const payload = customShotPayload({
    sceneAssetId: "scene-1",
    characterIds: ["character-1"],
    propIds: ["prop-1"],
    duration: 6,
    imagePrompt: " 主角走向大门 ",
    videoPrompt: "镜头缓慢推进",
    assetContext: "场景「山道」设定：晨雾笼罩",
  });

  expect(payload.source_ref).toBe("manual");
  expect(payload.scene_asset_id).toBe("scene-1");
  expect(payload.character_ids).toEqual(["character-1"]);
  expect(payload.prop_ids).toEqual(["prop-1"]);
  expect(payload.image_prompt_override).toBe(
    "场景「山道」设定：晨雾笼罩\n\n主角走向大门",
  );
  expect(payload.video_prompt_override).toBe(
    "场景「山道」设定：晨雾笼罩\n\n镜头缓慢推进",
  );
  expect(payload.transition_from_prev).toBe("scene_change");
});

test("test_free_create_shot_payload_omits_unselected_scene_asset", () => {
  const payload = customShotPayload({
    sceneAssetId: "",
    characterIds: [],
    propIds: [],
    duration: 8,
    imagePrompt: "空镜",
    videoPrompt: "缓慢拉远",
    assetContext: "",
  });

  expect(payload.scene_asset_id).toBeUndefined();
  expect(payload.character_ids).toEqual([]);
  expect(payload.prop_ids).toEqual([]);
  expect(payload.image_prompt_override).toBe("空镜");
});

test("test_free_create_shot_payload_supports_continuous_transition", () => {
  const payload = customShotPayload({
    sceneAssetId: "",
    characterIds: [],
    propIds: [],
    duration: 6,
    imagePrompt: "推门",
    videoPrompt: "推开木门",
    assetContext: "",
    transitionFromPrev: "continuous",
  });

  expect(payload.transition_from_prev).toBe("continuous");
});

test("test_free_create_shot_transition_is_continuous_after_first_shot", () => {
  expect(nextShotTransition(0)).toBe("scene_change");
  expect(nextShotTransition(1)).toBe("continuous");
  expect(nextShotTransition(5)).toBe("continuous");
});

test("test_free_shot_draft_strips_merged_asset_context", () => {
  const scene = makeAsset("scene-1", "scene", "山道", "晨雾笼罩");
  const context = selectedAssetContext([scene], "scene-1", [], []);
  const shot = {
    scene_asset_id: "scene-1",
    character_ids: [],
    prop_ids: [],
    image_prompt_override: `${context}\n\n主角走向大门`,
    video_prompt_override: `${context}\n\n镜头缓慢推进`,
    duration_sec: 6,
  } as unknown as Shot;

  const draft = draftFromFreeShot(shot, [scene]);

  expect(draft.imagePrompt).toBe("主角走向大门");
  expect(draft.videoPrompt).toBe("镜头缓慢推进");
  expect(draft.sceneAssetId).toBe("scene-1");
});

test("test_free_create_project_name_is_unique", () => {
  expect(nextFreeProjectName([])).toBe("自由制作");
  expect(nextFreeProjectName(["自由制作"])).toBe("自由制作 2");
  expect(nextFreeProjectName(["自由制作", "自由制作 2"])).toBe("自由制作 3");
});

describe("自由制作分镜位时长校验统一", () => {
  test("test_free_create_slot_duration_valid_accepts_valid_values", () => {
    expect(isSlotDurationValid(5)).toBe(true);
    expect(isSlotDurationValid(6)).toBe(true);
    expect(isSlotDurationValid(12.5)).toBe(true);
  });

  test("test_free_create_slot_duration_valid_rejects_invalid_values", () => {
    // NaN 等非法数值与过短时长统一视为非法：批量跳过该槽且保存禁用
    expect(isSlotDurationValid(NaN)).toBe(false);
    expect(isSlotDurationValid(Infinity)).toBe(false);
    expect(isSlotDurationValid(-Infinity)).toBe(false);
    expect(isSlotDurationValid(4)).toBe(false);
    expect(isSlotDurationValid(0)).toBe(false);
  });
});

describe("自由项目迁移单飞", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem("free-create-project-id");
    localStorage.removeItem("free-create-project-ids");
  });

  test("test_free_project_bootstrap_concurrent_calls_share_one_execution", async () => {
    // 存在待打标旧项目且没有自由项目，迁移后会触发一次创建
    freeBootstrapMocks.projectsList.mockImplementation(
      async (options?: { kind?: string }) =>
        options?.kind === "free"
          ? []
          : [{ id: "legacy-1", name: "手动分镜", is_free_create: false }],
    );
    freeBootstrapMocks.projectsUpdate.mockResolvedValue(undefined);
    freeBootstrapMocks.projectsCreate.mockResolvedValue({
      id: "free-new",
      name: "自由制作",
      is_free_create: true,
    });
    freeBootstrapMocks.chaptersList.mockResolvedValue([]);
    freeBootstrapMocks.chaptersCreate.mockResolvedValue({ id: "chap-new" });

    const first = runFreeProjectBootstrap();
    const second = runFreeProjectBootstrap();
    // 并发调用复用同一次执行，避免重复创建「自由制作」项目
    expect(second).toBe(first);

    const result = await first;
    expect(result.projectId).toBe("free-new");
    expect(result.chapterId).toBe("chap-new");
    expect(freeBootstrapMocks.projectsCreate).toHaveBeenCalledTimes(1);

    // 执行结束后清空 in-flight，后续挂载仍按原语义重新执行
    const third = runFreeProjectBootstrap();
    expect(third).not.toBe(first);
    await third;
    // 重新执行时创建接口再次被调用（各自独立执行）
    expect(freeBootstrapMocks.projectsCreate).toHaveBeenCalledTimes(2);
  });
});
