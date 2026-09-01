<template>
  <div class="v2-diagnostics-panel" v-loading="loading">
    <el-alert
      v-if="!isV2Node"
      title="该节点尚未注册 V2 能力快照"
      description="节点使用 V2 会话注册后，能力、插件库存和诊断信息会显示在这里。"
      type="info"
      show-icon
      :closable="false"
    />
    <el-alert
      v-else-if="errorMessage"
      title="V2 诊断数据暂不可用"
      :description="errorMessage"
      type="warning"
      show-icon
      :closable="false"
    />
    <div v-if="isV2Node" class="panel-head">
      <div class="panel-label">V2 能力与诊断</div>
      <el-button size="small" :loading="collecting" @click="collect">立即采集诊断</el-button>
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
          <el-empty v-if="snapshot.snapshot.plugin_inventory.length === 0" description="暂无 V2 插件库存" :image-size="50" />
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
    <el-empty v-if="isV2Node && !loading && !snapshot && !diagnostics" description="暂无诊断快照" :image-size="50" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { aetpApi, type V2CapabilitySnapshotView, type V2DiagnosticsSnapshotView, type V2PluginAvailability } from "@/api/endpoints";

const props = defineProps<{ nodeId: string }>();
const queryClient = useQueryClient();
const isV2Node = computed(() => /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(props.nodeId));
const capabilityQuery = useQuery({
  queryKey: computed(() => ["v2-node-capability", props.nodeId]),
  queryFn: () => aetpApi.assets.v2CapabilitySnapshot(props.nodeId),
  enabled: isV2Node,
});
const diagnosticsQuery = useQuery({
  queryKey: computed(() => ["v2-node-diagnostics", props.nodeId]),
  queryFn: () => aetpApi.assets.v2Diagnostics(props.nodeId),
  enabled: isV2Node,
});
const snapshot = computed<V2CapabilitySnapshotView | null>(() => capabilityQuery.data.value ?? null);
const diagnostics = computed<V2DiagnosticsSnapshotView | null>(() => diagnosticsQuery.data.value ?? null);
const loading = computed(() => capabilityQuery.isLoading.value || diagnosticsQuery.isLoading.value);
const errorMessage = computed(() => {
  const error = capabilityQuery.error.value || diagnosticsQuery.error.value;
  return error instanceof Error ? error.message : "";
});
const collecting = ref(false);

async function collect() {
  collecting.value = true;
  try {
    await aetpApi.assets.v2CollectDiagnostics(props.nodeId);
    ElMessage.success("诊断请求已下发");
    await queryClient.invalidateQueries({ queryKey: ["v2-node-diagnostics", props.nodeId] });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "诊断请求失败");
  } finally {
    collecting.value = false;
  }
}

function availabilityType(availability: V2PluginAvailability) {
  if (availability === "available") return "success";
  if (availability === "error") return "danger";
  if (availability === "blocked") return "warning";
  return "info";
}
function formatTime(value: string) { return new Date(value).toLocaleString(); }
function formatMemory(mb: number) { return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`; }
</script>

<style scoped>
.v2-diagnostics-panel { padding: 14px 28px 18px 56px; background: #fbfcfd; border-top: 1px solid #edf1f4; }
.snapshot-strip { display: flex; flex-wrap: wrap; gap: 22px; padding: 2px 0 14px; color: #71808b; font-size: 12px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.panel-label { color: #42566a; font-size: 12px; font-weight: 700; }
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
@media (max-width: 900px) { .v2-diagnostics-panel { padding-left: 20px; padding-right: 20px; }.diagnostics-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .snapshot-strip { gap: 10px 16px; }.system-grid { grid-template-columns: 1fr; }.system-grid b { text-align: left; } }
</style>
