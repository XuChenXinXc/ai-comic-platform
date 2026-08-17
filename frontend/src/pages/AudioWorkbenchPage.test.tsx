import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import AudioWorkbenchPage from "./AudioWorkbenchPage";

describe("配音工作台初始化", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  test("test_audio_workbench_initial_load_error_is_visible", async () => {
    vi.spyOn(api.chapters, "get").mockRejectedValue(new Error("章节服务不可用"));
    vi.spyOn(api.audio, "extractDialogues").mockResolvedValue([]);
    vi.spyOn(api.providers, "list").mockResolvedValue([]);
    vi.spyOn(api.audio, "getConfig").mockResolvedValue({
      project_id: "project-1",
      tts_provider_id: null,
      bgm_volume: 0.3,
      dialogue_volume: 1,
      sound_effect_volume: 0.5,
    });
    vi.spyOn(api.audio, "getEdgeTTSVoices").mockResolvedValue([]);
    vi.spyOn(api.audio, "getChapterAudios").mockResolvedValue([]);

    render(
      <MemoryRouter initialEntries={["/audio/project-1/chapter-1"]}>
        <Routes>
          <Route
            path="/audio/:projectId/:chapterId"
            element={<AudioWorkbenchPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("章节服务不可用");
  });
});