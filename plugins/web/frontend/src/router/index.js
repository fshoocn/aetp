import AppLayout from '@/layout/AppLayout.vue';
import PluginManagement from '@/views/PluginManagement.vue';
import NotFound from '@/views/pages/NotFound.vue';
import { createRouter, createWebHistory } from 'vue-router';

const router = createRouter({
    history: createWebHistory(),
    routes: [
        {
            path: '/',
            name: 'app-shell',
            component: AppLayout,
            children: [
                { path: '', redirect: '/plugins' },
                { path: '/plugins', name: 'plugin-management', component: PluginManagement },
                { path: ':pathMatch(.*)*', name: 'not-found', component: NotFound }
            ]
        }
    ]
});

export default router;
