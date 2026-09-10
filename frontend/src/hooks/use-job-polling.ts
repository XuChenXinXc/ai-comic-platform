import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, queryKeys } from "../api/client";

const ACTIVE_STATUSES = new Set([
  "queued",
  "running",
  "waiting_provider",
  "downloading",
]);

export function useJobPolling(projectId: string) {
  const queryClient = useQueryClient();
  const lastTerminalSignature = useRef("");
  // 是否已记录初始基线：首次拿到任务数据时把已有终态签名记为基线，
  // 避免挂载时已完成的历史任务触发一次全量失效。
  const hasBaselineRef = useRef(false);
  // 切换项目时基线必须重置：旧项目的终态签名与新项目数据必然不同，
  // 否则会触发一次多余的全前缀失效风暴。
  const baselineProjectRef = useRef(projectId);
  if (baselineProjectRef.current !== projectId) {
    baselineProjectRef.current = projectId;
    hasBaselineRef.current = false;
    lastTerminalSignature.current = "";
  }
  const query = useQuery({
    queryKey: queryKeys.jobs(projectId),
    queryFn: () => api.jobs.list({ project_id: projectId }),
    enabled: Boolean(projectId),
    refetchInterval: (result) => {
      const jobs = result.state.data || [];
      // 无活跃任务时大幅拉长间隔：任务只由本页操作触发，低频兜底即可，
      // 避免空闲页面每 10 秒空轮询一次。
      return jobs.some((job) => ACTIVE_STATUSES.has(job.status)) ? 1_500 : 30_000;
    },
  });

  const terminalSignature = (query.data || [])
    .filter((job) => !ACTIVE_STATUSES.has(job.status))
    .map((job) => `${job.id}:${job.status}:${job.updated_at}`)
    .sort()
    .join("|");

  useEffect(() => {
    // 任务数据尚未到达时不记录基线（空签名不代表任务状态）。
    if (query.data === undefined) return;
    if (!hasBaselineRef.current) {
      hasBaselineRef.current = true;
      lastTerminalSignature.current = terminalSignature;
      return;
    }
    if (lastTerminalSignature.current !== terminalSignature) {
      const prefixes = [
        "projects",
        "analysis",
        "screenplay",
        "assets",
        "asset-images",
        "chapters",
        "shots",
        "keyframes",
        "videos",
        "shot-quality",
      ];
      void queryClient.invalidateQueries({
        predicate: (query) => {
          const key = query.queryKey;
          const first = String(key[0]);
          if (!prefixes.includes(first)) return false;
          const second = key[1];
          // 无第二维度的全局列表（projects）必须刷新。
          if (second === undefined) return true;
          // 键维度即 projectId 的查询精确匹配当前项目，避免误失效其他项目缓存。
          if (
            first === "projects" ||
            first === "assets" ||
            first === "chapters" ||
            first === "shot-quality"
          ) {
            return String(second) === projectId;
          }
          // analysis/screenplay/asset-images/shots/keyframes/videos 的键维度是
          // 章节/素材/分镜 ID，无法直接映射项目；其活跃观察者都在当前项目页面
          // 上下文中，保持按前缀失效。
          return true;
        },
      });
    }
    lastTerminalSignature.current = terminalSignature;
  }, [projectId, queryClient, query.data, terminalSignature]);

  return query;
}

export function isJobActive(status: string) {
  return ACTIVE_STATUSES.has(status);
}
