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
  onEvent: (ev: DomainEvent) => void
): () => void {
  const token = localStorage.getItem("token");
  if (!token || !projectId) return () => {};

  let stopped = false;
  let controller: AbortController | null = null;
  let retryTimer: number | null = null;
  let retryMs = RETRY_BASE_MS;
  let lastEventId = "";

  async function connect() {
    if (stopped) return;
    controller = new AbortController();
    try {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${token}`,
      };
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;
      const resp = await fetch(
        `${BASE}/api/v1/events?project_id=${encodeURIComponent(projectId)}`,
        {
        headers,
        signal: controller.signal,
        }
      );
      if (!resp.ok || !resp.body) throw new Error(`sse status ${resp.status}`);
      retryMs = RETRY_BASE_MS; // 连接成功，重置退避

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      // 定时重连检查：fetch 流可能因代理空闲超时静默断开
      while (!stopped) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const idLine = chunk.split("\n").find((l) => l.startsWith("id: "));
          const dataLine = chunk
            .split("\n")
            .find((l) => l.startsWith("data: "));
          if (dataLine) {
            try {
              const event = JSON.parse(dataLine.slice(6)) as DomainEvent;
              if (idLine) lastEventId = idLine.slice(4).trim();
              else if (event.sequence != null) lastEventId = String(event.sequence);
              onEvent(event);
            } catch {
              // 忽略无法解析的事件
            }
          }
        }
      }
    } catch {
      // 网络错误 / 主动 abort：走重连
    }
    if (!stopped) {
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
