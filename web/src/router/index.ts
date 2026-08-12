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
        path: "devices",
        name: "Devices",
        component: () => import("@/views/Devices.vue"),
      },
    ],
  },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

// 路由守卫：未登录跳转登录页
router.beforeEach((to, _from, next) => {
  const token = localStorage.getItem("token");
  if (to.name !== "Login" && !token) {
    next({ name: "Login" });
  } else if (to.name === "Login" && token) {
    next({ name: "Dashboard" });
  } else {
    next();
  }
});

export default router;
