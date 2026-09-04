import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal, Tabs } from "../ui";

describe("基础交互组件", () => {
  test("test_tabs_selecting_item_reports_selected_value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Tabs
        value="character"
        onChange={onChange}
        items={[
          { value: "character", label: "角色" },
          { value: "scene", label: "场景" },
        ]}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "场景" }));
    expect(onChange).toHaveBeenCalledWith("scene");
  });

  test("test_modal_pressing_escape_closes_dialog", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="编辑素材">
        <p>内容</p>
      </Modal>,
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("test_modal_focuses_first_focusable_element_when_opened", () => {
    render(
      <Modal open onClose={() => {}} title="编辑素材">
        <p>内容</p>
      </Modal>,
    );

    // 打开后焦点应移入对话框内的第一个可聚焦元素（关闭按钮），而不是停留在背景页面
    const closeButton = screen.getByRole("button", { name: "关闭" });
    expect(document.activeElement).toBe(closeButton);
  });
});
