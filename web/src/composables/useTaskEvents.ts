import { onUnmounted, ref, watch } from "vue";
import type { QueryClient } from "@tanstack/vue-query";
import { connectEvents, type DomainEvent } from "@/api/sse";
import { useProjectStore } from "@/stores/project";

/**
 * 订阅任务相关 SSE 事件，并失效 TanStack Query 缓存。
 * 任务创建/状态变更后，任务列表、任务详情与日志会自动刷新。
 *
 * 返回值暴露连接状态与最近错误，便于 UI 展示连接指示器。
 */
export function useTaskEvents(queryClient: QueryClient) {
  const projectStore = useProjectStore();
  let stop: (() => void) | null = null;

  /** SSE 连接是否活跃 */
  const connected = ref(false);
  /** 最近一次连接错误（成功后清空） */
  const lastError = ref<string | null>(null);
  /** 已连续重试次数（由底层 sse.ts 重连逻辑驱动） */
  const retryCount = ref(0);

  const stopWatching = watch(
    () => projectStore.currentProjectId,
    (projectId) => {
      stop?.();
      connected.value = false;
      lastError.value = null;
      retryCount.value = 0;

      if (!projectId) {
        stop = null;
        return;
      }

      // connectEvents 返回断连时自动重试的连接；onError 回调用于状态追踪
      stop = connectEvents(
        projectId,
        (ev: DomainEvent) => {
          // 首次收到事件时标记已连接
          if (!connected.value) {
            connected.value = true;
            lastError.value = null;
            retryCount.value = 0;
          }

          if (
            ev.type === "task.created" ||
            ev.type === "task.updated" ||
            ev.type.startsWith("run.") ||
            ev.type.startsWith("node.")
          ) {
            queryClient.invalidateQueries({ queryKey: ["tasks"] });
            queryClient.invalidateQueries({ queryKey: ["task"] });
            queryClient.invalidateQueries({ queryKey: ["logs"] });
            queryClient.invalidateQueries({ queryKey: ["runs"] });
            queryClient.invalidateQueries({ queryKey: ["run"] });
            queryClient.invalidateQueries({ queryKey: ["assets", "nodes"] });
            queryClient.invalidateQueries({ queryKey: ["projectNodes"] });
          }
        },
        (error: Error) => {
          connected.value = false;
          lastError.value = error.message || "SSE connection lost";
          retryCount.value += 1;
          console.warn(
            `[useTaskEvents] SSE error (retry #${retryCount.value}):`,
            error.message
          );
        },
        () => {
          connected.value = true;
          lastError.value = null;
          retryCount.value = 0;
        },
      );
    },
    { immediate: true }
  );

  onUnmounted(() => {
    stopWatching();
    stop?.();
    connected.value = false;
  });

  return { connected, lastError, retryCount };
}
