<template>
  <el-container class="shell">
    <el-aside :width="collapsed ? '72px' : '244px'" class="shell-aside">
      <div class="brand" @click="router.push('/dashboard')">
        <div class="brand-mark">A</div>
        <div v-if="!collapsed" class="brand-copy">
          <strong>AETP</strong>
          <span>测试控制台</span>
        </div>
      </div>

      <el-menu :default-active="route.path" :collapse="collapsed" router class="shell-menu">
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <template #title>项目总览</template>
        </el-menu-item>
        <el-menu-item index="/projects">
          <el-icon><Collection /></el-icon>
          <template #title>项目管理</template>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <template #title>任务队列</template>
        </el-menu-item>
        <el-menu-item index="/devices">
          <el-icon><Cpu /></el-icon>
          <template #title>节点与设备</template>
        </el-menu-item>
        <el-menu-item v-if="canManageProject" index="/members">
          <el-icon><UserFilled /></el-icon>
          <template #title>成员与权限</template>
        </el-menu-item>
        <el-menu-item v-if="auth.user?.platform_role === 'admin'" index="/users">
          <el-icon><UserFilled /></el-icon>
          <template #title>账户审核</template>
        </el-menu-item>
      </el-menu>

      <div v-if="!collapsed" class="aside-foot">
        <span class="health-dot"></span>
        <span>Master 在线</span>
        <span class="version">v0.1</span>
      </div>
    </el-aside>

    <el-container class="shell-main">
      <el-header class="topbar">
        <div class="topbar-start">
          <el-button class="collapse-btn" text @click="collapsed = !collapsed">
            <el-icon :size="19"><Expand v-if="collapsed" /><Fold v-else /></el-icon>
          </el-button>
          <div class="breadcrumb-wrap">
            <span class="eyebrow">AETP / WORKSPACE</span>
            <strong>{{ pageTitle }}</strong>
          </div>
        </div>

        <div class="topbar-end">
          <el-select
            v-model="currentProjectId"
            class="project-picker"
            placeholder="选择项目"
            :loading="projectLoading"
            @change="onProjectChange"
          >
            <template #prefix><el-icon><Collection /></el-icon></template>
            <el-option
              v-for="project in projectStore.projects"
              :key="project.project_id"
              :label="project.name"
              :value="project.project_id"
            >
              <div class="project-option">
                <span>{{ project.name }}</span>
                <small>{{ project.project_key }}</small>
              </div>
            </el-option>
          </el-select>

          <el-divider direction="vertical" />
          <el-dropdown trigger="click" @command="onUserCommand">
            <button class="account-button">
              <el-avatar :size="32" class="account-avatar">
                {{ (auth.user?.display_name || auth.user?.username || "A").slice(0, 1).toUpperCase() }}
              </el-avatar>
              <span class="account-text">
                <strong>{{ auth.user?.display_name || auth.user?.username }}</strong>
                <small>{{ roleLabel }}</small>
              </span>
              <el-icon><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>{{ auth.user?.username }}</el-dropdown-item>
                <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="workspace">
        <div v-if="!projectStore.currentProjectId && !['Dashboard', 'Devices', 'Users', 'Projects'].includes(String(route.name))" class="no-project">
          <el-result icon="info" title="还没有可用项目" sub-title="请联系平台管理员将你的账户加入项目后再开始工作" />
        </div>
        <router-view v-else v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ArrowDown, Collection, Cpu, Expand, Fold, List, Odometer, UserFilled } from "@element-plus/icons-vue";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();
const projectStore = useProjectStore();
const collapsed = ref(false);
const projectLoading = ref(false);
const currentProjectId = ref<string | null>(projectStore.currentProjectId);

const pageTitle = computed(() => ({
  Dashboard: "项目总览",
  Projects: "项目管理",
  Tasks: "任务队列",
  TaskDetail: "任务详情",
  Devices: "节点与设备",
  Members: "成员与权限",
  Users: "账户审核",
}[String(route.name)] || "工作区"));
const roleLabel = computed(() => auth.user?.platform_role === "admin" ? "平台管理员" : "项目成员");
const canManageProject = computed(() => auth.user?.platform_role === "admin" || ["maintainer", "owner"].includes(projectStore.currentRole || ""));

function onProjectChange(projectId: string) {
  projectStore.select(projectId);
  window.dispatchEvent(new CustomEvent("project-changed", { detail: projectId }));
}
function onUserCommand(command: string) {
  if (command === "logout") auth.logout();
}
watch(() => projectStore.currentProjectId, (value) => { currentProjectId.value = value; });

if (projectStore.projects.length === 0) {
  projectLoading.value = true;
  projectStore.load().catch(() => undefined).finally(() => { projectLoading.value = false; });
}
</script>

<style scoped>
.shell { min-height: 100vh; background: var(--aetp-bg); }
.shell-aside { position: relative; display: flex; flex-direction: column; overflow: hidden; background: #142330; transition: width .2s ease; }
.brand { display: flex; align-items: center; gap: 11px; min-height: 82px; padding: 18px 20px; cursor: pointer; color: #fff; }
.brand-mark { display: grid; width: 37px; height: 37px; place-items: center; border: 1px solid rgba(255,255,255,.3); border-radius: 8px; background: #236bbd; font-size: 20px; font-weight: 800; }
.brand-copy { display: flex; flex-direction: column; line-height: 1.2; }
.brand-copy strong { font-size: 16px; letter-spacing: .14em; }
.brand-copy span { margin-top: 5px; color: #92a4b2; font-size: 11px; }
.shell-menu { flex: 1; border-right: 0; background: transparent; padding: 12px 10px; }
:deep(.el-menu-item) { height: 46px; margin: 4px 0; border-radius: 6px; color: #aebdca; }
:deep(.el-menu-item:hover) { background: rgba(83,151,218,.12); color: #fff; }
:deep(.el-menu-item.is-active) { background: #236bbd; color: #fff; }
:deep(.el-menu-item .el-icon) { margin-right: 13px; font-size: 18px; }
.aside-foot { display: flex; align-items: center; gap: 7px; padding: 16px 20px 20px; color: #8ca0af; font-size: 12px; }
.health-dot { width: 7px; height: 7px; border-radius: 50%; background: #3fbd8a; box-shadow: 0 0 0 4px rgba(63,189,138,.12); }
.version { margin-left: auto; color: #5f7280; }
.shell-main { min-width: 0; }
.topbar { display: flex; align-items: center; justify-content: space-between; height: 82px; padding: 0 30px; border-bottom: 1px solid var(--aetp-line); background: rgba(255,255,255,.92); }
.topbar-start, .topbar-end, .account-button { display: flex; align-items: center; }
.topbar-start { gap: 15px; }
.collapse-btn { color: #667783; }
.breadcrumb-wrap { display: flex; flex-direction: column; gap: 5px; }
.eyebrow { color: #9aa7b0; font-size: 10px; font-weight: 750; letter-spacing: .14em; }
.breadcrumb-wrap strong { font-size: 18px; }
.topbar-end { gap: 14px; }
.project-picker { width: 230px; }
.project-option { display: flex; justify-content: space-between; gap: 20px; }
.project-option small { color: #9aa7b0; }
.account-button { gap: 9px; border: 0; background: transparent; cursor: pointer; text-align: left; }
.account-avatar { background: #e5effb; color: #236bbd; font-weight: 750; }
.account-text { display: flex; flex-direction: column; min-width: 82px; line-height: 1.3; }
.account-text strong { color: var(--aetp-ink); font-size: 13px; }
.account-text small { color: var(--aetp-muted); font-size: 11px; }
.workspace { padding: 30px; background: var(--aetp-bg); }
.no-project { min-height: 56vh; display: grid; place-items: center; }
@media (max-width: 760px) {
  .shell-aside { width: 72px !important; }
  .brand { justify-content: center; padding: 18px 10px; }
  .topbar { min-height: 82px; height: auto; padding: 16px; }
  .project-picker { width: 150px; }
  .account-text, .topbar-start .collapse-btn { display: none; }
  .workspace { padding: 16px; }
}
</style>
