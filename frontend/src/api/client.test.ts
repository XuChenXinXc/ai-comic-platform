import { afterEach, describe, expect, test, vi } from "vitest";
import { api } from "./client";

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API 契约适配", () => {
  test("test_asset_create_maps_storage_fields_to_request_schema", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "asset-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.assets.create("project-1", {
      asset_type: "character",
      name: "林昭",
      profile_json: { appearance: "短发" },
      consistency_json: ["银色发夹"],
      prompt: "角色定妆图",
      negative_prompt: "多人",
      composition: "半身正面",
      importance: "core",
      status: "ready",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toEqual({
      asset_type: "character",
      name: "林昭",
      profile: { appearance: "短发" },
      consistency_keywords: ["银色发夹"],
      prompt: "角色定妆图",
      negative_prompt: "多人",
      composition: "半身正面",
      importance: "core",
      status: "ready",
    });
    expect(body).not.toHaveProperty("profile_json");
  });

  test("test_shot_update_maps_json_aliases_and_strips_readonly_fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "shot-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.shots.update("shot-1", {
      id: "shot-1",
      project_id: "project-1",
      shot_no: 8,
      shot_order: 8,
      scene_no: 3,
      source_type: "novel",
      source_ref: "scene:2|paragraph-4",
      character_ids_json: ["char-1"],
      prop_ids_json: ["prop-1"],
      positions_json: [{ character_id: "char-1", side: "left" }],
      expressions_json: [],
      dialogue_json: [],
      reference_bindings_json: [],
      action: "抬头",
      duration_sec: 4,
      status: "ready",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.source_ref).toBe("scene:3|paragraph-4");
    expect(body.character_ids).toEqual(["char-1"]);
    expect(body.prop_ids).toEqual(["prop-1"]);
    expect(body).not.toHaveProperty("character_ids_json");
    expect(body).not.toHaveProperty("shot_no");
    expect(body).not.toHaveProperty("project_id");
    expect(body).not.toHaveProperty("source_type");
  });

  test("test_shot_partial_update_without_source_fields_preserves_source_ref", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "shot-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.shots.update("shot-1", {
      image_prompt_override: "用户编辑后的最终关键帧提示词",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toEqual({
      image_prompt_override: "用户编辑后的最终关键帧提示词",
    });
  });

  test("test_provider_update_does_not_send_immutable_kind", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "provider-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.providers.update("provider-1", {
      kind: "video",
      name: "视频模型",
      adapter: "http_video",
      params: { duration: 6 },
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toMatchObject({
      name: "视频模型",
      adapter: "http_video",
      params: { duration: 6 },
    });
    expect(body).not.toHaveProperty("kind");
  });

  test("test_provider_update_with_direct_api_key_sends_api_key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "provider-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.providers.update("provider-1", {
      api_key: "direct-provider-secret",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.api_key).toBe("direct-provider-secret");
  });

  test("test_provider_update_with_blank_api_key_omits_api_key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "provider-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.providers.update("provider-1", { api_key: "" });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).not.toHaveProperty("api_key");
  });

  test("test_project_storage_get_requests_settings_endpoint", async () => {
    const payload = {
      path: "D:\\漫画项目",
      source: "database" as const,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.settings.projectStorage.get();

    expect(result).toEqual(payload);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/settings/project-storage",
    );
  });

  test("test_project_storage_select_posts_without_body", async () => {
    const payload = {
      selected: false,
      path: "D:\\漫画项目",
      source: "database" as const,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.settings.projectStorage.select();

    expect(result).toEqual(payload);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/settings/project-storage/select",
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
  });
});
