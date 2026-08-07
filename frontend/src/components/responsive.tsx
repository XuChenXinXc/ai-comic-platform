/**
 * 响应式工具组件
 *
 * 提供移动端适配和响应式布局支持
 */

import { useState } from "react";
import { useMediaQuery, useWindowSize } from "../hooks";

/**
 * 响应式容器
 *
 * 根据屏幕尺寸自动调整布局
 */
interface ResponsiveContainerProps {
  children: React.ReactNode;
  className?: string;
}

export function ResponsiveContainer({
  children,
  className = "",
}: ResponsiveContainerProps) {
  const isMobile = useMediaQuery("(max-width: 768px)");

  return (
    <div
      className={`${
        isMobile ? "px-4 py-3" : "px-6 py-4"
      } mx-auto max-w-7xl ${className}`}
    >
      {children}
    </div>
  );
}

/**
 * 移动端侧边栏
 *
 * 支持滑动打开/关闭
 */
interface MobileSidebarProps {
  isOpen: boolean;
  onClose: () => void;
  children: React.ReactNode;
}

export function MobileSidebar({
  isOpen,
  onClose,
  children,
}: MobileSidebarProps) {
  if (!isOpen) return null;

  return (
    <>
      {/* 遮罩层 */}
      <div
        className="fixed inset-0 z-40 bg-black bg-opacity-50 transition-opacity md:hidden"
        onClick={onClose}
      />

      {/* 侧边栏 */}
      <div
        className={`fixed inset-y-0 left-0 z-50 w-64 transform bg-white shadow-xl transition-transform md:hidden ${
          isOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {children}
      </div>
    </>
  );
}

/**
 * 响应式网格
 *
 * 自动调整列数
 */
interface ResponsiveGridProps {
  children: React.ReactNode;
  minWidth?: number;
  gap?: number;
  className?: string;
}

export function ResponsiveGrid({
  children,
  minWidth = 280,
  gap = 16,
  className = "",
}: ResponsiveGridProps) {
  return (
    <div
      className={`grid ${className}`}
      style={{
        gridTemplateColumns: `repeat(auto-fill, minmax(${minWidth}px, 1fr))`,
        gap: `${gap}px`,
      }}
    >
      {children}
    </div>
  );
}

/**
 * 仅桌面端显示
 */
interface DesktopOnlyProps {
  children: React.ReactNode;
}

export function DesktopOnly({ children }: DesktopOnlyProps) {
  const isMobile = useMediaQuery("(max-width: 768px)");
  return isMobile ? null : <>{children}</>;
}

/**
 * 仅移动端显示
 */
interface MobileOnlyProps {
  children: React.ReactNode;
}

export function MobileOnly({ children }: MobileOnlyProps) {
  const isMobile = useMediaQuery("(max-width: 768px)");
  return isMobile ? <>{children}</> : null;
}

/**
 * 断点常量
 */
export const BREAKPOINTS = {
  xs: 320,
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
  "2xl": 1536,
} as const;

/**
 * 使用断点
 *
 * @returns 当前断点
 *
 * @example
 * const breakpoint = useBreakpoint();
 * if (breakpoint === "xs" || breakpoint === "sm") {
 *   // 移动端布局
 * }
 */
export function useBreakpoint(): keyof typeof BREAKPOINTS {
  const { width } = useWindowSize();

  if (width < BREAKPOINTS.sm) return "xs";
  if (width < BREAKPOINTS.md) return "sm";
  if (width < BREAKPOINTS.lg) return "md";
  if (width < BREAKPOINTS.xl) return "lg";
  if (width < BREAKPOINTS["2xl"]) return "xl";
  return "2xl";
}

/**
 * 触摸滑动 Hook
 *
 * 检测水平滑动手势
 */
interface UseSwipeOptions {
  onSwipeLeft?: () => void;
  onSwipeRight?: () => void;
  threshold?: number;
}

export function useSwipe({
  onSwipeLeft,
  onSwipeRight,
  threshold = 50,
}: UseSwipeOptions) {
  const [touchStart, setTouchStart] = useState<number | null>(null);
  const [touchEnd, setTouchEnd] = useState<number | null>(null);

  const onTouchStart = (e: React.TouchEvent) => {
    setTouchEnd(null);
    setTouchStart(e.targetTouches[0].clientX);
  };

  const onTouchMove = (e: React.TouchEvent) => {
    setTouchEnd(e.targetTouches[0].clientX);
  };

  const onTouchEnd = () => {
    if (!touchStart || !touchEnd) return;

    const distance = touchStart - touchEnd;
    const isLeftSwipe = distance > threshold;
    const isRightSwipe = distance < -threshold;

    if (isLeftSwipe && onSwipeLeft) {
      onSwipeLeft();
    }
    if (isRightSwipe && onSwipeRight) {
      onSwipeRight();
    }
  };

  return {
    onTouchStart,
    onTouchMove,
    onTouchEnd,
  };
}
