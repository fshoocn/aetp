<template>
  <div class="page">
    <header class="page-heading">
      <div>
        <span class="eyebrow">SCRIPT DEFINITIONS</span>
        <h1>脚本定义</h1>
        <p>每个脚本都绑定精确 executor 版本，并保存不可变的用例索引。</p>
      </div>
      <div class="heading-actions">
        <el-button v-if="canManage" type="primary" :icon="Upload" @click="dialogVisible = true">上传脚本</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </header>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="脚本定义加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <section v-if="projectId" class="summary-band">
      <div><span class="summary-label">当前项目</span><strong>{{ projectStore.currentProject?.name || projectId }}</strong></div>
      <div><span class="summary-label">定义数量</span><strong>{{ definitions.length }}</strong></div>
      <div><span class="summary-label">可用用例</span><strong>{{ caseCount }}</strong></div>
      <div><span class="summary-label">Executor</span><strong>{{ executorCount }}</strong></div>
    </section>

    <el-card v-if="projectId" v-loading="loading" shadow="never" class="content-card">
      <template #header>
        <div class="card-heading"><div><strong>不可变脚本版本</strong><span>ScriptDefinition revision</span></div></div>
      </template>
      <el-table :data="definitions" row-key="script_definition_id" @row-click="showDefinition">
        <el-table-column label="定义" min-width="280">
          <template #default="{ row }">
            <div class="definition-cell"><span class="definition-mark"><el-icon><Document /></el-icon></span><div><strong>{{ row.name }}</strong><small>{{ row.script_definition_id }} · revision {{ row.revision }}</small></div></div>
          </template>
        </el-table-column>
        <el-table-column label="Executor" min-width="240"><template #default="{ row }"><span class="mono">{{ row.executor.plugin_id }}@{{ row.executor.version }}</span></template></el-table-column>
        <el-table-column label="用例" width="90"><template #default="{ row }">{{ row.cases.length }}</template></el-table-column>
        <el-table-column label="脚本文件" min-width="180"><template #default="{ row }"><span>{{ row.source.filename }}</span><small class="subline">{{ formatBytes(row.source.size) }}</small></template></el-table-column>
        <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" effect="light">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && definitions.length === 0" description="当前项目尚未创建 ScriptDefinition" />
    </el-card>

    <el-dialog v-model="dialogVisible" title="上传 ScriptDefinition" width="560px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="定义名称"><el-input v-model="form.name" placeholder="例如 Python 回归脚本" /></el-form-item>
        <el-form-item label="Executor 版本"><el-select v-model="form.executor" placeholder="选择已启用的 executor" style="width:100%"><el-option v-for="plugin in executors" :key="`${plugin.plugin_id}@${plugin.version}`" :label="`${plugin.manifest.display_name} · ${plugin.plugin_id}@${plugin.version}`" :value="`${plugin.plugin_id}|${plugin.version}`" /></el-select></el-form-item>
        <el-form-item label="脚本 ZIP"><el-upload drag :auto-upload="false" :limit="1" accept=".zip,.py" :on-change="onFileChange" :on-remove="onFileRemove"><el-icon class="upload-icon"><UploadFilled /></el-icon><div class="el-upload__text">拖入脚本包，或 <em>选择文件</em></div><template #tip><div class="el-upload__tip">executor 在 Master 侧解析用例，上传后生成固定 revision。</div></template></el-upload></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="uploading" :disabled="!file" @click="upload">上传并解析</el-button></template>
    </el-dialog>

    <el-drawer v-model="detailVisible" :title="selected?.name || 'ScriptDefinition 详情'" size="520px">
      <template v-if="selected"><el-descriptions :column="1" border><el-descriptions-item label="定义 ID"><span class="mono">{{ selected.script_definition_id }}</span></el-descriptions-item><el-descriptions-item label="Executor"><span class="mono">{{ selected.executor.plugin_id }}@{{ selected.executor.version }}</span></el-descriptions-item><el-descriptions-item label="文件"><span>{{ selected.source.filename }} · {{ formatBytes(selected.source.size) }}</span></el-descriptions-item><el-descriptions-item label="SHA-256"><span class="mono hash">{{ selected.source.sha256 }}</span></el-descriptions-item></el-descriptions><h3>用例索引</h3><el-table :data="selected.cases" size="small"><el-table-column prop="name" label="名称" min-width="150" /><el-table-column prop="stable_key" label="stable_key" min-width="220" /></el-table></template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Document, Refresh, Upload, UploadFilled } from "@element-plus/icons-vue";
import { aetpApi, type PluginVersion, type ScriptDefinition } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const auth = useAuthStore();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
const projectId = computed(() => projectStore.currentProjectId ?? "");
const canManage = computed(() => auth.user?.platform_role === "admin" || ["maintainer", "owner"].includes(projectStore.currentRole || ""));
const definitionsQuery = useQuery({ queryKey: ["script-definitions", projectId], queryFn: () => aetpApi.tasks.listScriptDefinitions(projectId.value), enabled: computed(() => !!projectId.value) });
const pluginsQuery = useQuery({ queryKey: ["plugins"], queryFn: () => aetpApi.plugins.list() });
const definitions = computed(() => definitionsQuery.data.value ?? []);
const executors = computed(() => (pluginsQuery.data.value ?? []).filter((plugin: PluginVersion) => plugin.point === "executor" && plugin.status === "enabled"));
const loading = computed(() => definitionsQuery.isLoading.value || definitionsQuery.isFetching.value);
const errorMessage = computed(() => definitionsQuery.error.value?.message || "");
const caseCount = computed(() => definitions.value.reduce((total, definition) => total + definition.cases.length, 0));
const executorCount = computed(() => new Set(definitions.value.map((definition) => definition.executor.plugin_id)).size);
const dialogVisible = ref(false);
const detailVisible = ref(false);
const selected = ref<ScriptDefinition | null>(null);
const uploading = ref(false);
const file = ref<File | null>(null);
const form = reactive({ name: "", executor: "" });

function refresh() { queryClient.invalidateQueries({ queryKey: ["script-definitions"] }); }
function onFileChange(uploadFile: { raw?: File }) { file.value = uploadFile.raw ?? null; }
function onFileRemove() { file.value = null; }
async function upload() {
  if (!projectId.value || !file.value || !form.name.trim() || !form.executor) return;
  const [executorPluginId, executorVersion] = form.executor.split("|");
  uploading.value = true;
  try {
    await aetpApi.tasks.uploadScriptDefinition(projectId.value, file.value, { name: form.name.trim(), executorPluginId, executorVersion });
    ElMessage.success("ScriptDefinition 已解析并保存");
    dialogVisible.value = false;
    form.name = "";
    form.executor = "";
    file.value = null;
    refresh();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "上传失败"); }
  finally { uploading.value = false; }
}
function showDefinition(definition: ScriptDefinition) { selected.value = definition; detailVisible.value = true; }
function formatBytes(size: number) { return size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`; }
</script>

<style scoped>
.page { max-width: 1480px; margin: 0 auto; }.page-heading { display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:22px; }.eyebrow { color:var(--aetp-cyan); font-size:10px; font-weight:800; letter-spacing:.16em; }.page-heading h1 { margin:8px 0 6px; color:var(--aetp-ink); font-size:28px; }.page-heading p { margin:0; color:var(--aetp-muted); font-size:13px; }.heading-actions { display:flex; gap:9px; }.page-alert { margin-top:14px; }.summary-band { display:grid; grid-template-columns:2fr repeat(3,1fr); gap:1px; margin-bottom:14px; overflow:hidden; border:1px solid #e4ebf0; border-radius:8px; background:#e4ebf0; }.summary-band > div { display:flex; flex-direction:column; gap:7px; min-height:80px; padding:16px 18px; background:#fff; }.summary-band strong { color:var(--aetp-ink); font-size:20px; }.summary-label { color:var(--aetp-muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }.content-card :deep(.el-card__body) { padding-top:0; }.card-heading { display:flex; align-items:center; justify-content:space-between; }.card-heading div { display:flex; align-items:baseline; gap:10px; }.card-heading strong { font-size:15px; }.card-heading span,.subline { color:var(--aetp-muted); font-size:11px; }.definition-cell { display:flex; align-items:center; gap:10px; }.definition-mark { display:grid; width:34px; height:34px; place-items:center; border-radius:7px; background:#eaf3ff; color:var(--aetp-blue); }.definition-cell div { display:flex; flex-direction:column; gap:3px; }.definition-cell small { color:var(--aetp-muted); font:11px ui-monospace,monospace; }.mono { font:12px ui-monospace,monospace; }.hash { word-break:break-all; }.upload-icon { color:var(--aetp-blue); font-size:30px; }.page h3 { margin:24px 0 12px; font-size:15px; }
@media (max-width:760px) { .page-heading { align-items:flex-start; flex-direction:column; gap:14px; }.summary-band { grid-template-columns:1fr 1fr; }.summary-band > div:first-child { grid-column:1/-1; } }
</style>
