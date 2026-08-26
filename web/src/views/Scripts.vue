<template>
  <div class="scripts-page">
    <header class="scripts-hero">
      <div class="hero-copy">
        <span class="eyebrow"><i></i>PROJECT SCRIPT LIBRARY</span>
        <h1>测试脚本库</h1>
        <p>集中管理脚本版本、插件配置与用例索引，让每次上传都能直接进入可执行状态。</p>
      </div>
      <div class="hero-actions">
        <el-button v-if="canManage" class="upload-trigger" type="primary" :icon="Plus" @click="openUpload">上传脚本</el-button>
        <el-button class="refresh-trigger" :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </header>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="脚本库加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <section v-if="projectId" class="library-stats">
      <div class="stats-intro">
        <span class="section-kicker">LIBRARY PULSE</span>
        <strong>{{ projectStore.currentProject?.name || '当前项目' }}</strong>
        <span>脚本资产概览</span>
      </div>
      <div class="stat-block">
        <span>全部脚本</span>
        <strong>{{ scripts.length }}</strong>
      </div>
      <div class="stat-block stat-success">
        <span>已解析</span>
        <strong>{{ parsedScripts }}</strong>
      </div>
      <div class="stat-block stat-pending">
        <span>处理中</span>
        <strong>{{ pendingScripts }}</strong>
      </div>
    </section>

    <el-card v-if="projectId" class="scripts-panel" v-loading="loading" shadow="never">
      <template #header>
        <div class="panel-heading">
          <div>
            <span class="section-kicker">VERSION CATALOG</span>
            <strong>脚本版本</strong>
          </div>
          <span class="panel-hint">点击脚本行查看用例索引</span>
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
              <el-tag v-if="row.file_missing" type="danger" size="small" effect="dark">文件缺失</el-tag>
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
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="viewCases(row)">用例</el-button>
            <el-button v-if="canManage" link type="warning" :loading="reparsingId === row.script_id" @click.stop="reparse(row)">重解析</el-button>
            <el-button link @click.stop="download(row)">下载</el-button>
            <el-button v-if="canManage" link type="danger" :loading="deletingId === row.script_id" @click.stop="deleteScript(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && scripts.length === 0" description="当前项目尚未上传脚本" />
    </el-card>

    <!-- 上传流程：第一步选择任务类型，第二步进入对应插件的 UI 工作台 -->
    <el-dialog v-model="typeSelectVisible" title="选择任务类型" width="620px">
      <div v-loading="taskTypesLoading" class="type-grid">
        <button
          v-for="plugin in taskTypes"
          :key="plugin.task_type"
          class="type-card"
          type="button"
          @click="choosePlugin(plugin)"
        >
          <span class="type-mark"><el-icon :size="20"><Grid /></el-icon></span>
          <span class="type-copy">
            <strong>{{ plugin.display_name }}</strong>
            <small>{{ plugin.task_type }} · v{{ plugin.plugin_version }}</small>
          </span>
          <span class="type-tags">
            <el-tag v-if="plugin.agent_available" type="success" effect="light" size="small">Agent 可用</el-tag>
            <el-tag type="info" effect="plain" size="small">UI 工作台</el-tag>
          </span>
          <el-icon class="type-arrow"><ArrowRight /></el-icon>
        </button>
        <el-empty v-if="!taskTypesLoading && taskTypes.length === 0" description="Master 未加载任何任务类型插件" :image-size="60" />
      </div>
      <template #footer>
        <el-button @click="typeSelectVisible = false">取消</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="uploadVisible"
      class="script-upload-dialog"
      :title="`上传测试脚本 · ${currentPlugin?.display_name || ''}`"
      width="1120px"
      destroy-on-close
      @closed="closePluginUi"
    >
      <div v-if="pluginUiObjectUrl" class="plugin-ui-shell">
        <div class="plugin-ui-toolbar">
          <span><i></i>PLUGIN UI</span>
          <span v-if="contextLoading">同步项目能力中...</span>
          <span v-else>配置、上传与验证均由插件页面提供</span>
        </div>
        <div v-loading="pluginUiLoading || contextLoading" class="plugin-ui-frame-wrap">
          <iframe
            ref="pluginFrame"
            :src="pluginUiObjectUrl"
            title="任务类型插件配置页面"
            class="plugin-ui-frame"
            @load="postPluginContext"
          />
        </div>
      </div>
      <div v-else-if="pluginUiLoading" class="plugin-state plugin-state-loading">
        <span class="state-pulse"></span>
        <strong>正在加载插件工作区</strong>
        <p>正在读取插件包内的配置与上传页面...</p>
      </div>
      <div v-else class="plugin-state plugin-state-warning">
        <span class="state-mark">!</span>
        <strong>该插件没有可用的 UI</strong>
        <p>请安装包含 <code>ui/index.html</code> 的插件包后再上传。</p>
      </div>
    </el-dialog>

    <el-drawer v-model="casesVisible" :title="`用例索引 · ${activeScript?.name || ''}`" size="520px">
      <div v-loading="casesLoading">
        <div v-if="cases.length" class="cases-summary">
          <el-tag effect="plain" size="small">{{ cases.length }} 个用例</el-tag>
          <span>耗时数据：{{ durationSamples }} 个样本</span>
        </div>
        <el-table :data="cases" size="small" max-height="60vh">
          <el-table-column prop="stable_key" label="稳定键 (stable_key)" min-width="240">
            <template #default="{ row }">
              <div class="case-cell"><strong>{{ row.name || row.stable_key }}</strong><small>{{ row.stable_key }}</small></div>
            </template>
          </el-table-column>
          <el-table-column label="平均耗时" width="100"><template #default="{ row }">{{ row.avg_duration_s != null ? `${row.avg_duration_s}s` : '-' }}</template></el-table-column>
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
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, toRaw, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Document, Grid, Plus, Refresh, ArrowRight } from "@element-plus/icons-vue";
import { aetpApi, type TaskTypeConfigContext, type TestScript, type ScriptCase, type TaskTypePlugin } from "@/api/endpoints";
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
const parsedScripts = computed(() => scripts.value.filter((script) => script.parse_status === "parsed").length);
const pendingScripts = computed(() => scripts.value.filter((script) => ["pending", "parsing"].includes(script.parse_status)).length);

function refresh() { qc.invalidateQueries({ queryKey: ["scripts"] }); }

// ---- 上传流程：第一步选择任务类型，第二步进入对应插件的 UI 工作台 ----
const typeSelectVisible = ref(false);
const uploadVisible = ref(false);
const uploadForm = reactive({ taskType: "", scriptId: "", config: {} as Record<string, unknown> });
const currentPlugin = computed(() => taskTypes.value.find((plugin) => plugin.task_type === uploadForm.taskType) ?? null);
const taskTypesLoading = computed(() => taskTypesQuery.isLoading.value || taskTypesQuery.isFetching.value);

function openUpload() {
  uploadForm.taskType = "";
  uploadForm.scriptId = "";
  uploadForm.config = {};
  typeSelectVisible.value = true;
}

function choosePlugin(plugin: TaskTypePlugin) {
  uploadForm.taskType = plugin.task_type;
  typeSelectVisible.value = false;
  uploadVisible.value = true;
}

// ---- 上传 ----
const pluginUiUrl = computed(() => currentPlugin.value?.ui?.url || "");
const pluginUiObjectUrl = ref("");
const pluginUiLoading = ref(false);
const pluginUploading = ref(false);
const pluginFrame = ref<HTMLIFrameElement>();
const pluginContext = ref<TaskTypeConfigContext | null>(null);
const verifyNodeId = ref("");
const verifyRequestId = ref("");
let verifyPollTimer: number | null = null;
const contextQuery = useQuery({
  queryKey: ["taskTypeConfigContext", projectId, () => uploadForm.taskType],
  queryFn: () => aetpApi.plugins.configContext(projectId.value, uploadForm.taskType),
  enabled: computed(() => uploadVisible.value && !!projectId.value && !!uploadForm.taskType),
});
const contextLoading = computed(() => contextQuery.isLoading.value || contextQuery.isFetching.value);

async function loadPluginUi() {
  closePluginUi();
  if (!pluginUiUrl.value) return;
  pluginUiLoading.value = true;
  try {
    const htmlBlob = await aetpApi.plugins.uiAsset(pluginUiUrl.value);
    const html = await htmlBlob.text();
    const inlineHtml = await inlinePluginAssets(html, pluginUiUrl.value);
    pluginUiObjectUrl.value = URL.createObjectURL(new Blob([inlineHtml], { type: "text/html" }));
  } catch (error) {
    ElMessage.error(`插件配置页面加载失败: ${(error as Error).message}`);
  } finally {
    pluginUiLoading.value = false;
  }
}

async function inlinePluginAssets(html: string, entryUrl: string): Promise<string> {
  const assetPattern = /(?:src|href)=["'](vendor\/[^"']+)["']/g;
  const assets = [...html.matchAll(assetPattern)].map((match) => match[1]);
  const replacements = await Promise.all(
    [...new Set(assets)].map(async (assetPath) => {
      const assetUrl = new URL(assetPath, `${window.location.origin}${entryUrl}`).pathname;
      const asset = await aetpApi.plugins.uiAsset(assetUrl);
      const bytes = new Uint8Array(await asset.arrayBuffer());
      let binary = "";
      const chunkSize = 0x8000;
      for (let index = 0; index < bytes.length; index += chunkSize) {
        binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
      }
      const mime = asset.type || (assetPath.endsWith(".css") ? "text/css" : "text/javascript");
      return [assetPath, `data:${mime};base64,${btoa(binary)}`] as const;
    }),
  );
  return replacements.reduce((result, [path, dataUrl]) => result.split(path).join(dataUrl), html);
}

function closePluginUi() {
  if (verifyPollTimer !== null) window.clearTimeout(verifyPollTimer);
  verifyPollTimer = null;
  if (pluginUiObjectUrl.value) URL.revokeObjectURL(pluginUiObjectUrl.value);
  pluginUiObjectUrl.value = "";
  pluginContext.value = null;
  verifyRequestId.value = "";
}
function postPluginContext() {
  if (!pluginFrame.value?.contentWindow || !contextQuery.data.value) return;
  const context = cloneForPostMessage(toRaw(contextQuery.data.value));
  const config = cloneForPostMessage(toRaw(uploadForm.config));
  pluginContext.value = context;
  pluginFrame.value.contentWindow.postMessage(
    {
      type: "aetp.plugin.context",
      payload: {
        context: {
          ...context,
          config,
          script_id: uploadForm.scriptId,
        },
      },
    },
    window.location.origin,
  );
}
function sendPluginMessage(type: string, payload: Record<string, unknown>) {
  const target = pluginFrame.value?.contentWindow;
  if (!target) return;
  try {
    target.postMessage({ type, payload: cloneForPostMessage(toRaw(payload)) }, window.location.origin);
  } catch (error) {
    console.warn(`插件消息发送失败: ${type}`, error);
  }
}
function cloneForPostMessage<T>(value: T): T {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value)) as T;
}
function asConfig(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}
function asUploadFile(value: unknown, fallbackName: string): File | null {
  if (typeof File !== "undefined" && value instanceof File) return value;
  if (typeof Blob !== "undefined" && value instanceof Blob) {
    return new File([value], fallbackName, { type: value.type });
  }
  return null;
}
async function handlePluginUpload(payload: Record<string, unknown>) {
  const file = asUploadFile(payload.file, typeof payload.filename === "string" ? payload.filename : "script");
  const name = typeof payload.name === "string" ? payload.name.trim() : "";
  if (!file || !name) {
    sendPluginMessage("aetp.plugin.upload-result", { status: "error", errors: ["请填写脚本名称并选择脚本文件"] });
    return;
  }
  if (!projectId.value || !uploadForm.taskType || pluginUploading.value) return;
  const config = asConfig(payload.config);
  uploadForm.config = config;
  pluginUploading.value = true;
  let script: TestScript;
  try {
    script = await aetpApi.scripts.upload(projectId.value, file, uploadForm.taskType, name, config);
  } catch (error) {
    const message = error instanceof Error ? error.message : "脚本上传失败";
    pluginUploading.value = false;
    sendPluginMessage("aetp.plugin.upload-result", { status: "error", errors: [message] });
    ElMessage.error(message);
    return;
  }

  // API 已成功落库后，后续 UI 同步异常不能再把上传结果改报为失败。
  uploadForm.scriptId = script.script_id;
  pluginUploading.value = false;
  refresh();
  ElMessage.success("脚本已上传并解析成功");
  sendPluginMessage("aetp.plugin.upload-result", { status: "success", script });
  try {
    await nextTick();
    postPluginContext();
  } catch (error) {
    console.warn("插件上传成功，但上下文同步失败", error);
  }
}
async function onPluginMessage(event: MessageEvent) {
  if (event.source !== pluginFrame.value?.contentWindow || event.origin !== window.location.origin) return;
  const message = event.data as { type?: string; payload?: Record<string, unknown> };
  if (message.type === "aetp.plugin.ready") postPluginContext();
  if (message.type === "aetp.plugin.config") {
    uploadForm.config = asConfig(message.payload?.config);
    verifyNodeId.value = typeof message.payload?.node_id === "string" ? message.payload.node_id : "";
  }
  if (message.type === "aetp.plugin.upload") await handlePluginUpload(message.payload || {});
  if (message.type === "aetp.plugin.verify") {
    const scriptId = typeof message.payload?.script_id === "string" ? message.payload.script_id : uploadForm.scriptId;
    const nodeId = typeof message.payload?.node_id === "string" ? message.payload.node_id : verifyNodeId.value;
    if (!scriptId || !nodeId) {
      sendPluginMessage("aetp.plugin.verify-result", { errors: ["请先上传脚本并选择验证节点"] });
      return;
    }
    try {
      const dispatch = await aetpApi.scripts.verify(projectId.value, scriptId, nodeId, uploadForm.config);
      verifyRequestId.value = dispatch.verify_id;
      sendPluginMessage("aetp.plugin.verify-dispatch", { message: "验证命令已下发", ...dispatch });
      pollVerifyResult(scriptId, dispatch.verify_id);
    } catch (error) {
      sendPluginMessage("aetp.plugin.verify-result", { errors: [(error as Error).message] });
    }
  }
}
async function pollVerifyResult(scriptId: string, verifyId: string, attempt = 0) {
  if (attempt >= 60 || verifyRequestId.value !== verifyId) return;
  try {
    const result = await aetpApi.scripts.verifyResult(projectId.value, scriptId, verifyId);
    sendPluginMessage("aetp.plugin.verify-result", result);
    verifyPollTimer = null;
  } catch {
    verifyPollTimer = window.setTimeout(() => pollVerifyResult(scriptId, verifyId, attempt + 1), 1000);
  }
}
watch(pluginUiUrl, loadPluginUi);
watch(() => contextQuery.data.value, postPluginContext);
watch(() => uploadForm.scriptId, postPluginContext);
onMounted(() => window.addEventListener("message", onPluginMessage));
onUnmounted(() => { window.removeEventListener("message", onPluginMessage); closePluginUi(); });

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
const deletingId = ref<string | null>(null);
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

async function deleteScript(row: TestScript) {
  try {
    await ElMessageBox.confirm(
      `确认删除脚本「${row.name}」v${row.version}？删除后将同时移除该版本的用例索引，且无法恢复。`,
      "删除脚本确认",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  deletingId.value = row.script_id;
  try {
    await aetpApi.scripts.remove(projectId.value, row.script_id);
    ElMessage.success("脚本已删除");
    refresh();
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    deletingId.value = null;
  }
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
.scripts-page { width: 100%; max-width: 1480px; margin: 0 auto; }
.scripts-hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; margin-bottom: 18px; padding: 30px 32px 28px; border: 1px solid #d9e6e9; border-radius: 12px; background: linear-gradient(118deg, #f9fcfc 0%, #eef7f7 58%, #f4f8fb 100%); }
.hero-copy { min-width: 0; }
.eyebrow, .section-kicker, .workbench-step { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.eyebrow { display: inline-flex; align-items: center; gap: 8px; }
.eyebrow i, .plugin-ui-toolbar i { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--aetp-cyan); box-shadow: 0 0 0 4px rgba(23, 162, 164, .12); }
.hero-copy h1 { margin: 10px 0 7px; color: var(--aetp-ink); font-size: 30px; letter-spacing: .01em; }
.hero-copy p { max-width: 600px; margin: 0; color: var(--aetp-muted); font-size: 13px; line-height: 1.7; }
.hero-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 10px; }
.upload-trigger { min-width: 116px; }
.refresh-trigger { border-color: #cbd9de; background: rgba(255, 255, 255, .76); }
.page-alert { margin-bottom: 14px; }
.library-stats { display: grid; grid-template-columns: minmax(230px, 1.7fr) repeat(3, minmax(120px, .7fr)); gap: 0; margin-bottom: 14px; overflow: hidden; border: 1px solid var(--aetp-line); border-radius: 10px; background: var(--aetp-panel); }
.stats-intro, .stat-block { min-height: 86px; padding: 17px 20px; }
.stats-intro { display: flex; flex-direction: column; justify-content: center; gap: 4px; background: #f7fafb; }
.stats-intro strong { color: var(--aetp-ink); font-size: 15px; }
.stats-intro > span:last-child { color: var(--aetp-muted); font-size: 11px; }
.stat-block { display: flex; flex-direction: column; justify-content: center; gap: 5px; border-left: 1px solid var(--aetp-line); }
.stat-block span { color: var(--aetp-muted); font-size: 11px; }
.stat-block strong { color: var(--aetp-ink); font-size: 25px; line-height: 1; }
.stat-success strong { color: #2f9d71; }
.stat-pending strong { color: var(--aetp-amber); }
.scripts-panel { border: 1px solid var(--aetp-line); }
.scripts-panel :deep(.el-card__header) { padding: 17px 20px; border-bottom-color: var(--aetp-line); }
.scripts-panel :deep(.el-card__body) { padding: 0; }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.panel-heading > div { display: flex; flex-direction: column; gap: 5px; }
.panel-heading strong { color: var(--aetp-ink); font-size: 15px; }
.panel-hint { color: var(--aetp-muted); font-size: 11px; }
.script-cell { display: flex; align-items: center; gap: 10px; }
.script-cell .el-tag { margin-left: auto; }
.script-mark { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 8px; background: #eaf3ff; color: var(--aetp-blue); }
.script-cell div { display: flex; flex-direction: column; gap: 3px; }
.script-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.cases-summary { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; color: var(--aetp-muted); font-size: 12px; }
.case-cell { display: flex; flex-direction: column; gap: 2px; }
.case-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }
.case-tag { margin-right: 4px; }
.type-grid { display: grid; gap: 12px; padding: 2px; }
.type-card { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto 18px; align-items: center; gap: 14px; width: 100%; padding: 15px 16px; border: 1px solid #dce7eb; border-radius: 10px; color: var(--aetp-ink); background: linear-gradient(135deg, #fbfefe 0%, #f3f8f9 100%); cursor: pointer; text-align: left; transition: border-color .18s ease, background .18s ease, box-shadow .18s ease, transform .18s ease; }
.type-card:hover { border-color: #8ac8c6; background: #fff; box-shadow: 0 8px 20px rgba(25, 93, 98, .1); transform: translateY(-1px); }
.type-card:focus-visible { outline: 3px solid rgba(30, 111, 217, .2); outline-offset: 2px; border-color: var(--aetp-blue); }
.type-mark { display: grid; width: 44px; height: 44px; place-items: center; border: 1px solid #c6e5e2; border-radius: 11px; color: var(--aetp-cyan); background: #eaf8f7; }
.type-copy { display: flex; min-width: 0; flex-direction: column; gap: 5px; }
.type-copy strong { overflow: hidden; color: var(--aetp-ink); font-size: 15px; font-weight: 750; text-overflow: ellipsis; white-space: nowrap; }
.type-copy small { overflow: hidden; color: var(--aetp-muted); font: 11px ui-monospace, monospace; text-overflow: ellipsis; white-space: nowrap; }
.type-tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.type-arrow { color: #91a5aa; transition: color .18s ease, transform .18s ease; }
.type-card:hover .type-arrow { color: var(--aetp-cyan); transform: translateX(2px); }

:deep(.script-upload-dialog.el-dialog) { width: min(1120px, calc(100vw - 40px)); max-width: 1120px; max-height: calc(100dvh - 32px); margin: 16px auto; overflow: hidden; border-radius: 12px; }
:deep(.script-upload-dialog.el-dialog .el-dialog__header) { margin-right: 0; padding: 20px 24px 17px; border-bottom: 1px solid var(--aetp-line); }
:deep(.script-upload-dialog.el-dialog .el-dialog__title) { color: var(--aetp-ink); font-size: 17px; font-weight: 750; }
:deep(.script-upload-dialog.el-dialog .el-dialog__body) { display: flex; height: calc(100dvh - 110px); min-height: 0; max-height: calc(100dvh - 110px); flex-direction: column; padding: 0; overflow: hidden; background: #f4f7f8; }
:deep(.script-upload-dialog.el-dialog .el-dialog__footer) { padding: 14px 24px; border-top: 1px solid var(--aetp-line); background: #fbfcfd; }
.plugin-ui-shell { display: flex; min-height: 0; height: 100%; flex: 1; flex-direction: column; overflow: hidden; border: 1px solid #d5e1e4; border-radius: 9px; background: #fff; box-shadow: 0 8px 24px rgba(34, 66, 76, .08); }
.plugin-ui-toolbar { display: flex; align-items: center; justify-content: space-between; min-height: 38px; padding: 0 13px; border-bottom: 1px solid #e3ecee; color: #789096; background: #fbfdfd; font-size: 10px; letter-spacing: .08em; }
.plugin-ui-toolbar span { display: inline-flex; align-items: center; gap: 7px; }
.plugin-ui-toolbar span:last-child { letter-spacing: 0; }
.plugin-ui-frame-wrap { min-height: 0; height: auto; flex: 1; background: #fff; }
.plugin-ui-frame { display: block; width: 100%; height: 100%; border: 0; background: #fff; }
.plugin-state { display: flex; min-height: 300px; height: 100%; flex: 1; flex-direction: column; align-items: center; justify-content: center; padding: 30px; text-align: center; border: 1px dashed #cbdadd; border-radius: 9px; background: rgba(255, 255, 255, .6); }
.plugin-state strong { margin-top: 15px; color: var(--aetp-ink); font-size: 15px; }
.plugin-state p { max-width: 360px; margin: 8px 0 0; color: var(--aetp-muted); font-size: 12px; line-height: 1.6; }
.plugin-state code { padding: 2px 5px; border-radius: 4px; color: var(--aetp-blue-deep); background: #eaf3ff; font-family: ui-monospace, monospace; }
.state-mark { display: grid; width: 46px; height: 46px; place-items: center; border: 1px solid #c8dadd; border-radius: 50%; color: var(--aetp-cyan); background: #edf8f7; font-size: 15px; font-weight: 800; }
.plugin-state-warning .state-mark { color: var(--aetp-amber); border-color: #efd7a9; background: #fff8e9; }
.state-pulse { width: 18px; height: 18px; border: 3px solid #c8e5e2; border-top-color: var(--aetp-cyan); border-radius: 50%; animation: script-spin .8s linear infinite; }
@keyframes script-spin { to { transform: rotate(360deg); } }

@media (max-width: 760px) {
  .scripts-hero { align-items: flex-start; flex-direction: column; gap: 18px; padding: 24px 20px; }
  .hero-actions { width: 100%; justify-content: space-between; }
  .library-stats { grid-template-columns: 1fr 1fr; }
  .stats-intro { grid-column: 1 / -1; }
  .stat-block:nth-child(3) { border-left: 0; }
  .type-card { grid-template-columns: 40px minmax(0, 1fr) 18px; gap: 11px; padding: 13px; }
  .type-mark { width: 40px; height: 40px; }
  .type-tags { grid-column: 2 / -1; justify-content: flex-start; }
  :deep(.script-upload-dialog.el-dialog) { width: calc(100vw - 16px); max-height: calc(100dvh - 16px); margin: 8px auto; }
  :deep(.script-upload-dialog.el-dialog .el-dialog__body) { height: calc(100dvh - 110px); max-height: calc(100dvh - 110px); padding: 0; }
  .plugin-ui-frame-wrap { height: auto; }
  .plugin-state { min-height: 260px; height: 100%; padding: 20px; }
}
</style>
