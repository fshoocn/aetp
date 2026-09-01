import { api } from "@/api/client";
import type {
  AdminUser,
  Device,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  EventDeliveryOut,
  EventSubscriptionOut,
  LoginResponse,
  ManagedPlugin,
  V2PluginVersion,
  Node,
  NotificationEndpointOut,
  Project,
  ProjectCreateRequest,
  ProjectMember,
  ProjectNodeBinding,
  ProjectUpdateRequest,
  Run,
  RunArtifact,
  RunDetail,
  RunEvent,
  RunLog,
  ScheduleCreateRequest,
  ScheduleUpdateRequest,
  ScriptCase,
  SubscriptionCreateRequest,
  SubscriptionUpdateRequest,
  TaskScheduleOut,
  TaskTypeConfigContext,
  TaskTypePlugin,
  TestScript,
  TestTask,
  TestTaskCreateRequest,
  UserInfo,
} from "@/api/types";

const API_V1 = "/api/v1";

export const aetpApi = {
  auth: {
    login(username: string, password: string) {
      return api.post<LoginResponse>(`${API_V1}/auth/login`, { username, password });
    },
    register(username: string, password: string, displayName: string) {
      return api.post<UserInfo>(`${API_V1}/auth/register`, {
        username, password, display_name: displayName,
      });
    },
    me() { return api.get<UserInfo>(`${API_V1}/auth/me`); },
    refresh(refreshToken: string) {
      return api.post<LoginResponse>(`${API_V1}/auth/refresh`, { refresh_token: refreshToken });
    },
    logout(refreshToken?: string) {
      return api.post<void>(`${API_V1}/auth/logout`, { refresh_token: refreshToken ?? null });
    },
  },

  devices: {
    list(projectId: string, online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_V1}/projects/${projectId}/devices${query}`);
    },
    get(projectId: string, deviceId: string) {
      return api.get<Device>(`${API_V1}/projects/${projectId}/devices/${deviceId}`);
    },
    listGlobal(online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_V1}/devices${query}`);
    },
  },

  runs: {
    list(projectId: string, limit = 100, offset = 0) {
      return api.get<Run[]>(`${API_V1}/projects/${projectId}/runs?limit=${limit}&offset=${offset}`);
    },
    trigger(projectId: string, taskId: string, caseFilter?: string[]) {
      return api.post<Run>(`${API_V1}/projects/${projectId}/runs`, {
        task_id: taskId, case_filter: caseFilter?.length ? caseFilter : null,
      });
    },
    get(projectId: string, runId: string) {
      return api.get<RunDetail>(`${API_V1}/projects/${projectId}/runs/${runId}`);
    },
    logs(projectId: string, runId: string, afterSequence = 0) {
      return api.get<RunLog[]>(`${API_V1}/projects/${projectId}/runs/${runId}/logs?after_sequence=${afterSequence}`);
    },
    events(projectId: string, runId: string) {
      return api.get<RunEvent[]>(`${API_V1}/projects/${projectId}/runs/${runId}/events`);
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
    cancel(projectId: string, runId: string) {
      return api.post<Run>(`${API_V1}/projects/${projectId}/runs/${runId}/cancel`);
    },
  },

  scripts: {
    list(projectId: string) { return api.get<TestScript[]>(`${API_V1}/projects/${projectId}/scripts`); },
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
        `${API_V1}/projects/${projectId}/scripts/${scriptId}/verify`, { node_id: nodeId, config },
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
    list() { return api.get<Project[]>(`${API_V1}/projects`); },
    create(request: ProjectCreateRequest) { return api.post<Project>(`${API_V1}/projects`, request); },
    get(projectId: string) { return api.get<Project>(`${API_V1}/projects/${projectId}`); },
    update(projectId: string, request: ProjectUpdateRequest) {
      return api.patch<Project>(`${API_V1}/projects/${projectId}`, request);
    },
    members(projectId: string) { return api.get<ProjectMember[]>(`${API_V1}/projects/${projectId}/members`); },
    addMember(projectId: string, userId: number, role: ProjectMember["project_role"]) {
      return api.post<ProjectMember>(`${API_V1}/projects/${projectId}/members`, { user_id: userId, project_role: role });
    },
    updateMember(projectId: string, userId: number, role: ProjectMember["project_role"]) {
      return api.patch<ProjectMember>(`${API_V1}/projects/${projectId}/members/${userId}`, { project_role: role });
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
    getNode(nodeId: string) { return api.get<Node>(`${API_V1}/nodes/${nodeId}`); },
    devices(online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_V1}/devices${query}`);
    },
    getDevice(deviceId: string) { return api.get<Device>(`${API_V1}/devices/${deviceId}`); },
  },

  plugins: {
    list() { return api.get<TaskTypePlugin[]>(`${API_V1}/task-types`); },
    configContext(projectId: string, taskType: string) {
      return api.get<TaskTypeConfigContext>(
        `${API_V1}/projects/${projectId}/task-types/${encodeURIComponent(taskType)}/config-context`,
      );
    },
    uiAsset(url: string) { return api.blob(url); },
    managed() { return api.get<ManagedPlugin[]>(`${API_V1}/task-types/managed`); },
    upload(file: File) { return api.upload<ManagedPlugin>(`${API_V1}/task-types/managed`, file); },
    download(pluginId: string) {
      return api.blob(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}/download`);
    },
    install(pluginId: string) {
      return api.post<ManagedPlugin>(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}/install`);
    },
    setEnabled(pluginId: string, enabled: boolean) {
      return api.patch<ManagedPlugin>(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}?enabled=${enabled}`);
    },
    remove(pluginId: string) {
      return api.delete(`${API_V1}/task-types/managed/${encodeURIComponent(pluginId)}`);
    },
    v2List() { return api.get<V2PluginVersion[]>('/api/v2/plugins'); },
    v2Upload(file: File) { return api.upload<V2PluginVersion>('/api/v2/plugins', file); },
    v2Install(pluginId: string, version: string) {
      return api.post<V2PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/install`);
    },
    v2Enable(pluginId: string, version: string) {
      return api.post<V2PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/enable`);
    },
    v2Disable(pluginId: string, version: string) {
      return api.post<V2PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/disable`);
    },
    v2Remove(pluginId: string, version: string) {
      return api.delete(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}`);
    },
  },

  admin: {
    users(accountStatus?: AdminUser["account_status"]) {
      const query = accountStatus ? `?account_status=${accountStatus}` : "";
      return api.get<AdminUser[]>(`${API_V1}/users${query}`);
    },
    updateUser(userId: number, request: Partial<Pick<AdminUser, "account_status" | "platform_role">>) {
      return api.patch<AdminUser>(`${API_V1}/users/${userId}`, request);
    },
  },

  notifications: {
    listEndpoints(projectId: string) {
      return api.get<NotificationEndpointOut[]>(`${API_V1}/projects/${projectId}/notification-endpoints`);
    },
    createEndpoint(projectId: string, body: EndpointCreateRequest) {
      return api.post<NotificationEndpointOut>(`${API_V1}/projects/${projectId}/notification-endpoints`, body);
    },
    updateEndpoint(projectId: string, endpointId: string, body: EndpointUpdateRequest) {
      return api.patch<NotificationEndpointOut>(
        `${API_V1}/projects/${projectId}/notification-endpoints/${endpointId}`, body,
      );
    },
    deleteEndpoint(projectId: string, endpointId: string) {
      return api.delete(`${API_V1}/projects/${projectId}/notification-endpoints/${endpointId}`);
    },
    listSubscriptions(projectId: string) {
      return api.get<EventSubscriptionOut[]>(`${API_V1}/projects/${projectId}/event-subscriptions`);
    },
    createSubscription(projectId: string, body: SubscriptionCreateRequest) {
      return api.post<EventSubscriptionOut>(`${API_V1}/projects/${projectId}/event-subscriptions`, body);
    },
    updateSubscription(projectId: string, subscriptionId: string, body: SubscriptionUpdateRequest) {
      return api.patch<EventSubscriptionOut>(
        `${API_V1}/projects/${projectId}/event-subscriptions/${subscriptionId}`, body,
      );
    },
    deleteSubscription(projectId: string, subscriptionId: string) {
      return api.delete(`${API_V1}/projects/${projectId}/event-subscriptions/${subscriptionId}`);
    },
    listDeliveries(projectId: string, status?: string) {
      const query = status ? `?status_filter=${status}` : "";
      return api.get<EventDeliveryOut[]>(`${API_V1}/projects/${projectId}/event-deliveries${query}`);
    },
    retryDelivery(projectId: string, deliveryId: string) {
      return api.post<EventDeliveryOut>(`${API_V1}/projects/${projectId}/event-deliveries/${deliveryId}/retry`);
    },
  },

  schedules: {
    list(projectId: string, taskId: string) {
      return api.get<TaskScheduleOut[]>(`${API_V1}/projects/${projectId}/tasks/${taskId}/schedules`);
    },
    create(projectId: string, taskId: string, body: ScheduleCreateRequest) {
      return api.post<TaskScheduleOut>(`${API_V1}/projects/${projectId}/tasks/${taskId}/schedules`, body);
    },
    update(projectId: string, taskId: string, scheduleId: string, body: ScheduleUpdateRequest) {
      return api.patch<TaskScheduleOut>(
        `${API_V1}/projects/${projectId}/tasks/${taskId}/schedules/${scheduleId}`, body,
      );
    },
    remove(projectId: string, taskId: string, scheduleId: string) {
      return api.delete(`${API_V1}/projects/${projectId}/tasks/${taskId}/schedules/${scheduleId}`);
    },
  },
};
