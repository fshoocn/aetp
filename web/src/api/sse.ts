import { clearSession, refreshAccessToken } from "@/api/client";
/**
 * SSE 实时事件流客户端。
 *
 * 使用 fetch + ReadableStream（而非 EventSource），以便携带 Authorization
 * 头（JWT 在 localStorage 中）。断线自动重连（指数退避，上限 15s）。
 *
 * 服务端事件格式（SSE）：`data: <json>\n\n`
 *   { "type": "task.created", "data": {...}, "ts": "..." }
 */

const BASE = "";
const RETRY_BASE_MS = 2000;
const RETRY_MAX_MS = 15000;

export interface DomainEvent {
  event_id?: string;
  sequence?: number | null;
  project_id?: string | null;
  type: string;
  data: Record<string, unknown>;
  ts: string;
}

export function connectEvents(
  projectId: string,
  onEvent: (ev: DomainEvent) => void,
  onError?: (error: Error) => void,
  onOpen?: () => void
): () => void {
  if (!projectId) return () => {};
  return connectSse(
    `${BASE}/api/v2/events?project_id=${encodeURIComponent(projectId)}`,
    onEvent,
    onError,
    onOpen,
  );
}

export function connectAgentLogs(
  nodeId: string,
  onEvent: (ev: DomainEvent) => void,
  onError?: (error: Error) => void,
  onOpen?: () => void,
): () => void {
  if (!nodeId) return () => {};
  return connectSse(
    `${BASE}/api/v2/nodes/${encodeURIComponent(nodeId)}/logs/stream`,
    onEvent,
    onError,
    onOpen,
  );
}

function connectSse(
  url: string,
  onEvent: (ev: DomainEvent) => void,
  onError?: (error: Error) => void,
  onOpen?: () => void,
): () => void {
  if (!localStorage.getItem("token")) return () => {};

  let stopped = false;
  let controller: AbortController | null = null;
  let retryTimer: number | null = null;
  let retryMs = RETRY_BASE_MS;
  let lastEventId = "";

  async function connect() {
    if (stopped) return;
    controller = new AbortController();
    try {
      const token = localStorage.getItem("token");
      if (!token) return;
      const headers: Record<string, string> = {
        Authorization: `Bearer ${token}`,
      };
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;
      const resp = await fetch(url, { headers, signal: controller.signal });
      if (resp.status === 401) {
        if (await refreshAccessToken()) return connect();
        clearSession();
        window.location.hash = "#/login";
        return;
      }
      if (!resp.ok || !resp.body) {
        throw new Error(`SSE status ${resp.status}`);
      }
      retryMs = RETRY_BASE_MS; // 连接成功，重置退避
      onOpen?.();

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamClosed = false;
      // 定时重连检查：fetch 流可能因代理空闲超时静默断开
      while (!stopped) {
        const { done, value } = await reader.read();
        if (done) {
          streamClosed = true;
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const lines = chunk.split("\n");
          const idLine = lines.find((line) => line.startsWith("id:"));
          const dataLines = lines
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).replace(/^ /, ""));
          if (idLine) lastEventId = idLine.slice(3).trim();
          if (dataLines.length) {
            try {
              const event = JSON.parse(dataLines.join("\n")) as DomainEvent;
              if (!idLine && event.sequence != null) lastEventId = String(event.sequence);
              onEvent(event);
            } catch {
              // 忽略无法解析的事件
            }
          }
        }
      }
      if (streamClosed && !stopped) onError?.(new Error("SSE stream closed"));
    } catch (err: unknown) {
      // 网络错误 / 主动 abort：走重连
      if (!stopped && err instanceof Error && err.name !== "AbortError") {
        onError?.(err);
      }
    }
    if (!stopped && localStorage.getItem("token")) {
      retryTimer = window.setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
    }
  }

  connect();

  return () => {
    stopped = true;
    if (retryTimer !== null) window.clearTimeout(retryTimer);
    controller?.abort();
  };
}
