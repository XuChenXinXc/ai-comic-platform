import { defineConfig, devices } from "@playwright/test";

const backendPython =
  process.platform === "win32"
    ? "../backend/.venv/Scripts/python.exe"
    : "../backend/.venv/bin/python";
const backendCommand =
  process.env.E2E_BACKEND_COMMAND ??
  `"${backendPython}" -m uvicorn app.main:app --app-dir ../backend --host 127.0.0.1 --port 8000`;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: backendCommand,
      url: "http://127.0.0.1:8000/docs",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "npm run dev",
      url: "http://127.0.0.1:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
