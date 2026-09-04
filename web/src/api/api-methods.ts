import { api, newIdempotencyKey } from "@/api/client";
import type {
  AdminUser,
  Device,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  EventDeliveryOut,
  EventSubscriptionOut,
  LoginResponse,
  PluginVersion,
  ScriptDefinition,
  TestTask,
  TaskView,
  RunView,
  RunListView,
  RunDetailView,
  RunLogView,
  RunEventView,
  TriggerType,
  CapabilitySnapshotView,
  DiagnosticsSnapshotView,
  DiagnosticsCollectResponse,
  AgentLogView,
  RemoteOperation,
  PluginSyncView,
  LogLevelUpdateRequest,
  MaintenanceRequest,
  Node,
  NotificationEndpointOut,
  Project,
  ProjectCreateRequest,
  ProjectMember,
  ProjectNodeBinding,
  ProjectUpdateRequest,
  RunArtifact,
  ScheduleCreateRequest,
  ScheduleUpdateRequest,
  SubscriptionCreateRequest,
  SubscriptionUpdateRequest,
  TaskScheduleOut,
  UserInfo,
} from "@/api/types";

const API_BASE = "/api/v2";

function idempotencyHeaders(operation: string): Record<string, string> {
  return { "Idempotency-Key": newIdempotencyKey(operation) };
}

export const aetpApi = {
  auth: {
    login(username: string, password: string) {
      return api.post<LoginResponse>(`${API_BASE}/auth/login`, { username, password });
    },
    register(username: string, password: string, displayName: string) {
      return api.post<UserInfo>(`${API_BASE}/auth/register`, {
        username, password, display_name: displayName,
      });
    },
    me() { return api.get<UserInfo>(`${API_BASE}/auth/me`); },
    refresh(refreshToken: string) {
      return api.post<LoginResponse>(`${API_BASE}/auth/refresh`, { refresh_token: refreshToken });
    },
    logout(refreshToken?: string) {
      return api.post<void>(`${API_BASE}/auth/logout`, { refresh_token: refreshToken ?? null });
    },
  },

  runs: {
    list(projectId: string, limit = 100, offset = 0) {
      return api.get<RunListView[]>(`${API_BASE}/projects/${projectId}/runs?limit=${limit}&offset=${offset}`);
    },
    trigger(projectId: string, taskId: string, caseFilter?: string[]) {
      return api.post<RunView>(`${API_BASE}/projects/${projectId}/runs`, {
        task_id: taskId, case_filter: caseFilter?.length ? caseFilter : null,
      }, idempotencyHeaders("run-create"));
    },
    get(projectId: string, runId: string) {
      return api.get<RunDetailView>(`${API_BASE}/projects/${projectId}/runs/${runId}`);
    },
    logs(projectId: string, runId: string, afterSequence = 0) {
      return api.get<RunLogView[]>(`${API_BASE}/projects/${projectId}/runs/${runId}/logs?after_sequence=${afterSequence}`);
    },
    events(projectId: string, runId: string) {
      return api.get<RunEventView[]>(`${API_BASE}/projects/${projectId}/runs/${runId}/events`);
    },
    artifacts(projectId: string, runId: string) {
      return api.get<RunArtifact[]>(`${API_BASE}/projects/${projectId}/runs/${runId}/artifacts`);
    },
    downloadArtifact(projectId: string, runId: string, artifactId: string) {
      return api.blob(`${API_BASE}/projects/${projectId}/runs/${runId}/artifacts/${artifactId}/download`);
    },
    retry(projectId: string, runId: string) {
      return api.post<RunView>(`${API_BASE}/projects/${projectId}/runs/${runId}/retry`, {}, idempotencyHeaders("run-retry"));
    },
    retryFailed(projectId: string, runId: string) {
      return api.post<RunView>(`${API_BASE}/projects/${projectId}/runs/${runId}/retry-failed`, {}, idempotencyHeaders("run-retry-failed"));
    },
    cancel(projectId: string, runId: string) {
      return api.post<RunListView>(`${API_BASE}/projects/${projectId}/runs/${runId}/cancel`, {}, idempotencyHeaders("run-cancel"));
    },
  },

  projects: {
    list() { return api.get<Project[]>(`${API_BASE}/projects`); },
    create(request: ProjectCreateRequest) { return api.post<Project>(`${API_BASE}/projects`, request, idempotencyHeaders("project-create")); },
    get(projectId: string) { return api.get<Project>(`${API_BASE}/projects/${projectId}`); },
    update(projectId: string, request: ProjectUpdateRequest) {
      return api.patch<Project>(`${API_BASE}/projects/${projectId}`, request, idempotencyHeaders("project-update"));
    },
    members(projectId: string) { return api.get<ProjectMember[]>(`${API_BASE}/projects/${projectId}/members`); },
    addMember(projectId: string, userId: number, role: ProjectMember["project_role"]) {
      return api.post<ProjectMember>(`${API_BASE}/projects/${projectId}/members`, { user_id: userId, project_role: role }, idempotencyHeaders("member-add"));
    },
    updateMember(projectId: string, userId: number, role: ProjectMember["project_role"]) {
      return api.patch<ProjectMember>(`${API_BASE}/projects/${projectId}/members/${userId}`, { project_role: role }, idempotencyHeaders("member-update"));
    },
    removeMember(projectId: string, userId: number) {
      return api.delete(`${API_BASE}/projects/${projectId}/members/${userId}`, idempotencyHeaders("member-remove"));
    },
    nodes(projectId: string) {
      return api.get<ProjectNodeBinding[]>(`${API_BASE}/projects/${projectId}/nodes`);
    },
    bindNode(projectId: string, nodeId: string) {
      return api.post<ProjectNodeBinding>(`${API_BASE}/projects/${projectId}/nodes`, { node_id: nodeId }, idempotencyHeaders("node-bind"));
    },
    updateNode(projectId: string, nodeId: string, enabled: boolean) {
      return api.patch<ProjectNodeBinding>(`${API_BASE}/projects/${projectId}/nodes/${nodeId}`, { enabled }, idempotencyHeaders("node-update"));
    },
    removeNode(projectId: string, nodeId: string) {
      return api.delete(`${API_BASE}/projects/${projectId}/nodes/${nodeId}`, idempotencyHeaders("node-remove"));
    },
  },

  assets: {
    nodes(online?: boolean, enabled?: boolean) {
      const params = new URLSearchParams();
      if (online !== undefined) params.set("online", String(online));
      if (enabled !== undefined) params.set("enabled", String(enabled));
      const query = params.toString();
      return api.get<Node[]>(`${API_BASE}/nodes${query ? `?${query}` : ""}`);
    },
    getNode(nodeId: string) { return api.get<Node>(`${API_BASE}/nodes/${nodeId}`); },
    devices(online?: boolean) {
      const query = online === undefined ? "" : `?online=${online}`;
      return api.get<Device[]>(`${API_BASE}/nodes/devices${query}`);
    },
    getDevice(deviceId: string) { return api.get<Device>(`${API_BASE}/nodes/devices/${deviceId}`); },
    capabilitySnapshot(nodeId: string) {
      return api.get<CapabilitySnapshotView>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/capability-snapshot`);
    },
    diagnostics(nodeId: string) {
      return api.get<DiagnosticsSnapshotView>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/diagnostics`);
    },
    collectDiagnostics(nodeId: string) {
      return api.post<DiagnosticsCollectResponse>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/diagnostics/collect`, undefined, idempotencyHeaders("diagnostics-collect"));
    },
    logs(nodeId: string, params: { afterSequence?: number; limit?: number; level?: string; component?: string; keyword?: string } = {}) {
      const query = new URLSearchParams();
      if (params.afterSequence !== undefined) query.set("after_sequence", String(params.afterSequence));
      if (params.limit !== undefined) query.set("limit", String(params.limit));
      if (params.level) query.set("level", params.level);
      if (params.component) query.set("component", params.component);
      if (params.keyword) query.set("keyword", params.keyword);
      const suffix = query.toString() ? `?${query.toString()}` : "";
      return api.get<AgentLogView[]>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/logs${suffix}`);
    },
    maintenanceOperations(nodeId: string, limit = 50) {
      return api.get<RemoteOperation[]>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/maintenance/operations?limit=${limit}`);
    },
    maintenanceOperation(nodeId: string, operationId: string) {
      return api.get<RemoteOperation>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/maintenance/operations/${encodeURIComponent(operationId)}`);
    },
    setLogLevel(nodeId: string, body: LogLevelUpdateRequest) {
      return api.post<RemoteOperation>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/log-level`, body, idempotencyHeaders("node-log-level"));
    },
    drain(nodeId: string, body: MaintenanceRequest = {}) {
      return api.post<RemoteOperation>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/maintenance/drain`, body, idempotencyHeaders("node-drain"));
    },
    restart(nodeId: string, body: MaintenanceRequest = {}) {
      return api.post<RemoteOperation>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/maintenance/restart`, body, idempotencyHeaders("node-restart"));
    },
    pluginSync(nodeId: string, body: { items: unknown[]; drain_timeout_s?: number; restart_after?: boolean }) {
      return api.post<PluginSyncView>(`/api/v2/nodes/${encodeURIComponent(nodeId)}/maintenance/sync`, body, idempotencyHeaders("plugin-sync"));
    },
  },

  plugins: {
    list() { return api.get<PluginVersion[]>('/api/v2/plugins'); },
    upload(file: File) { return api.upload<PluginVersion>('/api/v2/plugins', file, false, idempotencyHeaders("plugin-upload")); },
    install(pluginId: string, version: string) {
      return api.post<PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/install`, undefined, idempotencyHeaders("plugin-install"));
    },
    enable(pluginId: string, version: string) {
      return api.post<PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/enable`, undefined, idempotencyHeaders("plugin-enable"));
    },
    disable(pluginId: string, version: string) {
      return api.post<PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/disable`, undefined, idempotencyHeaders("plugin-disable"));
    },
    rollback(pluginId: string, version: string) {
      return api.post<PluginVersion>(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}/rollback`, undefined, idempotencyHeaders("plugin-rollback"));
    },
    remove(pluginId: string, version: string) {
      return api.delete(`/api/v2/plugins/${encodeURIComponent(pluginId)}/${encodeURIComponent(version)}`, idempotencyHeaders("plugin-remove"));
    },
  },

  tasks: {
    listScriptDefinitions(projectId: string, enabled?: boolean) {
      const query = enabled === undefined ? "" : `?enabled=${enabled}`;
      return api.get<ScriptDefinition[]>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/script-definitions${query}`);
    },
    getScriptDefinition(projectId: string, scriptDefinitionId: string, revision: number) {
      return api.get<ScriptDefinition>(
        `${API_BASE}/projects/${encodeURIComponent(projectId)}/script-definitions/${encodeURIComponent(scriptDefinitionId)}?revision=${revision}`,
      );
    },
    uploadScriptDefinition(
      projectId: string,
      file: File,
      fields: {
        name: string;
        executorPluginId: string;
        executorVersion: string;
        configuration?: Record<string, unknown>;
        cases?: { stable_key: string; name: string; parent_path?: string; tags?: string[] }[];
      },
    ) {
      const form = new FormData();
      form.append("file", file);
      form.append("name", fields.name);
      form.append("executor_plugin_id", fields.executorPluginId);
      form.append("executor_version", fields.executorVersion);
      form.append("configuration", JSON.stringify(fields.configuration ?? {}));
      if (fields.cases?.length) {
        form.append("cases", JSON.stringify(fields.cases));
      }
      return api.upload<ScriptDefinition>(
        `${API_BASE}/projects/${encodeURIComponent(projectId)}/script-definitions/upload`,
        form,
        false,
        idempotencyHeaders("script-upload"),
      );
    },
    listTasks(projectId: string, enabled?: boolean) {
      const query = enabled === undefined ? "" : `?enabled=${enabled}`;
      return api.get<TaskView[]>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks${query}`);
    },
    getTask(projectId: string, taskId: string, revision?: number) {
      const query = revision === undefined ? "" : `?revision=${revision}`;
      return api.get<TaskView>(
        `${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks/${encodeURIComponent(taskId)}${query}`,
      );
    },
    createScriptDefinition(projectId: string, definition: ScriptDefinition) {
      return api.post<ScriptDefinition>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/script-definitions`, { definition }, idempotencyHeaders("script-create"));
    },
    createTask(projectId: string, task: TestTask) {
      return api.post<TaskView>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks`, { task }, idempotencyHeaders("task-create"));
    },
    createRun(projectId: string, body: { task_id: string; task_revision?: number; run_id?: string; trigger_type?: TriggerType; original_run_id?: string; case_filter?: string[] | null }) {
      return api.post<RunView>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/runs`, body, idempotencyHeaders("run-create"));
    },
  },

  admin: {
    users(accountStatus?: AdminUser["account_status"]) {
      const query = accountStatus ? `?account_status=${accountStatus}` : "";
      return api.get<AdminUser[]>(`${API_BASE}/users${query}`);
    },
    updateUser(userId: number, request: Partial<Pick<AdminUser, "account_status" | "platform_role">>) {
      return api.patch<AdminUser>(`${API_BASE}/users/${userId}`, request, idempotencyHeaders("user-update"));
    },
  },

  notifications: {
    listEndpoints(projectId: string) {
      return api.get<NotificationEndpointOut[]>(`${API_BASE}/projects/${projectId}/notification-endpoints`);
    },
    createEndpoint(projectId: string, body: EndpointCreateRequest) {
      return api.post<NotificationEndpointOut>(`${API_BASE}/projects/${projectId}/notification-endpoints`, body, idempotencyHeaders("notification-endpoint-create"));
    },
    updateEndpoint(projectId: string, endpointId: string, body: EndpointUpdateRequest) {
      return api.patch<NotificationEndpointOut>(
        `${API_BASE}/projects/${projectId}/notification-endpoints/${endpointId}`, body, idempotencyHeaders("notification-endpoint-update"),
      );
    },
    deleteEndpoint(projectId: string, endpointId: string) {
      return api.delete(`${API_BASE}/projects/${projectId}/notification-endpoints/${endpointId}`, idempotencyHeaders("notification-endpoint-delete"));
    },
    listSubscriptions(projectId: string) {
      return api.get<EventSubscriptionOut[]>(`${API_BASE}/projects/${projectId}/event-subscriptions`);
    },
    createSubscription(projectId: string, body: SubscriptionCreateRequest) {
      return api.post<EventSubscriptionOut>(`${API_BASE}/projects/${projectId}/event-subscriptions`, body, idempotencyHeaders("subscription-create"));
    },
    updateSubscription(projectId: string, subscriptionId: string, body: SubscriptionUpdateRequest) {
      return api.patch<EventSubscriptionOut>(
        `${API_BASE}/projects/${projectId}/event-subscriptions/${subscriptionId}`, body, idempotencyHeaders("subscription-update"),
      );
    },
    deleteSubscription(projectId: string, subscriptionId: string) {
      return api.delete(`${API_BASE}/projects/${projectId}/event-subscriptions/${subscriptionId}`, idempotencyHeaders("subscription-delete"));
    },
    listDeliveries(projectId: string, status?: string) {
      const query = status ? `?status_filter=${status}` : "";
      return api.get<EventDeliveryOut[]>(`${API_BASE}/projects/${projectId}/event-deliveries${query}`);
    },
    retryDelivery(projectId: string, deliveryId: string) {
      return api.post<EventDeliveryOut>(`${API_BASE}/projects/${projectId}/event-deliveries/${deliveryId}/retry`, undefined, idempotencyHeaders("delivery-retry"));
    },
  },

  schedules: {
    list(projectId: string, taskId: string) {
      return api.get<TaskScheduleOut[]>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks/${encodeURIComponent(taskId)}/schedules`);
    },
    create(projectId: string, taskId: string, body: ScheduleCreateRequest) {
      return api.post<TaskScheduleOut>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks/${encodeURIComponent(taskId)}/schedules`, body, idempotencyHeaders("schedule-create"));
    },
    update(projectId: string, taskId: string, scheduleId: string, body: ScheduleUpdateRequest) {
      return api.patch<TaskScheduleOut>(
        `${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks/${encodeURIComponent(taskId)}/schedules/${encodeURIComponent(scheduleId)}`, body, idempotencyHeaders("schedule-update"),
      );
    },
    remove(projectId: string, taskId: string, scheduleId: string) {
      return api.delete(`${API_BASE}/projects/${encodeURIComponent(projectId)}/test-tasks/${encodeURIComponent(taskId)}/schedules/${encodeURIComponent(scheduleId)}`, idempotencyHeaders("schedule-delete"));
    },
  },
};
