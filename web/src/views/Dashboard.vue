<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="num">{{ stats.totalTasks }}</div>
            <div class="label">任务总数</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="num running">{{ stats.runningTasks }}</div>
            <div class="label">运行中</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="num online">{{ stats.onlineDevices }}</div>
            <div class="label">在线设备</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <h3 class="section">最近任务</h3>
    <el-card v-loading="loading">
      <el-table :data="recentTasks" @row-click="gotoTask">
        <el-table-column prop="task_id" label="任务ID" width="180" />
        <el-table-column prop="device_id" label="设备" width="160" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && recentTasks.length === 0" description="暂无任务数据" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import { useQueryClient } from "@tanstack/vue-query";
import { useQuery } from "@tanstack/vue-query";
import { aetpApi, type Task } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";

const router = useRouter();
const projectStore = useProjectStore();
const queryClient = useQueryClient();

useTaskEvents(queryClient);

const projectId = computed(() => projectStore.currentProjectId ?? "");

const tasksQuery = useQuery({
  queryKey: ["tasks", "dashboard", projectId],
  queryFn: () => aetpApi.tasks.list(projectId.value, { limit: 200 }),
  enabled: computed(() => !!projectId.value),
});

const devicesQuery = useQuery({
  queryKey: ["devices", "dashboard", projectId],
  queryFn: () => aetpApi.devices.list(projectId.value),
  enabled: computed(() => !!projectId.value),
});

const loading = computed(() => tasksQuery.isLoading.value || devicesQuery.isLoading.value);

const allTasks = computed(() => tasksQuery.data.value ?? []);
const allDevices = computed(() => devicesQuery.data.value ?? []);

const recentTasks = computed(() => allTasks.value.slice(0, 10));
const stats = computed(() => ({
  totalTasks: allTasks.value.length,
  runningTasks: allTasks.value.filter((t) => t.status === "running").length,
  onlineDevices: allDevices.value.filter((d) => d.online).length,
}));

function statusText(s: string) {
  const map: Record<string, string> = {
    pending: "待派发",
    dispatched: "已派发",
    accepted: "已接受",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    timeout: "超时",
  };
  return map[s] ?? s;
}

function statusTag(s: string) {
  const map: Record<string, "success" | "danger" | "warning" | "info"> = {
    pending: "info",
    dispatched: "info",
    accepted: "warning",
    running: "warning",
    completed: "success",
    failed: "danger",
    cancelled: "info",
    timeout: "danger",
  };
  return map[s] ?? "info";
}

function fmt(ts: string) {
  return new Date(ts).toLocaleString("zh-CN");
}

function gotoTask(row: Task) {
  router.push(`/tasks/${row.task_id}`);
}
</script>

<style scoped>
.stat {
  text-align: center;
  padding: 12px 0;
}
.num {
  font-size: 32px;
  font-weight: 600;
  color: #1a73e8;
}
.num.running {
  color: #e6a23c;
}
.num.online {
  color: #52c41a;
}
.label {
  color: #999;
  font-size: 13px;
  margin-top: 4px;
}
.section {
  margin: 24px 0 12px;
}
</style>
