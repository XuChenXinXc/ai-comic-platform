import { render, screen } from "@testing-library/react";
import { within } from "@testing-library/react";
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

  test("test_failed_image_versions_display_each_failure_and_delete_calls_handler", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    const failedVersion3: MediaVersion = {
      ...versions[0],
      id: "media-failed",
      version_no: 3,
      file_path: null,
      status: "failed",
      is_current: false,
      error: "图片服务商返回失败",
    };
    const failedVersion1: MediaVersion = {
      ...versions[1],
      id: "media-failed-old",
      version_no: 1,
      file_path: null,
      status: "failed",
      is_current: false,
      error: "旧失败",
    };

    render(
      <MediaVersions
        versions={[failedVersion1, failedVersion3]}
        mediaKind="image"
        alt="角色参考图"
        onSelect={vi.fn()}
        onDelete={onDelete}
      />,
    );

    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(2);
    expect(within(alerts[0]).getByText("V3 生成失败")).toBeInTheDocument();
    expect(within(alerts[0]).getByText("图片服务商返回失败")).toBeInTheDocument();
    expect(
      within(alerts[0]).getByRole("button", { name: "删除失败版本 3" }),
    ).toBeInTheDocument();
    expect(within(alerts[1]).getByText("V1 生成失败")).toBeInTheDocument();
    expect(within(alerts[1]).getByText("旧失败")).toBeInTheDocument();
    expect(
      within(alerts[1]).getByRole("button", { name: "删除失败版本 1" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("媒体版本")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "选择版本 3" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "选择版本 1" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("还没有图像版本")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "删除失败版本 3" }),
    );
    expect(onDelete).toHaveBeenCalledWith(failedVersion3);

    await user.click(
      screen.getByRole("button", { name: "删除失败版本 1" }),
    );
    expect(onDelete).toHaveBeenCalledWith(failedVersion1);
  });
});
