import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#F2F0EA",
        ink: "#191A17",
        sidebar: "#1C211F",
        vermilion: "#E8613A",
        paper: "#FAF9F5",
        line: "#D9D5CC",
        muted: "#6B6E68",
      },
      boxShadow: {
        panel: "0 16px 44px rgba(25, 26, 23, 0.08)",
      },
    },
  },
  plugins: [],
} satisfies Config;
