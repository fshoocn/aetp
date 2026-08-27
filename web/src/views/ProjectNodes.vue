<template>
  <div class="project-nodes-page">
    <div class="page-heading">
      <div><span class="eyebrow">PROJECT / NODE BINDINGS</span><h1>项目节点管理</h1><p>管理当前项目可调度的 Agent 节点。绑定的节点才能被任务定义选中和调度。</p></div>
      <div class="heading-actions">
        <el-button v-if="canManage" type="primary" :icon="Plus" @click="openBind">绑定节点</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="项目节点加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <el-card v-if="projectId" v-loading="loading" shadow="never">
      <template #header><div class="card-heading"><div><strong>已绑定节点</strong><span>{{ projectStore.currentProject?.name || '当前项目' }}</span></div><el-tag effect="light">{{ bindings.length }} 个节点</el-tag></div></template>
      <el-table :data="bindings" row-key="node_id">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="cap-panel">
              <div class="cap-panel-title">节点能力</div>
              <CapabilityPanel :capabilities="row.capabilities" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="节点" min-width="230">
          <template #default="{ row }"><div class="node-cell"><span class="node-mark"><el-icon><Cpu /></el-icon></span><div><strong>{{ row.name || row.node_id }}</strong><small>{{ row.node_id }}</small></div></div></template>
        </el-table-column>
        <el-table-column label="主机" min-width="170"><template #default="{ row }"><span class="mono">{{ row.hostname || '-' }}</span></template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.online ? 'success' : 'info'" effect="light">{{ row.online ? '在线' : '离线' }}</el-tag></template></el-table-column>
        <el-table-column label="全局启用" width="110"><template #default="{ row }"><el-tag :type="row.node_enabled ? 'success' : 'danger'" effect="plain" size="small">{{ row.node_enabled ? '是' : '否' }}</el-tag></template></el-table-column>
        <el-table-column label="项目启用" width="110"><template #default="{ row }"><el-switch :model-value="row.enabled" :disabled="!canManage" @change="(val: boolean) => toggle(row, val)" /></template></el-table-column>
        <el-table-column label="外设" width="80"><template #default="{ row }"><el-tag effect="plain" size="small">{{ row.devices.length }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="100"><template #default="{ row }"><el-button v-if="canManage" link type="danger" @click="remove(row)">解绑</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && bindings.length === 0" description="当前项目未绑定任何节点" />
    </el-card>

    <el-card v-if="projectId && canManage" class="section bindable-section" shadow="never">
      <template #header><strong>可绑定的平台节点</strong></template>
      <el-table :data="unboundNodes" row-key="node_id">
        <el-table-column label="节点" min-width="230">
          <template #default="{ row }"><div class="node-cell"><span class="node-mark"><el-icon><Cpu /></el-icon></span><div><strong>{{ row.name || row.node_id }}</strong><small>{{ row.node_id }}</small></div></div></template>
        </el-table-column>
        <el-table-column label="主机" min-width="170"><template #default="{ row }"><span class="mono">{{ row.hostname || '-' }}</span></template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.online ? 'success' : 'info'" effect="light">{{ row.online ? '在线' : '离线' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click="bind(row)">绑定</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="unboundNodes.length === 0" description="所有平台节点均已绑定到当前项目" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Plus, Refresh, Cpu } from "@element-plus/icons-vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { aetpApi, type ProjectNodeBinding, type Node } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";
import CapabilityPanel from "@/components/CapabilityPanel.vue";

const auth = useAuthStore();
const projectStore = useProjectStore();
const qc = useQueryClient();
useTaskEvents(qc);

const projectId = computed(() => projectStore.currentProjectId ?? "");
const canManage = computed(() => auth.user?.platform_role === "admin" || ["maintainer", "owner"].includes(projectStore.currentRole || ""));

const bindingsQuery = useQuery({
  queryKey: ["projectNodes", projectId],
  queryFn: () => aetpApi.projects.nodes(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const nodesQuery = useQuery({
  queryKey: ["assets", "nodes", "bindable"],
  queryFn: () => aetpApi.assets.nodes(undefined, true),
});
const bindings = computed(() => bindingsQuery.data.value ?? []);
const loading = computed(() => bindingsQuery.isLoading.value || nodesQuery.isLoading.value);
const errorMessage = computed(() => bindingsQuery.error.value?.message || "");
const unboundNodes = computed(() => {
  const boundIds = new Set(bindings.value.map((b) => b.node_id));
  return (nodesQuery.data.value ?? []).filter((n) => n.enabled && !boundIds.has(n.node_id));
});

function refresh() { qc.invalidateQueries({ queryKey: ["projectNodes"] }); qc.invalidateQueries({ queryKey: ["assets", "nodes"] }); }

const bindMutation = useMutation({
  mutationFn: (nodeId: string) => aetpApi.projects.bindNode(projectId.value, nodeId),
  onSuccess: () => { ElMessage.success("节点已绑定"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
const toggleMutation = useMutation({
  mutationFn: ({ nodeId, enabled }: { nodeId: string; enabled: boolean }) => aetpApi.projects.updateNode(projectId.value, nodeId, enabled),
  onSuccess: () => { ElMessage.success("绑定状态已更新"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
const removeMutation = useMutation({
  mutationFn: (nodeId: string) => aetpApi.projects.removeNode(projectId.value, nodeId),
  onSuccess: () => { ElMessage.success("节点已解绑"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});

function bind(row: Node) { bindMutation.mutate(row.node_id); }
function toggle(row: ProjectNodeBinding, enabled: boolean) { toggleMutation.mutate({ nodeId: row.node_id, enabled }); }
async function remove(row: ProjectNodeBinding) { try { await ElMessageBox.confirm(`确认解绑节点 ${row.node_id}？`, "解绑节点", { type: "warning" }); removeMutation.mutate(row.node_id); } catch { /* cancelled */ } }

function openBind() {
  document.querySelector<HTMLElement>(".bindable-section")?.scrollIntoView({ behavior: "smooth" });
}
</script>

<style scoped>
.project-nodes-page { max-width: 1480px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }
.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }
.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }
.heading-actions { display: flex; align-items: center; gap: 10px; }
.page-alert { margin-bottom: 14px; }
.section { margin-top: 14px; }
.card-heading { display: flex; justify-content: space-between; align-items: center; }
.card-heading div { display: flex; align-items: baseline; gap: 10px; }
.card-heading strong { font-size: 15px; }
.card-heading span { color: var(--aetp-muted); font-size: 11px; }
.node-cell { display: flex; align-items: center; gap: 10px; }
.node-mark { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 7px; background: #eaf3ff; color: var(--aetp-blue); }
.node-cell div { display: flex; flex-direction: column; gap: 3px; }
.node-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.cap-panel { padding: 3px 28px 12px 56px; }
.cap-panel-title { color: #596a78; font-size: 12px; font-weight: 650; margin-bottom: 10px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }.heading-actions { width: 100%; justify-content: space-between; } }
</style>
