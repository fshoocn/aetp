const BASE = ""; // 所有 API 调用前缀（开发时 vite proxy 处理 /api）

function getToken(): string | null {
  return localStorage.getItem("token");
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function extractDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
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
  return fallback;
}

async function request<T>(
  path: string,
  options: RequestInit = {}
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

  if (resp.status === 401) {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    if (window.location.hash !== "#/login") {
      window.location.hash = "#/login";
    }
    throw new ApiError(401, "登录已过期，请重新登录");
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new ApiError(resp.status, extractDetail(body, `HTTP ${resp.status}`));
  }

  return resp.json();
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
};
