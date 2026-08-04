import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const backendUrl = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8012";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 3000,
    proxy: {
      "/api": {
        target: backendUrl,
        changeOrigin: true,
      },
      "/media": {
        target: backendUrl,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    css: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
