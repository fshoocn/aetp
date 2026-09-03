/**
 * 插件 UI 宿主 postMessage 桥（规范 §6.3 消息协议）。
 *
 * 同源 iframe 内的插件页面不得直接请求平台 API；本桥负责：
 * - 宿主 -> 插件：initialize / context.updated / validation.result / command.result
 * - 插件 -> 宿主：ready / configuration.changed / requirements.preview / validate / command.request
 *
 * 所有消息使用结构化对象：{ protocol: "aetp.plugin-ui.v2", session_id, request_id,
 * type, payload }。宿主校验 event.origin / event.source / protocol / session_id，
 * 拒绝未知来源与不兼容版本；未知消息只记录，不抛错。
 */
interface IframeRef {
  value: HTMLIFrameElement | null;
}

export const PLUGIN_UI_PROTOCOL = "aetp.plugin-ui.v2" as const;

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

interface PluginUiMessage<TType extends string, TPayload> {
  protocol: typeof PLUGIN_UI_PROTOCOL;
  session_id: string;
  request_id: string;
  type: TType;
  payload: TPayload;
}

export interface PluginUiContext {
  plugin_id: string;
  version: string;
  point: string;
  display_name: string;
  ui_protocol_version: number | null;
  project_id?: string | null;
}

type JsonMap = { [key: string]: JsonValue };

interface PluginUiBridgeOptions {
  iframe: IframeRef;
  context: PluginUiContext;
  onReady?: (payload: JsonMap) => void;
  onConfigurationChanged?: (payload: { configuration?: JsonMap }) => void;
  onValidate?: (payload: { configuration?: JsonMap }) => void;
  onRequirementsPreview?: (payload: JsonMap) => void;
  onCommandRequest?: (payload: JsonMap) => void;
}

export interface PluginUiBridge {
  initialize: () => void;
  send: <T extends JsonMap>(type: string, payload: T) => void;
  destroy: () => void;
}

function randomId(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${rand}`;
}

export function usePluginUiBridge(options: PluginUiBridgeOptions): PluginUiBridge {
  const { iframe, context } = options;
  const sessionId = randomId("uis");
  let handler: ((event: MessageEvent) => void) | null = null;

  function sendRaw<TType extends string, TPayload>(type: TType, payload: TPayload): void {
    const frame = iframe.value?.contentWindow;
    if (!frame) return;
    const message: PluginUiMessage<TType, TPayload> = {
      protocol: PLUGIN_UI_PROTOCOL,
      session_id: sessionId,
      request_id: randomId("req"),
      type,
      payload,
    };
    // 同源 iframe；指定 origin 让浏览器拒绝跨源误投，配合下方校验形成双向防护
    frame.postMessage(message, window.location.origin);
  }

  function handleMessage(event: MessageEvent): void {
    const frame = iframe.value;
    if (!frame) return;
    // 宿主侧校验：必须同源、来自目标 iframe、协议匹配、会话匹配
    if (event.origin !== window.location.origin) return;
    if (event.source !== frame.contentWindow) return;
    const data = event.data as PluginUiMessage<string, JsonMap> | null;
    if (!data || data.protocol !== PLUGIN_UI_PROTOCOL) return;
    if (data.session_id && data.session_id !== sessionId) return;
    switch (data.type) {
      case "ready":
        options.onReady?.(data.payload);
        break;
      case "configuration.changed":
        options.onConfigurationChanged?.(data.payload);
        break;
      case "validate":
        options.onValidate?.(data.payload);
        break;
      case "requirements.preview":
        options.onRequirementsPreview?.(data.payload);
        break;
      case "command.request":
        options.onCommandRequest?.(data.payload);
        break;
      default:
        // 未知消息类型：记录但不抛错，避免插件破坏宿主
        console.warn("[plugin-ui] 未知消息类型", data.type);
    }
  }

  function initialize(): void {
    sendRaw("initialize", { context });
  }

  function send<T extends JsonMap>(type: string, payload: T): void {
    sendRaw(type, payload);
  }

  function destroy(): void {
    if (handler) {
      window.removeEventListener("message", handler);
      handler = null;
    }
  }

  handler = handleMessage;
  window.addEventListener("message", handleMessage);

  return { initialize, send, destroy };
}

export type { JsonValue, JsonMap };
