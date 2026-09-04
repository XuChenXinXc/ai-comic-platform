import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { toast } from "sonner";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import AudioWorkbenchPage from "./AudioWorkbenchPage";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

/**
 * 渲染配音工作台并 mock 好基础数据：1 个对话、1 个可选 TTS Provider
 * （OpenAI 模式音色回填 "alloy"，单条生成按钮可用）。
 */
function renderWorkbench() {
  return render(
    <MemoryRouter initialEntries={["/audio/project-1/chapter-1"]}>
      <Routes>
        <Route
          path="/audio/:projectId/:chapterId"
          element={<AudioWorkbenchPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

/** 请求中返回的「排队中」配音版本（后端异步生成的初始状态）。 */
const QUEUED_AUDIO = {
  id: "audio-1",
  shot_id: "shot-1",
  dialogue_text: "你好",
  speaker_name: "甲",
  voice_id: "alloy",
  file_path: null,
  duration_sec: null,
  version_no: 1,
  status: "queued",
  is_current: 1,
  created_at: "2026-09-01T00:00:00Z",
};

function mockWorkbenchLoad() {
  vi.spyOn(api.chapters, "get").mockResolvedValue({
    id: "chapter-1",
    project_id: "project-1",
    title: "第一章",
    chapter_no: 1,
  } as never);
  vi.spyOn(api.audio, "extractDialogues").mockResolvedValue([
    { shot_no: 1, speaker: "甲", text: "你好" },
  ]);
  vi.spyOn(api.providers, "list").mockResolvedValue([
    { id: "tts-1", kind: "tts", name: "测试TTS", adapter: "openai", enabled: true },
  ] as never);
  vi.spyOn(api.audio, "getConfig").mockResolvedValue({
    project_id: "project-1",
    tts_provider_id: null,
    bgm_volume: 0.3,
    dialogue_volume: 1,
    sound_effect_volume: 0.5,
    voice_mapping: {},
  } as never);
  vi.spyOn(api.audio, "getEdgeTTSVoices").mockResolvedValue([]);
  vi.spyOn(api.shots, "list").mockResolvedValue([
    { id: "shot-1", shot_no: 1 },
  ] as never);
}

/** 音色映射持久化场景：提供 2 个 Edge TTS 声音以便 select 有可选项。 */
function mockVoiceMappingLoad(voiceMapping: Record<string, string>) {
  vi.spyOn(api.chapters, "get").mockResolvedValue({
    id: "chapter-1",
    project_id: "project-1",
    title: "第一章",
    chapter_no: 1,
  } as never);
  vi.spyOn(api.audio, "extractDialogues").mockResolvedValue([
    { shot_no: 1, speaker: "甲", text: "你好" },
  ]);
  vi.spyOn(api.providers, "list").mockResolvedValue([]);
  vi.spyOn(api.audio, "getConfig").mockResolvedValue({
    project_id: "project-1",
    tts_provider_id: null,
    bgm_volume: 0.3,
    dialogue_volume: 1,
    sound_effect_volume: 0.5,
    voice_mapping: voiceMapping,
  } as never);
  vi.spyOn(api.audio, "getEdgeTTSVoices").mockResolvedValue([
    { name: "zh-CN-A", display_name: "声音A", gender: "female", description: "" },
    { name: "zh-CN-B", display_name: "声音B", gender: "male", description: "" },
  ]);
  vi.spyOn(api.audio, "getChapterAudios").mockResolvedValue([]);
}

describe("配音工作台初始化", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
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
      voice_mapping: {},
    });
    vi.spyOn(api.audio, "getEdgeTTSVoices").mockResolvedValue([]);
    vi.spyOn(api.audio, "getChapterAudios").mockResolvedValue([]);

    renderWorkbench();

    expect(await screen.findByRole("alert")).toHaveTextContent("章节服务不可用");
  });
});

describe("配音工作台异步生成", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  test(
    "test_audio_workbench_polls_chapter_audios_until_generation_completes",
    async () => {
      mockWorkbenchLoad();
      const getChapterAudiosSpy = vi.spyOn(api.audio, "getChapterAudios");
      getChapterAudiosSpy
        .mockResolvedValueOnce([])
        .mockResolvedValue([
          {
            shot_id: "shot-1",
            shot_no: 1,
            audio: {
              id: "audio-1",
              dialogue_text: "你好",
              voice_id: "alloy",
              file_path: "audio/1.mp3",
              duration_sec: 2.1,
              status: "completed",
            },
          },
        ]);
      vi.spyOn(api.audio, "generateShotAudio").mockResolvedValue(
        QUEUED_AUDIO as never,
      );

      const { unmount } = renderWorkbench();

      const generateButton = await screen.findByText("生成配音");
      fireEvent.click(generateButton);

      // 生成接口返回 queued：状态徽标先显示「生成中」
      await screen.findByText("生成中");
      expect(getChapterAudiosSpy).toHaveBeenCalledTimes(1);

      // 轮询（2s 间隔）拿到 completed 后徽标切换为「已生成」
      await new Promise(resolve => {
        setTimeout(resolve, 3000);
      });
      expect(
        await screen.findByText("已生成", undefined, { timeout: 2000 }),
      ).toBeInTheDocument();
      expect(getChapterAudiosSpy.mock.calls.length).toBeGreaterThanOrEqual(2);

      unmount();
    },
    10000,
  );

  test("test_audio_workbench_single_generate_ignores_result_after_unmount", async () => {
    mockWorkbenchLoad();
    vi.spyOn(api.audio, "getChapterAudios").mockResolvedValue([]);
    let resolveGenerate: (value: typeof QUEUED_AUDIO) => void = () => {};
    const generatePromise = new Promise<typeof QUEUED_AUDIO>(resolve => {
      resolveGenerate = resolve;
    });
    vi.spyOn(api.audio, "generateShotAudio").mockReturnValue(
      generatePromise as never,
    );

    const { unmount } = renderWorkbench();

    const generateButton = await screen.findByText("生成配音");
    fireEvent.click(generateButton);
    await waitFor(() => expect(api.audio.generateShotAudio).toHaveBeenCalled());

    // 生成请求未返回时离开页面：结果不应再触发 setState / 成功提示
    unmount();
    await act(async () => {
      resolveGenerate(QUEUED_AUDIO);
      await generatePromise;
    });

    expect(toast.success).not.toHaveBeenCalledWith("分镜 1 配音生成成功");
  });
});

describe("配音工作台音色映射持久化", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  test("test_audio_workbench_voice_mapping_loads_from_server_config", async () => {
    mockVoiceMappingLoad({ 甲: "edge-tts:zh-CN-B" });
    const updateConfigSpy = vi.spyOn(api.audio, "updateConfig").mockResolvedValue({
      project_id: "project-1",
      tts_provider_id: null,
      bgm_volume: 0.3,
      dialogue_volume: 1,
      sound_effect_volume: 0.5,
      voice_mapping: { 甲: "edge-tts:zh-CN-B" },
    } as never);

    renderWorkbench();

    // 服务端映射覆盖默认值：select 选中服务端返回的音色
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveValue("edge-tts:zh-CN-B");
    });
    expect(updateConfigSpy).not.toHaveBeenCalled();
  });

  test("test_audio_workbench_voice_mapping_change_calls_update_config", async () => {
    mockVoiceMappingLoad({ 甲: "edge-tts:zh-CN-B" });
    vi.spyOn(api.audio, "updateConfig").mockResolvedValue({
      project_id: "project-1",
      tts_provider_id: null,
      bgm_volume: 0.3,
      dialogue_volume: 1,
      sound_effect_volume: 0.5,
      voice_mapping: { 甲: "edge-tts:zh-CN-A" },
    } as never);

    renderWorkbench();

    const select = await screen.findByRole("combobox");
    fireEvent.change(select, { target: { value: "edge-tts:zh-CN-A" } });

    // 本地即时更新
    expect(select).toHaveValue("edge-tts:zh-CN-A");
    // 并异步保存到服务端
    await waitFor(() => {
      expect(api.audio.updateConfig).toHaveBeenCalledWith("project-1", {
        voice_mapping: { 甲: "edge-tts:zh-CN-A" },
      });
    });
  });

  test("test_audio_workbench_legacy_local_storage_mapping_is_migrated_and_removed", async () => {
    // 服务端映射为空、本地存在旧 localStorage 值：应写入服务端并删除本地键
    mockVoiceMappingLoad({});
    const updateConfigSpy = vi.spyOn(api.audio, "updateConfig").mockResolvedValue({
      project_id: "project-1",
      tts_provider_id: null,
      bgm_volume: 0.3,
      dialogue_volume: 1,
      sound_effect_volume: 0.5,
      voice_mapping: { 甲: "edge-tts:zh-CN-B" },
    } as never);
    localStorage.setItem(
      "audio-voice-mapping-project-1",
      JSON.stringify({ 甲: "edge-tts:zh-CN-B" }),
    );

    renderWorkbench();

    await waitFor(() => {
      expect(updateConfigSpy).toHaveBeenCalledWith("project-1", {
        voice_mapping: { 甲: "edge-tts:zh-CN-B" },
      });
    });
    // 迁移成功后删除旧 localStorage 键
    expect(localStorage.getItem("audio-voice-mapping-project-1")).toBeNull();
    // 旧本地值在迁移期间作为初始映射生效
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toHaveValue("edge-tts:zh-CN-B");
    });
  });
});
