import { createRouter, createWebHashHistory } from "vue-router";

const routes = [
  {
    path: "/",
    redirect: "/dashboard",
  },
  {
    path: "/login",
    name: "Login",
    component: () => import("@/views/Login.vue"),
  },
  {
    path: "/",
    component: () => import("@/views/AppLayout.vue"),
    children: [
      {
        path: "dashboard",
        name: "Dashboard",
        component: () => import("@/views/Dashboard.vue"),
      },
      {
        path: "projects",
        name: "Projects",
        component: () => import("@/views/Projects.vue"),
      },
      {
        path: "tasks",
        name: "Tasks",
        component: () => import("@/views/Tasks.vue"),
      },
      {
        path: "tasks/:taskId",
        name: "TaskDetail",
        component: () => import("@/views/TaskDetail.vue"),
        props: true,
      },
      {
        path: "runs",
        name: "Runs",
        component: () => import("@/views/Runs.vue"),
      },
      {
        path: "runs/:runId",
        name: "RunDetail",
        component: () => import("@/views/RunDetail.vue"),
        props: true,
      },
      {
        path: "scripts",
        name: "Scripts",
        component: () => import("@/views/Scripts.vue"),
      },
      {
        path: "test-tasks",
        name: "TestTasks",
        component: () => import("@/views/TestTasks.vue"),
      },
      {
        path: "plugins",
        name: "Plugins",
        component: () => import("@/views/Plugins.vue"),
      },
      {
        path: "project-nodes",
        name: "ProjectNodes",
        component: () => import("@/views/ProjectNodes.vue"),
      },
      {
        path: "devices",
        name: "Devices",
        component: () => import("@/views/Devices.vue"),
      },
      {
        path: "members",
        name: "Members",
        component: () => import("@/views/Members.vue"),
      },
      {
        path: "notifications",
        name: "Notifications",
        component: () => import("@/views/Notifications.vue"),
      },
      {
        path: "users",
        name: "Users",
        meta: { requiresAdmin: true },
        component: () => import("@/views/Users.vue"),
      },
    ],
  },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

function isAdminUser(): boolean {
  try {
    return JSON.parse(localStorage.getItem("user") || "null")?.platform_role === "admin";
  } catch {
    return false;
  }
}

// 路由守卫：未登录跳转登录页
router.beforeEach((to, _from, next) => {
  const token = localStorage.getItem("token");
  if (to.name !== "Login" && !token) {
    next({ name: "Login" });
  } else if (to.meta.requiresAdmin && !isAdminUser()) {
    next({ name: "Dashboard" });
  } else if (to.name === "Login" && token) {
    next({ name: "Dashboard" });
  } else {
    next();
  }
});

export default router;
