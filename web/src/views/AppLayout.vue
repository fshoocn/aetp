<template>
  <el-container class="layout">
    <el-aside width="220px" class="sidebar">
      <div class="logo" @click="$router.push('/dashboard')">AETP</div>
      <el-menu
        :default-active="$route.path"
        router
        background-color="#001529"
        text-color="rgba(255,255,255,0.7)"
        active-text-color="#fff"
        class="nav"
      >
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/devices">
          <el-icon><Monitor /></el-icon>
          <span>设备管理</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <span class="page-title">{{ pageTitle }}</span>
        </div>
        <div class="header-right">
          <!-- 项目切换器 -->
          <el-select
            v-model="currentProjectId"
            placeholder="选择项目"
            class="project-switch"
            :loading="projectLoading"
            @change="onProjectChange"
          >
            <el-option
              v-for="p in projectStore.projects"
              :key="p.project_id"
              :label="`${p.name} (${p.project_key})`"
              :value="p.project_id"
            />
          </el-select>

          <el-dropdown @command="onUserCommand">
            <span class="user-trigger">
              <el-icon><User /></el-icon>
              {{ auth.user?.display_name || auth.user?.username }}
              <el-icon><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  角色：{{ roleLabel }}
                </el-dropdown-item>
                <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  Odometer,
  List,
  Monitor,
  User,
  ArrowDown,
} from "@element-plus/icons-vue";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const auth = useAuthStore();
const projectStore = useProjectStore();
const route = useRoute();

const currentProjectId = ref<string | null>(projectStore.currentProjectId);
const projectLoading = ref(false);

const pageTitle = computed(() => {
  const map: Record<string, string> = {
    Dashboard: "仪表盘",
    Tasks: "任务管理",
    TaskDetail: "任务详情",
    Devices: "设备管理",
  };
  return map[route.name as string] ?? "AETP";
});

const roleLabel = computed(() => {
  if (auth.user?.platform_role === "admin") return "平台管理员";
  return "普通用户";
});

function onProjectChange(projectId: string) {
  projectStore.select(projectId);
  // 切换项目后刷新各页面数据（依赖 currentProjectId 的 query 自动失效）
  window.dispatchEvent(new CustomEvent("project-changed", { detail: projectId }));
}

function onUserCommand(command: string) {
  if (command === "logout") auth.logout();
}

watch(
  () => projectStore.currentProjectId,
  (v) => {
    currentProjectId.value = v;
  }
);

// 进入布局时加载项目列表（store 内存缓存）
if (projectStore.projects.length === 0) {
  projectLoading.value = true;
  projectStore
    .load()
    .catch(() => {})
    .finally(() => {
      projectLoading.value = false;
    });
}
</script>

<style scoped>
.layout {
  min-height: 100vh;
}
.sidebar {
  background: #001529;
  display: flex;
  flex-direction: column;
}
.logo {
  color: #1a73e8;
  text-align: center;
  cursor: pointer;
  padding: 20px 0 16px;
  font-size: 22px;
  font-weight: 600;
}
.nav {
  border-right: none;
  flex: 1;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
}
.page-title {
  font-size: 16px;
  font-weight: 600;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.project-switch {
  width: 240px;
}
.user-trigger {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: #333;
  font-size: 14px;
  outline: none;
}
.main {
  background: #f0f2f5;
  padding: 20px;
}
</style>
