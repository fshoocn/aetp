<template>
  <div class="plugins-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">TASK TYPES / PLUGINS</span>
        <h1>插件中心</h1>
        <p>查看 Master 已加载的任务类型插件及其 Agent 执行能力。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
    </div>
    <el-alert
      v-if="errorMessage"
      title="插件清单加载失败"
      :description="errorMessage"
      type="error"
      show-icon
      :closable="false"
    />
    <el-card v-loading="loading" shadow="never"
      ><template #header
        ><div class="card-head">
          <strong>已加载任务类型</strong>
          <div>
            <input
              ref="fileInput"
              type="file"
              accept=".zip,application/zip"
              hidden
              @change="upload"
            /><el-button
              v-if="isAdmin"
              type="primary"
              :loading="uploading"
              @click="fileInput?.click()"
              >上传 ZIP 插件</el-button
            ><el-button :icon="Refresh" @click="refresh">刷新</el-button>
          </div>
        </div></template
      ><el-alert
        v-if="isAdmin"
        class="upload-hint"
        title="V2 插件归档规范"
        description="归档必须通过 Manifest、入口路径和 SHA-256 校验。安装、启用和停用写入管理状态，重启后生效。"
        type="info"
        show-icon
        :closable="false" /><el-table :data="plugins" row-key="task_type"
        ><el-table-column label="任务类型" min-width="220"
          ><template #default="{ row }"
            ><div class="plugin-name">
              <span class="plugin-mark"
                ><el-icon><Grid /></el-icon
              ></span>
              <div>
                <strong>{{ row.display_name }}</strong
                ><small>{{ row.task_type }}</small>
              </div>
            </div></template
          ></el-table-column
        ><el-table-column label="版本" width="130"
          ><template #default="{ row }"
            ><el-tag effect="plain">{{ row.plugin_version }}</el-tag></template
          ></el-table-column
        ><el-table-column label="兼容版本" min-width="180"
          ><template #default="{ row }">{{
            row.supported_versions.join(", ")
          }}</template></el-table-column
        ><el-table-column label="Master" width="120"
          ><template #default
            ><el-tag type="success">已加载</el-tag></template
          ></el-table-column
        ><el-table-column label="Agent 执行面" width="150"
          ><template #default="{ row }"
            ><el-tag :type="row.agent_available ? 'success' : 'warning'">{{
              row.agent_available ? "可用" : "未声明"
            }}</el-tag></template
          ></el-table-column
        ><el-table-column label="操作" width="100"
          ><template #default="{ row }"
            ><el-button link type="primary" @click="showDetail(row)"
              >详情</el-button
            ></template
          ></el-table-column
        ></el-table
      ><el-empty
        v-if="!loading && plugins.length === 0"
        description="Master 当前未加载任务类型插件"
    /></el-card>
    <el-card v-if="isAdmin" class="managed-card" shadow="never"
      ><template #header><strong>插件包生命周期</strong></template
      ><el-table :data="managed" :row-key="(row: V2PluginVersion) => `${row.plugin_id}:${row.version}`"
        ><el-table-column
          prop="plugin_id"
          label="插件 ID"
          min-width="220"
        /><el-table-column
          prop="version"
          label="版本"
          width="130"
        /><el-table-column prop="point" label="扩展点" width="130" /><el-table-column
          prop="archive_sha256"
          label="归档 SHA-256"
          min-width="250"
        /><el-table-column label="状态" width="160"
          ><template #default="{ row }"
            ><el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag></template
          ></el-table-column
        ><el-table-column label="操作" min-width="340"
          ><template #default="{ row }"
            ><el-button
              v-if="row.status === 'verified'"
              link
              type="success"
              @click="install(row)"
              >安装</el-button
            ><el-button v-if="['installed', 'disabled', 'enabled'].includes(row.status)" link type="primary" @click="toggle(row)">{{
              row.status === "enabled" ? "停用" : "启用"
            }}</el-button
            ><el-button link type="danger" @click="remove(row)" :disabled="!['disabled', 'error'].includes(row.status)"
              >删除</el-button
            ></template
          ></el-table-column
        ></el-table
      ></el-card
    >
    <el-dialog
      v-model="detailVisible"
      :title="selected?.display_name || '插件详情'"
      width="680px"
      ><el-descriptions v-if="selected" :column="2" border
        ><el-descriptions-item label="任务类型">{{
          selected.task_type
        }}</el-descriptions-item
        ><el-descriptions-item label="当前版本">{{
          selected.plugin_version
        }}</el-descriptions-item
        ><el-descriptions-item label="兼容版本" :span="2">{{
          selected.supported_versions.join(", ")
        }}</el-descriptions-item
        ><el-descriptions-item label="Agent 包" :span="2">{{
          selected.agent_package
            ? `${selected.agent_package.package_name}@${selected.agent_package.version}`
            : "未配置可分发 Agent 包"
        }}</el-descriptions-item></el-descriptions
      >
      <h4>配置 Schema</h4>
      <pre>{{ pretty(selected?.config_schema) }}</pre>
      <h4>上传规格</h4>
      <pre>{{ pretty(selected?.upload_spec) }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Grid, Refresh } from "@element-plus/icons-vue";
import { aetpApi, type TaskTypePlugin, type V2PluginVersion } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
const qc = useQueryClient();
const auth = useAuthStore();
const isAdmin = computed(() => auth.user?.platform_role === "admin");
const fileInput = ref<HTMLInputElement>();
const query = useQuery({
  queryKey: ["plugins", "task-types"],
  queryFn: () => aetpApi.plugins.list(),
});
const managedQuery = useQuery({
  queryKey: ["plugins", "v2"],
  queryFn: () => aetpApi.plugins.v2List(),
  enabled: isAdmin,
});
const plugins = computed(() => query.data.value ?? []);
const managed = computed(() => managedQuery.data.value ?? []);
const loading = computed(() => query.isLoading.value || query.isFetching.value);
const errorMessage = computed(
  () => query.error.value?.message || managedQuery.error.value?.message || ""
);
const detailVisible = ref(false);
const selected = ref<TaskTypePlugin | null>(null);
const uploading = ref(false);
function refresh() {
  qc.invalidateQueries({ queryKey: ["plugins"] });
}
function showDetail(plugin: TaskTypePlugin) {
  selected.value = plugin;
  detailVisible.value = true;
}
function pretty(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}
async function upload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    ElMessage.error("请选择 ZIP 插件包");
    (event.target as HTMLInputElement).value = "";
    return;
  }
  uploading.value = true;
  try {
    await aetpApi.plugins.v2Upload(file);
    ElMessage.success("V2 插件包已上传并完成校验");
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    uploading.value = false;
    (event.target as HTMLInputElement).value = "";
  }
}
function statusLabel(status: V2PluginVersion["status"]) {
  return {
    uploaded: "待校验",
    verified: "已校验",
    installed: "已安装",
    pending_restart: "待重启",
    enabled: "已启用",
    disabled: "已停用",
    removed: "已移除",
    error: "错误",
  }[status];
}
function statusType(status: V2PluginVersion["status"]) {
  return status === "enabled" ? "success" : status === "error" ? "danger" : "info";
}
async function install(row: V2PluginVersion) {
  try {
    await aetpApi.plugins.v2Install(row.plugin_id, row.version);
    ElMessage.success("V2 插件已安装");
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
async function toggle(row: V2PluginVersion) {
  try {
    if (row.status === "enabled") {
      await aetpApi.plugins.v2Disable(row.plugin_id, row.version);
    } else {
      await aetpApi.plugins.v2Enable(row.plugin_id, row.version);
    }
    ElMessage.success("V2 插件状态已更新，重启后生效");
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
async function remove(row: V2PluginVersion) {
  try {
    await ElMessageBox.confirm(`确认删除 ${row.plugin_id}？`, "删除插件", {
      type: "warning",
    });
    await aetpApi.plugins.v2Remove(row.plugin_id, row.version);
    ElMessage.success("V2 插件已删除");
    refresh();
  } catch (e) {
    if ((e as Error).message !== "cancel") ElMessage.error((e as Error).message);
  }
}
</script>

<style scoped>
.plugins-page {
  max-width: 1480px;
  margin: 0 auto;
}
.page-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: 22px;
}
.eyebrow {
  color: var(--aetp-cyan);
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.16em;
}
.page-heading h1 {
  margin: 8px 0 6px;
  font-size: 28px;
}
.page-heading p {
  margin: 0;
  color: var(--aetp-muted);
  font-size: 13px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.upload-hint {
  margin-bottom: 14px;
}
.plugin-name {
  display: flex;
  align-items: center;
  gap: 10px;
}
.plugin-mark {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  border-radius: 7px;
  background: #eaf3ff;
  color: var(--aetp-blue);
}
.plugin-name div {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.plugin-name small {
  color: var(--aetp-muted);
  font:
    11px ui-monospace,
    monospace;
}
.managed-card {
  margin-top: 16px;
}
.installed {
  margin-left: 6px;
}
h4 {
  margin: 18px 0 8px;
}
pre {
  max-height: 180px;
  overflow: auto;
  padding: 12px;
  border-radius: 6px;
  background: #f5f7fa;
  font:
    12px ui-monospace,
    monospace;
  white-space: pre-wrap;
}
@media (max-width: 760px) {
  .page-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 14px;
  }
  .card-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 10px;
  }
}
</style>
