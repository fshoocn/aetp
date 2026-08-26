<template>
  <div class="dashboard">
    <div class="page-heading">
      <div>
        <span class="eyebrow">PROJECT OPERATIONS</span>
        <h1>{{ currentProject?.name || "项目总览" }}</h1>
        <p>{{ currentProject?.description || "查看项目运行状态、任务队列和可用设备。" }}</p>
      </div>
      <div class="heading-actions">
        <el-tag v-if="currentProject" :type="currentProject.status === 'active' ? 'success' : 'info'" effect="light">
          {{ currentProject.status === "active" ? "项目运行中" : "项目已归档" }}
        </el-tag>
        <el-button :icon="Refresh" circle text :loading="loading" title="刷新数据" @click="refresh" />
      </div>
    </div>

    <el-alert v-if="queryError" title="项目数据加载失败" :description="queryError" type="error" show-icon :closable="false" class="page-alert" />
    <el-alert v-if="!projectId" title="请选择项目" description="从顶部项目选择器选择一个项目后，这里会显示对应的任务与设备。" type="info" show-icon :closable="false" class="page-alert" />

    <el-row v-loading="loading" :gutter="14" class="stat-row">
      <el-col :xs="24" :sm="12" :lg="6"><el-card class="stat-card" shadow="never"><el-statistic title="运行总数" :value="stats.totalRuns"><template #prefix><el-icon class="stat-icon blue"><List /></el-icon></template></el-statistic><span class="stat-caption">当前项目 Run 记录</span></el-card></el-col>
      <el-col :xs="24" :sm="12" :lg="6"><el-card class="stat-card" shadow="never"><el-statistic title="执行中" :value="stats.runningRuns"><template #prefix><el-icon class="stat-icon amber"><Timer /></el-icon></template></el-statistic><span class="stat-caption">等待、派发或正在运行</span></el-card></el-col>
      <el-col :xs="24" :sm="12" :lg="6"><el-card class="stat-card" shadow="never"><el-statistic title="在线节点" :value="stats.onlineNodes"><template #prefix><el-icon class="stat-icon green"><Connection /></el-icon></template></el-statistic><span class="stat-caption">Master 已注册节点</span></el-card></el-col>
      <el-col :xs="24" :sm="12" :lg="6"><el-card class="stat-card" shadow="never"><el-statistic title="设备在线率" :value="stats.deviceRate ?? 0" :suffix="stats.deviceRate !== null ? '%' : ''"><template #prefix><el-icon class="stat-icon cyan"><DataLine /></el-icon></template></el-statistic><el-progress :percentage="stats.deviceRate ?? 0" :show-text="false" :stroke-width="5" color="#17a2a4" /></el-card></el-col>
    </el-row>

    <el-row :gutter="14" class="content-row">
      <el-col :xs="24" :lg="16">
        <el-card class="table-card" shadow="never">
          <template #header><div class="card-heading"><div><strong>最近运行</strong><span>按最新创建时间排列</span></div><el-button text type="primary" @click="router.push('/runs')">查看全部 <el-icon><ArrowRight /></el-icon></el-button></div></template>
          <el-table :data="recentRuns" row-key="run_id" @row-click="gotoRun">
            <el-table-column prop="run_id" label="Run ID" min-width="210" />
            <el-table-column prop="task_id" label="任务 ID" min-width="150" />
            <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="statusTag(row.status)" effect="light" size="small">{{ statusText(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间" min-width="170"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
          </el-table>
          <el-empty v-if="!loading && recentRuns.length === 0" description="当前项目还没有运行记录" />
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="8">
        <el-card class="side-card" shadow="never">
          <template #header><div class="card-heading"><div><strong>资产状态</strong><span>项目绑定设备</span></div><el-button text type="primary" @click="router.push('/devices')">资产页</el-button></div></template>
          <div class="asset-summary"><div class="asset-number">{{ allDevices.length }}</div><div><strong>台设备</strong><p>{{ stats.onlineDevices }} 台在线，{{ allDevices.length - stats.onlineDevices }} 台离线</p></div></div>
          <el-divider />
          <div v-for="device in allDevices.slice(0, 5)" :key="device.device_id" class="asset-row"><span class="asset-pip" :class="{ online: device.online }"></span><div class="asset-copy"><strong>{{ device.name || device.device_id }}</strong><small>{{ device.node_id || "未分配节点" }}</small></div><el-tag :type="device.online ? 'success' : 'info'" size="small" effect="plain">{{ device.online ? "在线" : "离线" }}</el-tag></div>
          <el-empty v-if="!loading && allDevices.length === 0" description="暂无绑定设备" :image-size="70" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { ArrowRight, Connection, DataLine, List, Refresh, Timer } from "@element-plus/icons-vue";
import { aetpApi, type Run } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";
import { runStatusText, runStatusTag } from "@/utils/statusMaps";

const router = useRouter();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
useTaskEvents(queryClient);
const projectId = computed(() => projectStore.currentProjectId ?? "");
const currentProject = computed(() => projectStore.projects.find((project) => project.project_id === projectId.value));
const runsQuery = useQuery({ queryKey: ["runs", "dashboard", projectId], queryFn: () => aetpApi.runs.list(projectId.value, 200), enabled: computed(() => !!projectId.value), refetchInterval: 5000 });
const nodesQuery = useQuery({ queryKey: ["assets", "nodes", "dashboard"], queryFn: () => aetpApi.assets.nodes(undefined, true), refetchInterval: 5000 });
const devicesQuery = useQuery({ queryKey: ["devices", "dashboard", projectId], queryFn: () => aetpApi.devices.list(projectId.value), enabled: computed(() => !!projectId.value), refetchInterval: 5000 });
const loading = computed(() => runsQuery.isLoading.value || nodesQuery.isLoading.value || devicesQuery.isLoading.value);
const queryError = computed(() => (runsQuery.error.value || nodesQuery.error.value || devicesQuery.error.value)?.message || "");
const allRuns = computed(() => runsQuery.data.value ?? []);
const allNodes = computed(() => nodesQuery.data.value ?? []);
const allDevices = computed(() => devicesQuery.data.value ?? []);
const recentRuns = computed(() => allRuns.value.slice(0, 8));
const stats = computed(() => { const total = allDevices.value.length; const online = allDevices.value.filter((device) => device.online).length; return { totalRuns: allRuns.value.length, runningRuns: allRuns.value.filter((run) => ["created", "dispatched", "acked", "running"].includes(run.status)).length, onlineNodes: allNodes.value.filter((node) => node.online).length, onlineDevices: online, deviceRate: total ? Math.round((online / total) * 100) : null }; });
function refresh() { queryClient.invalidateQueries({ queryKey: ["runs"] }); queryClient.invalidateQueries({ queryKey: ["assets", "nodes"] }); queryClient.invalidateQueries({ queryKey: ["devices"] }); }
function gotoRun(row: Run) { router.push(`/runs/${row.run_id}`); }
function fmt(ts: string) { return new Date(ts).toLocaleString("zh-CN", { hour12: false }); }
const statusText = runStatusText;
const statusTag = runStatusTag;
</script>

<style scoped>
.dashboard { max-width: 1480px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 24px; }
.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.page-heading h1 { margin: 8px 0 6px; font-size: 28px; letter-spacing: -.03em; }
.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }
.heading-actions { display: flex; align-items: center; gap: 8px; }
.page-alert { margin-bottom: 14px; }
.stat-row, .content-row { margin-bottom: 14px; }
.stat-card { height: 142px; padding: 5px 3px; }
.stat-card :deep(.el-statistic__head) { color: var(--aetp-muted); font-size: 12px; font-weight: 650; }
.stat-card :deep(.el-statistic__content) { margin-top: 7px; color: var(--aetp-ink); font-size: 31px; font-weight: 700; }
.stat-icon { margin-right: 10px; font-size: 19px; }
.stat-icon.blue { color: var(--aetp-blue); }.stat-icon.amber { color: var(--aetp-amber); }.stat-icon.green { color: #2f9d71; }.stat-icon.cyan { color: var(--aetp-cyan); }
.stat-caption { display: block; margin: 12px 0 10px; color: #9ba7af; font-size: 11px; }
.table-card, .side-card { height: 100%; }
.card-heading { display: flex; align-items: center; justify-content: space-between; }
.card-heading div { display: flex; align-items: baseline; gap: 10px; }.card-heading strong { font-size: 15px; }.card-heading span { color: #97a3ab; font-size: 11px; }
.asset-summary { display: flex; align-items: center; gap: 16px; padding: 8px 0; }.asset-number { color: var(--aetp-blue); font-size: 38px; font-weight: 750; }.asset-summary strong { font-size: 14px; }.asset-summary p { margin: 5px 0 0; color: var(--aetp-muted); font-size: 12px; }
.asset-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid #edf0f2; }.asset-row:last-child { border-bottom: 0; }.asset-pip { width: 8px; height: 8px; border-radius: 50%; background: #b8c2c8; }.asset-pip.online { background: #35aa76; box-shadow: 0 0 0 4px #e6f6ee; }.asset-copy { display: flex; flex: 1; flex-direction: column; gap: 3px; min-width: 0; }.asset-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }.asset-copy small { color: #99a5ad; font-size: 11px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }.page-heading h1 { font-size: 24px; } }
</style>
