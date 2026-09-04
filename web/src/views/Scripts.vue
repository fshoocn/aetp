<template>
  <div class="page">
    <header class="page-heading">
      <div>
        <span class="eyebrow">SCRIPT DEFINITIONS</span>
        <h1>脚本定义</h1>
        <p>每个脚本都绑定精确 executor 版本，并保存不可变的用例索引。</p>
      </div>
      <div class="heading-actions">
        <el-button v-if="canManage" type="primary" :icon="Upload" @click="openUploadDialog">上传脚本</el-button>
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

    <el-dialog v-model="dialogVisible" title="上传 ScriptDefinition" width="680px" destroy-on-close @closed="detachBridge">
      <el-form label-position="top">
        <el-form-item label="定义名称"><el-input v-model="form.name" placeholder="例如 Python 回归脚本" /></el-form-item>
        <el-form-item label="Executor 版本">
          <el-select v-model="form.executor" placeholder="选择已启用的 executor" style="width:100%" @change="onExecutorChange">
            <el-option v-for="plugin in executors" :key="`${plugin.plugin_id}@${plugin.version}`" :label="`${plugin.manifest.display_name} · ${plugin.plugin_id}@${plugin.version}`" :value="`${plugin.plugin_id}|${plugin.version}`" />
          </el-select>
        </el-form-item>

        <!-- 插件自带 UI：executor 声明 entrypoints.ui 时嵌入其界面，由插件决定如何收集资料/配置/生成用例 -->
        <el-form-item v-if="selectedUiPlugin" label="插件界面">
          <div class="ui-block-head">
            <span>{{ selectedUiPlugin.manifest.display_name }}</span>
            <el-tag v-if="pluginReady" type="success" size="small" effect="light">已连接</el-tag>
            <el-tag v-else type="info" size="small" effect="light">加载中…</el-tag>
          </div>
          <div class="ui-frame-wrap">
            <iframe
              :ref="(el) => setPluginFrame(el)"
              class="plugin-frame"
              :src="pluginUiSrc"
              @load="onPluginFrameLoad"
            ></iframe>
          </div>
          <div v-if="pluginPayload.filename" class="plugin-picked">
            <el-icon><Document /></el-icon>
            <span>{{ pluginPayload.filename }}</span>
            <small v-if="pluginPayload.file">{{ formatBytes(pluginPayload.file.size) }}</small>
          </div>
          <div v-if="pluginPayload.cases?.length" class="plugin-picked">
            <el-icon><Collection /></el-icon>
            <span>插件已生成 {{ pluginPayload.cases.length }} 个用例</span>
          </div>
        </el-form-item>

        <!-- 无插件 UI：使用宿主通用文件上传 -->
        <el-form-item v-else label="脚本 ZIP">
          <el-upload drag :auto-upload="false" :limit="1" accept=".zip,.py,.xlsx,.csv" :on-change="onFileChange" :on-remove="onFileRemove"><el-icon class="upload-icon"><UploadFilled /></el-icon><div class="el-upload__text">拖入脚本包，或 <em>选择文件</em></div><template #tip><div class="el-upload__tip">executor 在 Master 侧解析用例，上传后生成固定 revision。</div></template></el-upload>
        </el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="uploading" :disabled="!canSubmit" @click="upload">上传并解析</el-button></template>
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
import { Collection, Document, Refresh, Upload, UploadFilled } from "@element-plus/icons-vue";
import { aetpApi, type PluginVersion, type ScriptDefinition } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const PLUGIN_UI_PROTOCOL = "aetp.plugin-ui.v2" as const;

interface PluginTestCase {
  stable_key: string;
  name: string;
  parent_path?: string;
  tags?: string[];
}

interface PluginSubmitPayload {
  file?: File;
  filename?: string;
  configuration?: Record<string, unknown>;
  cases?: PluginTestCase[];
}

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

// ---- 插件自带 UI 集成：executor 声明 entrypoints.ui 时在弹层内嵌其界面 ----
const pluginFrameEl = ref<HTMLIFrameElement | null>(null);
const pluginReady = ref(false);
const pluginSessionId = ref("");
const pluginPayload = reactive<PluginSubmitPayload>({});

function hasUiEntry(plugin: PluginVersion): boolean {
  return Boolean((plugin.manifest as { entrypoints?: { ui?: string } }).entrypoints?.ui);
}

const selectedUiPlugin = computed<PluginVersion | null>(() => {
  if (!form.executor) return null;
  const [pluginId, version] = form.executor.split("|");
  return executors.value.find((plugin) => plugin.plugin_id === pluginId && plugin.version === version && hasUiEntry(plugin)) ?? null;
});

const pluginUiSrc = computed(() => {
  const plugin = selectedUiPlugin.value;
  if (!plugin) return "";
  return `/plugins/${encodeURIComponent(plugin.plugin_id)}/${encodeURIComponent(plugin.version)}/ui`;
});

const canSubmit = computed(() => {
  if (!projectId.value || !form.name.trim() || !form.executor) return false;
  return selectedUiPlugin.value ? Boolean(pluginPayload.file) : Boolean(file.value);
});

function randomId(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${rand}`;
}

function setPluginFrame(el: unknown): void {
  pluginFrameEl.value = (el as HTMLIFrameElement | null) ?? null;
}

function onPluginFrameLoad(): void {
  const frame = pluginFrameEl.value;
  const plugin = selectedUiPlugin.value;
  if (!frame || !plugin || !projectId.value) return;
  // 宿主 -> 插件：握手告知上下文（mode=script-upload：脚本上传弹层）
  pluginSessionId.value = randomId("uis");
  frame.contentWindow?.postMessage(
    {
      protocol: PLUGIN_UI_PROTOCOL,
      session_id: pluginSessionId.value,
      request_id: randomId("req"),
      type: "initialize",
      payload: {
        context: {
          plugin_id: plugin.plugin_id,
          version: plugin.version,
          point: plugin.manifest.point,
          display_name: plugin.manifest.display_name,
          project_id: projectId.value,
          mode: "script-upload",
        },
      },
    },
    window.location.origin,
  );
}

function handlePluginMessage(event: MessageEvent): void {
  const frame = pluginFrameEl.value;
  if (!frame || event.origin !== window.location.origin || event.source !== frame.contentWindow) return;
  const data = event.data as {
    protocol?: string;
    session_id?: string;
    type?: string;
    payload?: { file?: File; filename?: string; configuration?: Record<string, unknown>; cases?: PluginTestCase[]; context?: Record<string, unknown> };
  } | null;
  if (!data || data.protocol !== PLUGIN_UI_PROTOCOL || !data.type) return;
  if (data.session_id && data.session_id !== pluginSessionId.value) return;
  const payload = data.payload ?? {};
  if (data.type === "ready") {
    pluginReady.value = true;
  } else if (data.type === "submit") {
    // 插件把 资料文件 + 配置 + （可选）已生成用例 交回宿主；宿主负责认证上传
    pluginPayload.file = payload.file;
    pluginPayload.filename = payload.filename || payload.file?.name;
    pluginPayload.configuration = payload.configuration ?? {};
    pluginPayload.cases = payload.cases;
    pluginReady.value = true;
  } else if (data.type === "configuration.changed") {
    pluginPayload.configuration = (payload as { configuration?: Record<string, unknown> }).configuration ?? {};
  }
}

function attachBridge(): void {
  window.addEventListener("message", handlePluginMessage);
}
function detachBridge(): void {
  window.removeEventListener("message", handlePluginMessage);
}

function openUploadDialog(): void {
  // 弹窗打开前先挂消息监听：destroy-on-close 会重建 DOM，iframe 随后加载并握手
  attachBridge();
  dialogVisible.value = true;
}

function resetPluginPayload(): void {
  pluginReady.value = false;
  pluginSessionId.value = "";
  pluginPayload.file = undefined;
  pluginPayload.filename = undefined;
  pluginPayload.configuration = undefined;
  pluginPayload.cases = undefined;
}

function onExecutorChange(): void {
  // 切换 executor：清掉旧插件的负载与宿主文件，等新 iframe 重握手
  file.value = null;
  resetPluginPayload();
}

function refresh() { queryClient.invalidateQueries({ queryKey: ["script-definitions"] }); }
function onFileChange(uploadFile: { raw?: File }) { file.value = uploadFile.raw ?? null; }
function onFileRemove() { file.value = null; }

async function upload() {
  if (!projectId.value || !form.name.trim() || !form.executor) return;
  const [executorPluginId, executorVersion] = form.executor.split("|");
  const uploadFile = selectedUiPlugin.value ? pluginPayload.file ?? null : file.value;
  if (!uploadFile) return;
  uploading.value = true;
  try {
    await aetpApi.tasks.uploadScriptDefinition(projectId.value, uploadFile, {
      name: form.name.trim(),
      executorPluginId,
      executorVersion,
      configuration: pluginPayload.configuration ?? {},
      cases: pluginPayload.cases,
    });
    ElMessage.success("ScriptDefinition 已解析并保存");
    dialogVisible.value = false;
    form.name = "";
    form.executor = "";
    file.value = null;
    resetPluginPayload();
    refresh();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "上传失败"); }
  finally { uploading.value = false; }
}
function showDefinition(definition: ScriptDefinition) { selected.value = definition; detailVisible.value = true; }
function formatBytes(size: number) { return size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`; }
</script>

<style scoped>
.page { max-width: 1480px; margin: 0 auto; }.page-heading { display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:22px; }.eyebrow { color:var(--aetp-cyan); font-size:10px; font-weight:800; letter-spacing:.16em; }.page-heading h1 { margin:8px 0 6px; color:var(--aetp-ink); font-size:28px; }.page-heading p { margin:0; color:var(--aetp-muted); font-size:13px; }.heading-actions { display:flex; gap:9px; }.page-alert { margin-top:14px; }.summary-band { display:grid; grid-template-columns:2fr repeat(3,1fr); gap:1px; margin-bottom:14px; overflow:hidden; border:1px solid #e4ebf0; border-radius:8px; background:#e4ebf0; }.summary-band > div { display:flex; flex-direction:column; gap:7px; min-height:80px; padding:16px 18px; background:#fff; }.summary-band strong { color:var(--aetp-ink); font-size:20px; }.summary-label { color:var(--aetp-muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }.content-card :deep(.el-card__body) { padding-top:0; }.card-heading { display:flex; align-items:center; justify-content:space-between; }.card-heading div { display:flex; align-items:baseline; gap:10px; }.card-heading strong { font-size:15px; }.card-heading span,.subline { color:var(--aetp-muted); font-size:11px; }.definition-cell { display:flex; align-items:center; gap:10px; }.definition-mark { display:grid; width:34px; height:34px; place-items:center; border-radius:7px; background:#eaf3ff; color:var(--aetp-blue); }.definition-cell div { display:flex; flex-direction:column; gap:3px; }.definition-cell small { color:var(--aetp-muted); font:11px ui-monospace,monospace; }.mono { font:12px ui-monospace,monospace; }.hash { word-break:break-all; }.upload-icon { color:var(--aetp-blue); font-size:30px; }.page h3 { margin:24px 0 12px; font-size:15px; }
.ui-block-head { display:flex; align-items:center; justify-content:space-between; width:100%; font-size:13px; font-weight:600; }
.ui-frame-wrap { width:100%; border:1px solid var(--aetp-border, #e4e9f2); border-radius:8px; overflow:hidden; background:#fff; }
.plugin-frame { display:block; width:100%; height:380px; border:0; }
.plugin-picked { display:flex; align-items:center; gap:8px; margin-top:10px; font-size:13px; color:var(--aetp-ink); }
.plugin-picked small { color:var(--aetp-muted); }
@media (max-width:760px) { .page-heading { align-items:flex-start; flex-direction:column; gap:14px; }.summary-band { grid-template-columns:1fr 1fr; }.summary-band > div:first-child { grid-column:1/-1; } }
</style>
