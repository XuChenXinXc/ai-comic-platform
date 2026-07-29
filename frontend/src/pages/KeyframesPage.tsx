import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Images } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api, queryKeys } from "../api/client";
import { ProjectRequired } from "../components/project-required";
import { ShotMediaCard } from "../components/shot-media-card";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from "../components/status";
import { Button, Card, Input, Label, Select } from "../components/ui";
import { useProject } from "../hooks/use-project";
import { formatError } from "../lib/utils";

export function KeyframesPage() {
  const queryClient = useQueryClient();
  const { activeProjectId } = useProject();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scope, setScope] = useState<"all" | "scene">("all");
  const [sceneNo, setSceneNo] = useState("");
  const shotsQuery = useQuery({
    queryKey: queryKeys.shots(activeProjectId),
    queryFn: () => api.shots.list(activeProjectId),
    enabled: Boolean(activeProjectId),
  });
  const assetsQuery = useQuery({
    queryKey: queryKeys.assets(activeProjectId),
    queryFn: () => api.assets.list(activeProjectId),
    enabled: Boolean(activeProjectId),
  });
  const shots = useMemo(() => shotsQuery.data || [], [shotsQuery.data]);
  const assetMap = useMemo(
    () => new Map((assetsQuery.data || []).map((asset) => [asset.id, asset])),
    [assetsQuery.data],
  );
  const batchBody = useMemo(() => {
    if (selected.size) return { ids: [...selected] };
    if (scope === "scene" && sceneNo) return { scene_no: Number(sceneNo) };
    return {};
  }, [sceneNo, scope, selected]);

  useEffect(() => {
    setSelected(new Set());
  }, [activeProjectId]);

  const batchMutation = useMutation({
    mutationFn: () =>
      api.shots.keyframes.batch(activeProjectId, batchBody),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(activeProjectId),
      });
      const failures = result.preflight_failures ?? [];
      if (failures.length) {
        const description = failures
          .map(({ target_id, error }) => {
            const shot = shots.find((item) => item.id === target_id);
            return `分镜 ${shot?.shot_no ?? target_id}：${error}`;
          })
          .join("；");
        toast.warning(
          `已入队 ${result.jobs.length} 个关键帧任务，跳过 ${failures.length} 项`,
          { description },
        );
        return;
      }
      toast.success(`已入队 ${result.jobs.length} 个关键帧任务`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  return (
    <ProjectRequired>
      <div>
        <PageHeader
          eyebrow="Step 06 / Keyframes"
          title="分镜关键帧"
          description="先锁定人物、场景与构图，再把确定画面交给视频模型。参考图缺失时会提示，但允许纯文字降级。"
          actions={
            <Button
              variant="primary"
              busy={batchMutation.isPending}
              disabled={
                !shots.length ||
                (scope === "scene" && !sceneNo && selected.size === 0)
              }
              onClick={() => batchMutation.mutate()}
            >
              <Images className="h-4 w-4" />
              {selected.size
                ? `生成所选（${selected.size}）`
                : scope === "scene"
                  ? "生成当前场次"
                  : "生成全部关键帧"}
            </Button>
          }
        />

        <Card className="mb-6 flex flex-col justify-between gap-4 p-4 lg:flex-row lg:items-end">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <Label htmlFor="keyframe-scope">批量范围</Label>
              <Select
                id="keyframe-scope"
                value={scope}
                className="w-40"
                onChange={(event) =>
                  setScope(event.target.value as "all" | "scene")
                }
              >
                <option value="all">全部分镜</option>
                <option value="scene">指定场次</option>
              </Select>
            </div>
            {scope === "scene" ? (
              <div>
                <Label htmlFor="keyframe-scene">场次编号</Label>
                <Input
                  id="keyframe-scene"
                  type="number"
                  min={1}
                  className="w-32"
                  value={sceneNo}
                  onChange={(event) => setSceneNo(event.target.value)}
                />
              </div>
            ) : null}
          </div>
          {shots.length ? (
            <label className="flex items-center gap-2 py-2 text-xs font-semibold text-muted">
              <input
                type="checkbox"
                className="accent-vermilion"
                checked={shots.every((shot) => selected.has(shot.id))}
                onChange={(event) =>
                  setSelected(
                    event.target.checked
                      ? new Set(shots.map((shot) => shot.id))
                      : new Set(),
                  )
                }
              />
              全选分镜
            </label>
          ) : null}
        </Card>

        {shotsQuery.isLoading ? <LoadingState label="正在读取分镜…" /> : null}
        {shotsQuery.error ? (
          <ErrorState
            message={formatError(shotsQuery.error)}
            onRetry={() => void shotsQuery.refetch()}
          />
        ) : null}
        {!shotsQuery.isLoading && !shotsQuery.error && !shots.length ? (
          <EmptyState
            title="还没有可生成的分镜"
            description="先在“分镜与提示词”页面提取分镜并补齐关键帧图片提示词。"
          />
        ) : null}

        <div className="space-y-5">
          {shots.map((shot) => (
            <ShotMediaCard
              key={shot.id}
              shot={shot}
              kind="keyframe"
              assetMap={assetMap}
              selected={selected.has(shot.id)}
              onToggle={(checked) =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (checked) next.add(shot.id);
                  else next.delete(shot.id);
                  return next;
                })
              }
            />
          ))}
        </div>
      </div>
    </ProjectRequired>
  );
}
