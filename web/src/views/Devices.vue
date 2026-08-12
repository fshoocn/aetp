<template>
  <div>
    <h2>设备管理</h2>
    <el-card v-loading="loading">
      <el-table :data="devices">
        <el-table-column prop="device_id" label="设备ID" width="180" />
        <el-table-column prop="name" label="名称" />
        <el-table-column prop="node_id" label="节点" width="160">
          <template #default="{ row }">{{ row.node_id ?? "-" }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="120" />
        <el-table-column label="在线" width="100">
          <template #default="{ row }">
            <el-tag :type="row.online ? 'success' : 'info'" size="small">
              {{ row.online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后在线">
          <template #default="{ row }">
            {{ row.last_seen_at ? fmt(row.last_seen_at) : "-" }}
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && devices.length === 0" description="暂无设备数据" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { aetpApi } from "@/api/endpoints";
import { useProjectStore } from "@/stores/project";

const projectStore = useProjectStore();
const projectId = computed(() => projectStore.currentProjectId ?? "");

const query = useQuery({
  queryKey: ["devices", "list", projectId],
  queryFn: () => aetpApi.devices.list(projectId.value),
  enabled: computed(() => !!projectId.value),
});

const loading = computed(() => query.isLoading.value);
const devices = computed(() => query.data.value ?? []);

function fmt(ts: string) {
  return new Date(ts).toLocaleString("zh-CN");
}
</script>
