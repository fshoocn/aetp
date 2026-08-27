const BASE = ""; // 所有 API 调用前缀（开发时 vite proxy 处理 /api）

const ACCESS_KEY = "token";
const REFRESH_KEY = "refresh_token";
const USER_KEY = "user";

function getToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

export function clearSession(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(USER_KEY);
}

function storeTokens(body: { access_token: string; refresh_token: string }) {
  localStorage.setItem(ACCESS_KEY, body.access_token);
  localStorage.setItem(REFRESH_KEY, body.refresh_token);
}

/** 并发去重的静默刷新：多个 401 同时到达时只发起一次 /auth/refresh。 */
let refreshPromise: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  const raw = localStorage.getItem(REFRESH_KEY);
  if (!raw) return false;
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const resp = await fetch(`${BASE}/api/v1/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: raw }),
        });
        if (!resp.ok) {
          clearSession();
          return false;
        }
        const data = (await resp.json()) as {
          access_token: string;
          refresh_token: string;
        };
        storeTokens(data);
        return true;
      } catch {
        return false;
      }
    })().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

export const refreshAccessToken = tryRefresh;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function extractDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    // 三段式错误响应：{"code","message","data"}
    const msg = (body as { message?: unknown }).message;
    if (typeof msg === "string" && "code" in body) return msg;
    // 旧式 detail 响应与 FastAPI 422 校验错误
    if ("detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      // FastAPI 422 校验错误：detail 为数组，取第一条
      if (Array.isArray(detail) && detail.length > 0) {
        const first = detail[0] as { msg?: string; loc?: unknown[] };
        const loc = Array.isArray(first?.loc)
          ? first.loc.filter((x) => typeof x === "string").join(".")
          : "";
        return `${loc ? `${loc}: ` : ""}${first?.msg ?? "参数错误"}`;
      }
    }
  }
  return fallback;
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  retried = false
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options.headers as Record<string, string>) || {}),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const resp = await fetch(`${BASE}${path}`, { ...options, headers });

  const isAuthEndpoint = path.includes("/auth/login") || path.includes("/auth/refresh");

  if (resp.status === 401 && !isAuthEndpoint && !retried) {
    // 访问令牌过期：静默用刷新令牌换新令牌后重试一次原请求
    const refreshed = await tryRefresh();
    if (refreshed) {
      return request<T>(path, options, true);
    }
  }

  if (resp.status === 401) {
    clearSession();
    if (window.location.hash !== "#/login") {
      window.location.hash = "#/login";
    }
    throw new ApiError(401, "登录已过期，请重新登录");
  }

  if (resp.status === 204) {
    return undefined as T;
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new ApiError(resp.status, extractDetail(body, `HTTP ${resp.status}`));
  }

  return resp.json();
}

async function requestBlob(path: string, retried = false): Promise<Blob> {
  const token = getToken();
  const resp = await fetch(`${BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (resp.status === 401 && !retried && await tryRefresh()) {
    return requestBlob(path, true);
  }
  if (resp.status === 401) {
    clearSession();
    window.location.hash = "#/login";
    throw new ApiError(401, "登录已过期，请重新登录");
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new ApiError(resp.status, extractDetail(body, `HTTP ${resp.status}`));
  }
  return resp.blob();
}

export const api = {
  get<T>(path: string): Promise<T> {
    return request<T>(path);
  },
  post<T>(path: string, body?: unknown): Promise<T> {
    return request<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    });
  },
  patch<T>(path: string, body?: unknown): Promise<T> {
    return request<T>(path, {
      method: "PATCH",
      body: body ? JSON.stringify(body) : undefined,
    });
  },
  delete<T = void>(path: string): Promise<T> {
    return request<T>(path, { method: "DELETE" });
  },
  blob(path: string): Promise<Blob> {
    return requestBlob(path);
  },
  /** 上传：可传单个 File（自动包成 FormData）或直接传 FormData（多字段）。 */
  async upload<T>(path: string, fileOrForm: File | FormData, retried = false): Promise<T> {
    const token = getToken();
    const form = fileOrForm instanceof FormData ? fileOrForm : (() => {
      const f = new FormData();
      f.append("file", fileOrForm);
      return f;
    })();
    const resp = await fetch(`${BASE}${path}`, { method: "POST", headers: token ? { Authorization: `Bearer ${token}` } : undefined, body: form });
    if (resp.status === 401 && !retried && await tryRefresh()) {
      return api.upload<T>(path, form, true);
    }
    if (resp.status === 401) {
      clearSession();
      window.location.hash = "#/login";
      throw new ApiError(401, "登录已过期，请重新登录");
    }
    if (!resp.ok) { const body = await resp.json().catch(() => ({})); throw new ApiError(resp.status, extractDetail(body, `HTTP ${resp.status}`)); }
    return resp.json() as Promise<T>;
  },
};
