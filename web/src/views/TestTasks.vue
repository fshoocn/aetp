<template>
  <div class="page">
    <header class="page-heading">
      <div>
        <span class="eyebrow">TEST TASKS</span>
        <h1>测试任务</h1>
        <p>组合多个 ScriptDefinition，固定执行顺序、节点范围和重试策略。</p>
      </div>
      <div class="heading-actions">
        <el-button v-if="canManage" type="primary" :icon="Plus" @click="openCreate">新建任务</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </header>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="任务加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <el-card v-if="projectId" v-loading="loading" shadow="never" class="content-card">
      <template #header><div class="card-heading"><div><strong>可执行任务</strong><span>TestTask revision</span></div><el-tag effect="plain">{{ tasks.length }} 个</el-tag></div></template>
      <el-table :data="tasks" row-key="task.task_id">
        <el-table-column label="任务" min-width="280"><template #default="{ row }"><div class="task-cell"><span class="task-mark"><el-icon><List /></el-icon></span><div><strong>{{ row.task.name }}</strong><small>{{ row.task.task_id }} · revision {{ row.task.revision }}</small></div></div></template></el-table-column>
        <el-table-column label="脚本" width="90"><template #default="{ row }">{{ row.task.scripts.length }}</template></el-table-column>
        <el-table-column label="执行模式" width="120"><template #default="{ row }"><el-tag effect="plain" size="small">{{ row.task.execution_mode }}</el-tag></template></el-table-column>
        <el-table-column label="节点范围" min-width="220"><template #default="{ row }"><span class="mono">{{ row.task.node_ids.length ? row.task.node_ids.join(', ') : '自动匹配' }}</span></template></el-table-column>
        <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.task.enabled ? 'success' : 'info'" effect="light">{{ row.task.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="120"><template #default="{ row }"><el-button link type="primary" @click="run(row.task)">运行</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && tasks.length === 0" description="当前项目尚未创建 TestTask" />
    </el-card>

    <el-dialog v-model="dialogVisible" title="新建 TestTask" width="640px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="任务名称"><el-input v-model="form.name" placeholder="例如夜间回归任务" /></el-form-item>
        <el-form-item label="选择脚本"><el-select v-model="form.scriptIds" multiple filterable style="width:100%" placeholder="至少选择一个 ScriptDefinition"><el-option v-for="definition in definitions" :key="definition.script_definition_id" :label="`${definition.name} · ${definition.script_definition_id}`" :value="definition.script_definition_id" /></el-select></el-form-item>
        <el-form-item label="执行模式"><el-radio-group v-model="form.executionMode"><el-radio-button value="parallel">并行</el-radio-button><el-radio-button value="sequence">顺序</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="目标节点"><el-select v-model="form.nodeIds" multiple filterable clearable style="width:100%" placeholder="留空由 Master 自动匹配"><el-option v-for="node in nodes" :key="node.node_id" :label="`${node.name || node.node_id} · ${node.online ? '在线' : '离线'}`" :value="node.node_id" :disabled="!node.enabled" /></el-select></el-form-item>
        <el-form-item label="失败策略"><el-switch v-model="form.stopOnFailure" active-text="顺序执行遇到失败时停止后续脚本" /></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="form.priority" :min="0" :max="1000" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">创建任务</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRouter } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { List, Plus, Refresh } from "@element-plus/icons-vue";
import { aetpApi, type Node, type ScriptDefinition, type TestTask, type TaskView } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
const projectId = computed(() => projectStore.currentProjectId ?? "");
const canManage = computed(() => auth.user?.platform_role === "admin" || ["maintainer", "owner"].includes(projectStore.currentRole || ""));
const tasksQuery = useQuery({ queryKey: ["tasks", projectId], queryFn: () => aetpApi.tasks.listTasks(projectId.value), enabled: computed(() => !!projectId.value) });
const definitionsQuery = useQuery({ queryKey: ["script-definitions", projectId], queryFn: () => aetpApi.tasks.listScriptDefinitions(projectId.value), enabled: computed(() => !!projectId.value) });
const nodesQuery = useQuery({ queryKey: ["nodes"], queryFn: () => aetpApi.assets.nodes(undefined, true) });
const tasks = computed(() => tasksQuery.data.value ?? []);
const definitions = computed(() => definitionsQuery.data.value ?? []);
const nodes = computed(() => nodesQuery.data.value ?? []);
const loading = computed(() => tasksQuery.isLoading.value || tasksQuery.isFetching.value);
const errorMessage = computed(() => tasksQuery.error.value?.message || definitionsQuery.error.value?.message || "");
const dialogVisible = ref(false);
const saving = ref(false);
const form = reactive({ name: "", scriptIds: [] as string[], nodeIds: [] as string[], executionMode: "parallel" as "parallel" | "sequence", stopOnFailure: false, priority: 0 });

function newBusinessId(): string {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let time = Date.now();
  let value = "";
  for (let index = 0; index < 10; index += 1) { value = alphabet[time % 32] + value; time = Math.floor(time / 32); }
  for (const byte of bytes) value += alphabet[byte % 32];
  return value;
}

function openCreate() {
  Object.assign(form, { name: "", scriptIds: [], nodeIds: [], executionMode: "parallel", stopOnFailure: false, priority: 0 });
  dialogVisible.value = true;
}
function refresh() { queryClient.invalidateQueries({ queryKey: ["tasks"] }); queryClient.invalidateQueries({ queryKey: ["script-definitions"] }); }
async function save() {
  if (!projectId.value || !form.name.trim() || !form.scriptIds.length) { ElMessage.warning("请填写任务名称并至少选择一个脚本"); return; }
  const selected = form.scriptIds.map((id) => definitions.value.find((item) => item.script_definition_id === id)).filter((item): item is ScriptDefinition => !!item);
  const task: TestTask = {
    task_id: newBusinessId(), project_id: projectId.value, revision: 1, name: form.name.trim(),
    scripts: selected.map((definition, index) => ({
      binding_id: newBusinessId(), script_definition_id: definition.script_definition_id, script_revision: definition.revision,
      case_selection: { selected_keys: [], include_all: true }, configuration: definition.configuration,
      split_policy: { type: "none", target_count: null, target_duration_s: null, plugin_id: null }, order_index: index, enabled: true,
    })),
    execution_mode: form.executionMode, stop_on_failure: form.stopOnFailure,
    retry_policy: { max_attempts: 1, failover_nodes: false, retry_failed_cases: false, backoff_initial_s: 1, backoff_max_s: 60 },
    node_ids: [...form.nodeIds], priority: form.priority, enabled: true,
  };
  saving.value = true;
  try { await aetpApi.tasks.createTask(projectId.value, task); ElMessage.success("TestTask 已创建"); dialogVisible.value = false; refresh(); }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : "创建失败"); }
  finally { saving.value = false; }
}
async function run(task: TestTask) {
  if (!projectId.value) return;
  try { const result = await aetpApi.tasks.createRun(projectId.value, { task_id: task.task_id, task_revision: task.revision }); router.push(`/runs/${result.run_id}`); }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : "启动失败"); }
}
</script>

<style scoped>
.page { max-width:1480px; margin:0 auto; }.page-heading { display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:22px; }.eyebrow { color:var(--aetp-cyan); font-size:10px; font-weight:800; letter-spacing:.16em; }.page-heading h1 { margin:8px 0 6px; color:var(--aetp-ink); font-size:28px; }.page-heading p { margin:0; color:var(--aetp-muted); font-size:13px; }.heading-actions { display:flex; gap:9px; }.page-alert { margin-top:14px; }.content-card :deep(.el-card__body) { padding-top:0; }.card-heading { display:flex; align-items:center; justify-content:space-between; }.card-heading div { display:flex; align-items:baseline; gap:10px; }.card-heading strong { font-size:15px; }.card-heading span { color:var(--aetp-muted); font-size:11px; }.task-cell { display:flex; align-items:center; gap:10px; }.task-mark { display:grid; width:34px; height:34px; place-items:center; border-radius:7px; background:#eaf3ff; color:var(--aetp-blue); }.task-cell div { display:flex; flex-direction:column; gap:3px; }.task-cell small { color:var(--aetp-muted); font:11px ui-monospace,monospace; }.mono { font:12px ui-monospace,monospace; }
@media (max-width:760px) { .page-heading { align-items:flex-start; flex-direction:column; gap:14px; } }
</style>
