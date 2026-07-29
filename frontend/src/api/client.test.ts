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
});
