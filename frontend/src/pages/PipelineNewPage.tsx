import { ArrowLeft, Check, FileText, Play, Sparkles } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router";
import { ApiError, api } from "../api/client";
import { Button, Card, Input, Label, Select, Textarea } from "../components/ui";
import { formatError } from "../lib/utils";
import type { DynamicRunCreateInput } from "../types/api";

type SourceMode = "source_text" | "synopsis";

interface PipelineForm {
  title: string;
  chapter_title: string;
  source_text: string;
  synopsis: string;
  target_duration_sec: string;
  aspect_ratio: string;
  style_prompt: string;
  auto_run: boolean;
  generate_audio: boolean;
  generate_subtitle: boolean;
  generate_bgm: boolean;
  generate_composition: boolean;
}

const initialForm: PipelineForm = {
  title: "",
  chapter_title: "",
  source_text: "",
  synopsis: "",
  target_duration_sec: "30",
  aspect_ratio: "9:16",
  style_prompt: "",
  auto_run: false,
  generate_audio: true,
  generate_subtitle: true,
  generate_bgm: true,
  generate_composition: true,
};

function ToggleField({
  id,
  label,
  description,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-3 rounded-lg border border-line bg-white px-3.5 py-3 transition hover:border-[#bdb8ad]"
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-1 h-4 w-4 accent-[#e8613a]"
      />
      <span className="min-w-0">
        <span className="block text-sm font-semibold text-ink">{label}</span>
        <span className="mt-0.5 block text-xs leading-5 text-muted">{description}</span>
      </span>
    </label>
  );
}

export function PipelineNewPage() {
  const { projectId } = useParams<{ projectId?: string }>();
  const navigate = useNavigate();
  const [form, setForm] = useState<PipelineForm>(initialForm);
  const [sourceMode, setSourceMode] = useState<SourceMode>("source_text");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const setField = <K extends keyof PipelineForm>(key: K, value: PipelineForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setFieldErrors((current) => {
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  };

  const validate = () => {
    const errors: Record<string, string> = {};
    if (!form.title.trim()) errors.title = "请填写运行标题";
    if (!form.chapter_title.trim()) errors.chapter_title = "请填写章节标题";
    if (!form[sourceMode].trim()) {
      errors[sourceMode] = sourceMode === "source_text" ? "请填写原文" : "请填写剧情梗概";
    }
    const duration = Number(form.target_duration_sec);
    if (!Number.isFinite(duration) || duration <= 0) {
      errors.target_duration_sec = "目标时长必须是大于 0 的数字";
    }
    setFieldErrors(errors);
    return errors;
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError("");
    const errors = validate();
    if (Object.keys(errors).length) return;

    setSubmitting(true);
    try {
      const payload: DynamicRunCreateInput = {
        title: form.title.trim(),
        chapter_title: form.chapter_title.trim(),
        ...(sourceMode === "source_text"
          ? { source_text: form.source_text.trim() }
          : { synopsis: form.synopsis.trim() }),
        target_duration_sec: Number(form.target_duration_sec),
        aspect_ratio: form.aspect_ratio,
        style_prompt: form.style_prompt.trim(),
        auto_run: form.auto_run,
        generate_audio: form.generate_audio,
        generate_subtitle: form.generate_subtitle,
        generate_bgm: form.generate_bgm,
        generate_composition: form.generate_composition,
      };
      const result = projectId
        ? await api.dynamicRuns.createForProject(projectId, payload)
        : await api.dynamicRuns.createFreeCreate(payload);
      const resultProjectId = result.run.project_id || projectId;
      if (!resultProjectId) {
        throw new Error("服务端未返回项目 ID，无法打开工作区");
      }
      if (projectId && result.run.project_id !== projectId) {
        throw new Error("创建结果与当前项目不匹配，无法打开工作区");
      }
      navigate(`/free-create/${resultProjectId}/pipeline/${result.run.id}`, {
        state: { initialWorkspace: result },
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) {
        setSubmitError(error.message);
      } else {
        setSubmitError(formatError(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#111715] text-white">
      <header className="border-b border-white/10 bg-[#151d1a]">
        <div className="mx-auto flex max-w-[1240px] items-center gap-4 px-5 py-5 sm:px-8 lg:px-12">
          <button
            type="button"
            aria-label="返回动态漫工作区"
            className="flex h-9 w-9 items-center justify-center rounded-lg border border-white/10 text-white/65 transition hover:border-white/25 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f29a70]"
            onClick={() => navigate(projectId ? `/free-create/${projectId}` : "/free-create")}
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div>
            <p className="text-[10px] font-bold uppercase tracking-[0.28em] text-[#f29a70]">
              New dynamic run
            </p>
            <h1 className="mt-1 text-xl font-extrabold tracking-[-0.04em]">新建动态漫</h1>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1240px] px-5 py-8 sm:px-8 lg:px-12 lg:py-12">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.3fr)_360px]">
          <form onSubmit={handleSubmit} noValidate>
            <Card className="border-line bg-paper p-5 text-ink sm:p-7">
              <div className="flex items-start gap-4 border-b border-line pb-6">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[#fff0e9] text-vermilion">
                  <FileText className="h-5 w-5" />
                </span>
                <div>
                  <h2 className="text-lg font-bold">输入内容</h2>
                  <p className="mt-1 text-sm leading-6 text-muted">
                    这一页只负责创建运行。创建完成后，所有阶段内容都在独立工作区中读取和保存。
                  </p>
                </div>
              </div>

              {submitError ? (
                <div role="alert" className="mt-5 rounded-lg border border-[#e1b3ad] bg-[#fff5f2] px-4 py-3 text-sm leading-6 text-[#9b342c]">
                  {submitError}
                </div>
              ) : null}

              <div className="mt-6 grid gap-5 sm:grid-cols-2">
                <div>
                  <Label htmlFor="pipeline-title">运行标题</Label>
                  <Input
                    id="pipeline-title"
                    value={form.title}
                    placeholder="例如：雨夜旧案·第一集"
                    aria-invalid={Boolean(fieldErrors.title)}
                    onChange={(event) => setField("title", event.target.value)}
                  />
                  {fieldErrors.title ? <p className="mt-1.5 text-xs text-[#a4362d]">{fieldErrors.title}</p> : null}
                </div>
                <div>
                  <Label htmlFor="pipeline-chapter-title">章节标题</Label>
                  <Input
                    id="pipeline-chapter-title"
                    value={form.chapter_title}
                    placeholder="例如：第一章 雨巷"
                    aria-invalid={Boolean(fieldErrors.chapter_title)}
                    onChange={(event) => setField("chapter_title", event.target.value)}
                  />
                  {fieldErrors.chapter_title ? <p className="mt-1.5 text-xs text-[#a4362d]">{fieldErrors.chapter_title}</p> : null}
                </div>
              </div>

              <fieldset className="mt-6">
                <legend className="mb-2 text-xs font-semibold text-[#555852]">内容来源（二选一）</legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className={`cursor-pointer rounded-lg border px-4 py-3 transition ${sourceMode === "source_text" ? "border-vermilion bg-[#fff5f0]" : "border-line bg-white hover:border-[#bdb8ad]"}`}>
                    <input
                      type="radio"
                      name="source-mode"
                      value="source_text"
                      checked={sourceMode === "source_text"}
                      onChange={() => setSourceMode("source_text")}
                      className="sr-only"
                    />
                    <span className="flex items-center justify-between gap-3 text-sm font-semibold text-ink">
                      原文
                      {sourceMode === "source_text" ? <Check className="h-4 w-4 text-vermilion" /> : null}
                    </span>
                    <span className="mt-1 block text-xs leading-5 text-muted">适合已有完整章节内容的情况。</span>
                  </label>
                  <label className={`cursor-pointer rounded-lg border px-4 py-3 transition ${sourceMode === "synopsis" ? "border-vermilion bg-[#fff5f0]" : "border-line bg-white hover:border-[#bdb8ad]"}`}>
                    <input
                      type="radio"
                      name="source-mode"
                      value="synopsis"
                      checked={sourceMode === "synopsis"}
                      onChange={() => setSourceMode("synopsis")}
                      className="sr-only"
                    />
                    <span className="flex items-center justify-between gap-3 text-sm font-semibold text-ink">
                      剧情梗概
                      {sourceMode === "synopsis" ? <Check className="h-4 w-4 text-vermilion" /> : null}
                    </span>
                    <span className="mt-1 block text-xs leading-5 text-muted">适合让流程从简要剧情开始展开。</span>
                  </label>
                </div>
                <div className="mt-4">
                  <Label htmlFor={sourceMode === "source_text" ? "pipeline-source-text" : "pipeline-synopsis"}>
                    {sourceMode === "source_text" ? "原文" : "剧情梗概"}
                  </Label>
                  <Textarea
                    id={sourceMode === "source_text" ? "pipeline-source-text" : "pipeline-synopsis"}
                    value={form[sourceMode]}
                    rows={9}
                    placeholder={sourceMode === "source_text" ? "粘贴这一章的原文…" : "描述这一章的主要人物、冲突和结局…"}
                    aria-invalid={Boolean(fieldErrors[sourceMode])}
                    onChange={(event) => setField(sourceMode, event.target.value)}
                  />
                  {fieldErrors[sourceMode] ? <p className="mt-1.5 text-xs text-[#a4362d]">{fieldErrors[sourceMode]}</p> : null}
                </div>
              </fieldset>

              <div className="mt-6 grid gap-5 sm:grid-cols-2">
                <div>
                  <Label htmlFor="pipeline-duration">目标时长（秒）</Label>
                  <Input
                    id="pipeline-duration"
                    type="number"
                    min="1"
                    max="3600"
                    step="1"
                    value={form.target_duration_sec}
                    aria-invalid={Boolean(fieldErrors.target_duration_sec)}
                    onChange={(event) => setField("target_duration_sec", event.target.value)}
                  />
                  {fieldErrors.target_duration_sec ? <p className="mt-1.5 text-xs text-[#a4362d]">{fieldErrors.target_duration_sec}</p> : null}
                </div>
                <div>
                  <Label htmlFor="pipeline-aspect-ratio">画面比例</Label>
                  <Select
                    id="pipeline-aspect-ratio"
                    value={form.aspect_ratio}
                    onChange={(event) => setField("aspect_ratio", event.target.value)}
                  >
                    <option value="9:16">9:16 竖屏</option>
                    <option value="16:9">16:9 横屏</option>
                    <option value="1:1">1:1 方形</option>
                  </Select>
                </div>
              </div>

              <div className="mt-6">
                <Label htmlFor="pipeline-style-prompt">风格提示（可选）</Label>
                <Textarea
                  id="pipeline-style-prompt"
                  rows={4}
                  value={form.style_prompt}
                  placeholder="例如：低饱和水彩、线条清晰、柔和侧光…"
                  onChange={(event) => setField("style_prompt", event.target.value)}
                />
              </div>

              <div className="mt-7 border-t border-line pt-6">
                <h3 className="text-sm font-bold">运行选项</h3>
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <ToggleField
                    id="pipeline-auto-run"
                    label="创建后自动运行"
                    description="提交后立即把第一阶段加入队列。"
                    checked={form.auto_run}
                    onChange={(checked) => setField("auto_run", checked)}
                  />
                  <ToggleField
                    id="pipeline-audio"
                    label="生成音频"
                    description="为交付阶段保留配音选项。"
                    checked={form.generate_audio}
                    onChange={(checked) => setField("generate_audio", checked)}
                  />
                  <ToggleField
                    id="pipeline-subtitle"
                    label="生成字幕"
                    description="为交付阶段保留字幕选项。"
                    checked={form.generate_subtitle}
                    onChange={(checked) => setField("generate_subtitle", checked)}
                  />
                  <ToggleField
                    id="pipeline-bgm"
                    label="生成 BGM"
                    description="为交付阶段保留背景音乐选项。"
                    checked={form.generate_bgm}
                    onChange={(checked) => setField("generate_bgm", checked)}
                  />
                  <ToggleField
                    id="pipeline-composition"
                    label="生成合成片"
                    description="为交付阶段保留最终合成选项。"
                    checked={form.generate_composition}
                    onChange={(checked) => setField("generate_composition", checked)}
                  />
                </div>
              </div>

              <div className="mt-7 flex flex-wrap justify-end gap-3 border-t border-line pt-6">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => navigate(projectId ? `/free-create/${projectId}` : "/free-create")}
                >
                  取消
                </Button>
                <Button type="submit" variant="primary" busy={submitting}>
                  <Play className="h-4 w-4" />
                  创建并打开工作区
                </Button>
              </div>
            </Card>
          </form>

          <aside className="space-y-5">
            <Card className="border-white/10 bg-[#1b2521] p-5 text-white">
              <div className="flex items-center gap-3">
                <Sparkles className="h-5 w-5 text-[#f29a70]" />
                <h2 className="font-bold">工作区会保留什么</h2>
              </div>
              <ul className="mt-5 space-y-4 text-sm leading-6 text-white/55">
                <li className="border-l border-[#f29a70]/50 pl-3">输入原文或梗概的快照</li>
                <li className="border-l border-[#f29a70]/50 pl-3">每个阶段的原始、规范化和草稿内容</li>
                <li className="border-l border-[#f29a70]/50 pl-3">服务端返回的门禁诊断和 revision</li>
              </ul>
            </Card>
            <Card className="border-white/10 bg-[#151d1a] p-5 text-white">
              <p className="text-xs font-bold uppercase tracking-[0.16em] text-white/35">提交前检查</p>
              <p className="mt-3 text-sm leading-6 text-white/55">
                原文和剧情梗概只能选择一个。创建请求成功后才会产生运行和章节资源。
              </p>
            </Card>
          </aside>
        </div>
      </main>
    </div>
  );
}
