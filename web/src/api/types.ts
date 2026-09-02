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

export interface HardwareChannel {
  name: string;
  hardware_model?: string | null;
  enabled?: boolean;
}

export interface VehicleBus {
  bus_type: string;
  channels: HardwareChannel[];
}

export interface VehicleVendor {
  name: string;
  buses: VehicleBus[];
}

export interface VehicleCapability {
  vendors: VehicleVendor[];
}

export interface LanguageRuntime {
  name: string;
  version: string;
}

export interface LanguageCapability {
  runtimes: LanguageRuntime[];
}

export interface OperatingSystem {
  name: string;
  version: string;
}

export interface SystemCapability {
  operating_system?: OperatingSystem | null;
  memory_mb?: number | null;
  cpu_cores?: number | null;
}

export interface SerialPortCapability {
  function: string;
  port: string;
  enabled?: boolean;
}

export interface SerialCapability {
  ports: SerialPortCapability[];
}

export interface NodeCapabilities {
  vehicle?: VehicleCapability | null;
  language?: LanguageCapability | null;
  system?: SystemCapability | null;
  serial?: SerialCapability | null;
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
  capabilities: NodeCapabilities;
  plugin_versions: Record<string, string>;
  load: Record<string, unknown>;
  resource_occupancy: Record<string, string>;
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
  capabilities: NodeCapabilities;
  plugin_versions: Record<string, string>;
  resource_occupancy: Record<string, string>;
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

export interface Run {
  run_id: string;
  project_id: string;
  task_id: string;
  status: string;
  trigger_type: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  scheduled?: number;
  pending_shard_ids?: string[];
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

export interface RunEvent {
  event_id: string;
  sequence: number | null;
  event_type: string;
  aggregate_id: string;
  payload: Record<string, unknown>;
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
    task_config_entry?: string;
    task_config_url?: string;
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

export type V2PluginStatus =
  | "uploaded"
  | "verified"
  | "installed"
  | "pending_restart"
  | "enabled"
  | "disabled"
  | "removed"
  | "error";

export interface V2PluginManifest {
  schema_version: 2;
  id: string;
  version: string;
  api_version: string;
  point: string;
  display_name: string;
  entrypoints: {
    master?: string | null;
    agent?: string | null;
    ui?: string | null;
  };
  capabilities: string[];
  static_requirements: {
    runtimes: Array<{
      runtime_type: string;
      version: { exact: string | null; minimum: string | null; maximum: string | null } | null;
    }>;
    software: Array<{
      name: string;
      version: { exact: string | null; minimum: string | null; maximum: string | null } | null;
      license_required: boolean;
    }>;
    resources: Array<{
      resource_type: string;
      quantity: number;
      vendor: string | null;
      model: string | null;
      properties: Record<string, unknown>;
      required_labels: Record<string, string>;
      preferred_labels: Record<string, string>;
      allow_switching: boolean;
    }>;
  };
  configuration_schema: string | null;
  ui_protocol_version: number | null;
}

export interface V2PluginVersion {
  plugin_id: string;
  version: string;
  point: string;
  status: V2PluginStatus;
  filename: string;
  archive_sha256: string;
  manifest_sha256: string;
  manifest: V2PluginManifest;
  installed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface V2PluginRef {
  plugin_id: string;
  version: string;
  archive_sha256: string;
}

export interface V2Configuration {
  schema_version: number;
  schema_hash: string;
  values: Record<string, unknown>;
}

export interface V2ScriptRef {
  script_id: string;
  version: number;
  filename: string;
  size: number;
  sha256: string;
  download_url: string | null;
}

export interface V2TestCase {
  stable_key: string;
  name: string;
  parent_path: string;
  tags: string[];
  estimated_duration_s: number | null;
}

export interface V2CaseSelection {
  selected_keys: string[];
  include_all: boolean;
}

export interface V2SplitPolicy {
  type: "none" | "by_time" | "by_case_count" | "custom";
  target_count: number | null;
  target_duration_s: number | null;
  plugin_id: string | null;
}

export interface V2VersionConstraint {
  exact: string | null;
  minimum: string | null;
  maximum: string | null;
}

export interface V2RuntimeRequirement {
  runtime_type: string;
  version: V2VersionConstraint | null;
}

export interface V2SoftwareRequirement {
  name: string;
  version: V2VersionConstraint | null;
  license_required: boolean;
}

export interface V2ResourceRequirement {
  resource_type: string;
  quantity: number;
  vendor: string | null;
  model: string | null;
  properties: Record<string, unknown>;
  required_labels: Record<string, string>;
  preferred_labels: Record<string, string>;
  allow_switching: boolean;
}

export interface V2ExecutionRequirement {
  executor: {
    plugin_id: string;
    version: V2VersionConstraint;
  };
  runtimes: V2RuntimeRequirement[];
  software: V2SoftwareRequirement[];
  resources: V2ResourceRequirement[];
  required_tags: string[];
}

export interface V2ScriptDefinition {
  script_definition_id: string;
  project_id: string;
  revision: number;
  name: string;
  executor: V2PluginRef;
  source: V2ScriptRef;
  configuration: V2Configuration;
  cases: V2TestCase[];
  requirement: V2ExecutionRequirement | null;
  enabled: boolean;
}

export interface V2TaskScriptRef {
  binding_id: string;
  script_definition_id: string;
  script_revision: number;
  case_selection: V2CaseSelection;
  configuration: V2Configuration;
  split_policy: V2SplitPolicy;
  order_index: number;
  enabled: boolean;
}

export interface V2RetryPolicy {
  max_attempts: number;
  failover_nodes: boolean;
  retry_failed_cases: boolean;
  backoff_initial_s: number;
  backoff_max_s: number;
}

export interface V2TestTask {
  task_id: string;
  project_id: string;
  revision: number;
  name: string;
  scripts: V2TaskScriptRef[];
  execution_mode: "parallel" | "sequence";
  stop_on_failure: boolean;
  retry_policy: V2RetryPolicy;
  node_ids: string[];
  priority: number;
  enabled: boolean;
}

export interface V2RunScriptSnapshot {
  binding_id: string;
  script_definition_id: string;
  script_revision: number;
  executor: V2PluginRef;
  source: V2ScriptRef;
  configuration: V2Configuration;
  requirement: V2ExecutionRequirement;
  selected_case_keys: string[];
  split_policy: V2SplitPolicy;
}

export interface V2RunSnapshot {
  task_id: string;
  task_revision: number;
  scripts: V2RunScriptSnapshot[];
  execution_mode: "parallel" | "sequence";
  stop_on_failure: boolean;
  retry_policy: V2RetryPolicy;
  node_ids: string[];
  trigger_type: V2TriggerType;
  original_run_id: string | null;
}

export type V2TriggerType = "manual_web" | "api" | "schedule" | "ci_webhook" | "retry" | "recovery";

export interface V2RunShard {
  shard_id: string;
  script_binding_id: string;
  shard_index: number;
  case_keys: string[];
  status: string;
}

export interface V2RunView {
  run_id: string;
  task_id: string;
  snapshot: V2RunSnapshot;
  status: string;
  shards: V2RunShard[];
  scheduled: number;
  pending_shard_ids: string[];
  cancelled_shard_ids: string[];
}

export type V2PluginAvailability = "available" | "blocked" | "updating" | "error" | "not_installed";
export type V2ResourceHealth = "ready" | "degraded" | "unavailable";
export type V2MaintenanceState = "ready" | "idle" | "busy" | "draining" | "updating" | "restarting" | "degraded";

export interface V2ExecutorCapability {
  plugin_id: string;
  version: string;
  capabilities: string[];
}

export interface V2RuntimeCapability {
  provider_id: string;
  runtime_id: string;
  runtime_type: string;
  version: string;
  executable_ref: string | null;
}

export interface V2SoftwareCapability {
  provider_id: string;
  name: string;
  version: string;
  properties: Record<string, unknown>;
}

export interface V2ResourceCapability {
  resource_id: string;
  provider_id: string;
  resource_type: string;
  vendor: string | null;
  model: string | null;
  channel: string | null;
  function: string | null;
  labels: Record<string, string>;
  properties: Record<string, unknown>;
  health: V2ResourceHealth;
}

export interface V2PluginInventoryItem {
  plugin_id: string;
  point: string;
  version: string;
  archive_sha256: string;
  availability: V2PluginAvailability;
  unavailable_reasons: string[];
  checked_at: string;
}

export interface V2CapabilitySnapshot {
  schema_version: 2;
  node_id: string;
  session_id: string;
  revision: number;
  reported_at: string;
  tags: string[];
  executors: V2ExecutorCapability[];
  runtimes: V2RuntimeCapability[];
  software: V2SoftwareCapability[];
  resources: V2ResourceCapability[];
  system: SystemCapability | null;
  maintenance_state: V2MaintenanceState;
  plugin_inventory: V2PluginInventoryItem[];
}

export interface V2CapabilitySnapshotView {
  node_id: string;
  session_id: string;
  revision: number;
  snapshot_sha256: string;
  snapshot: V2CapabilitySnapshot;
  reported_at: string;
  created_at: string | null;
}

export interface V2LogEvent {
  event_id: string;
  source: "master" | "agent" | "web" | "plugin";
  source_id: string;
  sequence: number;
  occurred_at: string;
  level: "debug" | "info" | "warn" | "error";
  component: string;
  event_code: string;
  message_template: string;
  message: string;
  context: {
    request_id: string | null;
    trace_id: string | null;
    node_id: string | null;
    project_id: string | null;
    run_id: string | null;
    attempt_id: string | null;
    plan_id: string | null;
    plugin_id: string | null;
    plugin_version: string | null;
  };
  detail: Record<string, unknown>;
  exception: {
    type_name: string;
    message: string;
    stack_trace: string | null;
  } | null;
}

export interface V2DiagnosticsSnapshot {
  request_id: string;
  node_id: string;
  collected_at: string;
  maintenance_state: V2MaintenanceState;
  system: {
    hostname: string;
    os_name: string;
    os_version: string;
    process_id: number;
    agent_started_at: string;
    python_version: string;
    cpu_cores: number;
    memory_total_mb: number;
    memory_available_mb: number;
    disk_free_mb: number;
    agent_version: string;
    protocol_version: number;
  };
  mqtt: {
    connected: boolean;
    broker_endpoint: string;
    last_connected_at: string | null;
    reconnect_count: number;
    last_error_code: string | null;
    last_error_message: string | null;
  };
  plugins: V2PluginInventoryItem[];
  active_attempts: Array<{
    attempt_id: string;
    plan_id: string;
    run_id: string;
    state: string;
    started_at: string | null;
  }>;
  capability_revision: number;
  log_tail: V2LogEvent[];
}

export interface V2DiagnosticsSnapshotView {
  request_id: string;
  node_id: string;
  session_id: string;
  snapshot: V2DiagnosticsSnapshot;
  collected_at: string;
  created_at: string | null;
}

export interface V2DiagnosticsCollectResponse {
  operation_id: string;
  request_id: string;
  node_id: string;
  status: "pending";
}

export type V2RemoteOperationKind = "diagnostics" | "plugin_sync" | "log_level" | "drain" | "restart";
export type V2RemoteOperationStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface V2RemoteOperation {
  operation_id: string;
  node_id: string;
  kind: V2RemoteOperationKind;
  status: V2RemoteOperationStatus;
  expected_session_id: string | null;
  request: Record<string, unknown>;
  error_code: string | null;
  message: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface V2PluginSyncView {
  sync_id: string;
  node_id: string;
  expected_session_id: string;
  state: "pending" | "draining" | "installing" | "restarting" | "succeeded" | "failed" | "cancelled";
  items: Array<Record<string, unknown>>;
  results: Array<Record<string, unknown>> | null;
  accepted: boolean | null;
  restart_required: boolean;
  completed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface V2AgentLogView {
  node_id: string;
  session_id: string;
  sequence: number;
  event: V2LogEvent;
  batch_first_sequence: number;
  received_at: string;
}

export interface V2LogLevelUpdateRequest {
  component: string;
  plugin_id?: string | null;
  level: "debug" | "info" | "warn" | "error";
  expires_at?: string | null;
}

export interface V2MaintenanceRequest {
  drain_timeout_s?: number;
  reason?: string;
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
    capabilities: NodeCapabilities;
    plugin_versions: Record<string, string>;
  }>;
  verification: {
    supported: boolean;
    location: string;
    endpoint_template: string;
  };
}

// ---- P7.3 鑴氭湰搴?----
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
  file_missing: boolean;
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

// ---- P7.4 浠诲姟瀹氫箟 ----
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
  config: Record<string, unknown>;
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
  config?: Record<string, unknown>;
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

// ---- P7.6 閫氱煡绔偣 / 浜嬩欢璁㈤槄 / 鎶曢€?----

export interface NotificationEndpointOut {
  endpoint_id: string;
  project_id: string;
  channel_type: string;
  name: string;
  config: Record<string, unknown>;
  has_secret: boolean;
  enabled: boolean;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EventSubscriptionOut {
  subscription_id: string;
  project_id: string;
  endpoint_id: string;
  task_id: string | null;
  event_types: string[];
  filter_json: Record<string, unknown>;
  throttle_policy: Record<string, unknown>;
  enabled: boolean;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EventDeliveryOut {
  delivery_id: string;
  project_id: string;
  event_id: string;
  subscription_id: string;
  endpoint_id: string;
  content: Record<string, unknown>;
  status: string;
  attempts: number;
  next_attempt_at: string | null;
  sent_at: string | null;
  response_summary: string | null;
  error_message: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EndpointCreateRequest {
  channel_type: string;
  name: string;
  config?: Record<string, unknown>;
  secret_value?: string;
}

export interface EndpointUpdateRequest {
  name?: string;
  config?: Record<string, unknown>;
  secret_value?: string;
  enabled?: boolean;
}

export interface SubscriptionCreateRequest {
  endpoint_id: string;
  task_id?: string;
  event_types: string[];
  filter_json?: Record<string, unknown>;
  throttle_policy?: Record<string, unknown>;
}

export interface SubscriptionUpdateRequest {
  task_id?: string;
  event_types?: string[];
  filter_json?: Record<string, unknown>;
  throttle_policy?: Record<string, unknown>;
  enabled?: boolean;
}

// ---- P8.2 浠诲姟璋冨害璁″垝 ----

export interface TaskScheduleOut {
  schedule_id: string;
  task_id: string;
  cron_expression: string | null;
  interval_seconds: number | null;
  timezone: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ScheduleCreateRequest {
  cron_expression?: string;
  interval_seconds?: number;
  timezone?: string;
  enabled?: boolean;
}

export interface ScheduleUpdateRequest {
  cron_expression?: string;
  interval_seconds?: number;
  timezone?: string;
  enabled?: boolean;
}
