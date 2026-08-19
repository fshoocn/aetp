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
  refresh_token: string;
  token_type: string;
  expires_in: number;
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
  status:
    | "pending"
    | "dispatching"
    | "running"
    | "cancelling"
    | "succeeded"
    | "failed"
    | "cancelled"
    | "timed_out";
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
  plugin_versions: Record<string, string>;
  load: Record<string, unknown>;
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
  project_role?: "viewer" | "operator" | "maintainer" | "owner" | null;
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

export interface ProjectMember {
  id: number;
  project_id: string;
  user_id: number;
  username: string;
  display_name: string;
  project_role: "viewer" | "operator" | "maintainer" | "owner";
  assigned_by: number;
  created_at: string;
  updated_at: string;
}

export interface ProjectNodeBinding {
  id: number;
  project_id: string;
  node_id: string;
  name: string;
  hostname: string;
  status: string;
  online: boolean;
  node_enabled: boolean;
  enabled: boolean;
  assigned_by: number;
  created_at: string;
  updated_at: string;
  capabilities: Record<string, unknown>;
  plugin_versions: Record<string, string>;
  devices: Device[];
}

export interface AdminUser {
  id: number;
  username: string;
  display_name: string;
  account_status: "pending" | "active" | "disabled";
  platform_role: "user" | "admin";
  created_at: string;
}

export interface TaskQuery {
  deviceId?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export interface Run {
  run_id: string;
  project_id: string;
  task_id: string;
  status: string;
  trigger_type: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunShard {
  shard_id: string;
  shard_index: number;
  case_keys: string[];
  status: string;
  final_node: string | null;
}

export interface RunDetail extends Run {
  shards: RunShard[];
  result: Record<string, unknown> | null;
  case_results: RunCaseResult[];
}

export interface RunLog {
  id: number;
  run_id: string;
  node_id: string;
  sequence: number;
  level: string;
  message: string;
  detail: Record<string, unknown> | null;
  occurred_at: string | null;
}

export interface RunArtifact {
  artifact_id: string;
  run_id: string;
  shard_id: string | null;
  node_id: string | null;
  kind: string;
  file_ref: string;
  size: number;
  sha256: string;
  uploaded_at: string | null;
}

export interface TaskTypePlugin {
  task_type: string;
  display_name: string;
  plugin_version: string;
  supported_versions: string[];
  config_schema: Record<string, unknown>;
  upload_spec: Record<string, unknown>;
  ui?: {
    config_page?: string;
    entry?: string;
    url?: string;
    min_frontend_version?: string;
    protocol_version?: number;
  };
  agent_available: boolean;
  agent_package: { package_name: string; version: string; entry_point: string } | null;
}

export interface ManagedPlugin {
  plugin_id: string;
  filename: string;
  task_type: string;
  version: string;
  sha256: string;
  enabled: boolean;
  installed: boolean;
}

export interface TaskTypeConfigContext {
  project_id: string;
  task_type: string;
  plugin_version: string;
  config_schema: Record<string, unknown>;
  upload_spec: Record<string, unknown>;
  ui: Record<string, unknown>;
  nodes: Array<{
    node_id: string;
    name: string;
    hostname: string;
    status: string;
    online: boolean;
    enabled: boolean;
    capabilities: Record<string, unknown>;
    plugin_versions: Record<string, string>;
  }>;
  verification: {
    supported: boolean;
    location: string;
    endpoint_template: string;
  };
}

// ---- P7.3 脚本库 ----
export interface TestScript {
  script_id: string;
  project_id: string;
  task_type: string;
  name: string;
  version: number;
  file_ref: string;
  size: number;
  sha256: string;
  parse_status: "pending" | "parsing" | "parsed" | "failed";
  parse_location: string;
  result_parse_location: string;
  plugin_version: string;
  created_by: number | null;
  last_parsed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ScriptCase {
  case_id: string;
  script_id: string;
  stable_key: string;
  name: string;
  parent_path: string;
  tags: string[];
  params: Record<string, unknown>;
  avg_duration_s: number | null;
  duration_samples: number;
  order_index: number;
  deleted: boolean;
}

// ---- P7.4 任务定义 ----
export interface TestTask {
  task_id: string;
  project_id: string;
  script_id: string;
  script_version: number;
  task_type: string;
  name: string;
  default_case_selection: string[];
  node_ids: string[];
  split_policy: Record<string, unknown>;
  retry_policy: Record<string, unknown>;
  timeout_s: number;
  enabled: boolean;
  priority: number;
  validation_warning?: string | null;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface TestTaskCreateRequest {
  name: string;
  script_id: string;
  default_case_selection?: string[];
  node_ids?: string[];
  split_policy?: Record<string, unknown>;
  retry_policy?: Record<string, unknown>;
  timeout_s?: number;
  priority?: number;
}

export interface RunCaseResult {
  run_id: string;
  shard_id: string;
  case_key: string;
  attempt_no: number;
  status: string;
  duration_ms: number | null;
  error_summary: string | null;
  detail: Record<string, unknown> | null;
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

    refresh(refreshToken: string) {
      return api.post<LoginResponse>(`${API_V1}/auth/refresh`, {
        refresh_token: refreshToken,
      });
    },

    logout(refreshToken?: string) {
      return api.post<void>(`${API_V1}/auth/logout`, {
        refresh_token: refreshToken ?? null,
      });
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

    listGlobal(online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_V1}/devices${query}`);
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

  runs: {
    list(projectId: string, limit = 100, offset = 0) {
      return api.get<Run[]>(`${API_V1}/projects/${projectId}/runs?limit=${limit}&offset=${offset}`);
    },
    trigger(projectId: string, taskId: string, caseFilter?: string[]) {
      return api.post<Run>(`${API_V1}/projects/${projectId}/runs`, {
        task_id: taskId,
        case_filter: caseFilter?.length ? caseFilter : null,
      });
    },
    get(projectId: string, runId: string) {
      return api.get<RunDetail>(`${API_V1}/projects/${projectId}/runs/${runId}`);
    },
    logs(projectId: string, runId: string, afterSequence = 0) {
      return api.get<RunLog[]>(`${API_V1}/projects/${projectId}/runs/${runId}/logs?after_sequence=${afterSequence}`);
    },
    artifacts(projectId: string, runId: string) {
      return api.get<RunArtifact[]>(`${API_V1}/projects/${projectId}/runs/${runId}/artifacts`);
    },
    downloadArtifact(projectId: string, runId: string, artifactId: string) {
      return api.blob(`${API_V1}/projects/${projectId}/runs/${runId}/artifacts/${artifactId}/download`);
    },
    retry(projectId: string, runId: string) {
      return api.post<Run>(`${API_V1}/projects/${projectId}/runs/${runId}/retry`);
    },
    retryFailed(projectId: string, runId: string) {
      return api.post<Run>(`${API_V1}/projects/${projectId}/runs/${runId}/retry-failed`);
    },
  },

  scripts: {
    list(projectId: string) {
      return api.get<TestScript[]>(`${API_V1}/projects/${projectId}/scripts`);
    },
    get(projectId: string, scriptId: string) {
      return api.get<TestScript>(`${API_V1}/projects/${projectId}/scripts/${scriptId}`);
    },
    cases(projectId: string, scriptId: string) {
      return api.get<ScriptCase[]>(`${API_V1}/projects/${projectId}/scripts/${scriptId}/cases`);
    },
    upload(projectId: string, file: File, taskType: string, name: string, config: Record<string, unknown>) {
      const form = new FormData();
      form.append("file", file);
      form.append("task_type", taskType);
      form.append("name", name);
      form.append("config", JSON.stringify(config));
      return api.upload<TestScript>(`${API_V1}/projects/${projectId}/scripts`, form);
    },
    reparse(projectId: string, scriptId: string) {
      return api.post<TestScript>(`${API_V1}/projects/${projectId}/scripts/${scriptId}/parse`);
    },
    remove(projectId: string, scriptId: string) {
      return api.delete(`${API_V1}/projects/${projectId}/scripts/${scriptId}`);
    },
    verify(projectId: string, scriptId: string, nodeId: string, config: Record<string, unknown>) {
      return api.post<{ verify_id: string; project_id: string; script_id: string; node_id: string; status: string }>(
        `${API_V1}/projects/${projectId}/scripts/${scriptId}/verify`,
        { node_id: nodeId, config },
      );
    },
    verifyResult(projectId: string, scriptId: string, verifyId: string) {
      return api.get<{ verify_id: string; script_id: string; node_id: string; errors: string[] }>(
        `${API_V1}/projects/${projectId}/scripts/${scriptId}/verify/${verifyId}`,
      );
    },
    download(projectId: string, scriptId: string) {
      return api.blob(`${API_V1}/projects/${projectId}/scripts/${scriptId}/download`);
    },
  },

  testTasks: {
    list(projectId: string, enabled?: boolean) {
      const query = enabled === undefined ? "" : `?enabled=${enabled}`;
      return api.get<TestTask[]>(`${API_V1}/projects/${projectId}/test-tasks${query}`);
    },
    get(projectId: string, taskId: string) {
      return api.get<TestTask>(`${API_V1}/projects/${projectId}/test-tasks/${taskId}`);
    },
    create(projectId: string, request: TestTaskCreateRequest) {
      return api.post<TestTask>(`${API_V1}/projects/${projectId}/test-tasks`, request);
    },
    update(projectId: string, taskId: string, request: Partial<TestTaskCreateRequest> & { enabled?: boolean }) {
      return api.patch<TestTask>(`${API_V1}/projects/${projectId}/test-tasks/${taskId}`, request);
    },
    remove(projectId: string, taskId: string) {
      return api.delete(`${API_V1}/projects/${projectId}/test-tasks/${taskId}`);
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

    members(projectId: string) {
      return api.get<ProjectMember[]>(`${API_V1}/projects/${projectId}/members`);
    },

    addMember(projectId: string, userId: number, role: ProjectMember["project_role"]) {
      return api.post<ProjectMember>(`${API_V1}/projects/${projectId}/members`, {
        user_id: userId,
        project_role: role,
      });
    },

    updateMember(projectId: string, userId: number, role: ProjectMember["project_role"]) {
      return api.patch<ProjectMember>(
        `${API_V1}/projects/${projectId}/members/${userId}`,
        { project_role: role }
      );
    },

    removeMember(projectId: string, userId: number) {
      return api.delete(`${API_V1}/projects/${projectId}/members/${userId}`);
    },

    nodes(projectId: string) {
      return api.get<ProjectNodeBinding[]>(`${API_V1}/projects/${projectId}/nodes`);
    },
    bindNode(projectId: string, nodeId: string) {
      return api.post<ProjectNodeBinding>(`${API_V1}/projects/${projectId}/nodes`, { node_id: nodeId });
    },
    updateNode(projectId: string, nodeId: string, enabled: boolean) {
      return api.patch<ProjectNodeBinding>(`${API_V1}/projects/${projectId}/nodes/${nodeId}`, { enabled });
    },
    removeNode(projectId: string, nodeId: string) {
      return api.delete(`${API_V1}/projects/${projectId}/nodes/${nodeId}`);
    },
  },

  assets: {
    nodes(online?: boolean, enabled?: boolean) {
      const params = new URLSearchParams();
      if (online !== undefined) params.set("online", String(online));
      if (enabled !== undefined) params.set("enabled", String(enabled));
      const query = params.toString();
      return api.get<Node[]>(`${API_V1}/nodes${query ? `?${query}` : ""}`);
    },
    getNode(nodeId: string) {
      return api.get<Node>(`${API_V1}/nodes/${nodeId}`);
    },

    devices(online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_V1}/devices${query}`);
    },
    getDevice(deviceId: string) {
      return api.get<Device>(`${API_V1}/devices/${deviceId}`);
    },
  },

  plugins: {
    list() {
      return api.get<TaskTypePlugin[]>(`${API_V1}/task-types`);
    },
    configContext(projectId: string, taskType: string) {
      return api.get<TaskTypeConfigContext>(
        `${API_V1}/projects/${projectId}/task-types/${encodeURIComponent(taskType)}/config-context`,
      );
    },
    uiAsset(url: string) {
      return api.blob(url);
    },
    managed() { return api.get<ManagedPlugin[]>(`${API_V1}/task-types/managed`); },
    upload(file: File) {
      return api.upload<ManagedPlugin>(`${API_V1}/task-types/managed`, file);
    },
    install(pluginId: string) { return api.post<ManagedPlugin>(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}/install`); },
    setEnabled(pluginId: string, enabled: boolean) { return api.patch<ManagedPlugin>(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}?enabled=${enabled}`); },
    remove(pluginId: string) { return api.delete(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}`); },
  },

  admin: {
    users(accountStatus?: AdminUser["account_status"]) {
      const query = accountStatus ? `?account_status=${accountStatus}` : "";
      return api.get<AdminUser[]>(`${API_V1}/users${query}`);
    },

    updateUser(
      userId: number,
      request: Partial<Pick<AdminUser, "account_status" | "platform_role">>
    ) {
      return api.patch<AdminUser>(`${API_V1}/users/${userId}`, request);
    },
  },
};
