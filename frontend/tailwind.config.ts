import type { Config } from "tailwindcss";

/**
 * 设计令牌（苹果官网风格，以 preview.html 为基准）。
 * 旧令牌名（canvas/ink/sidebar/vermilion/paper/line/muted）保留，
 * 值映射为新设计色板，并通过 CSS 变量支持明暗主题切换。
 */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--c-canvas) / <alpha-value>)",
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        sidebar: "rgb(var(--c-sidebar) / <alpha-value>)",
        vermilion: "rgb(var(--c-brand) / <alpha-value>)",
        paper: "rgb(var(--c-paper) / <alpha-value>)",
        line: "rgb(var(--c-line) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
        brand: {
          DEFAULT: "#0071e3",
          hover: "#0077ed",
        },
        card: "rgb(var(--c-card) / <alpha-value>)",
        cardDark: "rgb(var(--c-card-dark) / <alpha-value>)",
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "SF Pro Display",
          "SF Pro Text",
          "PingFang SC",
          "Microsoft YaHei",
          "Noto Sans SC",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
      },
      boxShadow: {
        panel: "0 16px 44px rgba(0, 0, 0, 0.08)",
        apple: "0 18px 60px rgba(0, 0, 0, 0.08)",
        appleDark: "0 22px 80px rgba(0, 0, 0, 0.32)",
        cta: "0 10px 30px rgba(0, 113, 227, 0.25)",
        card: "0 2px 10px rgba(0, 0, 0, 0.04)",
      },
      borderRadius: {
        card: "20px",
        sheet: "28px",
        field: "16px",
      },
    },
  },
  plugins: [],
} satisfies Config;
