<template>
  <div class="tasks-page">
    <div class="page-heading">
      <div><span class="eyebrow">RUN QUEUE / PROJECT SCOPE</span><h1>任务队列</h1><p>查看任务定义触发后的 Run 排队、派发和执行状态。</p></div>
      <div class="heading-actions"><el-button v-if="canDispatch" type="primary" @click="router.push('/test-tasks')">前往任务定义</el-button><el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button></div>
    </div>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个有权限的项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="运行队列加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <el-card v-if="projectId" class="filter-card" shadow="never">
      <el-form inline @submit.prevent="applyFilters">
        <el-form-item label="状态"><el-select v-model="filters.status" clearable placeholder="全部状态" style="width: 170px"><el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item>
        <el-form-item><el-button type="primary" :icon="Search" @click="applyFilters">筛选</el-button><el-button :icon="Refresh" @click="resetFilters">重置</el-button></el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="projectId" v-loading="loading" class="table-card" shadow="never">
      <template #header><div class="card-heading"><div><strong>Run 队列</strong><span>{{ projectStore.currentProject?.name || "当前项目" }}</span></div><el-tag effect="light">{{ filteredRuns.length }} 条</el-tag></div></template>
      <el-table :data="filteredRuns" row-key="run_id" @row-click="gotoRun">
        <el-table-column prop="run_id" label="Run ID" min-width="220" />
        <el-table-column prop="task_id" label="任务定义 ID" min-width="220" />
        <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="statusTag(row.status)" effect="light">{{ statusText(row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="触发方式" width="120"><template #default="{ row }">{{ triggerText(row.trigger_type) }}</template></el-table-column>
        <el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="完成时间" min-width="180"><template #default="{ row }">{{ row.finished_at ? fmt(row.finished_at) : '-' }}</template></el-table-column>
        <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click.stop="gotoRun(row)">查看</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && filteredRuns.length === 0" description="当前项目暂无运行队列记录" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Refresh, Search } from "@element-plus/icons-vue";
import { aetpApi, type Run } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";
import { runStatusText, runStatusTag, triggerText } from "@/utils/statusMaps";

const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
useTaskEvents(queryClient);
const projectId = computed(() => projectStore.currentProjectId ?? "");
const canDispatch = computed(() => auth.user?.platform_role === "admin" || ["operator", "maintainer", "owner"].includes(projectRole.value || ""));
const projectRole = computed(() => projectStore.currentRole);
const filters = reactive({ status: "" });
const statusOptions = [{ label: "等待调度", value: "created" }, { label: "已派发", value: "dispatched" }, { label: "已确认", value: "acked" }, { label: "运行中", value: "running" }, { label: "成功", value: "succeeded" }, { label: "失败", value: "failed" }, { label: "已取消", value: "cancelled" }, { label: "超时", value: "timed_out" }, { label: "丢失", value: "lost" }];
const runQuery = useQuery({ queryKey: ["runs", "queue", projectId], queryFn: () => aetpApi.runs.list(projectId.value), enabled: computed(() => !!projectId.value), refetchInterval: 5000 });
const runs = computed(() => runQuery.data.value ?? []);
const filteredRuns = computed(() => filters.status ? runs.value.filter((run) => run.status === filters.status) : runs.value);
const loading = computed(() => runQuery.isLoading.value || runQuery.isFetching.value);
const errorMessage = computed(() => runQuery.error.value?.message || "");
function applyFilters() { queryClient.invalidateQueries({ queryKey: ["runs", "queue"] }); }
function resetFilters() { filters.status = ""; }
function refresh() { queryClient.invalidateQueries({ queryKey: ["runs", "queue"] }); }
function gotoRun(row: Run) { router.push(`/runs/${row.run_id}`); }
const statusText = (status: string) => status === "created" ? "等待调度" : runStatusText(status);
const statusTag = runStatusTag;

function fmt(ts: string) { return new Date(ts).toLocaleString("zh-CN", { hour12: false }); }
</script>

<style scoped>
.tasks-page { max-width: 1480px; margin: 0 auto; }.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }.heading-actions { display: flex; align-items: center; gap: 10px; }.page-alert { margin-bottom: 14px; }.filter-card { margin-bottom: 14px; }.filter-card :deep(.el-form-item) { margin-bottom: 0; }.table-card :deep(.el-card__body) { padding-top: 0; }.card-heading { display: flex; justify-content: space-between; align-items: center; }.card-heading div { display: flex; align-items: baseline; gap: 10px; }.card-heading strong { font-size: 15px; }.card-heading span { color: var(--aetp-muted); font-size: 11px; }.device-cell { display: flex; align-items: center; gap: 7px; }.device-cell .el-icon { color: var(--aetp-blue); }.pagination-row { display: flex; align-items: center; justify-content: space-between; margin-top: 18px; color: var(--aetp-muted); font-size: 12px; }.dialog-alert { margin-bottom: 18px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 15px; }.heading-actions { width: 100%; justify-content: space-between; }.filter-card :deep(.el-form) { display: flex; flex-wrap: wrap; }.filter-card :deep(.el-form-item) { margin-bottom: 12px; }.pagination-row { align-items: flex-start; flex-direction: column; gap: 10px; } }
</style>
