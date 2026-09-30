<script setup>
import { computed } from 'vue';
import AppMenuItem from './AppMenuItem.vue';
import { pluginUiState } from '@/plugins/ui-state';

const model = computed(() => {
    const groups = new Map();
    for (const page of pluginUiState.contributions.filter((item) => item.kind === 'page' && item.title && item.path)) {
        const groupName = '已安装插件';
        if (!groups.has(groupName)) groups.set(groupName, []);
        groups.get(groupName).push({
            label: page.title,
            icon: page.menu_icon || 'pi pi-fw pi-box',
            to: page.path,
            order: page.menu_order,
            id: page.id
        });
    }

    return [
        { label: 'AETP', items: [{ label: '插件管理', icon: 'pi pi-fw pi-box', to: '/plugins' }] },
        ...[...groups.entries()]
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([label, items]) => ({
                label,
                path: `plugins-${label}`,
                items: items.sort((left, right) => left.order - right.order || left.label.localeCompare(right.label))
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
