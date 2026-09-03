<template>
  <div class="page">
    <header class="page-heading"><div><span class="eyebrow">PLUGIN GOVERNANCE</span><h1>插件中心</h1><p>管理已验证的插件归档、版本和扩展点。</p></div><div class="heading-actions"><input ref="fileInput" type="file" accept=".zip" hidden @change="upload" /><el-button v-if="isAdmin" type="primary" :icon="Upload" :loading="uploading" @click="fileInput?.click()">上传归档</el-button><el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button></div></header>
    <el-alert v-if="errorMessage" title="插件清单加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />
    <el-card v-loading="loading" shadow="never" class="content-card"><template #header><div class="card-heading"><div><strong>插件版本</strong><span>Manifest · SHA-256 · lifecycle</span></div><el-tag effect="plain">{{ plugins.length }} 个版本</el-tag></div></template><el-table :data="plugins" row-key="plugin_id"><el-table-column label="插件" min-width="270"><template #default="{ row }"><div class="plugin-cell"><span class="plugin-mark"><el-icon><Grid /></el-icon></span><div><strong>{{ row.manifest.display_name }}</strong><small class="mono">{{ row.plugin_id }}</small></div></div></template></el-table-column><el-table-column prop="version" label="版本" width="120" /><el-table-column prop="point" label="扩展点" width="130" /><el-table-column label="说明" min-width="260"><template #default="{ row }"><span v-if="row.manifest.description" class="plugin-desc" :title="row.manifest.description">{{ row.manifest.description }}</span><span v-else class="muted">—</span></template></el-table-column><el-table-column label="SHA-256" min-width="230"><template #default="{ row }"><span class="mono hash">{{ row.archive_sha256 }}</span></template></el-table-column><el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="statusType(row.status)" effect="light">{{ statusLabel(row.status) }}</el-tag></template></el-table-column><el-table-column label="操作" min-width="300"><template #default="{ row }"><el-button v-if="row.status === 'verified'" link type="success" @click="install(row)">安装</el-button><el-button v-if="['installed','disabled','enabled'].includes(row.status)" link type="primary" @click="toggle(row)">{{ row.status === 'enabled' ? '停用' : '启用' }}</el-button><el-button v-if="['installed','disabled'].includes(row.status)" link type="warning" @click="rollback(row)">回滚</el-button><el-button v-if="['disabled','error'].includes(row.status)" link type="danger" @click="remove(row)">移除</el-button></template></el-table-column></el-table><el-empty v-if="!loading && plugins.length === 0" description="尚未登记插件" /></el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Grid, Refresh, Upload } from "@element-plus/icons-vue";
import { aetpApi, type PluginVersion } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const queryClient = useQueryClient();
const isAdmin = computed(() => auth.user?.platform_role === "admin");
const fileInput = ref<HTMLInputElement>();
const query = useQuery({ queryKey: ["plugins", "governance"], queryFn: () => aetpApi.plugins.list() });
const plugins = computed(() => query.data.value ?? []);
const loading = computed(() => query.isLoading.value || query.isFetching.value);
const errorMessage = computed(() => query.error.value?.message || "");
const uploading = ref(false);
function refresh() { queryClient.invalidateQueries({ queryKey: ["plugins"] }); }
async function upload(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; uploading.value = true; try { await aetpApi.plugins.upload(file); ElMessage.success("插件归档已登记"); refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "上传失败"); } finally { uploading.value = false; (event.target as HTMLInputElement).value = ""; } }
function statusLabel(status: PluginVersion["status"]) { return ({ uploaded: "待校验", verified: "已校验", installed: "已安装", pending_restart: "待重启", enabled: "已启用", disabled: "已停用", removed: "已移除", error: "错误" } as Record<string, string>)[status] || status; }
function statusType(status: PluginVersion["status"]) { return status === "enabled" ? "success" : status === "error" ? "danger" : "info"; }
async function install(row: PluginVersion) { try { await aetpApi.plugins.install(row.plugin_id, row.version); ElMessage.success("插件已安装"); refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "安装失败"); } }
async function toggle(row: PluginVersion) { try { if (row.status === "enabled") await aetpApi.plugins.disable(row.plugin_id, row.version); else await aetpApi.plugins.enable(row.plugin_id, row.version); ElMessage.success("状态已更新，重启后生效"); refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "操作失败"); } }
async function rollback(row: PluginVersion) { try { await aetpApi.plugins.rollback(row.plugin_id, row.version); ElMessage.success("活动版本已切换"); refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "回滚失败"); } }
async function remove(row: PluginVersion) { try { await ElMessageBox.confirm(`确认移除 ${row.plugin_id}@${row.version}？`, "移除插件", { type: "warning" }); await aetpApi.plugins.remove(row.plugin_id, row.version); ElMessage.success("插件版本已移除"); refresh(); } catch (error) { if (error instanceof Error && error.message !== "cancel") ElMessage.error(error.message); } }
</script>

<style scoped>
.page { max-width:1480px; margin:0 auto; }.page-heading { display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:22px; }.eyebrow { color:var(--aetp-cyan); font-size:10px; font-weight:800; letter-spacing:.16em; }.page-heading h1 { margin:8px 0 6px; color:var(--aetp-ink); font-size:28px; }.page-heading p { margin:0; color:var(--aetp-muted); font-size:13px; }.heading-actions { display:flex; gap:9px; }.page-alert { margin-top:14px; }.content-card :deep(.el-card__body) { padding-top:0; }.card-heading { display:flex; align-items:center; justify-content:space-between; }.card-heading div { display:flex; align-items:baseline; gap:10px; }.card-heading strong { font-size:15px; }.card-heading span { color:var(--aetp-muted); font-size:11px; }.plugin-cell { display:flex; align-items:center; gap:10px; }.plugin-mark { display:grid; width:34px; height:34px; place-items:center; border-radius:7px; background:#eaf3ff; color:var(--aetp-blue); }.plugin-cell div { display:flex; flex-direction:column; gap:3px; }.mono { font:12px ui-monospace,monospace; }.plugin-cell small { color:var(--aetp-muted); }.hash { word-break:break-all; }
.plugin-desc { display:block; color:var(--aetp-muted); font-size:12px; line-height:1.5; max-height:3em; overflow:hidden; }
.muted { color:var(--aetp-muted); }
@media (max-width:760px) { .page-heading { align-items:flex-start; flex-direction:column; gap:14px; } }
</style>
