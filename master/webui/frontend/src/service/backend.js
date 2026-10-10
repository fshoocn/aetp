/**
 * 后端地址管理：SPA 默认与页面同源，可切换到其他 AETP 节点的后端。
 *
 * - 地址存 localStorage（键 `aetp.backendUrl`），刷新后保持；
 * - `resolveUrl()` 把 `/api/...`、`/plugin-ui/...`、`/ui-static/...` 等站内路径
 *   解析到当前后端；
 * - `request()` 是 JSON 请求封装（统一错误信息），各调用点共用。
 *
 * ⚠️ 跨源切换（后端不在页面同源）要求目标后端允许 CORS，或经反向代理以
 * 同源路径暴露。
 */

const STORAGE_KEY = 'aetp.backendUrl';

function readStored() {
    try {
        return window.localStorage.getItem(STORAGE_KEY) || '';
    } catch {
        return '';
    }
}

let backendUrl = readStored();

/** 当前后端地址；空字符串表示与页面同源。 */
export function getBackendUrl() {
    return backendUrl;
}

/**
 * 归一化后端地址：去空白与尾部斜杠；空值表示同源。
 *
 * @throws {TypeError} 非 http(s) 地址或无法解析的 URL
 */
export function normalizeBackendUrl(value) {
    const text = String(value ?? '').trim().replace(/\/+$/, '');
    if (!text) return '';
    let parsed;
    try {
        parsed = new URL(text);
    } catch {
        throw new TypeError(`后端地址无效：${text}`);
    }
    if (!/^https?:$/.test(parsed.protocol)) {
        throw new TypeError('后端地址必须是 http(s) 协议');
    }
    return text;
}

/**
 * 设置后端地址（空字符串 = 回到与页面同源），并持久化到 localStorage。
 *
 * @throws {TypeError} 地址不合法（见 :func:`normalizeBackendUrl`）
 */
export function setBackendUrl(value) {
    const normalized = normalizeBackendUrl(value);
    backendUrl = normalized;
    try {
        if (normalized) window.localStorage.setItem(STORAGE_KEY, normalized);
        else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
        // 存储不可用时仅本次会话生效
    }
    return normalized;
}

/** 把站内路径解析到当前后端；已是绝对 URL 的原样返回。 */
export function resolveUrl(path) {
    const text = String(path ?? '');
    if (!backendUrl || text.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(text)) {
        return text;
    }
    return text.startsWith('/') ? `${backendUrl}${text}` : `${backendUrl}/${text}`;
}

/** 发送 JSON 请求（自动拼当前后端地址），失败时抛带后端错误信息的 Error。
 *  后端若返回结构化 `details`（如同 id 冲突的新旧版本），挂在 `error.details` 上。 */
export async function request(url, options = {}) {
    const response = await fetch(resolveUrl(url), options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(data.error || `请求失败 (${response.status})`);
        error.details = data.details;
        error.status = response.status;
        throw error;
    }
    return data;
}
