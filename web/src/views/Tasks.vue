<template>
  <div>
    <div class="head">
      <h2>任务管理</h2>
      <el-button type="primary" @click="openCreate">+ 创建任务</el-button>
    </div>

    <!-- 创建任务弹窗 -->
    <el-dialog v-model="showCreate" title="创建任务" width="480px">
      <el-form label-width="80px">
        <el-form-item label="设备">
          <el-select
            v-model="createForm.device_id"
            placeholder="选择设备"
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="d in devices"
              :key="d.device_id"
              :label="`${d.name || d.device_id} (${d.device_id})`"
              :value="d.device_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="参数">
          <el-input
            v-model="createForm.commandText"
            type="textarea"
            :rows="6"
            placeholder='{"test":"vibration"}'
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- 列表 -->
    <el-card v-loading="loading">
      <el-table :data="tasks">
        <el-table-column prop="task_id" label="任务ID" width="180" />
        <el-table-column prop="device_id" label="设备" width="160" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button link type="primary" @click="$router.push(`/tasks/${row.task_id}`)">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && tasks.length === 0" description="暂无任务" />

      <div class="pager">
        <el-pagination
          layout="total, prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="onPageChange"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { aetpApi } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";
import { useTaskEvents } from "@/composables/useTaskEvents";

const projectStore = useProjectStore();
const queryClient = useQueryClient();

useTaskEvents(queryClient);

const projectId = computed(() => projectStore.currentProjectId ?? "");

const page = ref(1);
const pageSize = 50;

const tasksQuery = useQuery({
  queryKey: ["tasks", "list", projectId, page],
  queryFn: () =>
    aetpApi.tasks.list(projectId.value, {
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    }),
  enabled: computed(() => !!projectId.value),
});

const devicesQuery = useQuery({
  queryKey: ["devices", "select", projectId],
  queryFn: () => aetpApi.devices.list(projectId.value),
  enabled: computed(() => !!projectId.value),
});

const loading = computed(() => tasksQuery.isLoading.value);
const tasks = computed(() => tasksQuery.data.value ?? []);
const devices = computed(() => devicesQuery.data.value ?? []);
// 前端分页：后端按页返回，这里用总数近似（后端返回当前页条数）
const total = computed(() =>
  tasks.value.length < pageSize
    ? (page.value - 1) * pageSize + tasks.value.length
    : page.value * pageSize
);

const createMutation = useMutation({
  mutationFn: (payload: { deviceId: string; command: Record<string, unknown> }) =>
    aetpApi.tasks.create(projectId.value, payload.deviceId, payload.command),
  onSuccess: () => {
    ElMessage.success("任务创建成功");
    showCreate.value = false;
    createForm.device_id = "";
    createForm.commandText = "{}";
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
  },
  onError: (e: Error) => ElMessage.error(e.message),
});

const showCreate = ref(false);
const creating = computed(() => createMutation.isPending.value);
const createForm = reactive({ device_id: "", commandText: "{}" });

function openCreate() {
  createForm.device_id = "";
  createForm.commandText = "{}";
  showCreate.value = true;
}

function handleCreate() {
  let cmd: Record<string, unknown> = {};
  try {
    cmd = JSON.parse(createForm.commandText);
  } catch {
    ElMessage.error("参数 JSON 格式错误");
    return;
  }
  if (!createForm.device_id) {
    ElMessage.error("请选择设备");
    return;
  }
  createMutation.mutate({ deviceId: createForm.device_id, command: cmd });
}

function onPageChange(p: number) {
  page.value = p;
}

watch(
  () => projectStore.currentProjectId,
  () => {
    page.value = 1;
  }
);

function statusText(s: string) {
  const map: Record<string, string> = {
    pending: "待派发",
    dispatched: "已派发",
    accepted: "已接受",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    timeout: "超时",
  };
  return map[s] ?? s;
}

function statusTag(s: string) {
  const map: Record<string, "success" | "danger" | "warning" | "info"> = {
    pending: "info",
    dispatched: "info",
    accepted: "warning",
    running: "warning",
    completed: "success",
    failed: "danger",
    cancelled: "info",
    timeout: "danger",
  };
  return map[s] ?? "info";
}

function fmt(ts: string) {
  return new Date(ts).toLocaleString("zh-CN");
}
</script>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.pager {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
