import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../api/client";
import type { Chapter } from "../types/api";
import CompositionPage from "./CompositionPage";

const chapter = {
  id: "chapter-1",
  project_id: "project-1",
  chapter_no: 1,
  title: "第一章",
  source_text: "",
  source_signature: "source-1",
  source_revision: 1,
  status: "ready",
  created_at: "",
  updated_at: "",
} as Chapter;

function renderCompositionPage() {
  return render(
    <MemoryRouter initialEntries={["/composition/project-1"]}>
      <Routes>
        <Route path="/composition/:projectId" element={<CompositionPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("视频合成初始化", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  test("test_composition_initial_load_error_is_visible", async () => {
    vi.spyOn(api.chapters, "list").mockRejectedValue(new Error("章节列表不可用"));
    vi.spyOn(api.audio, "listBGMs").mockResolvedValue([]);

    renderCompositionPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("章节列表不可用");
  });

  test("test_composition_status_load_error_is_visible", async () => {
    vi.spyOn(api.chapters, "list").mockResolvedValue([chapter]);
    vi.spyOn(api.audio, "listBGMs").mockResolvedValue([]);
    vi.spyOn(api.composition, "getChapterComposition").mockRejectedValue(
      new Error("合成状态不可用"),
    );

    renderCompositionPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("合成状态不可用");
  });
});