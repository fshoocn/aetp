<template>
  <div class="users-page">
    <div class="page-heading"><div><span class="eyebrow">PLATFORM CONTROL / ACCESS</span><h1>账户审核</h1><p>管理平台账户状态与全局角色。项目角色在项目成员页中维护。</p></div><el-button text :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button></div>
    <el-alert v-if="errorMessage" title="账户列表加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />
    <el-card v-loading="loading" shadow="never"><template #header><div class="card-heading"><strong>平台账户</strong><el-radio-group v-model="statusFilter" size="small"><el-radio-button value="">全部</el-radio-button><el-radio-button value="pending">待审核</el-radio-button><el-radio-button value="active">已激活</el-radio-button><el-radio-button value="disabled">已禁用</el-radio-button></el-radio-group></div></template>
      <el-table :data="users" row-key="id"><el-table-column label="用户" min-width="220"><template #default="{ row }"><div class="user-cell"><el-avatar :size="32">{{ row.display_name.slice(0, 1).toUpperCase() }}</el-avatar><div><strong>{{ row.display_name }}</strong><small>{{ row.username }}</small></div></div></template></el-table-column><el-table-column label="账户状态" width="140"><template #default="{ row }"><el-tag :type="accountStatusTag(row.account_status)" effect="light">{{ accountStatusText(row.account_status) }}</el-tag></template></el-table-column><el-table-column label="平台角色" width="150"><template #default="{ row }"><el-tag :type="row.platform_role === 'admin' ? 'warning' : 'info'" effect="plain">{{ row.platform_role === "admin" ? "平台管理员" : "普通用户" }}</el-tag></template></el-table-column><el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="180"><template #default="{ row }"><el-button v-if="row.account_status === 'pending'" type="primary" size="small" @click="approve(row)">通过审核</el-button><el-button v-else-if="row.account_status === 'active' && row.platform_role !== 'admin'" type="danger" plain size="small" @click="disable(row)">禁用</el-button><el-button v-else-if="row.account_status === 'disabled'" type="success" plain size="small" @click="enable(row)">重新启用</el-button></template></el-table-column></el-table><el-empty v-if="!loading && users.length === 0" description="没有匹配的账户" /></el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Refresh } from "@element-plus/icons-vue";
import { aetpApi, type AdminUser } from "@/api/endpoints";
import { accountStatusTag, accountStatusText } from "@/utils/statusMaps";
const statusFilter = ref<AdminUser["account_status"] | "">("");
const queryClient = useQueryClient();
const query = useQuery({ queryKey: ["admin", "users", statusFilter], queryFn: () => aetpApi.admin.users(statusFilter.value || undefined) });
const users = computed(() => query.data.value ?? []);
const loading = computed(() => query.isLoading.value || query.isFetching.value);
const errorMessage = computed(() => query.error.value?.message || "");
function refresh() { queryClient.invalidateQueries({ queryKey: ["admin", "users"] }); }
async function update(row: AdminUser, account_status: AdminUser["account_status"]) { try { await aetpApi.admin.updateUser(row.id, { account_status }); ElMessage.success("账户状态已更新"); refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "更新失败"); } }
async function approve(row: AdminUser) { await update(row, "active"); }
async function disable(row: AdminUser) { try { await ElMessageBox.confirm(`确定禁用 ${row.display_name}？`, "确认操作", { type: "warning" }); await update(row, "disabled"); } catch { /* 用户取消 */ } }
async function enable(row: AdminUser) { await update(row, "active"); }
function fmt(value: string) { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }
</script>

<style scoped>
.users-page { max-width: 1480px; margin: 0 auto; }.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }.page-alert { margin-bottom: 14px; }.card-heading { display: flex; align-items: center; justify-content: space-between; }.user-cell { display: flex; align-items: center; gap: 10px; }.user-cell div { display: flex; flex-direction: column; gap: 3px; }.user-cell small { color: var(--aetp-muted); font-family: ui-monospace, monospace; font-size: 11px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }.card-heading { align-items: flex-start; flex-direction: column; gap: 12px; } }
</style>
