import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EmptyState, StaleNotice, StatusBadge } from "../status";
import { Button } from "../ui";

describe("状态组件", () => {
  test("test_status_badge_with_completed_status_shows_localized_label", () => {
    render(<StatusBadge status="completed" />);
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  test("test_stale_notice_with_reason_shows_actionable_message", () => {
    render(<StaleNotice reason="角色参考图已更新，请重新检查。" />);
    expect(
      screen.getByText("角色参考图已更新，请重新检查。"),
    ).toBeInTheDocument();
  });

  test("test_empty_state_with_action_invokes_recovery_action", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <EmptyState
        title="还没有数据"
        description="先创建一项内容。"
        action={<Button onClick={onClick}>立即创建</Button>}
      />,
    );

    await user.click(screen.getByRole("button", { name: "立即创建" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
