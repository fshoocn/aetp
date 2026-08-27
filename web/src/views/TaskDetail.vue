<template>
  <div>
    <el-button link type="primary" class="back" @click="$router.push('/tasks')">
      ← 返回任务列表
    </el-button>
    <h2>任务详情</h2>

    <el-card v-loading="taskLoading" class="info">
      <el-descriptions :column="2" border v-if="task">
        <el-descriptions-item label="任务ID">{{ task.task_id }}</el-descriptions-item>
        <el-descriptions-item label="节点">{{ task.node_ids.join(", ") || "-" }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="task.enabled ? 'success' : 'info'">{{ task.enabled ? "启用" : "停用" }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ task.created_at ? fmt(task.created_at) : "-" }}</el-descriptions-item>
        <el-descriptions-item label="更新时间">{{ task.updated_at ? fmt(task.updated_at) : "-" }}</el-descriptions-item>
        <el-descriptions-item label="脚本" :span="2">
          <code>{{ task.script_id }} · v{{ task.script_version }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="配置" :span="2">
          <code>{{ prettyJson(task.config) }}</code>
        </el-descriptions-item>
      </el-descriptions>
      <el-empty v-else-if="!taskLoading" description="任务不存在" />
    </el-card>

    <h3 class="section">任务定义说明</h3>
    <el-card>
      <el-empty description="任务定义本身不记录执行日志，请从运行记录查看具体执行过程" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { aetpApi } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";

const props = defineProps<{ taskId: string }>();
const projectStore = useProjectStore();

const projectId = computed(() => projectStore.currentProjectId ?? "");

const taskQuery = useQuery({
  queryKey: ["task", projectId, props.taskId],
  queryFn: () => aetpApi.testTasks.get(projectId.value, props.taskId),
  enabled: computed(() => !!projectId.value),
});

const taskLoading = computed(() => taskQuery.isLoading.value);
const task = computed(() => taskQuery.data.value ?? null);

function prettyJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
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
