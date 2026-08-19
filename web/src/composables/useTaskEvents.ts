import { onUnmounted, watch } from "vue";
import type { QueryClient } from "@tanstack/vue-query";
import { connectEvents, type DomainEvent } from "@/api/sse";
import { useProjectStore } from "@/stores/project";

/**
 * 订阅任务相关 SSE 事件，并失效 TanStack Query 缓存。
 * 任务创建/状态变更后，任务列表、任务详情与日志会自动刷新。
 */
export function useTaskEvents(queryClient: QueryClient) {
  const projectStore = useProjectStore();
  let stop: (() => void) | null = null;

  const stopWatching = watch(
    () => projectStore.currentProjectId,
    (projectId) => {
      stop?.();
      stop = projectId
        ? connectEvents(projectId, (ev: DomainEvent) => {
            if (ev.type === "task.created" || ev.type === "task.updated" || ev.type.startsWith("run.") || ev.type.startsWith("node.")) {
              queryClient.invalidateQueries({ queryKey: ["tasks"] });
              queryClient.invalidateQueries({ queryKey: ["task"] });
              queryClient.invalidateQueries({ queryKey: ["logs"] });
              queryClient.invalidateQueries({ queryKey: ["runs"] });
              queryClient.invalidateQueries({ queryKey: ["run"] });
              queryClient.invalidateQueries({ queryKey: ["assets", "nodes"] });
              queryClient.invalidateQueries({ queryKey: ["projectNodes"] });
            }
          })
        : null;
    },
    { immediate: true }
  );

  onUnmounted(() => {
    stopWatching();
    stop?.();
  });
}
