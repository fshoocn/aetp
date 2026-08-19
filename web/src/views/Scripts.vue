<template>
  <div class="scripts-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">SCRIPT LIBRARY / PROJECT SCOPE</span>
        <h1>脚本库</h1>
        <p>上传测试脚本包，Master 插件自动验证并解析用例索引；勾选用例创建任务定义。</p>
      </div>
      <div class="heading-actions">
        <el-button v-if="canManage" type="primary" :icon="Plus" @click="openUpload">上传脚本</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="脚本库加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <el-card v-if="projectId" v-loading="loading" shadow="never">
      <template #header>
        <div class="card-heading">
          <div><strong>脚本版本</strong><span>{{ projectStore.currentProject?.name || '当前项目' }}</span></div>
          <el-tag effect="light">{{ scripts.length }} 个</el-tag>
        </div>
      </template>
      <el-table :data="scripts" row-key="script_id" @row-click="openScript">
        <el-table-column label="脚本" min-width="220">
          <template #default="{ row }">
            <div class="script-cell">
              <span class="script-mark"><el-icon><Document /></el-icon></span>
              <div>
                <strong>{{ row.name }}</strong>
                <small>{{ row.script_id }} · v{{ row.version }}</small>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="任务类型" width="140"><template #default="{ row }"><el-tag effect="plain" size="small">{{ row.task_type }}</el-tag></template></el-table-column>
        <el-table-column label="解析状态" width="120">
          <template #default="{ row }">
            <el-tag :type="parseTag(row.parse_status)" effect="light">{{ parseText(row.parse_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="插件版本" width="110"><template #default="{ row }"><span class="mono">{{ row.plugin_version || '-' }}</span></template></el-table-column>
        <el-table-column label="大小" width="100"><template #default="{ row }">{{ formatBytes(row.size) }}</template></el-table-column>
        <el-table-column label="上传时间" min-width="170"><template #default="{ row }">{{ row.created_at ? fmt(row.created_at) : '-' }}</template></el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="viewCases(row)">用例</el-button>
            <el-button v-if="canManage" link type="warning" :loading="reparsingId === row.script_id" @click.stop="reparse(row)">重解析</el-button>
            <el-button link @click.stop="download(row)">下载</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && scripts.length === 0" description="当前项目尚未上传脚本" />
    </el-card>

    <!-- 上传弹窗 -->
    <el-dialog v-model="uploadVisible" title="上传测试脚本" width="560px" destroy-on-close>
      <el-alert title="脚本将立即验证并解析用例" description="支持 .py / .zip；解析结果会生成可用例树，勾选后即可创建任务定义。" type="info" show-icon :closable="false" class="dialog-alert" />
      <el-form ref="formRef" :model="uploadForm" :rules="formRules" label-position="top">
        <el-form-item label="任务类型" prop="taskType">
          <el-select v-model="uploadForm.taskType" filterable placeholder="选择任务类型" style="width: 100%">
            <el-option v-for="plugin in taskTypes" :key="plugin.task_type" :label="`${plugin.display_name} (${plugin.task_type})`" :value="plugin.task_type" />
          </el-select>
        </el-form-item>
        <el-form-item label="脚本名称" prop="name">
          <el-input v-model="uploadForm.name" placeholder="如 CAN 通信回归" />
        </el-form-item>
        <el-form-item label="脚本文件" prop="file">
          <el-upload
            drag
            :auto-upload="false"
            :limit="1"
            :on-change="onFileChange"
            :on-remove="onFileRemove"
            accept=".py,.zip"
          >
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">拖拽文件到此处，或 <em>点击选择</em></div>
            <template #tip>
              <div class="el-upload__tip">
                扩展名白名单：{{ currentUploadSpec }}
              </div>
            </template>
          </el-upload>
        </el-form-item>
        <el-form-item label="配置参数（JSON）" prop="configText">
          <el-input v-model="uploadForm.configText" type="textarea" :rows="4" spellcheck="false" placeholder="{}" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">上传并解析</el-button>
      </template>
    </el-dialog>

    <!-- 用例抽屉 -->
    <el-drawer v-model="casesVisible" :title="`用例索引 · ${activeScript?.name || ''}`" size="520px">
      <div v-loading="casesLoading">
        <div class="cases-summary" v-if="cases.length">
          <el-tag effect="plain" size="small">{{ cases.length }} 个用例</el-tag>
          <span>耗时数据：{{ durationSamples }} 个样本</span>
        </div>
        <el-table :data="cases" size="small" max-height="60vh">
          <el-table-column prop="stable_key" label="稳定键 (stable_key)" min-width="240">
            <template #default="{ row }">
              <div class="case-cell">
                <strong>{{ row.name || row.stable_key }}</strong>
                <small>{{ row.stable_key }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="平均耗时" width="100">
            <template #default="{ row }">{{ row.avg_duration_s != null ? `${row.avg_duration_s}s` : '-' }}</template>
          </el-table-column>
          <el-table-column label="标签" width="120">
            <template #default="{ row }">
              <el-tag v-for="tag in (row.tags || [])" :key="tag" size="small" effect="plain" class="case-tag">{{ tag }}</el-tag>
              <span v-if="!(row.tags || []).length">-</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!casesLoading && cases.length === 0" description="该脚本尚未解析出用例" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules, UploadFile } from "element-plus";
import { ElMessage, ElMessageBox } from "element-plus";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Document, Plus, Refresh, UploadFilled } from "@element-plus/icons-vue";
import { aetpApi, type TestScript, type ScriptCase, type TaskTypePlugin } from "@/api/endpoints";
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

const scriptsQuery = useQuery({
  queryKey: ["scripts", projectId],
  queryFn: () => aetpApi.scripts.list(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const taskTypesQuery = useQuery({
  queryKey: ["taskTypes"],
  queryFn: () => aetpApi.plugins.list(),
});
const scripts = computed(() => scriptsQuery.data.value ?? []);
const taskTypes = computed(() => taskTypesQuery.data.value ?? []);
const loading = computed(() => scriptsQuery.isLoading.value);
const errorMessage = computed(() => scriptsQuery.error.value?.message || "");

function refresh() { qc.invalidateQueries({ queryKey: ["scripts"] }); }

// ---- 上传 ----
const uploadVisible = ref(false);
const formRef = ref<FormInstance>();
const uploadForm = reactive({ taskType: "", name: "", configText: "{}" });
const formRules: FormRules = {
  taskType: [{ required: true, message: "请选择任务类型", trigger: "change" }],
  name: [{ required: true, message: "请输入脚本名称", trigger: "blur" }],
};
const selectedFile = ref<File | null>(null);
const currentUploadSpec = computed(() => {
  const plugin = taskTypes.value.find((p) => p.task_type === uploadForm.taskType);
  if (!plugin) return "请先选择任务类型";
  const spec = plugin.upload_spec as { extensions?: string[]; max_size_mb?: number };
  return `${(spec.extensions || []).join(", ") || "任意"} · 最大 ${spec.max_size_mb ?? 100}MB`;
});
const uploadMutation = useMutation({
  mutationFn: () => {
    const config = JSON.parse(uploadForm.configText || "{}");
    return aetpApi.scripts.upload(projectId.value, selectedFile.value!, uploadForm.taskType, uploadForm.name, config);
  },
  onSuccess: () => {
    ElMessage.success("脚本已上传并解析成功");
    uploadVisible.value = false;
    refresh();
  },
  onError: (e: Error) => ElMessage.error(e.message),
});
const uploading = computed(() => uploadMutation.isPending.value);

function openUpload() { uploadForm.taskType = ""; uploadForm.name = ""; uploadForm.configText = "{}"; selectedFile.value = null; uploadVisible.value = true; }
function onFileChange(file: UploadFile) { selectedFile.value = file.raw ?? null; }
function onFileRemove() { selectedFile.value = null; }
async function submitUpload() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  if (!selectedFile.value) { ElMessage.warning("请选择脚本文件"); return; }
  try { JSON.parse(uploadForm.configText || "{}"); } catch { ElMessage.error("配置参数必须是合法 JSON"); return; }
  uploadMutation.mutate();
}

// ---- 用例抽屉 ----
const casesVisible = ref(false);
const activeScript = ref<TestScript | null>(null);
const casesQuery = useQuery({
  queryKey: ["scripts", "cases", projectId, () => activeScript.value?.script_id ?? ""],
  queryFn: () => aetpApi.scripts.cases(projectId.value, activeScript.value!.script_id),
  enabled: computed(() => casesVisible.value && !!activeScript.value),
});
const cases = computed(() => casesQuery.data.value ?? []);
const casesLoading = computed(() => casesQuery.isLoading.value);
const durationSamples = computed(() => cases.value.reduce((sum, c) => sum + (c.duration_samples || 0), 0));

function viewCases(row: TestScript) {
  activeScript.value = row;
  casesVisible.value = true;
  qc.invalidateQueries({ queryKey: ["scripts", "cases"] });
}
function openScript(row: TestScript) { viewCases(row); }

// ---- 重解析 ----
const reparsingId = ref<string | null>(null);
const reparseMutation = useMutation({
  mutationFn: (scriptId: string) => aetpApi.scripts.reparse(projectId.value, scriptId),
  onSuccess: () => { ElMessage.success("脚本重解析完成"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
async function reparse(row: TestScript) {
  try { await ElMessageBox.confirm(`确认重新解析脚本 ${row.name}？`, "重解析确认", { type: "warning" }); } catch { return; }
  reparsingId.value = row.script_id;
  reparseMutation.mutate(row.script_id, { onSettled: () => { reparsingId.value = null; } });
}

// ---- 下载 ----
async function download(row: TestScript) {
  try {
    const blob = await aetpApi.scripts.download(projectId.value, row.script_id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${row.name}-v${row.version}`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { ElMessage.error((e as Error).message); }
}

// ---- 格式化 ----
function fmt(v: string) { return new Date(v).toLocaleString("zh-CN", { hour12: false }); }
function formatBytes(v: number) { return v < 1024 ? `${v} B` : v < 1024 * 1024 ? `${(v / 1024).toFixed(1)} KB` : `${(v / 1024 / 1024).toFixed(1)} MB`; }
function parseText(v: string) { return ({ pending: "待解析", parsing: "解析中", parsed: "已解析", failed: "解析失败" } as Record<string, string>)[v] || v; }
function parseTag(v: string) { return ({ parsed: "success", parsing: "warning", failed: "danger", pending: "info" } as Record<string, "success" | "danger" | "warning" | "info">)[v] || "info"; }
</script>

<style scoped>
.scripts-page { max-width: 1480px; margin: 0 auto; }
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
.script-cell { display: flex; align-items: center; gap: 10px; }
.script-mark { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 7px; background: #eaf3ff; color: var(--aetp-blue); }
.script-cell div { display: flex; flex-direction: column; gap: 3px; }
.script-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.dialog-alert { margin-bottom: 18px; }
.cases-summary { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; color: var(--aetp-muted); font-size: 12px; }
.case-cell { display: flex; flex-direction: column; gap: 2px; }
.case-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
.case-tag { margin-right: 4px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }.heading-actions { width: 100%; justify-content: space-between; } }
</style>
