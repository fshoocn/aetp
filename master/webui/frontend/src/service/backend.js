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
const TOKEN_STORAGE_KEY = 'aetp.authToken';

/** JSON 请求默认超时（毫秒）：后端卡死时不让调用方无限挂起。 */
const REQUEST_TIMEOUT_MS = 30000;

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

/** 当前访问令牌（``X-AETP-Token``）；空字符串表示未设置。 */
export function getAuthToken() {
    try {
        return window.localStorage.getItem(TOKEN_STORAGE_KEY) || '';
    } catch {
        return '';
    }
}

/** 设置访问令牌（空值 = 清除）；后端地址切换器的令牌输入框用。 */
export function setAuthToken(value) {
    const token = String(value ?? '')
        .trim();
    try {
        if (token) window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
        else window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    } catch {
        // 存储不可用时仅本次会话生效
    }
    return token;
}

/**
 * 归一化后端地址：去空白与尾部斜杠；空值表示同源。
 *
 * @throws {TypeError} 非 http(s) 地址或无法解析的 URL
 */
export function normalizeBackendUrl(value) {
    const text = String(value ?? '')
        .trim()
        .replace(/\/+$/, '');
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
 *  后端若返回结构化 `details`（如同 id 冲突的新旧版本），挂在 `error.details` 上。
 *  默认 30 秒超时；调用方自带 `signal` 时由调用方控制取消（如弹窗竞态防护）。 */
export async function request(url, options = {}) {
    const signal = options.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS);
    const token = getAuthToken();
    let response;
    try {
        response = await fetch(resolveUrl(url), {
            ...options,
            headers: { ...(options.headers || {}), ...(token ? { 'X-AETP-Token': token } : {}) },
            signal
        });
    } catch (reason) {
        if (reason instanceof Error && (reason.name === 'TimeoutError' || reason.name === 'AbortError')) {
            throw new Error('请求超时或已取消');
        }
        throw reason;
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(data.error || `请求失败 (${response.status})`);
        error.details = data.details;
        error.status = response.status;
        throw error;
    }
    return data;
}
