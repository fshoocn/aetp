<template>
  <div class="projects-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">WORKSPACE / PROJECTS</span>
        <h1>项目管理</h1>
        <p>项目是任务、成员与节点权限的共同边界。</p>
      </div>
      <el-button v-if="isAdmin" type="primary" :icon="Plus" @click="openCreate">新建项目</el-button>
    </div>

    <el-alert v-if="errorMessage" title="项目加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />
    <el-card v-loading="loading" shadow="never">
      <template #header>
        <div class="card-heading"><div><strong>可访问项目</strong><span>{{ projects.length }} 个项目</span></div><el-button text :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button></div>
      </template>
      <el-table :data="projects" row-key="project_id">
        <el-table-column label="项目" min-width="280">
          <template #default="{ row }"><div class="project-cell"><span class="project-code">{{ row.project_key.slice(0, 2) }}</span><div><strong>{{ row.name }}</strong><small>{{ row.project_key }} · {{ row.project_id }}</small></div></div></template>
        </el-table-column>
        <el-table-column label="访问级别" width="180"><template #default="{ row }"><el-tag :type="isAdmin ? 'danger' : roleTag(row.project_role)" effect="light">{{ isAdmin ? "平台管理员 / 全项目" : roleText(row.project_role) }}</el-tag></template></el-table-column>
        <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain">{{ row.status === "active" ? "运行中" : "已归档" }}</el-tag></template></el-table-column>
        <el-table-column label="说明" min-width="220"><template #default="{ row }"><span class="description">{{ row.description || "暂无项目说明" }}</span></template></el-table-column>
        <el-table-column label="操作" width="240"><template #default="{ row }"><el-button v-if="currentProjectId === row.project_id" type="success" plain size="small" disabled>当前项目</el-button><el-button v-else type="primary" plain size="small" @click="select(row)">进入项目</el-button><el-button v-if="isAdmin || ['maintainer', 'owner'].includes(row.project_role || '')" text type="primary" size="small" @click="openEdit(row)">编辑</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && projects.length === 0" description="暂无可访问项目" />
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑项目' : '新建项目'" width="520px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="项目标识" prop="project_key"><el-input v-model="form.project_key" :disabled="editing" placeholder="例如 ADAS_HIL" /></el-form-item>
        <el-form-item label="项目名称" prop="name"><el-input v-model="form.name" placeholder="输入项目名称" /></el-form-item>
        <el-form-item label="项目说明"><el-input v-model="form.description" type="textarea" :rows="4" placeholder="描述项目用途、范围或负责人" /></el-form-item>
        <el-form-item v-if="!editing && isAdmin" label="首个负责人"><el-input-number v-model="form.owner_id" :min="1" controls-position="right" style="width: 100%" placeholder="留空则由当前管理员负责" /></el-form-item>
        <el-form-item v-if="editing" label="项目状态"><el-radio-group v-model="form.status"><el-radio-button value="active">运行中</el-radio-button><el-radio-button value="archived">已归档</el-radio-button></el-radio-group></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import type { FormInstance, FormRules } from "element-plus";
import { ElMessage } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Plus, Refresh } from "@element-plus/icons-vue";
import { aetpApi, type Project } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const auth = useAuthStore();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
const isAdmin = computed(() => auth.user?.platform_role === "admin");
const currentProjectId = computed(() => projectStore.currentProjectId);
const query = useQuery({ queryKey: ["projects", "management"], queryFn: () => aetpApi.projects.list() });
const projects = computed(() => query.data.value ?? []);
const loading = computed(() => query.isLoading.value || query.isFetching.value);
const errorMessage = computed(() => query.error.value?.message || "");
const dialogVisible = ref(false);
const editing = ref(false);
const saving = ref(false);
const formRef = ref<FormInstance>();
const editingId = ref<string | null>(null);
const form = reactive({ project_key: "", name: "", description: "", owner_id: undefined as number | undefined, status: "active" as "active" | "archived" });
const rules: FormRules = { project_key: [{ required: true, message: "请输入项目标识", trigger: "blur" }, { pattern: /^[A-Za-z][A-Za-z0-9_-]*$/, message: "以字母开头，只能使用字母、数字、下划线和连字符", trigger: "blur" }], name: [{ required: true, message: "请输入项目名称", trigger: "blur" }] };
function refresh() { queryClient.invalidateQueries({ queryKey: ["projects"] }); projectStore.load(); }
function select(project: Project) { projectStore.select(project.project_id); ElMessage.success(`已切换到 ${project.name}`); }
function openCreate() { editing.value = false; editingId.value = null; Object.assign(form, { project_key: "", name: "", description: "", owner_id: undefined, status: "active" }); dialogVisible.value = true; }
function openEdit(project: Project) { editing.value = true; editingId.value = project.project_id; Object.assign(form, { project_key: project.project_key, name: project.name, description: project.description, owner_id: undefined, status: project.status }); dialogVisible.value = true; }
async function save() { if (!formRef.value) return; const valid = await formRef.value.validate().catch(() => false); if (!valid) return; saving.value = true; try { if (editing.value && editingId.value) { await aetpApi.projects.update(editingId.value, { name: form.name, description: form.description, status: form.status }); } else { await aetpApi.projects.create({ project_key: form.project_key, name: form.name, description: form.description, owner_id: form.owner_id }); } ElMessage.success("项目已保存"); dialogVisible.value = false; refresh(); } catch (error) { ElMessage.error(error instanceof Error ? error.message : "保存失败"); } finally { saving.value = false; } }
function roleText(role?: string | null) { return ({ viewer: "查看者", operator: "操作员", maintainer: "维护者", owner: "项目负责人" } as Record<string, string>)[role || ""] || "成员"; }
function roleTag(role?: string | null) { return ({ viewer: "info", operator: "success", maintainer: "warning", owner: "danger" } as Record<string, "info" | "success" | "warning" | "danger">)[role || ""] || "info"; }
</script>

<style scoped>
.projects-page { max-width: 1480px; margin: 0 auto; }.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }.page-alert { margin-bottom: 14px; }.card-heading { display: flex; align-items: center; justify-content: space-between; }.card-heading div { display: flex; align-items: baseline; gap: 10px; }.card-heading strong { font-size: 15px; }.card-heading span { color: var(--aetp-muted); font-size: 11px; }.project-cell { display: flex; align-items: center; gap: 11px; }.project-code { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 7px; background: #eaf3ff; color: var(--aetp-blue); font-size: 12px; font-weight: 800; }.project-cell div { display: flex; flex-direction: column; gap: 3px; }.project-cell small { overflow: hidden; color: var(--aetp-muted); font-family: ui-monospace, monospace; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.description { color: var(--aetp-muted); font-size: 13px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; } }
</style>
