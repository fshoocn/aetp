import { api } from "@/api/client";

/** 褰撳墠 API 鐗堟湰鐨勬牴璺緞锛涗互鍚庤縼绉荤増鏈彧闇€淇敼杩欓噷銆?*/
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
