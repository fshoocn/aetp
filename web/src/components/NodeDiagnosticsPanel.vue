<template>
  <div class="diagnostics-panel" v-loading="loading">
    <el-alert
      v-if="!isRegisteredNode"
      title="该节点尚未注册能力快照"
      description="节点使用会话注册后，能力、插件库存和诊断信息会显示在这里。"
      type="info"
      show-icon
      :closable="false"
    />
    <el-alert
      v-else-if="errorMessage"
      title="诊断数据暂不可用"
      :description="errorMessage"
      type="warning"
      show-icon
      :closable="false"
    />
    <div v-if="isRegisteredNode" class="panel-head">
      <div class="panel-label">能力与诊断</div>
      <div class="panel-actions">
        <el-button size="small" :loading="collecting" @click="collect">立即采集诊断</el-button>
        <el-button v-if="isAdmin" size="small" :icon="Setting" :loading="actionLoading" @click="updateLogLevel">设置日志</el-button>
        <el-button v-if="isAdmin" size="small" :icon="Upload" :loading="syncing" @click="syncVisible = true">同步插件</el-button>
        <el-button v-if="isAdmin" size="small" :icon="Refresh" :loading="actionLoading" @click="drain">排空节点</el-button>
        <el-button v-if="isAdmin" size="small" type="danger" plain :icon="SwitchButton" :loading="actionLoading" @click="restart">重启 Agent</el-button>
      </div>
    </div>
    <div v-if="isRegisteredNode && isAdmin" class="maintenance-controls">
      <span class="control-label">日志组件</span>
      <el-input v-model="logComponent" size="small" class="component-input" placeholder="agent.runtime" />
      <el-select v-model="logLevel" size="small" class="level-select" aria-label="日志级别">
        <el-option label="DEBUG" value="debug" />
        <el-option label="INFO" value="info" />
        <el-option label="WARN" value="warn" />
        <el-option label="ERROR" value="error" />
      </el-select>
      <span class="control-hint">维护操作只对平台管理员开放</span>
    </div>
    <template v-if="snapshot">
      <div class="snapshot-strip">
        <span><strong>能力 revision</strong><b>{{ snapshot.revision }}</b></span>
        <span><strong>维护状态</strong><el-tag size="small" effect="plain">{{ snapshot.snapshot.maintenance_state }}</el-tag></span>
        <span><strong>上报时间</strong><b class="mono">{{ formatTime(snapshot.reported_at) }}</b></span>
        <span><strong>插件库存</strong><b>{{ snapshot.snapshot.plugin_inventory.length }}</b></span>
      </div>

      <div class="diagnostics-grid">
        <section class="diagnostic-section">
          <div class="section-title">插件可用性</div>
          <el-table :data="snapshot.snapshot.plugin_inventory" size="small" row-key="archive_sha256">
            <el-table-column label="插件" min-width="210">
              <template #default="{ row }"><span class="mono">{{ row.plugin_id }}</span></template>
            </el-table-column>
            <el-table-column prop="version" label="版本" width="100" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag :type="availabilityType(row.availability)" size="small">{{ row.availability }}</el-tag></template>
            </el-table-column>
            <el-table-column label="原因" min-width="180">
              <template #default="{ row }">
                <span v-if="row.unavailable_reasons.length" class="reason-list">{{ row.unavailable_reasons.join(", ") }}</span>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="snapshot.snapshot.plugin_inventory.length === 0" description="暂无插件库存" :image-size="50" />
        </section>

        <section class="diagnostic-section compact-section">
          <div class="section-title">能力分区</div>
          <div class="metric-list">
            <div><span>Executor</span><strong>{{ snapshot.snapshot.executors.length }}</strong></div>
            <div><span>Runtime</span><strong>{{ snapshot.snapshot.runtimes.length }}</strong></div>
            <div><span>Software</span><strong>{{ snapshot.snapshot.software.length }}</strong></div>
            <div><span>Resource</span><strong>{{ snapshot.snapshot.resources.length }}</strong></div>
          </div>
          <div v-if="snapshot.snapshot.runtimes.length" class="value-list">
            <div v-for="item in snapshot.snapshot.runtimes" :key="item.runtime_id"><span>{{ item.runtime_type }}</span><b class="mono">{{ item.version }}</b></div>
          </div>
          <div v-if="snapshot.snapshot.software.length" class="value-list">
            <div v-for="item in snapshot.snapshot.software" :key="`${item.provider_id}:${item.name}`"><span>{{ item.name }}</span><b class="mono">{{ item.version }}</b></div>
          </div>
        </section>
      </div>
    </template>

    <template v-if="diagnostics">
      <div class="diagnostics-grid lower-grid">
        <section class="diagnostic-section">
          <div class="section-title">系统</div>
          <div class="system-grid">
            <div><span>主机</span><b>{{ diagnostics.snapshot.system.hostname }}</b></div>
            <div><span>操作系统</span><b>{{ diagnostics.snapshot.system.os_name }} {{ diagnostics.snapshot.system.os_version }}</b></div>
            <div><span>Python</span><b class="mono">{{ diagnostics.snapshot.system.python_version }}</b></div>
            <div><span>CPU</span><b>{{ diagnostics.snapshot.system.cpu_cores }} 核</b></div>
            <div><span>内存</span><b>{{ formatMemory(diagnostics.snapshot.system.memory_available_mb) }} / {{ formatMemory(diagnostics.snapshot.system.memory_total_mb) }}</b></div>
            <div><span>磁盘剩余</span><b>{{ formatMemory(diagnostics.snapshot.system.disk_free_mb) }}</b></div>
          </div>
        </section>
        <section class="diagnostic-section">
          <div class="section-title">通信与活动任务</div>
          <div class="connection-line">
            <el-tag :type="diagnostics.snapshot.mqtt.connected ? 'success' : 'danger'" size="small">{{ diagnostics.snapshot.mqtt.connected ? 'MQTT 在线' : 'MQTT 离线' }}</el-tag>
            <span class="mono">{{ diagnostics.snapshot.mqtt.broker_endpoint }}</span>
            <span>重连 {{ diagnostics.snapshot.mqtt.reconnect_count }} 次</span>
          </div>
          <div class="active-attempts">
            <div v-for="attempt in diagnostics.snapshot.active_attempts" :key="attempt.attempt_id"><span class="mono">{{ attempt.attempt_id }}</span><el-tag size="small" effect="plain">{{ attempt.state }}</el-tag></div>
            <span v-if="diagnostics.snapshot.active_attempts.length === 0" class="muted">无活动 Attempt</span>
          </div>
        </section>
      </div>
    </template>
    <section v-if="isRegisteredNode" class="maintenance-section">
      <div class="section-head">
        <div class="section-title">最近维护操作</div>
        <el-button link size="small" :loading="operationsQuery.isFetching.value" @click="operationsQuery.refetch()">刷新</el-button>
      </div>
      <el-table v-if="operations.length" :data="operations" size="small" row-key="operation_id">
        <el-table-column label="类型" width="110"><template #default="{ row }">{{ operationLabel(row.kind) }}</template></el-table-column>
        <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="operationType(row.status)" size="small">{{ row.status }}</el-tag></template></el-table-column>
        <el-table-column label="操作 ID" min-width="210"><template #default="{ row }"><span class="mono">{{ row.operation_id }}</span></template></el-table-column>
        <el-table-column label="结果" min-width="180"><template #default="{ row }"><span :class="row.error_code ? 'error-text' : 'muted'">{{ row.error_code || row.message || '-' }}</span></template></el-table-column>
      </el-table>
      <span v-else class="muted">暂无维护操作</span>
    </section>
    <section v-if="isRegisteredNode" class="maintenance-section logs-section">
      <div class="section-head">
        <div class="section-title">Agent 结构化日志 <span class="live-indicator">LIVE</span></div>
        <el-button link size="small" :loading="logsQuery.isFetching.value" @click="logsQuery.refetch()">刷新</el-button>
      </div>
      <el-table v-if="logRows.length" :data="logRows" size="small" height="260" row-key="event_id">
        <el-table-column label="时间" width="155"><template #default="{ row }"><span class="mono">{{ formatTime(row.occurred_at) }}</span></template></el-table-column>
        <el-table-column label="级别" width="78"><template #default="{ row }"><el-tag :type="logType(row.level)" size="small">{{ row.level }}</el-tag></template></el-table-column>
        <el-table-column label="组件" width="160"><template #default="{ row }"><span class="mono">{{ row.component }}</span></template></el-table-column>
        <el-table-column label="事件" width="190"><template #default="{ row }"><span class="mono">{{ row.event_code }}</span></template></el-table-column>
        <el-table-column label="消息" min-width="220" show-overflow-tooltip><template #default="{ row }">{{ row.message }}</template></el-table-column>
      </el-table>
      <span v-else class="muted">暂无 Agent 日志</span>
    </section>
    <el-dialog v-model="syncVisible" title="同步插件到节点" width="520px">
      <el-form label-position="top">
        <el-form-item label="插件版本">
          <el-select v-model="selectedPluginKey" filterable style="width: 100%" placeholder="选择已治理的插件版本">
            <el-option v-for="plugin in syncablePlugins" :key="`${plugin.plugin_id}:${plugin.version}`" :label="`${plugin.plugin_id} @ ${plugin.version}`" :value="`${plugin.plugin_id}:${plugin.version}`" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="syncVisible = false">取消</el-button>
        <el-button type="primary" :loading="syncing" :disabled="!selectedPlugin" @click="syncPlugin">下发同步</el-button>
      </template>
    </el-dialog>
    <el-empty v-if="isRegisteredNode && !loading && !snapshot && !diagnostics" description="暂无诊断快照" :image-size="50" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Refresh, Setting, SwitchButton, Upload } from "@element-plus/icons-vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { connectAgentLogs } from "@/api/sse";
import { aetpApi, type CapabilitySnapshotView, type DiagnosticsSnapshotView, type LogEvent, type PluginAvailability, type PluginVersion, type RemoteOperation } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";

const props = defineProps<{ nodeId: string }>();
const auth = useAuthStore();
const queryClient = useQueryClient();
const isRegisteredNode = computed(() => /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(props.nodeId));
const isAdmin = computed(() => auth.user?.platform_role === "admin");
const capabilityQuery = useQuery({
  queryKey: computed(() => ["node-capability", props.nodeId]),
  queryFn: () => aetpApi.assets.capabilitySnapshot(props.nodeId),
  enabled: isRegisteredNode,
});
const diagnosticsQuery = useQuery({
  queryKey: computed(() => ["node-diagnostics", props.nodeId]),
  queryFn: () => aetpApi.assets.diagnostics(props.nodeId),
  enabled: isRegisteredNode,
});
const snapshot = computed<CapabilitySnapshotView | null>(() => capabilityQuery.data.value ?? null);
const diagnostics = computed<DiagnosticsSnapshotView | null>(() => diagnosticsQuery.data.value ?? null);
const loading = computed(() => capabilityQuery.isLoading.value || diagnosticsQuery.isLoading.value);
const errorMessage = computed(() => {
  const error = capabilityQuery.error.value || diagnosticsQuery.error.value;
  return error instanceof Error ? error.message : "";
});
const collecting = ref(false);
const actionLoading = ref(false);
const syncing = ref(false);
const syncVisible = ref(false);
const selectedPluginKey = ref("");
const logComponent = ref("agent.runtime");
const logLevel = ref<"debug" | "info" | "warn" | "error">("info");
const liveLogs = ref<LogEvent[]>([]);
const logsQuery = useQuery({
  queryKey: computed(() => ["node-logs", props.nodeId]),
  queryFn: () => aetpApi.assets.logs(props.nodeId, { limit: 100 }),
  enabled: isRegisteredNode,
});
const operationsQuery = useQuery({
  queryKey: computed(() => ["node-maintenance", props.nodeId]),
  queryFn: () => aetpApi.assets.maintenanceOperations(props.nodeId),
  enabled: isRegisteredNode,
  refetchInterval: 5000,
});
const operations = computed<RemoteOperation[]>(() => operationsQuery.data.value ?? []);
const pluginVersionsQuery = useQuery({
  queryKey: ["plugin-versions"],
  queryFn: () => aetpApi.plugins.list(),
  enabled: isAdmin,
});
const syncablePlugins = computed<PluginVersion[]>(() => (pluginVersionsQuery.data.value ?? []).filter((plugin) => ["verified", "installed", "enabled", "disabled", "pending_restart"].includes(plugin.status)));
const selectedPlugin = computed(() => syncablePlugins.value.find((plugin) => `${plugin.plugin_id}:${plugin.version}` === selectedPluginKey.value) ?? null);
const logRows = computed<LogEvent[]>(() => {
  const unique = new Map<string, LogEvent>();
  for (const item of logsQuery.data.value ?? []) unique.set(item.event.event_id, item.event);
  for (const item of liveLogs.value) unique.set(item.event_id, item);
  return [...unique.values()].sort((a, b) => b.sequence - a.sequence).slice(0, 100);
});
let disconnectLogs = () => {};

onMounted(() => {
  if (!isRegisteredNode.value) return;
  disconnectLogs = connectAgentLogs(props.nodeId, (event) => {
    if (event.type !== "agent.log") return;
    const log = event.data as unknown as LogEvent;
    if (!log.event_id || !log.message) return;
    liveLogs.value = [log, ...liveLogs.value.filter((item) => item.event_id !== log.event_id)].slice(0, 100);
  });
});
onUnmounted(() => disconnectLogs());

async function collect() {
  collecting.value = true;
  try {
    await aetpApi.assets.collectDiagnostics(props.nodeId);
    ElMessage.success("诊断请求已下发");
    await queryClient.invalidateQueries({ queryKey: ["node-maintenance", props.nodeId] });
    await queryClient.invalidateQueries({ queryKey: ["node-diagnostics", props.nodeId] });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "诊断请求失败");
  } finally {
    collecting.value = false;
  }
}

async function updateLogLevel() {
  actionLoading.value = true;
  try {
    await aetpApi.assets.setLogLevel(props.nodeId, { component: logComponent.value, level: logLevel.value });
    ElMessage.success("日志级别更新请求已下发");
    await operationsQuery.refetch();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "日志级别更新失败");
  } finally {
    actionLoading.value = false;
  }
}

async function drain() {
  actionLoading.value = true;
  try {
    await aetpApi.assets.drain(props.nodeId, { drain_timeout_s: 1800, reason: "Web 运维操作" });
    ElMessage.success("排空请求已下发");
    await operationsQuery.refetch();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "排空请求失败");
  } finally {
    actionLoading.value = false;
  }
}

async function restart() {
  try {
    await ElMessageBox.confirm("Agent 将等待活动执行结束后重启，确定继续？", "重启 Agent", { type: "warning" });
  } catch {
    return;
  }
  actionLoading.value = true;
  try {
    await aetpApi.assets.restart(props.nodeId, { drain_timeout_s: 1800, reason: "Web 运维操作" });
    ElMessage.success("重启请求已下发");
    await operationsQuery.refetch();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "重启请求失败");
  } finally {
    actionLoading.value = false;
  }
}

async function syncPlugin() {
  const plugin = selectedPlugin.value;
  if (!plugin) return;
  syncing.value = true;
  try {
    await aetpApi.assets.pluginSync(props.nodeId, {
      items: [{
        plugin_id: plugin.plugin_id,
        point: plugin.point,
        version: plugin.version,
        action: "install",
        package: {
          plugin_id: plugin.plugin_id,
          version: plugin.version,
          archive_sha256: plugin.archive_sha256,
        },
      }],
      drain_timeout_s: 1800,
      restart_after: true,
    });
    ElMessage.success("插件同步请求已下发");
    syncVisible.value = false;
    selectedPluginKey.value = "";
    await operationsQuery.refetch();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "插件同步失败");
  } finally {
    syncing.value = false;
  }
}

function availabilityType(availability: PluginAvailability) {
  if (availability === "available") return "success";
  if (availability === "error") return "danger";
  if (availability === "blocked") return "warning";
  return "info";
}
function operationLabel(kind: RemoteOperation["kind"]) {
  return { diagnostics: "诊断", plugin_sync: "插件同步", log_level: "日志级别", drain: "排空", restart: "重启" }[kind];
}
function operationType(status: RemoteOperation["status"]) {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "cancelled") return "danger";
  if (status === "running") return "warning";
  return "info";
}
function logType(level: LogEvent["level"]) {
  if (level === "error") return "danger";
  if (level === "warn") return "warning";
  if (level === "info") return "success";
  return "info";
}
function formatTime(value: string) { return new Date(value).toLocaleString(); }
function formatMemory(mb: number) { return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`; }
</script>

<style scoped>
.diagnostics-panel { padding: 14px 28px 18px 56px; background: #fbfcfd; border-top: 1px solid #edf1f4; }
.snapshot-strip { display: flex; flex-wrap: wrap; gap: 22px; padding: 2px 0 14px; color: #71808b; font-size: 12px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 10px; }
.panel-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.panel-label { color: #42566a; font-size: 12px; font-weight: 700; }
.maintenance-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 14px; padding: 9px 10px; border: 1px solid #e1e9ed; background: #f7fafb; color: #6f7e87; font-size: 11px; }
.control-label { color: #42566a; font-weight: 700; }
.component-input { width: 190px; }
.level-select { width: 105px; }
.control-hint { color: #96a3aa; }
.snapshot-strip span { display: inline-flex; align-items: center; gap: 7px; }
.snapshot-strip strong { color: #52616b; font-weight: 650; }
.snapshot-strip b { color: #263843; font-weight: 700; }
.diagnostics-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(260px, 1fr); gap: 14px; }
.lower-grid { margin-top: 14px; }
.diagnostic-section { min-width: 0; padding: 12px 14px; border: 1px solid #e7edf1; border-radius: 6px; background: #fff; }
.section-title { margin-bottom: 10px; color: #42566a; font-size: 12px; font-weight: 700; }
.reason-list { color: #a26524; font-size: 11px; }
.muted { color: #9aa7ae; }
.mono { font-family: ui-monospace, monospace; font-size: 11px; }
.metric-list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.metric-list div, .value-list div, .system-grid div { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.metric-list div { padding: 8px 9px; background: #f7f9fb; border-radius: 4px; color: #77858d; font-size: 11px; }
.metric-list strong { color: #263843; font-size: 15px; }
.value-list { display: grid; gap: 6px; margin-top: 12px; color: #697983; font-size: 11px; }
.value-list b { color: #263843; }
.system-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 16px; color: #71808b; font-size: 11px; }
.system-grid b { color: #263843; text-align: right; }
.connection-line { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; color: #71808b; font-size: 11px; }
.active-attempts { display: grid; gap: 7px; margin-top: 13px; }
.active-attempts div { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.maintenance-section { margin-top: 14px; padding: 12px 14px; border: 1px solid #e7edf1; border-radius: 6px; background: #fff; }
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.section-head .section-title { margin-bottom: 0; }
.live-indicator { display: inline-flex; align-items: center; gap: 4px; margin-left: 7px; color: #2f9b70; font-size: 9px; letter-spacing: .08em; }
.live-indicator::before { width: 5px; height: 5px; border-radius: 50%; background: #3ab47e; content: ""; }
.error-text { color: #bc4c4c; }
.logs-section :deep(.el-table__body-wrapper) { min-height: 58px; }
@media (max-width: 900px) { .diagnostics-panel { padding-left: 20px; padding-right: 20px; }.diagnostics-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .snapshot-strip { gap: 10px 16px; }.system-grid { grid-template-columns: 1fr; }.system-grid b { text-align: left; }.panel-head { align-items: flex-start; flex-direction: column; }.panel-actions { justify-content: flex-start; }.component-input { width: 100%; }.level-select { width: 100%; } }
</style>
