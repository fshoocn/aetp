/** 运行状态 → 中文文本 */
export const RUN_STATUS_TEXT: Record<string, string> = {
  created: "已创建",
  dispatched: "已派发",
  acked: "已确认",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  timed_out: "超时",
  lost: "丢失",
};

/** 运行状态 → Element Plus Tag type */
export const RUN_STATUS_TAG: Record<string, "success" | "danger" | "warning" | "info"> = {
  succeeded: "success",
  running: "warning",
  dispatched: "info",
  acked: "info",
  created: "info",
  failed: "danger",
  timed_out: "danger",
  cancelled: "info",
  lost: "danger",
};

/** 任务状态 → 中文文本 */
export const TASK_STATUS_TEXT: Record<string, string> = {
  pending: "待处理",
  dispatching: "派发中",
  running: "运行中",
  cancelling: "取消中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  timed_out: "超时",
};

/** 任务状态 → Element Plus Tag type */
export const TASK_STATUS_TAG: Record<string, "success" | "danger" | "warning" | "info"> = {
  succeeded: "success",
  running: "warning",
  cancelling: "warning",
  dispatching: "info",
  pending: "info",
  failed: "danger",
  timed_out: "danger",
  cancelled: "info",
};

/** Case 状态 → 中文文本 */
export const CASE_STATUS_TEXT: Record<string, string> = {
  passed: "通过",
  failed: "失败",
  skipped: "跳过",
  error: "错误",
  pending: "待执行",
  running: "执行中",
};

/** Case 状态 → Element Plus Tag type */
export const CASE_STATUS_TAG: Record<string, "success" | "danger" | "warning" | "info"> = {
  passed: "success",
  running: "warning",
  failed: "danger",
  error: "danger",
  skipped: "info",
  pending: "info",
};

/** 触发方式 → 中文文本 */
export const TRIGGER_TEXT: Record<string, string> = {
  manual_web: "手动",
  api: "API",
  retry: "重试",
  recovery: "恢复",
};

/** Run 流程事件类型 → 中文文本（下发/执行全链路时间线） */
export const RUN_EVENT_TEXT: Record<string, string> = {
  "run.created": "Run 已创建",
  "run.split": "任务分割完成",
  "run.dispatched": "Shard 已派发",
  "run.ack": "Agent 已确认",
  "run.ack_rejected": "Agent 拒绝执行",
  "run.progress": "执行进度",
  "run.case-status": "用例状态更新",
  "run.log": "运行日志",
  "run.result": "结果上报",
  "run.log_complete": "日志围栏达成",
  "run.succeeded": "执行成功",
  "run.failed": "执行失败",
  "run.cancelled": "已取消",
  "run.timed_out": "超时",
};

export function runStatusText(status: string): string {
  return RUN_STATUS_TEXT[status] || status;
}

export function runStatusTag(status: string): "success" | "danger" | "warning" | "info" {
  return RUN_STATUS_TAG[status] || "info";
}

export function taskStatusText(status: string): string {
  return TASK_STATUS_TEXT[status] || status;
}

export function taskStatusTag(status: string): "success" | "danger" | "warning" | "info" {
  return TASK_STATUS_TAG[status] || "info";
}

export function caseStatusText(status: string): string {
  return CASE_STATUS_TEXT[status] || status;
}

export function caseStatusTag(status: string): "success" | "danger" | "warning" | "info" {
  return CASE_STATUS_TAG[status] || "info";
}

export function triggerText(value: string): string {
  return TRIGGER_TEXT[value] || value;
}

/** Run 流程事件类型 → 中文文本 */
export function runEventText(eventType: string): string {
  return RUN_EVENT_TEXT[eventType] || eventType;
}

/** Run 流程事件类型 → 时间线节点类型 */
export function runEventTag(eventType: string): "success" | "danger" | "warning" | "primary" | "info" {
  if (eventType.includes("rejected") || eventType.includes("failed") || eventType === "run.timed_out") return "danger";
  if (eventType.includes("succeeded")) return "success";
  if (eventType === "run.progress" || eventType === "run.case-status") return "warning";
  return "primary";
}
