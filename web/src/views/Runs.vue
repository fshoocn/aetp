<template>
  <div class="runs-page">
    <div class="page-heading">
      <div><span class="eyebrow">RUNS / EXECUTION HISTORY</span><h1>运行记录</h1><p>查看当前项目的下发、执行、结果和产物。</p></div>
      <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
    </div>
    <el-alert v-if="!projectId" title="尚未选择项目" description="请先选择项目。" type="info" show-icon :closable="false" />
    <el-card v-else v-loading="loading" shadow="never">
      <el-table :data="runs" row-key="run_id" @row-click="openRun">
        <el-table-column prop="run_id" label="Run ID" min-width="220" />
        <el-table-column prop="task_id" label="任务 ID" min-width="190" />
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="statusTag(row.status)">{{ statusText(row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="触发方式" width="120"><template #default="{ row }">{{ triggerText(row.trigger_type) }}</template></el-table-column>
        <el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click.stop="openRun(row)">查看</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && runs.length === 0" description="当前项目暂无运行记录" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Refresh } from "@element-plus/icons-vue";
import { aetpApi, type Run } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";
const router = useRouter(); const projectStore = useProjectStore(); const queryClient = useQueryClient(); useTaskEvents(queryClient);
const projectId = computed(() => projectStore.currentProjectId ?? "");
const query = useQuery({ queryKey: ["runs", "list", projectId], queryFn: () => aetpApi.runs.list(projectId.value), enabled: computed(() => !!projectId.value) });
const runs = computed(() => query.data.value ?? []); const loading = computed(() => query.isLoading.value);
function refresh() { queryClient.invalidateQueries({ queryKey: ["runs"] }); }
function openRun(run: Run) { router.push(`/runs/${run.run_id}`); }
function fmt(value: string) { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }
function triggerText(value: string) { return ({ manual_web: "手动", api: "API", retry: "重试", recovery: "恢复" } as Record<string, string>)[value] || value; }
function statusText(value: string) { return ({ created: "已创建", dispatched: "已派发", acked: "已确认", running: "运行中", succeeded: "成功", failed: "失败", cancelled: "已取消", timed_out: "超时", lost: "丢失" } as Record<string, string>)[value] || value; }
function statusTag(value: string) { return ({ succeeded: "success", running: "warning", failed: "danger", timed_out: "danger", cancelled: "info", dispatched: "info", acked: "info" } as Record<string, "success" | "danger" | "warning" | "info">)[value] || "info"; }
</script>

<style scoped>.runs-page { max-width: 1480px; margin: 0 auto; }.page-heading { display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:22px; }.eyebrow { color:var(--aetp-cyan); font-size:10px; font-weight:800; letter-spacing:.16em; }.page-heading h1 { margin:8px 0 6px; font-size:28px; }.page-heading p { margin:0; color:var(--aetp-muted); font-size:13px; }@media (max-width:760px) {.page-heading { align-items:flex-start; flex-direction:column; gap:15px; }}</style>
