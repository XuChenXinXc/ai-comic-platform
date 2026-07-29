import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Film } from "lucide-react";
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
import { formatError, makeDownloadName } from "../lib/utils";

export function VideosPage() {
  const queryClient = useQueryClient();
  const { activeProject, activeProjectId } = useProject();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scope, setScope] = useState<"all" | "scene">("all");
  const [sceneNo, setSceneNo] = useState("");
  const [exporting, setExporting] = useState(false);
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
    mutationFn: () => api.shots.videos.batch(activeProjectId, batchBody),
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
          `已入队 ${result.jobs.length} 个视频任务，跳过 ${failures.length} 项`,
          { description },
        );
        return;
      }
      toast.success(`已入队 ${result.jobs.length} 个视频任务`);
    },
    onError: (error) => toast.error(formatError(error)),
  });

  const exportZip = async () => {
    if (!activeProject) return;
    setExporting(true);
    try {
      await api.projects.exportVideos(
        activeProject.id,
        makeDownloadName(`${activeProject.name}-分镜视频`, "zip"),
      );
      toast.success("ZIP 导出已开始下载");
    } catch (error) {
      toast.error(formatError(error));
    } finally {
      setExporting(false);
    }
  };

  return (
    <ProjectRequired>
      <div>
        <PageHeader
          eyebrow="Step 07 / Video clips"
          title="分镜视频"
          description="当前关键帧与时间化视频提示词共同驱动单镜生成。单项失败不会影响其他镜头，最终可导出当前版本 ZIP。"
          actions={
            <>
              <Button busy={exporting} onClick={() => void exportZip()}>
                <Archive className="h-4 w-4" />
                导出当前版本 ZIP
              </Button>
              <Button
                variant="primary"
                busy={batchMutation.isPending}
                disabled={
                  !shots.length ||
                  (scope === "scene" && !sceneNo && selected.size === 0)
                }
                onClick={() => batchMutation.mutate()}
              >
                <Film className="h-4 w-4" />
                {selected.size
                  ? `生成所选（${selected.size}）`
                  : scope === "scene"
                    ? "生成当前场次"
                    : "生成全部视频"}
              </Button>
            </>
          }
        />

        <Card className="mb-6 flex flex-col justify-between gap-4 p-4 lg:flex-row lg:items-end">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <Label htmlFor="video-scope">批量范围</Label>
              <Select
                id="video-scope"
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
                <Label htmlFor="video-scene">场次编号</Label>
                <Input
                  id="video-scene"
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

        {shotsQuery.isLoading ? <LoadingState label="正在读取分镜视频…" /> : null}
        {shotsQuery.error ? (
          <ErrorState
            message={formatError(shotsQuery.error)}
            onRetry={() => void shotsQuery.refetch()}
          />
        ) : null}
        {!shotsQuery.isLoading && !shotsQuery.error && !shots.length ? (
          <EmptyState
            title="还没有可生成的视频分镜"
            description="先完成分镜与关键帧，再从当前关键帧生成单镜视频。"
          />
        ) : null}

        <div className="space-y-5">
          {shots.map((shot) => (
            <ShotMediaCard
              key={shot.id}
              shot={shot}
              kind="video"
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
