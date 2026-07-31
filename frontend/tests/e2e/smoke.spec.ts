import { expect, test, type APIRequestContext } from "@playwright/test";

interface ProjectResponse {
  id: string;
  name: string;
}

interface JobResponse {
  id: string;
  status: string;
  error?: string;
}

interface BatchResponse {
  jobs: JobResponse[];
}

interface AssetResponse {
  id: string;
  name: string;
}

interface ShotResponse {
  id: string;
}

interface MediaResponse {
  id: string;
  status: string;
  is_current: boolean;
  file_url?: string;
}

const terminalJobStatuses = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);
const mockProviderDefaults = {
  default_text_provider_id: "mock-text",
  default_image_provider_id: "mock-image",
  default_video_provider_id: "mock-video",
};

async function waitForJob(
  request: APIRequestContext,
  jobId: string,
  timeoutMs = 120_000,
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await request.get(`/api/jobs/${jobId}`);
    expect(response.status()).toBe(200);
    const job = (await response.json()) as JobResponse;
    if (terminalJobStatuses.has(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`任务未在 ${timeoutMs}ms 内结束：${jobId}`);
}

async function waitForBatch(
  request: APIRequestContext,
  response: { status(): number; json(): Promise<unknown> },
) {
  expect(response.status()).toBe(202);
  const batch = (await response.json()) as BatchResponse;
  const jobs = await Promise.all(
    batch.jobs.map((job) => waitForJob(request, job.id)),
  );
  for (const job of jobs) {
    expect(job.status, `${job.id}: ${job.error || "无错误信息"}`).toBe(
      "completed",
    );
  }
}

async function cleanupProject(
  request: APIRequestContext,
  projectName: string,
) {
  const response = await request.get("/api/projects");
  if (!response.ok()) return;
  const payload = (await response.json()) as
    | ProjectResponse[]
    | { items: ProjectResponse[] };
  const projects = Array.isArray(payload) ? payload : payload.items;
  await Promise.all(
    projects
      .filter((project) => project.name === projectName)
      .map((project) => request.delete(`/api/projects/${project.id}`)),
  );
}

test("test_create_project_and_open_writing_workspace", async ({
  page,
  request,
}) => {
  const projectName = `E2E-漫剧-${Date.now()}`;

  try {
    await page.goto("/project?create=1");
    await expect(
      page.getByRole("heading", { name: "创建漫剧项目" }),
    ).toBeVisible();

    await page.getByLabel("项目名称").fill(projectName);
    await page
      .getByLabel("小说正文")
      .fill("雨落在旧城巷口，林昭握紧信封，抬头看向亮着灯的阁楼。");
    await page.getByRole("button", { name: "创建并保存" }).click();

    await expect(
      page.getByRole("heading", { name: "项目小说" }),
    ).toBeVisible();
    const projectsResponse = await request.get("/api/projects");
    expect(projectsResponse.status()).toBe(200);
    const projects = (await projectsResponse.json()) as ProjectResponse[];
    const project = projects.find((item) => item.name === projectName);
    expect(project).toBeDefined();
    const providerResponse = await request.put(`/api/projects/${project!.id}`, {
      data: mockProviderDefaults,
    });
    expect(providerResponse.status()).toBe(200);

    await page.getByRole("link", { name: "小说分析与剧本" }).click();
    await expect(
      page.getByRole("heading", { name: "小说分析与剧本" }),
    ).toBeVisible();
    await expect(page.getByText("雨落在旧城巷口")).toBeVisible();

    await page.getByRole("button", { name: "开始生成" }).click();
    await expect(page.getByRole("textbox")).toHaveValue(/"summary"/, {
      timeout: 15_000,
    });
  } finally {
    await cleanupProject(request, projectName);
  }
});

test("test_narrow_viewport_exposes_settings_provider_and_job_navigation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/project");

  await expect(page.getByRole("link", { name: "平台设置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "接口配置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "任务中心" })).toBeVisible();
  const productionNav = page.getByRole("navigation", {
    name: "四步生产轨道",
  });
  await expect(productionNav.getByRole("link")).toHaveCount(4);
  await expect(
    productionNav.getByRole("link", { name: /分镜视频/ }),
  ).toHaveCount(1);
});

test("test_active_job_status_persists_across_pages_and_hides_after_completion", async ({
  page,
  request,
}) => {
  const projectName = `E2E-任务状态-${Date.now()}`;

  try {
    const projectResponse = await request.post("/api/projects", {
      data: {
        name: projectName,
        source_text: "林昭在雨夜推开旧剧院的门。",
        ...mockProviderDefaults,
      },
    });
    expect(projectResponse.status()).toBe(201);
    const project = (await projectResponse.json()) as ProjectResponse;

    await page.goto("/project");
    await page.getByLabel("选择项目").selectOption(project.id);

    const jobResponse = await request.post(
      `/api/projects/${project.id}/analysis/generate`,
      { data: { params: { delay_sec: 4 } } },
    );
    expect(jobResponse.status()).toBe(202);
    const job = (await jobResponse.json()) as JobResponse;
    await page.reload();
    const taskStatus = page.getByRole("status").filter({ hasText: "小说分析" });

    await expect(taskStatus).toBeVisible();
    await page.goto("/assets");
    await expect(taskStatus).toBeVisible();
    expect((await waitForJob(request, job.id)).status).toBe("completed");
    await expect(taskStatus).toHaveCount(0, { timeout: 5_000 });
  } finally {
    await cleanupProject(request, projectName);
  }
});

test("test_all_ten_pages_render_without_console_errors", async ({
  page,
  request,
}) => {
  const projectName = `E2E-逐页-${Date.now()}`;
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  try {
    const response = await request.post("/api/projects", {
      data: {
        name: projectName,
        source_text: "林昭推开旧剧院的门，尘埃在光束里缓慢落下。",
        style_prompt: "克制的二维电影感漫剧",
        aspect_ratio: "16:9",
        image_size: "1920x1080",
        video_size: "16:9",
        default_duration_sec: 6,
        ...mockProviderDefaults,
      },
    });
    expect(response.ok()).toBeTruthy();

    const pages = [
      ["/project", "项目小说"],
      ["/writing", "剧本生成"],
      ["/assets", "素材库"],
      ["/asset-images", "素材库"],
      ["/shots", "分镜视频"],
      ["/keyframes", "分镜视频"],
      ["/videos", "分镜视频"],
      ["/settings", "平台设置"],
      ["/providers", "接口配置"],
      ["/jobs", "任务中心"],
    ] as const;

    for (const [path, heading] of pages) {
      await page.goto(path);
      await expect(
        page.getByRole("heading", { name: heading, exact: true }),
      ).toBeVisible();
    }

    expect(consoleErrors).toEqual([]);
  } finally {
    await cleanupProject(request, projectName);
  }
});

test("test_mock_full_pipeline_renders_generated_image_and_video", async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const projectName = `E2E-全链路-${Date.now()}`;
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  try {
    const projectResponse = await request.post("/api/projects", {
      data: {
        name: projectName,
        source_text:
          "雨夜里，林昭推开旧剧院的门，发现舞台中央放着一封没有署名的信。",
        style_prompt: "克制的二维电影感漫剧，暖灰纸张质感，朱砂色点缀",
        aspect_ratio: "16:9",
        image_size: "1920x1080",
        video_size: "16:9",
        default_duration_sec: 2,
        ...mockProviderDefaults,
      },
    });
    expect(projectResponse.status()).toBe(201);
    const project = (await projectResponse.json()) as ProjectResponse;

    const analysisResponse = await request.post(
      `/api/projects/${project.id}/analysis/generate`,
      { data: {} },
    );
    expect(analysisResponse.status()).toBe(202);
    expect(
      (await waitForJob(
        request,
        ((await analysisResponse.json()) as JobResponse).id,
      )).status,
    ).toBe("completed");

    const screenplayResponse = await request.post(
      `/api/projects/${project.id}/screenplay/generate`,
      { data: {} },
    );
    expect(screenplayResponse.status()).toBe(202);
    expect(
      (await waitForJob(
        request,
        ((await screenplayResponse.json()) as JobResponse).id,
      )).status,
    ).toBe("completed");

    const extractionResponse = await request.post(
      `/api/projects/${project.id}/assets/extract`,
      { data: {} },
    );
    expect(extractionResponse.status()).toBe(202);
    expect(
      (await waitForJob(
        request,
        ((await extractionResponse.json()) as JobResponse).id,
      )).status,
    ).toBe("completed");

    await waitForBatch(
      request,
      await request.post(`/api/projects/${project.id}/assets/prompts/batch`, {
        data: {},
      }),
    );
    await waitForBatch(
      request,
      await request.post(`/api/projects/${project.id}/assets/images/batch`, {
        data: {},
      }),
    );

    const assetsResponse = await request.get(
      `/api/projects/${project.id}/assets`,
    );
    expect(assetsResponse.status()).toBe(200);
    const assets = (await assetsResponse.json()) as AssetResponse[];
    expect(assets).toHaveLength(3);
    for (const asset of assets) {
      const versionsResponse = await request.get(
        `/api/assets/${asset.id}/images`,
      );
      expect(versionsResponse.status()).toBe(200);
      const versions = (await versionsResponse.json()) as MediaResponse[];
      expect(versions).toHaveLength(1);
      expect(versions[0]).toMatchObject({
        status: "completed",
        is_current: true,
      });
    }

    const shotExtractionResponse = await request.post(
      `/api/projects/${project.id}/shots/extract`,
      { data: { source_type: "screenplay", scope: "all" } },
    );
    expect(shotExtractionResponse.status()).toBe(202);
    expect(
      (await waitForJob(
        request,
        ((await shotExtractionResponse.json()) as JobResponse).id,
      )).status,
    ).toBe("completed");

    const shotsResponse = await request.get(
      `/api/projects/${project.id}/shots`,
    );
    expect(shotsResponse.status()).toBe(200);
    const shots = (await shotsResponse.json()) as ShotResponse[];
    expect(shots).toHaveLength(1);
    const shot = shots[0];

    const keyframeResponse = await request.post(
      `/api/shots/${shot.id}/keyframes/generate`,
      { data: {} },
    );
    expect(keyframeResponse.status()).toBe(202);
    expect(
      (await waitForJob(
        request,
        ((await keyframeResponse.json()) as JobResponse).id,
      )).status,
    ).toBe("completed");

    const videoResponse = await request.post(
      `/api/shots/${shot.id}/videos/generate`,
      { data: {} },
    );
    expect(videoResponse.status()).toBe(202);
    expect(
      (await waitForJob(
        request,
        ((await videoResponse.json()) as JobResponse).id,
        180_000,
      )).status,
    ).toBe("completed");

    const videosResponse = await request.get(
      `/api/shots/${shot.id}/videos`,
    );
    expect(videosResponse.status()).toBe(200);
    const videos = (await videosResponse.json()) as MediaResponse[];
    expect(videos).toHaveLength(1);
    expect(videos[0]).toMatchObject({
      status: "completed",
      is_current: true,
    });

    const exportResponse = await request.get(
      `/api/projects/${project.id}/videos/export`,
    );
    expect(exportResponse.status()).toBe(200);
    expect(exportResponse.headers()["content-type"]).toContain(
      "application/zip",
    );
    expect((await exportResponse.body()).byteLength).toBeGreaterThan(0);

    await page.goto("/asset-images");
    await expect(
      page.getByRole("heading", { name: "素材库", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("img", { name: "主角 参考图", exact: true }),
    ).toBeVisible();

    await page.goto("/shots");
    await expect(
      page.getByRole("heading", {
        name: "分镜视频",
        exact: true,
        level: 1,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("img", { name: "分镜 1 关键帧", exact: true }),
    ).toBeVisible();
    await expect(page.locator("video")).toHaveCount(1);

    await page.reload();
    await expect(page.locator("video")).toHaveCount(1);
    expect(consoleErrors).toEqual([]);
  } finally {
    await cleanupProject(request, projectName);
  }
});
