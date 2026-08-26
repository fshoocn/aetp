<template>
  <div class="members-page">
    <div class="page-heading"><div><span class="eyebrow">PROJECT ACCESS / RBAC</span><h1>成员与权限</h1><p>{{ projectStore.currentProject?.name || "当前项目" }} 的成员角色与访问边界。</p></div><el-button v-if="canManage" type="primary" :icon="Plus" @click="openAdd">添加成员</el-button></div>
    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部项目选择器选择一个项目。" type="info" show-icon :closable="false" />
    <el-alert v-else-if="!canManage" title="只读视图" description="你可以查看项目上下文，但只有 maintainer、owner 或平台管理员可以管理成员。" type="info" show-icon :closable="false" />
    <el-card v-if="projectId" v-loading="loading" shadow="never"><template #header><div class="card-heading"><div><strong>项目成员</strong><span>{{ members.length }} 人</span></div><el-button text :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button></div></template>
      <el-table :data="members" row-key="user_id"><el-table-column label="成员" min-width="240"><template #default="{ row }"><div class="member-cell"><el-avatar :size="34">{{ row.display_name.slice(0, 1).toUpperCase() }}</el-avatar><div><strong>{{ row.display_name }}</strong><small>{{ row.username }}</small></div></div></template></el-table-column><el-table-column label="项目角色" width="170"><template #default="{ row }"><el-tag :type="roleTag(row.project_role)" effect="light">{{ roleText(row.project_role) }}</el-tag></template></el-table-column><el-table-column label="加入时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column><el-table-column v-if="canManage" label="操作" width="220"><template #default="{ row }"><el-select size="small" :model-value="row.project_role" style="width: 130px" @change="changeRole(row, $event)"><el-option v-for="item in roleOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select><el-button link type="danger" size="small" @click="remove(row)">移除</el-button></template></el-table-column></el-table><el-empty v-if="!loading && members.length === 0" description="暂无成员" /></el-card>

    <el-dialog v-model="addVisible" title="添加项目成员" width="470px" destroy-on-close><el-alert title="角色边界" description="maintainer 只能授予低于自身的角色；owner 可以授予 owner。" type="info" show-icon :closable="false" class="dialog-alert" /><el-form ref="formRef" :model="form" :rules="rules" label-position="top"><el-form-item label="用户" prop="userId"><el-select v-if="auth.user?.platform_role === 'admin' && adminUsers.length" v-model="form.userId" filterable placeholder="选择账户" style="width: 100%"><el-option v-for="user in selectableUsers" :key="user.id" :label="`${user.display_name} · ${user.username}`" :value="user.id" /></el-select><el-input-number v-else v-model="form.userId" :min="1" controls-position="right" style="width: 100%" placeholder="输入用户 ID" /></el-form-item><el-form-item label="项目角色" prop="role"><el-select v-model="form.role" style="width: 100%"><el-option v-for="item in roleOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item></el-form><template #footer><el-button @click="addVisible = false">取消</el-button><el-button type="primary" :loading="adding" @click="add">添加成员</el-button></template></el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type { FormInstance, FormRules } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Plus, Refresh } from "@element-plus/icons-vue";
import { aetpApi, type AdminUser, type ProjectMember } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const auth = useAuthStore();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
const projectId = computed(() => projectStore.currentProjectId ?? "");
const canManage = computed(() => auth.user?.platform_role === "admin" || ["maintainer", "owner"].includes(projectStore.currentRole || ""));
const query = useQuery({ queryKey: ["project", "members", projectId], queryFn: () => aetpApi.projects.members(projectId.value), enabled: computed(() => !!projectId.value) });
const adminUsersQuery = useQuery({ queryKey: ["admin", "users", "member-candidates"], queryFn: () => aetpApi.admin.users(), enabled: computed(() => auth.user?.platform_role === "admin") });
const members = computed(() => query.data.value ?? []);
const adminUsers = computed(() => adminUsersQuery.data.value ?? []);
const selectableUsers = computed(() => adminUsers.value.filter((user) => user.account_status === "active" && !members.value.some((member) => member.user_id === user.id)));
const loading = computed(() => query.isLoading.value || adminUsersQuery.isLoading.value);
const addVisible = ref(false);
const adding = ref(false);
const formRef = ref<FormInstance>();
const form = reactive<{ userId: number | undefined; role: ProjectMember["project_role"] }>({ userId: undefined, role: "viewer" });
const roleOptions = [{ label: "查看者", value: "viewer" as const }, { label: "操作员", value: "operator" as const }, { label: "维护者", value: "maintainer" as const }, { label: "负责人", value: "owner" as const }];
const rules: FormRules = { userId: [{ required: true, message: "请选择或输入用户", trigger: "change" }], role: [{ required: true, message: "请选择角色", trigger: "change" }] };
function refresh() { queryClient.invalidateQueries({ queryKey: ["project", "members", projectId.value] }); }
function openAdd() { form.userId = undefined; form.role = "viewer"; addVisible.value = true; }
async function add() { if (!formRef.value || !form.userId) return; const valid = await formRef.value.validate().catch(() => false); if (!valid) return; adding.value = true; try { await aetpApi.projects.addMember(projectId.value, form.userId, form.role); ElMessage.success("成员已添加"); addVisible.value = false; refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "添加失败"); } finally { adding.value = false; } }
async function changeRole(row: ProjectMember, role: ProjectMember["project_role"]) { try { await aetpApi.projects.updateMember(projectId.value, row.user_id, role); ElMessage.success("角色已更新"); refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "更新失败"); refresh(); } }
async function remove(row: ProjectMember) { try { await ElMessageBox.confirm(`确定移除 ${row.display_name}？`, "确认移除", { type: "warning" }); await aetpApi.projects.removeMember(projectId.value, row.user_id); ElMessage.success("成员已移除"); refresh(); } catch { /* 用户取消 */ } }
function roleText(role: string) { return ({ viewer: "查看者", operator: "操作员", maintainer: "维护者", owner: "项目负责人" } as Record<string, string>)[role] || role; }
function roleTag(role: string) { return ({ viewer: "info", operator: "success", maintainer: "warning", owner: "danger" } as Record<string, "info" | "success" | "warning" | "danger">)[role] || "info"; }
function fmt(value: string) { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }
</script>

<style scoped>
.members-page { max-width: 1480px; margin: 0 auto; }.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }.card-heading { display: flex; align-items: center; justify-content: space-between; }.card-heading div { display: flex; align-items: baseline; gap: 10px; }.card-heading strong { font-size: 15px; }.card-heading span { color: var(--aetp-muted); font-size: 11px; }.member-cell { display: flex; align-items: center; gap: 10px; }.member-cell div { display: flex; flex-direction: column; gap: 3px; }.member-cell small { color: var(--aetp-muted); font-family: ui-monospace, monospace; font-size: 11px; }.dialog-alert { margin-bottom: 18px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; } }
</style>
