<template>
  <div class="tasks-page">
    <div class="page-heading">
      <div><span class="eyebrow">TASK QUEUE / PROJECT SCOPE</span><h1>任务队列</h1><p>查看当前项目的任务状态与执行目标。</p></div>
      <div class="heading-actions"><el-tag v-if="canDispatch" type="success" effect="light"><el-icon><CircleCheck /></el-icon> 可下发任务</el-tag><el-button v-if="canDispatch" type="primary" :icon="Plus" @click="openCreate">创建任务</el-button></div>
    </div>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个有权限的项目。" type="info" show-icon :closable="false" />
    <el-alert v-if="errorMessage" title="任务加载失败" :description="errorMessage" type="error" show-icon :closable="false" class="page-alert" />

    <el-card v-if="projectId" class="filter-card" shadow="never">
      <el-form inline @submit.prevent="applyFilters">
        <el-form-item label="状态"><el-select v-model="filters.status" clearable placeholder="全部状态" style="width: 150px"><el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item>
        <el-form-item label="设备"><el-select v-model="filters.deviceId" clearable filterable placeholder="全部设备" style="width: 190px"><el-option v-for="device in devices" :key="device.device_id" :label="device.name || device.device_id" :value="device.device_id" /></el-select></el-form-item>
        <el-form-item><el-button type="primary" :icon="Search" @click="applyFilters">筛选</el-button><el-button :icon="Refresh" @click="resetFilters">重置</el-button></el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="projectId" v-loading="loading" class="table-card" shadow="never">
      <template #header><div class="card-heading"><div><strong>任务记录</strong><span>{{ projectStore.currentProject?.name || "当前项目" }}</span></div><el-button text :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button></div></template>
      <el-table :data="tasks" row-key="task_id" @row-click="gotoTask">
        <el-table-column prop="task_id" label="任务 ID" min-width="190" />
        <el-table-column label="目标设备" min-width="180"><template #default="{ row }"><div class="device-cell"><el-icon><Cpu /></el-icon><span>{{ row.device_id }}</span></div></template></el-table-column>
        <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="statusTag(row.status)" effect="light">{{ statusText(row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click.stop="gotoTask(row)">查看</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && tasks.length === 0" description="当前筛选条件下没有任务" />
      <div v-if="tasks.length" class="pagination-row"><span>第 {{ page }} 页</span><el-pagination background layout="prev, pager, next" :current-page="page" :page-size="pageSize" :total="total" @current-change="changePage" /></div>
    </el-card>

    <el-dialog v-model="createVisible" title="创建项目任务" width="520px" destroy-on-close>
      <el-alert title="任务会立即进入 pending 状态" description="只有项目 operator、maintainer、owner 或平台管理员可以下发任务。" type="info" show-icon :closable="false" class="dialog-alert" />
      <el-form ref="formRef" :model="createForm" :rules="formRules" label-position="top">
        <el-form-item label="目标设备" prop="deviceId"><el-select v-model="createForm.deviceId" filterable placeholder="选择项目设备" style="width: 100%"><el-option v-for="device in devices" :key="device.device_id" :label="`${device.name || device.device_id} · ${device.device_id}`" :value="device.device_id" /></el-select></el-form-item>
        <el-form-item label="命令参数 JSON" prop="commandText"><el-input v-model="createForm.commandText" type="textarea" :rows="7" spellcheck="false" placeholder='{"test":"vibration"}' /></el-form-item>
      </el-form>
      <template #footer><el-button @click="createVisible = false">取消</el-button><el-button type="primary" :loading="creating" @click="createTask">创建任务</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules } from "element-plus";
import { ElMessage } from "element-plus";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { CircleCheck, Cpu, Plus, Refresh, Search } from "@element-plus/icons-vue";
import { aetpApi, type Task } from "@/api/endpoints";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";

const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();
const queryClient = useQueryClient();
useTaskEvents(queryClient);
const projectId = computed(() => projectStore.currentProjectId ?? "");
const projectRole = computed(() => projectStore.currentRole);
const canDispatch = computed(() => auth.user?.platform_role === "admin" || ["operator", "maintainer", "owner"].includes(projectRole.value || ""));
const page = ref(1);
const pageSize = 25;
const filters = reactive({ status: "", deviceId: "" });
const statusOptions = [{ label: "待处理", value: "pending" }, { label: "已派发", value: "dispatched" }, { label: "运行中", value: "running" }, { label: "已完成", value: "completed" }, { label: "失败", value: "failed" }, { label: "已取消", value: "cancelled" }];
const taskQuery = useQuery({ queryKey: ["tasks", "list", projectId, page, filters], queryFn: () => aetpApi.tasks.list(projectId.value, { status: filters.status || undefined, deviceId: filters.deviceId || undefined, limit: pageSize, offset: (page.value - 1) * pageSize }), enabled: computed(() => !!projectId.value) });
const deviceQuery = useQuery({ queryKey: ["devices", "task-form", projectId], queryFn: () => aetpApi.devices.list(projectId.value), enabled: computed(() => !!projectId.value) });
const tasks = computed(() => taskQuery.data.value ?? []);
const devices = computed(() => deviceQuery.data.value ?? []);
const loading = computed(() => taskQuery.isLoading.value || deviceQuery.isLoading.value);
const errorMessage = computed(() => taskQuery.error.value?.message || deviceQuery.error.value?.message || "");
const total = computed(() => tasks.value.length < pageSize ? (page.value - 1) * pageSize + tasks.value.length : page.value * pageSize);
const createVisible = ref(false);
const formRef = ref<FormInstance>();
const createForm = reactive({ deviceId: "", commandText: "{}" });
const formRules: FormRules = { deviceId: [{ required: true, message: "请选择目标设备", trigger: "change" }], commandText: [{ required: true, message: "请输入 JSON 参数", trigger: "blur" }] };
const mutation = useMutation({ mutationFn: () => aetpApi.tasks.create(projectId.value, createForm.deviceId, JSON.parse(createForm.commandText)), onSuccess: () => { ElMessage.success("任务已创建"); createVisible.value = false; queryClient.invalidateQueries({ queryKey: ["tasks"] }); }, onError: (error: Error) => ElMessage.error(error.message) });
const creating = computed(() => mutation.isPending.value);
function openCreate() { createForm.deviceId = ""; createForm.commandText = "{}"; createVisible.value = true; }
async function createTask() { if (!formRef.value) return; const valid = await formRef.value.validate().catch(() => false); if (!valid) return; try { JSON.parse(createForm.commandText); } catch { ElMessage.error("命令参数必须是合法 JSON"); return; } mutation.mutate(); }
function applyFilters() { page.value = 1; queryClient.invalidateQueries({ queryKey: ["tasks", "list"] }); }
function resetFilters() { filters.status = ""; filters.deviceId = ""; page.value = 1; }
function changePage(value: number) { page.value = value; }
function refresh() { queryClient.invalidateQueries({ queryKey: ["tasks"] }); queryClient.invalidateQueries({ queryKey: ["devices"] }); }
function gotoTask(row: Task) { router.push(`/tasks/${row.task_id}`); }
function fmt(ts: string) { return new Date(ts).toLocaleString("zh-CN", { hour12: false }); }
function statusText(status: string) { return ({ pending: "待处理", dispatched: "已派发", accepted: "已接受", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", timeout: "超时" } as Record<string, string>)[status] || status; }
function statusTag(status: string) { return ({ completed: "success", running: "warning", accepted: "warning", dispatched: "info", pending: "info", failed: "danger", timeout: "danger", cancelled: "info" } as Record<string, "success" | "danger" | "warning" | "info">)[status] || "info"; }
watch(() => projectId.value, () => { page.value = 1; });
</script>

<style scoped>
.tasks-page { max-width: 1480px; margin: 0 auto; }.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }.heading-actions { display: flex; align-items: center; gap: 10px; }.page-alert { margin-bottom: 14px; }.filter-card { margin-bottom: 14px; }.filter-card :deep(.el-form-item) { margin-bottom: 0; }.table-card :deep(.el-card__body) { padding-top: 0; }.card-heading { display: flex; justify-content: space-between; align-items: center; }.card-heading div { display: flex; align-items: baseline; gap: 10px; }.card-heading strong { font-size: 15px; }.card-heading span { color: var(--aetp-muted); font-size: 11px; }.device-cell { display: flex; align-items: center; gap: 7px; }.device-cell .el-icon { color: var(--aetp-blue); }.pagination-row { display: flex; align-items: center; justify-content: space-between; margin-top: 18px; color: var(--aetp-muted); font-size: 12px; }.dialog-alert { margin-bottom: 18px; }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; gap: 15px; }.heading-actions { width: 100%; justify-content: space-between; }.filter-card :deep(.el-form) { display: flex; flex-wrap: wrap; }.filter-card :deep(.el-form-item) { margin-bottom: 12px; }.pagination-row { align-items: flex-start; flex-direction: column; gap: 10px; } }
</style>
