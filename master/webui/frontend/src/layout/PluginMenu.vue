<script setup>
import { computed } from 'vue';
import AppMenuItem from './AppMenuItem.vue';
import { pluginUiState } from '@/plugins/ui-state';

const model = computed(() => {
    // 宿主自己的页面固定在 AETP 组；插件页面按 register_ui 的 menu_group 分组，
    // 未声明 menu_group 的并入 AETP 组，不再硬编码「已安装插件」分类
    const baseItems = [{ label: '插件管理', icon: 'pi pi-fw pi-box', to: '/plugins', order: 0 }];
    const groups = new Map();
    for (const page of pluginUiState.contributions.filter((item) => item.kind === 'page' && item.title && item.path)) {
        const entry = {
            label: page.title,
            icon: page.menu_icon || 'pi pi-fw pi-box',
            to: page.path,
            order: page.menu_order,
            id: page.id
        };
        const groupName = typeof page.menu_group === 'string' ? page.menu_group.trim() : '';
        if (groupName) {
            if (!groups.has(groupName)) groups.set(groupName, []);
            groups.get(groupName).push(entry);
        } else {
            baseItems.push(entry);
        }
    }

    const byOrder = (left, right) => left.order - right.order || left.label.localeCompare(right.label);
    return [
        { label: 'AETP', items: baseItems.sort(byOrder) },
        ...[...groups.entries()]
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([label, items]) => ({
                label,
                path: `plugins-${label}`,
                items: items.sort(byOrder)
            }))
    ];
});
</script>

<template>
    <ul class="layout-menu">
        <template v-for="(item, index) in model" :key="item.label">
            <app-menu-item :item="item" :index="index" />
        </template>
    </ul>
</template>

<style lang="scss" scoped></style>
