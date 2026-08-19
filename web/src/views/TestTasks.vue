<template>
  <div class="test-tasks-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">TASK DEFINITIONS / PROJECT SCOPE</span>
        <h1>任务定义</h1>
        <p>引用脚本版本 + 勾选用例 + 绑定节点，创建可重复执行的任务模板。</p>
      </div>
      <div class="heading-actions">
        <el-button v-if="canManage" type="primary" :icon="Plus" @click="openCreate">新建任务定义</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="任务定义加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <el-card v-if="projectId" v-loading="loading" shadow="never">
      <template #header>
        <div class="card-heading">
          <div><strong>任务模板</strong><span>{{ projectStore.currentProject?.name || '当前项目' }}</span></div>
          <el-tag effect="light">{{ tasks.length }} 个</el-tag>
        </div>
      </template>
      <el-table :data="tasks" row-key="task_id" @row-click="openTask">
        <el-table-column label="定义名" min-width="220">
          <template #default="{ row }">
            <div class="task-cell">
              <span class="task-mark"><el-icon><List /></el-icon></span>
              <div>
                <strong>{{ row.name }}</strong>
                <small>{{ row.task_id }}</small>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="任务类型" width="130"><template #default="{ row }"><el-tag effect="plain" size="small">{{ row.task_type }}</el-tag></template></el-table-column>
        <el-table-column label="用例数" width="90"><template #default="{ row }">{{ row.default_case_selection.length }}</template></el-table-column>
        <el-table-column label="绑定节点" min-width="160">
          <template #default="{ row }">
            <el-tag v-for="node in row.node_ids" :key="node" size="small" effect="plain" class="node-tag">{{ node }}</el-tag>
            <span v-if="!row.node_ids.length">-</span>
          </template>
        </el-table-column>
        <el-table-column label="分割策略" width="140"><template #default="{ row }">{{ splitText(row.split_policy) }}</template></el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" effect="light">{{ row.enabled ? '启用' : '停用' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="trigger(row)">运行</el-button>
            <el-button v-if="canManage" link type="warning" @click.stop="openEdit(row)">编辑</el-button>
            <el-button v-if="canManage" link type="danger" @click.stop="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && tasks.length === 0" description="当前项目尚未创建任务定义" />
    </el-card>

    <!-- 创建/编辑弹窗 -->
    <el-dialog v-model="editorVisible" :title="editing ? '编辑任务定义' : '新建任务定义'" width="720px" destroy-on-close>
      <el-form ref="formRef" :model="form" :rules="formRules" label-position="top">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="定义名称" prop="name"><el-input v-model="form.name" placeholder="如 CAN 通信回归" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="引用脚本" prop="scriptId">
              <el-select v-model="form.scriptId" filterable placeholder="选择已解析的脚本" style="width: 100%" @change="onScriptChange">
                <el-option v-for="s in parsedScripts" :key="s.script_id" :label="`${s.name} v${s.version} (${s.task_type})`" :value="s.script_id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="绑定节点">
              <el-select v-model="form.nodeIds" multiple filterable placeholder="候选 = 项目绑定节点" style="width: 100%">
                <el-option v-for="b in bindings" :key="b.node_id" :label="`${b.name || b.node_id} · ${b.node_id}`" :value="b.node_id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分割策略">
              <el-select v-model="form.splitType" style="width: 100%">
                <el-option label="不分割（单 Shard）" value="none" />
                <el-option label="按用例数量" value="by_case_count" />
                <el-option label="按测试时间" value="by_time" :disabled="!hasDurationData" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row v-if="form.splitType === 'by_case_count'" :gutter="16">
          <el-col :span="12">
            <el-form-item label="每 Shard 用例数">
              <el-input-number v-model="casesPerShard" :min="1" :max="1000" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row v-if="form.splitType === 'by_time'" :gutter="16">
          <el-col :span="12">
            <el-form-item label="目标时长（秒）">
              <el-input-number v-model="targetDurationS" :min="1" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-alert title="按 case 平均耗时切分；全部用例无耗时数据时禁用（D-21）" type="info" show-icon :closable="false" />
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="超时（秒，0=不限）">
              <el-input-number v-model="form.timeoutS" :min="0" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="重试策略">
              <el-select v-model="form.retryPolicy" style="width: 100%">
                <el-option label="不重试" value="none" />
                <el-option label="换节点重试 1 次" value="failover" />
                <el-option label="换节点重试 2 次" value="failover2" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="默认勾选用例（可全选 / 过滤）">
          <div class="case-selector">
            <div class="case-selector-bar">
              <el-input v-model="caseKeyword" placeholder="过滤用例" clearable size="small" style="width: 220px" />
              <el-button size="small" @click="selectAllCases">全选</el-button>
              <el-button size="small" @click="selectNoneCases">清空</el-button>
              <el-tag effect="plain" size="small">{{ form.caseKeys.length }} 已选</el-tag>
            </div>
            <el-table :data="filteredCases" size="small" max-height="280" @selection-change="onCaseSelection">
              <el-table-column type="selection" width="46" :reserve-selection="false" />
              <el-table-column prop="stable_key" label="稳定键" min-width="240">
                <template #default="{ row }">
                  <div class="case-cell">
                    <strong>{{ row.name || row.stable_key }}</strong>
                    <small>{{ row.stable_key }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="平均耗时" width="110">
                <template #default="{ row }">{{ row.avg_duration_s != null ? `${row.avg_duration_s}s` : '-' }}</template>
              </el-table-column>
            </el-table>
            <el-empty v-if="!caseLoading && filteredCases.length === 0" description="请先选择引用脚本" :image-size="50" />
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editorVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules } from "element-plus";
import { ElMessage, ElMessageBox } from "element-plus";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { List, Plus, Refresh } from "@element-plus/icons-vue";
import { aetpApi, type TestScript, type ScriptCase, type TestTask } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";

const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();
const qc = useQueryClient();
useTaskEvents(qc);

const projectId = computed(() => projectStore.currentProjectId ?? "");
const canManage = computed(() => auth.user?.platform_role === "admin" || ["maintainer", "owner"].includes(projectStore.currentRole || ""));

const tasksQuery = useQuery({
  queryKey: ["testTasks", projectId],
  queryFn: () => aetpApi.testTasks.list(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const scriptsQuery = useQuery({
  queryKey: ["scripts", projectId],
  queryFn: () => aetpApi.scripts.list(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const bindingsQuery = useQuery({
  queryKey: ["projectNodes", projectId],
  queryFn: () => aetpApi.projects.nodes(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const tasks = computed(() => tasksQuery.data.value ?? []);
const scripts = computed(() => scriptsQuery.data.value ?? []);
const bindings = computed(() => bindingsQuery.data.value ?? []);
const loading = computed(() => tasksQuery.isLoading.value);
const errorMessage = computed(() => tasksQuery.error.value?.message || "");
const parsedScripts = computed(() => scripts.value.filter((s) => s.parse_status === "parsed"));

function refresh() { qc.invalidateQueries({ queryKey: ["testTasks"] }); qc.invalidateQueries({ queryKey: ["scripts"] }); }

// ---- 编辑器 ----
const editorVisible = ref(false);
const editing = ref<TestTask | null>(null);
const formRef = ref<FormInstance>();
const caseKeyword = ref("");
const casesPerShard = ref(20);
const targetDurationS = ref(300);
const form = reactive({
  name: "",
  scriptId: "",
  nodeIds: [] as string[],
  splitType: "none",
  timeoutS: 0,
  retryPolicy: "none",
  caseKeys: [] as string[],
});
const formRules: FormRules = {
  name: [{ required: true, message: "请输入定义名称", trigger: "blur" }],
  scriptId: [{ required: true, message: "请选择引用脚本", trigger: "change" }],
};

// 脚本用例
const scriptCasesQuery = useQuery({
  queryKey: ["scripts", "cases", projectId, () => form.scriptId],
  queryFn: () => aetpApi.scripts.cases(projectId.value, form.scriptId),
  enabled: computed(() => !!form.scriptId),
});
const allCases = computed(() => scriptCasesQuery.data.value ?? []);
const caseLoading = computed(() => scriptCasesQuery.isLoading.value);
const hasDurationData = computed(() => allCases.value.some((c) => c.avg_duration_s != null));
const filteredCases = computed(() => {
  const kw = caseKeyword.value.trim().toLowerCase();
  if (!kw) return allCases.value;
  return allCases.value.filter((c) => c.stable_key.toLowerCase().includes(kw) || (c.name || "").toLowerCase().includes(kw));
});

function onScriptChange() {
  form.caseKeys = [];
  caseKeyword.value = "";
}
function onCaseSelection(rows: ScriptCase[]) { form.caseKeys = rows.map((r) => r.stable_key); }
function selectAllCases() { form.caseKeys = allCases.value.map((c) => c.stable_key); }
function selectNoneCases() { form.caseKeys = []; }

function openCreate() {
  editing.value = null;
  form.name = ""; form.scriptId = ""; form.nodeIds = []; form.splitType = "none";
  form.timeoutS = 0; form.retryPolicy = "none"; form.caseKeys = []; caseKeyword.value = "";
  casesPerShard.value = 20; targetDurationS.value = 300;
  editorVisible.value = true;
}
function openEdit(task: TestTask) {
  editing.value = task;
  form.name = task.name;
  form.scriptId = task.script_id;
  form.nodeIds = [...task.node_ids];
  form.splitType = (task.split_policy?.type as string) || "none";
  form.timeoutS = task.timeout_s;
  form.caseKeys = [...task.default_case_selection];
  const policy = task.split_policy as Record<string, unknown>;
  casesPerShard.value = typeof policy.cases_per_shard === "number" ? policy.cases_per_shard : 20;
  targetDurationS.value = typeof policy.target_duration_s === "number" ? policy.target_duration_s : 300;
  form.retryPolicy = (task.retry_policy?.max_attempts as number) > 1 ? "failover" : "none";
  caseKeyword.value = "";
  editorVisible.value = true;
}

const saveMutation = useMutation({
  mutationFn: () => {
    const splitPolicy = buildSplitPolicy();
    const retryPolicy = buildRetryPolicy();
    const payload = {
      name: form.name,
      script_id: form.scriptId,
      default_case_selection: form.caseKeys,
      node_ids: form.nodeIds,
      split_policy: splitPolicy,
      retry_policy: retryPolicy,
      timeout_s: form.timeoutS,
    };
    return editing.value
      ? aetpApi.testTasks.update(projectId.value, editing.value.task_id, payload)
      : aetpApi.testTasks.create(projectId.value, payload);
  },
  onSuccess: () => {
    ElMessage.success(editing.value ? "任务定义已更新" : "任务定义已创建");
    editorVisible.value = false;
    refresh();
  },
  onError: (e: Error) => ElMessage.error(e.message),
});
const saving = computed(() => saveMutation.isPending.value);

function buildSplitPolicy(): Record<string, unknown> {
  if (form.splitType === "by_case_count") return { type: "by_case_count", cases_per_shard: casesPerShard.value };
  if (form.splitType === "by_time") return { type: "by_time", target_duration_s: targetDurationS.value };
  return { type: "none" };
}
function buildRetryPolicy(): Record<string, unknown> {
  if (form.retryPolicy === "failover") return { max_attempts: 2, failover_nodes: true };
  if (form.retryPolicy === "failover2") return { max_attempts: 3, failover_nodes: true };
  return { max_attempts: 1, failover_nodes: false };
}
async function submit() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  if (form.splitType === "by_time" && !hasDurationData.value) {
    ElMessage.warning("全部用例无耗时数据，无法使用按时间分割（D-21）");
    return;
  }
  saveMutation.mutate();
}

// ---- 运行 ----
const triggeringId = ref<string | null>(null);
const triggerMutation = useMutation({
  mutationFn: (taskId: string) => aetpApi.runs.trigger(projectId.value, taskId),
  onSuccess: (run) => {
    ElMessage.success("Run 已创建并进入调度");
    router.push(`/runs/${run.run_id}`);
  },
  onError: (e: Error) => ElMessage.error(e.message),
});
async function trigger(row: TestTask) {
  if (!row.enabled) { ElMessage.warning("该任务定义已停用，无法触发"); return; }
  try { await ElMessageBox.confirm(`确认立即运行任务定义 ${row.name}？`, "运行确认", { type: "warning" }); } catch { return; }
  triggeringId.value = row.task_id;
  triggerMutation.mutate(row.task_id, { onSettled: () => { triggeringId.value = null; } });
}

// ---- 删除 ----
const removeMutation = useMutation({
  mutationFn: (taskId: string) => aetpApi.testTasks.remove(projectId.value, taskId),
  onSuccess: () => { ElMessage.success("任务定义已删除（停用）"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
async function remove(row: TestTask) {
  try { await ElMessageBox.confirm(`确认删除任务定义 ${row.name}？`, "删除确认", { type: "warning" }); } catch { return; }
  removeMutation.mutate(row.task_id);
}

function openTask(row: TestTask) { openEdit(row); }
function splitText(policy: Record<string, unknown>) {
  const type = policy?.type as string;
  if (type === "by_case_count") return `按数量 (${policy.cases_per_shard ?? '-'}/shard)`;
  if (type === "by_time") return `按时间 (${policy.target_duration_s ?? '-'}s)`;
  return "不分割";
}

watch(() => projectId.value, () => { refresh(); });
</script>

<style scoped>
.test-tasks-page { max-width: 1480px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }
.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }
.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }
.heading-actions { display: flex; align-items: center; gap: 10px; }
.page-alert { margin-bottom: 14px; }
.card-heading { display: flex; justify-content: space-between; align-items: center; }
.card-heading div { display: flex; align-items: baseline; gap: 10px; }
.card-heading strong { font-size: 15px; }
.card-heading span { color: var(--aetp-muted); font-size: 11px; }
.task-cell { display: flex; align-items: center; gap: 10px; }
.task-mark { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 7px; background: #eaf3ff; color: var(--aetp-blue); }
.task-cell div { display: flex; flex-direction: column; gap: 3px; }
.task-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
.node-tag { margin-right: 4px; }
.case-selector { width: 100%; border: 1px solid var(--el-border-color); border-radius: 6px; padding: 10px; }
.case-selector-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.case-cell { display: flex; flex-direction: column; gap: 2px; }
.case-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }.heading-actions { width: 100%; justify-content: space-between; } }
</style>
