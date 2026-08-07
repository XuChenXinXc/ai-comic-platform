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
  const query = useQuery({
    queryKey: queryKeys.jobs(projectId),
    queryFn: () => api.jobs.list({ project_id: projectId }),
    enabled: Boolean(projectId),
    refetchInterval: (result) => {
      const jobs = result.state.data || [];
      return jobs.some((job) => ACTIVE_STATUSES.has(job.status)) ? 1_500 : 10_000;
    },
  });

  const terminalSignature = (query.data || [])
    .filter((job) => !ACTIVE_STATUSES.has(job.status))
    .map((job) => `${job.id}:${job.status}:${job.updated_at}`)
    .sort()
    .join("|");

  useEffect(() => {
    if (!terminalSignature) return;
    if (lastTerminalSignature.current !== terminalSignature) {
      const prefixes = [
        "projects",
        "analysis",
        "screenplay",
        "assets",
        "asset-images",
        "shot-quality",
        "shots",
        "keyframes",
        "videos",
      ];
      void queryClient.invalidateQueries({
        predicate: (item) => prefixes.includes(String(item.queryKey[0])),
      });
    }
    lastTerminalSignature.current = terminalSignature;
  }, [queryClient, terminalSignature]);

  return query;
}

export function isJobActive(status: string) {
  return ACTIVE_STATUSES.has(status);
}
