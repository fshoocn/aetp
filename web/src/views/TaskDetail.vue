<template>
  <div>
    <el-button link type="primary" class="back" @click="$router.push('/tasks')">
      ← 返回任务列表
    </el-button>
    <h2>任务详情</h2>

    <el-card v-loading="taskLoading" class="info">
      <el-descriptions :column="2" border v-if="task">
        <el-descriptions-item label="任务ID">{{ task.task_id }}</el-descriptions-item>
        <el-descriptions-item label="设备">{{ task.device_id }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusTag(task.status)">{{ statusText(task.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ fmt(task.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">
          {{ task.started_at ? fmt(task.started_at) : "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="完成时间">
          {{ task.finished_at ? fmt(task.finished_at) : "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="命令" :span="2">
          <code>{{ prettyJson(task.command) }}</code>
        </el-descriptions-item>
        <el-descriptions-item v-if="task.result" label="结果" :span="2">
          <code>{{ prettyJson(task.result) }}</code>
        </el-descriptions-item>
        <el-descriptions-item v-if="task.error" label="错误" :span="2">
          <span class="err">{{ task.error }}</span>
        </el-descriptions-item>
      </el-descriptions>
      <el-empty v-else-if="!taskLoading" description="任务不存在" />
    </el-card>

    <h3 class="section">执行日志</h3>
    <el-card>
      <div v-loading="logsLoading" class="log-box">
        <div v-for="log in logs" :key="log.sequence" class="log-line" :class="`log-${log.level}`">
          <span class="seq">#{{ log.sequence }}</span>
          <span class="ts">{{ fmt(log.ts) }}</span>
          <span>{{ log.message }}</span>
        </div>
        <el-empty v-if="!logsLoading && logs.length === 0" description="暂无日志" />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { aetpApi } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";

const props = defineProps<{ taskId: string }>();
const projectStore = useProjectStore();
const queryClient = useQueryClient();

useTaskEvents(queryClient);

const projectId = computed(() => projectStore.currentProjectId ?? "");

const taskQuery = useQuery({
  queryKey: ["task", projectId, props.taskId],
  queryFn: () => aetpApi.tasks.get(projectId.value, props.taskId),
  enabled: computed(() => !!projectId.value),
});

const logsQuery = useQuery({
  queryKey: ["logs", projectId, props.taskId],
  queryFn: () => aetpApi.tasks.logs(projectId.value, props.taskId),
  enabled: computed(() => !!projectId.value),
  refetchInterval: 5000, // 日志兜底轮询（SSE 之外）
});

const taskLoading = computed(() => taskQuery.isLoading.value);
const logsLoading = computed(() => logsQuery.isLoading.value);
const task = computed(() => taskQuery.data.value ?? null);
const logs = computed(() => logsQuery.data.value ?? []);

function prettyJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function statusText(s: string) {
  const map: Record<string, string> = {
    pending: "待派发",
    dispatching: "派发中",
    running: "运行中",
    cancelling: "取消中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    timed_out: "超时",
  };
  return map[s] ?? s;
}

function statusTag(s: string) {
  const map: Record<string, "success" | "danger" | "warning" | "info"> = {
    pending: "info",
    dispatching: "info",
    running: "warning",
    cancelling: "warning",
    succeeded: "success",
    failed: "danger",
    cancelled: "info",
    timed_out: "danger",
  };
  return map[s] ?? "info";
}

function fmt(ts: string) {
  return new Date(ts).toLocaleString("zh-CN");
}
</script>

<style scoped>
.back {
  margin-bottom: 12px;
}
.section {
  margin: 24px 0 12px;
}
.info code {
  display: block;
  white-space: pre-wrap;
  background: #f5f5f5;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
}
.err {
  color: #e74c3c;
}
.log-box {
  background: #1e1e1e;
  border-radius: 6px;
  padding: 16px;
  max-height: 480px;
  overflow-y: auto;
  font-family: monospace;
  font-size: 13px;
  min-height: 120px;
}
.log-line {
  padding: 4px 0;
  border-bottom: 1px solid #333;
  color: #ccc;
}
.log-line:last-child {
  border-bottom: none;
}
.seq {
  color: #888;
  margin-right: 8px;
}
.ts {
  color: #666;
  margin-right: 8px;
}
.log-error {
  color: #e74c3c;
}
.log-warning {
  color: #e6a23c;
}
.log-success {
  color: #52c41a;
}
</style>
