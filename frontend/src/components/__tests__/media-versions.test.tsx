import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MediaVersion } from "../../types/api";
import { MediaVersions } from "../media-versions";

const versions: MediaVersion[] = [
  {
    id: "media-current",
    version_no: 2,
    file_path: "/media/keyframes/shot-1/v002.png",
    prompt_snapshot: "current",
    negative_prompt_snapshot: "",
    model: "mock-image",
    status: "completed",
    is_current: true,
    input_signature: "old-signature",
    source: "generated",
    created_at: "2026-07-28T10:00:00Z",
    is_stale: true,
    stale_reason: "分镜提示词已经变化",
  },
  {
    id: "media-old",
    version_no: 1,
    file_path: "/media/keyframes/shot-1/v001.png",
    prompt_snapshot: "old",
    negative_prompt_snapshot: "",
    model: "mock-image",
    status: "completed",
    is_current: false,
    input_signature: "older-signature",
    source: "generated",
    created_at: "2026-07-28T09:00:00Z",
  },
];

describe("媒体版本组件", () => {
  test("test_click_current_image_opens_large_preview", async () => {
    const user = userEvent.setup();
    render(
      <MediaVersions
        versions={versions}
        mediaKind="image"
        alt="关键帧"
        onSelect={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "放大关键帧" }));

    expect(
      screen.getByRole("dialog", { name: "关键帧" }),
    ).toBeInTheDocument();
    expect(screen.getByAltText("关键帧大图")).toBeInTheDocument();
  });

  test("test_media_versions_with_current_version_disables_current_delete", () => {
    render(
      <MediaVersions
        versions={versions}
        mediaKind="image"
        alt="关键帧"
        onSelect={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "删除版本 2" }),
    ).toBeDisabled();
    expect(screen.getByText("分镜提示词已经变化")).toBeInTheDocument();
  });

  test("test_media_versions_selecting_old_version_calls_handler", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <MediaVersions
        versions={versions}
        mediaKind="image"
        alt="关键帧"
        onSelect={onSelect}
        onDelete={vi.fn()}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "设为当前版本 1" }),
    );
    expect(onSelect).toHaveBeenCalledWith(versions[1]);
  });

  test("test_media_versions_deleting_non_current_version_calls_handler", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    render(
      <MediaVersions
        versions={versions}
        mediaKind="image"
        alt="关键帧"
        onSelect={vi.fn()}
        onDelete={onDelete}
      />,
    );

    await user.click(screen.getByRole("button", { name: "删除版本 1" }));
    expect(onDelete).toHaveBeenCalledWith(versions[1]);
  });
});
