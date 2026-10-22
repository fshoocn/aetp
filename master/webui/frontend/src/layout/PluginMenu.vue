<script setup>
import PanelMenu from 'primevue/panelmenu';
import { ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useNavModels } from './composables/nav.js';

const route = useRoute();
const { panelModel, onLeafClick } = useNavModels();

// 受控展开：命中当前路由的分组自动展开，其余分组保持用户手动的展开状态
const expandedKeys = ref({});
watch(
    [panelModel, () => route.path],
    () => {
        for (const item of panelModel.value) {
            if (item.items?.some((child) => child.route === route.path)) {
                expandedKeys.value = { ...expandedKeys.value, [item.key]: true };
            }
        }
    },
    { immediate: true }
);
</script>

<template>
    <!-- PrimeVue PanelMenu：分组手风琴；条目用官方 Router 示例的 router-link 模板 -->
    <PanelMenu v-model:expandedKeys="expandedKeys" :model="panelModel">
        <template #item="{ item, hasSubmenu, active }">
            <router-link v-if="item.route" v-slot="{ href, navigate }" :to="item.route" custom>
                <a v-ripple class="panelmenu-link" :class="{ 'active-route': item.route === route.path }" :href="href" @click.stop="onLeafClick(item, navigate, $event)">
                    <span v-if="item.icon" :class="item.icon" />
                    <span class="panelmenu-label">{{ item.label }}</span>
                </a>
            </router-link>
            <!-- 分组标题：点击交给 PanelMenu 切换展开（不拦截冒泡） -->
            <a v-else v-ripple class="panelmenu-link panelmenu-group">
                <span class="panelmenu-label">{{ item.label }}</span>
                <span v-if="hasSubmenu" class="pi pi-angle-down panelmenu-toggler" :class="{ 'panelmenu-toggler-open': active }" />
            </a>
        </template>
    </PanelMenu>
</template>

<style scoped>
/* sakai 抽屉观感：去掉 PanelMenu 自带的面板卡片，条目沿用 sakai 菜单样式 */
:deep(.p-panelmenu) {
    display: flex;
    flex-direction: column;
    gap: 0;
}

:deep(.p-panelmenu-panel) {
    background: transparent;
    border: none;
    padding: 0;
    border-radius: 0;
}

.panelmenu-link {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    border-radius: var(--content-border-radius);
    color: var(--text-color);
    cursor: pointer;
    user-select: none;
    transition: background-color var(--element-transition-duration);
}

.panelmenu-link:hover {
    background-color: var(--surface-hover);
}

.panelmenu-label {
    flex: 1;
}

.panelmenu-link.active-route {
    font-weight: 700;
    color: var(--primary-color);
}

/* 分组标题沿用 sakai 的小号大写根标题观感 */
.panelmenu-group .panelmenu-label {
    font-size: 0.857rem;
    text-transform: uppercase;
    font-weight: 700;
}

.panelmenu-toggler {
    margin-left: auto;
    font-size: 75%;
    color: var(--primary-color);
    transition: transform var(--element-transition-duration);
}

.panelmenu-toggler-open {
    transform: rotate(-180deg);
}
</style>
