import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  FileText,
  Film,
  FolderPlus,
  Save,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import { ErrorState, PageHeader } from "../components/status";
import { Button, Card, Input, Label, Select } from "../components/ui";
import { useProject } from "../hooks/use-project";
import { formatError } from "../lib/utils";
import type { Project, ProjectInput, ProviderKind } from "../types/api";

const emptyProject: ProjectInput = {
  name: "",
  source_text: "",
  style_prompt:
    "电影感古风仙域二维国漫，稳定自然的半写实人物比例，东方审美骨相，克制细腻的古风线条，低饱和青灰、玉白、玄黑与暖金综合色调，仙气通透但不过度梦幻，真实清晰的微表情，层次分明的电影光影，东方山水空间美学，精致但不浮夸。",
  aspect_ratio: "16:9",
  image_size: "2560x1440",
  video_size: "16:9",
  default_duration_sec: 6,
  default_text_provider_id: null,
  default_image_provider_id: null,
  default_video_provider_id: null,
};

function projectFormFromProject(project: Project): ProjectInput {
  return {
    name: project.name,
    source_text: "",
    style_prompt: project.style_prompt,
    aspect_ratio: project.aspect_ratio || "16:9",
    image_size: project.image_size || "2560x1440",
    video_size: project.video_size || "16:9",
    default_duration_sec: project.default_duration_sec ?? 6,
    default_text_provider_id: project.default_text_provider_id,
    default_image_provider_id: project.default_image_provider_id,
    default_video_provider_id: project.default_video_provider_id,
  };
}

export function ProjectPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const {
    activeProject,
    activeProjectId,
    selectProject,
    projects,
    isLoading,
    error,
  } = useProject();
  const creating = searchParams.get("create") === "1" || !activeProject;
  const [form, setForm] = useState<ProjectInput>(emptyProject);
  // 用户修改过任一字段后置 true：后台任务终态刷新 projects 时不再自动回填表单，
  // 避免正在编辑的值被服务器值静默覆盖（参照 WritingPage 的 dirty 保护模式）。
  const formDirtyRef = useRef(false);
  // 记录表单当前回填所属的项目/模式标识：切换项目或进出新建模式时重置 dirty。
  const hydratedKeyRef = useRef("");

  useEffect(() => {
    if (creating) {
      // 仅在「进入创建模式」时重置一次：activeProject 对象随后台项目列表
      // 刷新反复变化，若每次都重置会把用户正在输入的项目名清空。
      if (hydratedKeyRef.current !== "create") {
        setForm(emptyProject);
        formDirtyRef.current = false;
        hydratedKeyRef.current = "create";
      }
      return;
    }
    if (activeProject) {
      if (hydratedKeyRef.current !== activeProject.id) {
        // 首次进入或切换项目：恢复自动回填并写入服务器值。
        hydratedKeyRef.current = activeProject.id;
        formDirtyRef.current = false;
      }
      // 用户修改过任一字段后保持本地值，等待用户保存或显式切换项目。
      if (!formDirtyRef.current) {
        setForm(projectFormFromProject(activeProject));
      }
    }
  }, [activeProject, creating]);

  const providersQuery = useQuery({
    queryKey: queryKeys.providers,
    queryFn: () => api.providers.list(),
  });
  const providersByKind = useMemo(() => {
    const result: Record<ProviderKind, Array<{ id: string; name: string }>> = {
      text: [],
      image: [],
      video: [],
      tts: [],
    };
    (providersQuery.data || [])
      .filter((provider) => provider.enabled)
      .forEach((provider) => {
        result[provider.kind].push({ id: provider.id, name: provider.name });
      });
    return result;
  }, [providersQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () => {
      if (creating) {
        return api.projects.create(form);
      }
      // 编辑模式不携带 source_text：后端收到该字段会重算签名并清空存量正文。
      const editable: Partial<ProjectInput> = { ...form };
      delete editable.source_text;
      return api.projects.update(activeProjectId, editable);
    },
    onSuccess: async (project) => {
      // 保存成功后恢复自动回填，后续任务（如自动画风识别）可同步表单。
      formDirtyRef.current = false;
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      selectProject(project.id);
      setSearchParams({}, { replace: true });
      toast.success(creating ? "项目已创建" : "项目设置已保存");
    },
    onError: (mutationError) => toast.error(formatError(mutationError)),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.projects.remove(activeProjectId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      toast.success("项目已删除");
      setSearchParams({ create: "1" }, { replace: true });
    },
    onError: (mutationError) => toast.error(formatError(mutationError)),
  });

  if (error && !projects.length) {
    return (
      <ErrorState
        message={formatError(error)}
        onRetry={() =>
          void queryClient.invalidateQueries({ queryKey: queryKeys.projects })
        }
      />
    );
  }

  const setField = <K extends keyof ProjectInput>(
    key: K,
    value: ProjectInput[K],
  ) => {
    // 用户修改过任一字段：停止后续自动回填。
    formDirtyRef.current = true;
    setForm((current) => ({ ...current, [key]: value }));
  };

  const canSave = Boolean(form.name.trim());

  return (
    <div>
      <PageHeader
        eyebrow="Step 01 / Source"
        title={creating ? "创建漫剧项目" : "项目小说"}
        description="管理项目配置；小说原文在写作页按章节编辑。"
        actions={
          <>
            {!creating && activeProjectId && (
              <Button
                variant="secondary"
                onClick={() => navigate(`/composition/${activeProjectId}`)}
              >
                <Film className="h-4 w-4" />
                最终合成
              </Button>
            )}
            {!creating ? (
              <Button
                onClick={() => setSearchParams({ create: "1" })}
                disabled={saveMutation.isPending}
              >
                <FolderPlus className="h-4 w-4" />
                新建项目
              </Button>
            ) : null}
            <Button
              variant="primary"
              busy={saveMutation.isPending}
              disabled={!canSave}
              onClick={() => saveMutation.mutate()}
            >
              <Save className="h-4 w-4" />
              {creating ? "创建并保存" : "保存修改"}
            </Button>
          </>
        }
      />

      <div className="grid gap-6 2xl:grid-cols-[minmax(0,1.55fr)_minmax(330px,0.65fr)]">
        <Card className="overflow-hidden">
          <div className="border-b border-line px-5 py-4 sm:px-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-bold">小说与创作设定</h2>
                <p className="mt-1 text-xs text-muted">
                  小说原文请在各章节的写作页编辑。
                </p>
              </div>
            </div>
          </div>
          <div className="space-y-5 p-5 sm:p-6">
            <div>
              <Label htmlFor="project-name">项目名称</Label>
              <Input
                id="project-name"
                value={form.name}
                maxLength={120}
                placeholder="例如：雨夜旧案"
                onChange={(event) => setField("name", event.target.value)}
              />
            </div>
            {!creating && activeProject ? (
              <div>
                <Label htmlFor="project-storage-path">项目存储路径</Label>
                <Input
                  id="project-storage-path"
                  value={activeProject.storage_path}
                  readOnly
                  title={activeProject.storage_path}
                />
                <p className="mt-2 text-xs leading-5 text-muted">
                  角色、物品、场景、分镜图和分镜视频会分别存放在该目录的子文件夹中。
                </p>
              </div>
            ) : null}
          </div>
        </Card>

        <div className="space-y-6">
          <Card className="p-5 sm:p-6">
            <div className="flex items-center justify-between gap-3">
              <h2 className="font-bold">默认接口</h2>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => navigate("/providers")}
              >
                管理接口
              </Button>
            </div>
            <div className="mt-5 space-y-4">
              {(
                [
                  ["text", "文字模型", "default_text_provider_id"],
                  ["image", "图片模型", "default_image_provider_id"],
                  ["video", "视频模型", "default_video_provider_id"],
                ] as const
              ).map(([kind, label, field]) => (
                <div key={kind}>
                  <Label htmlFor={`provider-${kind}`}>{label}</Label>
                  <Select
                    id={`provider-${kind}`}
                    value={String(form[field] || "")}
                    onChange={(event) => setField(field, event.target.value || null)}
                  >
                    <option value="">使用系统默认</option>
                    {providersByKind[kind].map((provider) => (
                      <option key={provider.id} value={provider.id}>
                        {provider.name}
                      </option>
                    ))}
                  </Select>
                </div>
              ))}
              {!providersQuery.isLoading &&
              providersByKind.text.length +
                providersByKind.image.length +
                providersByKind.video.length ===
                0 ? (
                <p className="rounded-md border border-[#dfb98f] bg-[#fff5e9] px-3 py-2 text-xs leading-5 text-[#7b4b22]">
                  尚未配置接口。可先使用默认 Mock Provider 跑通生产链。
                </p>
              ) : null}
            </div>
          </Card>

          {!creating ? (
            <Card className="p-5 sm:p-6">
              <h2 className="font-bold">下一步</h2>
              <p className="mt-2 text-sm leading-6 text-muted">
                保存小说后，进入剧本生成；小说分析会在后台自动完成。
              </p>
              <Button
                className="mt-4 w-full"
                variant="primary"
                onClick={() => navigate("/writing")}
              >
                进入剧本生成
                <ArrowRight className="h-4 w-4" />
              </Button>
            </Card>
          ) : null}

          {!creating && projects.length > 1 ? (
            <Card className="p-5 sm:p-6">
              <h2 className="font-bold">项目切换</h2>
              <div className="mt-3 space-y-2">
                {projects.map((project) => (
                  <button
                    type="button"
                    key={project.id}
                    onClick={() => selectProject(project.id)}
                    className={`flex w-full items-center gap-3 rounded-md border px-3 py-3 text-left ${
                      activeProjectId === project.id
                        ? "border-vermilion bg-[#fff3ed]"
                        : "border-line bg-white hover:border-[#bdb8ad]"
                    }`}
                  >
                    <FileText className="h-4 w-4 text-muted" />
                    <span className="min-w-0 flex-1 truncate text-sm font-semibold">
                      {project.name}
                    </span>
                  </button>
                ))}
              </div>
            </Card>
          ) : null}

          {!creating ? (
            <Button
              variant="danger"
              className="w-full"
              busy={deleteMutation.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    `确认删除项目“${activeProject?.name}”及其全部本地媒体吗？此操作不可恢复。`,
                  )
                ) {
                  deleteMutation.mutate();
                }
              }}
            >
              <Trash2 className="h-4 w-4" />
              删除当前项目
            </Button>
          ) : (
            <Button
              className="w-full"
              disabled={isLoading || !activeProject}
              onClick={() => setSearchParams({}, { replace: true })}
            >
              取消新建
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
