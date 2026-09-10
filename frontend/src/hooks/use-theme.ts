import { useEffect, useState } from "react";

/**
 * 明暗主题共享状态：AppShell 的主题开关与 Toaster 等全局组件
 * 通过订阅同一份模块级状态保持一致。
 */
const THEME_STORAGE_KEY = "manzhiju-theme";

type Theme = "light" | "dark";

function readInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "dark" || stored === "light") return stored;
    if (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
    ) {
      return "dark";
    }
  } catch {
    // localStorage 不可用时退化为浅色主题
  }
  return "light";
}

let currentTheme: Theme = readInitialTheme();
const listeners = new Set<(theme: Theme) => void>();

function applyTheme(theme: Theme) {
  currentTheme = theme;
  if (typeof document !== "undefined") {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 忽略持久化失败
  }
  listeners.forEach((listener) => listener(theme));
}

// 模块加载时应用一次初始主题（浏览器环境）。
if (typeof document !== "undefined") {
  applyTheme(currentTheme);
}

export function useTheme() {
  const [dark, setDark] = useState<boolean>(currentTheme === "dark");

  useEffect(() => {
    const listener = (theme: Theme) => setDark(theme === "dark");
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  return {
    dark,
    toggle: () => applyTheme(dark ? "light" : "dark"),
  };
}
