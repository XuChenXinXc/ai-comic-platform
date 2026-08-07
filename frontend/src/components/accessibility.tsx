/**
 * 无障碍性辅助组件
 *
 * 提供 ARIA 支持和键盘导航
 */

import { useEffect, useRef } from "react";

/**
 * 跳过导航链接
 *
 * 允许键盘用户快速跳过导航栏
 */
export function SkipToContent() {
  return (
    <a
      href="#main-content"
      className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-blue-600 focus:px-4 focus:py-2 focus:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
    >
      跳到主内容
    </a>
  );
}

/**
 * 屏幕阅读器专用文本
 *
 * 仅对屏幕阅读器可见
 */
interface VisuallyHiddenProps {
  children: React.ReactNode;
}

export function VisuallyHidden({ children }: VisuallyHiddenProps) {
  return <span className="sr-only">{children}</span>;
}

/**
 * 焦点陷阱
 *
 * 将焦点限制在模态框内（用于对话框、弹窗）
 */
interface FocusTrapProps {
  children: React.ReactNode;
  active?: boolean;
}

export function FocusTrap({ children, active = true }: FocusTrapProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active) return;

    const container = containerRef.current;
    if (!container) return;

    // 获取所有可聚焦元素
    const focusableElements = container.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );

    const firstElement = focusableElements[0];
    const lastElement = focusableElements[focusableElements.length - 1];

    // 保存之前的焦点元素
    const previousActiveElement = document.activeElement as HTMLElement;

    // 聚焦第一个元素
    firstElement?.focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;

      if (e.shiftKey) {
        // Shift + Tab
        if (document.activeElement === firstElement) {
          e.preventDefault();
          lastElement?.focus();
        }
      } else {
        // Tab
        if (document.activeElement === lastElement) {
          e.preventDefault();
          firstElement?.focus();
        }
      }
    };

    container.addEventListener("keydown", handleKeyDown);

    return () => {
      container.removeEventListener("keydown", handleKeyDown);
      // 恢复之前的焦点
      previousActiveElement?.focus();
    };
  }, [active]);

  return <div ref={containerRef}>{children}</div>;
}

/**
 * 实时区域
 *
 * 用于通知屏幕阅读器动态内容更新
 */
interface LiveRegionProps {
  message: string;
  politeness?: "polite" | "assertive" | "off";
}

export function LiveRegion({ message, politeness = "polite" }: LiveRegionProps) {
  return (
    <div
      role="status"
      aria-live={politeness}
      aria-atomic="true"
      className="sr-only"
    >
      {message}
    </div>
  );
}

/**
 * 键盘快捷键提示
 */
interface KeyboardShortcutProps {
  keys: string[];
  description: string;
}

export function KeyboardShortcut({ keys, description }: KeyboardShortcutProps) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <div className="flex gap-1">
        {keys.map((key, index) => (
          <kbd
            key={index}
            className="rounded border border-gray-300 bg-gray-100 px-2 py-1 font-mono text-xs"
          >
            {key}
          </kbd>
        ))}
      </div>
      <span className="text-gray-600">{description}</span>
    </div>
  );
}

/**
 * 使用键盘快捷键
 *
 * @example
 * useKeyboardShortcut("ctrl+s", (e) => {
 *   e.preventDefault();
 *   handleSave();
 * });
 */
export function useKeyboardShortcut(
  combination: string,
  callback: (e: KeyboardEvent) => void
) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const keys = combination.toLowerCase().split("+");
      const modifiers = {
        ctrl: e.ctrlKey || e.metaKey,
        alt: e.altKey,
        shift: e.shiftKey,
      };

      const matches = keys.every((key) => {
        if (key === "ctrl") return modifiers.ctrl;
        if (key === "alt") return modifiers.alt;
        if (key === "shift") return modifiers.shift;
        return e.key.toLowerCase() === key;
      });

      if (matches) {
        callback(e);
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [combination, callback]);
}

/**
 * ARIA 属性辅助函数
 */
export function getAriaProps(
  isExpanded?: boolean,
  isSelected?: boolean,
  isDisabled?: boolean
) {
  return {
    "aria-expanded": isExpanded,
    "aria-selected": isSelected,
    "aria-disabled": isDisabled,
  };
}

/**
 * 加载中公告
 *
 * 通知屏幕阅读器正在加载
 */
interface LoadingAnnouncementProps {
  isLoading: boolean;
  message?: string;
}

export function LoadingAnnouncement({
  isLoading,
  message = "正在加载...",
}: LoadingAnnouncementProps) {
  return isLoading ? <LiveRegion message={message} /> : null;
}
