import AppLayout from '@/layout/AppLayout.vue';
import PluginManagement from '@/views/PluginManagement.vue';
import NotFound from '@/views/pages/NotFound.vue';
import { createRouter, createWebHistory } from 'vue-router';

import { homePath } from '@/layout/composables/menu.js';

const router = createRouter({
    history: createWebHistory(),
    routes: [
        {
            path: '/',
            name: 'app-shell',
            component: AppLayout,
            children: [
                // 默认首页 = 侧边栏权重最靠前的菜单项（见 layout/composables/menu.js）
                { path: '', redirect: () => homePath() },
                { path: '/plugins', name: 'plugin-management', component: PluginManagement },
                { path: ':pathMatch(.*)*', name: 'not-found', component: NotFound }
            ]
        }
    ]
});

export default router;
