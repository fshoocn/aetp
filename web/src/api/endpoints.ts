import { api } from "@/api/client";

/** 当前 API 版本的根路径；以后迁移版本只需修改这里。 */
const API_V1 = "/api/v1";

export interface UserInfo {
  id: number;
  username: string;
  display_name: string;
  account_status: "pending" | "active" | "disabled";
  platform_role: "user" | "admin";
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export interface Device {
  id: number;
  device_id: string;
  node_id: string | null;
  name: string;
  status: string;
  online: boolean;
  last_seen_at: string | null;
}

export interface Task {
  id: number;
  project_id: string;
  task_id: string;
  device_id: string;
  status: string;
  command: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  created_by: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskLog {
  id: number;
  task_id: string;
  sequence: number;
  level: string;
  message: string;
  ts: string;
}

export interface Node {
  id: number;
  node_id: string;
  name: string;
  hostname: string;
  status: string;
  online: boolean;
  enabled: boolean;
  tags: unknown[];
  capabilities: Record<string, unknown>;
  protocol_version: string;
  last_seen_at: string | null;
  devices: Device[];
}

export interface Project {
  project_id: string;
  project_key: string;
  name: string;
  description: string;
  status: "active" | "archived";
  created_by: number;
  created_at: string;
  updated_at: string;
}

export interface ProjectCreateRequest {
  project_key: string;
  name: string;
  description?: string;
  owner_id?: number;
}

export interface ProjectUpdateRequest {
  name?: string;
  description?: string;
  status?: "active" | "archived";
}

export interface TaskQuery {
  deviceId?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export const aetpApi = {
  auth: {
    login(username: string, password: string) {
      return api.post<LoginResponse>(`${API_V1}/auth/login`, {
        username,
        password,
      });
    },

    register(username: string, password: string, displayName: string) {
      return api.post<UserInfo>(`${API_V1}/auth/register`, {
        username,
        password,
        display_name: displayName,
      });
    },

    me() {
      return api.get<UserInfo>(`${API_V1}/auth/me`);
    },
  },

  devices: {
    list(projectId: string, online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_V1}/projects/${projectId}/devices${query}`);
    },

    get(projectId: string, deviceId: string) {
      return api.get<Device>(
        `${API_V1}/projects/${projectId}/devices/${deviceId}`
      );
    },
  },

  tasks: {
    list(projectId: string, query: TaskQuery = {}) {
      const params = new URLSearchParams();
      if (query.deviceId) params.set("device_id", query.deviceId);
      if (query.status) params.set("status_filter", query.status);
      if (query.limit !== undefined) params.set("limit", String(query.limit));
      if (query.offset !== undefined) params.set("offset", String(query.offset));
      const qs = params.toString();
      return api.get<Task[]>(
        `${API_V1}/projects/${projectId}/tasks${qs ? `?${qs}` : ""}`
      );
    },

    create(projectId: string, deviceId: string, command: Record<string, unknown>) {
      return api.post<Task>(`${API_V1}/projects/${projectId}/tasks`, {
        device_id: deviceId,
        command,
      });
    },

    get(projectId: string, taskId: string) {
      return api.get<Task>(`${API_V1}/projects/${projectId}/tasks/${taskId}`);
    },

    logs(projectId: string, taskId: string) {
      return api.get<TaskLog[]>(
        `${API_V1}/projects/${projectId}/tasks/${taskId}/logs`
      );
    },
  },

  projects: {
    list() {
      return api.get<Project[]>(`${API_V1}/projects`);
    },

    create(request: ProjectCreateRequest) {
      return api.post<Project>(`${API_V1}/projects`, request);
    },

    get(projectId: string) {
      return api.get<Project>(`${API_V1}/projects/${projectId}`);
    },

    update(projectId: string, request: ProjectUpdateRequest) {
      return api.patch<Project>(`${API_V1}/projects/${projectId}`, request);
    },
  },
};
