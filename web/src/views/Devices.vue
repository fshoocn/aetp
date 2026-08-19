<template>
  <div class="assets-page">
    <div class="page-heading"><div><span class="eyebrow">PLATFORM ASSETS / READ ONLY</span><h1>节点与设备</h1><p>全平台运行节点和其管理外设的实时快照。</p></div><el-button text :icon="Refresh" :loading="loading" @click="refresh">刷新资产</el-button></div>
    <el-alert v-if="errorMessage" title="资产加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />
    <el-row :gutter="14" class="summary-row">
      <el-col :xs="24" :sm="8"><el-card shadow="never"><el-statistic title="节点总数" :value="nodes.length"><template #prefix><el-icon class="blue"><Cpu /></el-icon></template></el-statistic></el-card></el-col>
      <el-col :xs="24" :sm="8"><el-card shadow="never"><el-statistic title="在线节点" :value="onlineNodes"><template #prefix><el-icon class="green"><Connection /></el-icon></template></el-statistic></el-card></el-col>
      <el-col :xs="24" :sm="8"><el-card shadow="never"><el-statistic title="外设数量" :value="deviceCount"><template #prefix><el-icon class="amber"><SetUp /></el-icon></template></el-statistic></el-card></el-col>
    </el-row>
    <el-card v-loading="loading" shadow="never" class="node-card">
      <template #header><div class="card-heading"><div><strong>执行节点</strong><span>Node → Device</span></div><el-checkbox v-model="onlyOnline">仅在线</el-checkbox></div></template>
      <el-table :data="visibleNodes" row-key="node_id" :default-expand-all="false">
        <el-table-column type="expand"><template #default="{ row }"><div class="device-list">
          <div class="device-list-head">该节点管理的外设 <el-tag size="small" effect="plain">{{ row.devices.length }} 台</el-tag></div>
          <el-table v-if="row.devices.length" :data="row.devices" size="small"><el-table-column prop="device_id" label="设备 ID" min-width="180" /><el-table-column prop="name" label="名称" min-width="160" /><el-table-column prop="status" label="状态" width="120" /><el-table-column label="在线" width="100"><template #default="{ row: device }"><el-tag :type="device.online ? 'success' : 'info'" size="small">{{ device.online ? "在线" : "离线" }}</el-tag></template></el-table-column></el-table>
          <el-empty v-else description="节点尚未上报外设" :image-size="58" />
          <div class="device-list-head cap-head">节点能力 <el-tag size="small" effect="plain" :type="hasCaps(row) ? 'success' : 'info'">{{ hasCaps(row) ? '已上报' : '无' }}</el-tag></div>
          <div v-if="hasCaps(row)" class="cap-grid">
            <div v-for="cap in capabilityList(row.capabilities)" :key="cap.key" class="cap-item">
              <span class="cap-key">{{ cap.key }}</span>
              <span class="cap-value">{{ cap.value }}</span>
            </div>
          </div>
          <div v-else class="cap-empty">节点未上报能力（Agent 启动自动扫描 system/language/serial，CAN 通道由台架侧实现）</div>
          <div v-if="Object.keys(row.plugin_versions || {}).length" class="device-list-head cap-head">执行插件版本</div>
          <div v-if="Object.keys(row.plugin_versions || {}).length" class="cap-grid">
            <div v-for="(version, taskType) in row.plugin_versions" :key="taskType" class="cap-item">
              <span class="cap-key">{{ taskType }}</span>
              <span class="cap-value mono">{{ version }}</span>
            </div>
          </div>
        </div></template></el-table-column>
        <el-table-column label="Node" min-width="230"><template #default="{ row }"><div class="node-cell"><span class="node-mark"><el-icon><Cpu /></el-icon></span><div><strong>{{ row.name || row.node_id }}</strong><small>{{ row.node_id }}</small></div></div></template></el-table-column>
        <el-table-column label="主机" min-width="170"><template #default="{ row }"><span class="mono">{{ row.hostname || "-" }}</span></template></el-table-column>
        <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="row.online ? 'success' : 'info'" effect="light">{{ row.online ? "在线" : "离线" }}</el-tag></template></el-table-column>
        <el-table-column label="启用" width="100"><template #default="{ row }"><el-switch v-model="row.enabled" disabled /></template></el-table-column>
        <el-table-column label="最近心跳" min-width="180"><template #default="{ row }">{{ row.last_seen_at ? fmt(row.last_seen_at) : "尚未上报" }}</template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && visibleNodes.length === 0" description="暂无节点资产" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Connection, Cpu, Refresh, SetUp } from "@element-plus/icons-vue";
import { aetpApi } from "@/api/endpoints";
import { useTaskEvents } from "@/composables/useTaskEvents";

const onlyOnline = ref(false);
const queryClient = useQueryClient();
useTaskEvents(queryClient);
const query = useQuery({ queryKey: ["assets", "nodes", onlyOnline], queryFn: () => aetpApi.assets.nodes(onlyOnline.value ? true : undefined, true), refetchInterval: 5000 });
const nodes = computed(() => query.data.value ?? []);
const visibleNodes = computed(() => nodes.value);
const loading = computed(() => query.isLoading.value || query.isFetching.value);
const errorMessage = computed(() => query.error.value?.message || "");
const onlineNodes = computed(() => nodes.value.filter((node) => node.online).length);
const deviceCount = computed(() => nodes.value.reduce((sum, node) => sum + node.devices.length, 0));
function refresh() { queryClient.invalidateQueries({ queryKey: ["assets", "nodes"] }); }
function fmt(value: string) { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }

// ---- 能力树展示 ----
interface CapabilityEntry { key: string; value: string; }
function hasCaps(row: { capabilities: Record<string, unknown> }): boolean {
  const caps = row.capabilities || {};
  return Object.keys(caps).length > 0 && Object.values(caps).some((v) => v != null);
}
function capabilityList(capsRaw: Record<string, unknown>): CapabilityEntry[] {
  const entries: CapabilityEntry[] = [];
  const caps = capsRaw || {};
  // vehicle：厂商 → 总线 → 通道
  const vehicle = caps.vehicle as { vendors?: Array<{ name: string; buses?: Array<{ bus_type: string; channels?: Array<{ name: string; enabled?: boolean }> }> }> } | null;
  if (vehicle?.vendors?.length) {
    for (const vendor of vehicle.vendors) {
      for (const bus of vendor.buses || []) {
        const enabled = (bus.channels || []).filter((c) => c.enabled !== false);
        entries.push({ key: `vehicle.${vendor.name}.${bus.bus_type}`, value: enabled.map((c) => c.name).join(", ") || "-" });
      }
    }
  }
  // language：运行时 + 版本
  const language = caps.language as { runtimes?: Array<{ name: string; version: string }> } | null;
  if (language?.runtimes?.length) {
    entries.push({ key: "language", value: language.runtimes.map((r) => `${r.name} ${r.version}`).join("、") });
  }
  // system：OS / 内存 / CPU
  const system = caps.system as { operating_system?: { name: string; version: string } | null; memory_mb?: number | null; cpu_cores?: number | null } | null;
  if (system) {
    if (system.operating_system) entries.push({ key: "system.os", value: `${system.operating_system.name} ${system.operating_system.version}` });
    if (system.memory_mb != null) entries.push({ key: "system.memory", value: `${system.memory_mb} MB` });
    if (system.cpu_cores != null) entries.push({ key: "system.cpu", value: `${system.cpu_cores} 核` });
  }
  // serial：功能 → 端口
  const serial = caps.serial as { ports?: Array<{ function: string; port: string; enabled?: boolean }> } | null;
  if (serial?.ports?.length) {
    entries.push({
      key: "serial",
      value: serial.ports
        .map((p) => `${p.function}=${p.port}${p.enabled === false ? "(禁用)" : ""}`)
        .join("、"),
    });
  }
  return entries;
}
</script>

<style scoped>
.assets-page { max-width: 1480px; margin: 0 auto; }.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }.page-alert { margin-bottom: 14px; }.summary-row { margin-bottom: 14px; }.summary-row :deep(.el-card) { height: 112px; }.summary-row :deep(.el-statistic__head) { color: var(--aetp-muted); font-size: 12px; }.summary-row :deep(.el-statistic__content) { margin-top: 8px; font-weight: 700; }.blue { color: var(--aetp-blue); }.green { color: #2f9d71; }.amber { color: var(--aetp-amber); }.card-heading { display: flex; align-items: center; justify-content: space-between; }.card-heading div { display: flex; align-items: baseline; gap: 10px; }.card-heading strong { font-size: 15px; }.card-heading span { color: var(--aetp-muted); font-size: 11px; }.node-cell { display: flex; align-items: center; gap: 10px; }.node-mark { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 7px; background: #eaf3ff; color: var(--aetp-blue); }.node-cell div { display: flex; flex-direction: column; gap: 3px; }.node-cell small { color: #96a3ac; font-family: ui-monospace, monospace; font-size: 11px; }.mono { font-family: ui-monospace, monospace; font-size: 12px; }.device-list { padding: 3px 28px 12px 56px; }.device-list-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; color: #596a78; font-size: 12px; font-weight: 650; }.cap-head { margin-top: 18px; }.cap-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 8px 14px; margin-bottom: 6px; }.cap-item { display: flex; align-items: center; gap: 10px; background: #f7f9fb; border: 1px solid #edf1f4; border-radius: 6px; padding: 7px 12px; }.cap-key { color: #42566a; font-size: 12px; font-weight: 650; white-space: nowrap; }.cap-value { color: #2c3e50; font-size: 12px; word-break: break-all; }.cap-empty { color: #96a3ac; font-size: 12px; padding: 6px 0 2px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }.page-heading h1 { font-size: 24px; }.device-list { padding: 3px 8px 12px; } }
</style>
