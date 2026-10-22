<script setup>
import ContextMenu from 'primevue/contextmenu';
import { useLayout } from '@/layout/composables/layout';
import { menuTitleFor, useWorkspaceTabs } from '@/layout/composables/tabs';
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import AppSidebar from './AppSidebar.vue';
import AppTopbar from './AppTopbar.vue';

const { layoutConfig, layoutState, hideMobileMenu } = useLayout();
const route = useRoute();
const router = useRouter();

const containerClass = computed(() => {
    return {
        'layout-overlay': layoutConfig.menuMode === 'overlay',
        'layout-static': layoutConfig.menuMode === 'static',
        'layout-overlay-active': layoutState.overlayMenuActive,
        'layout-mobile-active': layoutState.mobileMenuActive,
        'layout-static-inactive': layoutState.staticMenuInactive
    };
});

// ---- 工作区标签页：任何导航（菜单 / 直链）都在主区打开对应 tab ----
const { tabs, activePath, openTab, closeTab, closeLeft, closeRight, closeOthers } = useWorkspaceTabs();

watch(
    () => route.path,
    (path) => {
        if (route.name === 'not-found') return;
        openTab(path, menuTitleFor(path) || String(route.meta?.title || path));
    },
    { immediate: true }
);

function onTabSwitch(value) {
    if (value && value !== route.path) router.push(value);
}

function navigateAfterClose(next) {
    if (next && next !== route.path) router.push(next);
}

function onTabClose(tab) {
    navigateAfterClose(closeTab(tab.path));
}

// ---- 标签页右键菜单：关闭当前 / 右侧全部 / 左侧全部 / 非当前 ----
const contextMenuRef = ref(null);
const contextTab = ref(null);

const contextMenuItems = computed(() => [
    { label: '关闭当前', icon: 'pi pi-times', command: () => navigateAfterClose(closeTab(contextTab.value.path)) },
    { label: '关闭右侧所有', icon: 'pi pi-arrow-right', command: () => navigateAfterClose(closeRight(contextTab.value.path)) },
    { label: '关闭左侧所有', icon: 'pi pi-arrow-left', command: () => navigateAfterClose(closeLeft(contextTab.value.path)) },
    { separator: true },
    { label: '关闭非当前选项', icon: 'pi pi-clone', command: () => navigateAfterClose(closeOthers(contextTab.value.path)) }
]);

function onTabContextMenu(event, tab) {
    contextTab.value = tab;
    contextMenuRef.value?.show(event);
}
</script>

<template>
    <div class="layout-wrapper" :class="containerClass">
        <AppTopbar />
        <AppSidebar />
        <div class="layout-main-container">
            <div class="layout-main">
                <!-- 主区标签页集合：点击标签切换页面，悬浮出关闭钮，右键出批量关闭菜单 -->
                <Tabs :value="activePath ?? undefined" class="workspace-tabs" @update:value="onTabSwitch">
                    <TabList>
                        <Tab v-for="tab in tabs" :key="tab.path" :value="tab.path" @contextmenu="onTabContextMenu($event, tab)">
                            <span class="workspace-tabs__label">{{ tab.title }}</span>
                            <span class="workspace-tabs__close" title="关闭" @click.stop="onTabClose(tab)">
                                <i class="pi pi-times"></i>
                            </span>
                        </Tab>
                    </TabList>
                    <TabPanels>
                        <TabPanel :value="activePath ?? undefined">
                            <!-- 全部 tab 关闭后主区显示空白 -->
                            <router-view v-if="tabs.length" />
                        </TabPanel>
                    </TabPanels>
                </Tabs>
            </div>
        </div>
        <div class="layout-mask animate-fadein" @click="hideMobileMenu" />
        <ContextMenu ref="contextMenuRef" :model="contextMenuItems" />
    </div>
    <Toast />
    <ConfirmDialog />
</template>

<style scoped>
/* 标签标题不随主题色变化：背景透明，文字与激活下划线用默认前景色 */
:deep(.p-tab),
:deep(.p-tab.p-tab-active) {
    background: transparent;
    color: var(--text-color);
}

:deep(.p-tab.p-tab-active) {
    border-color: var(--primary-color);
}

/* PrimeVue 自带的主色活动指示条会盖住上面的前景色下划线，且只在激活页变化时
   重定位——关闭激活页左侧的标签后它会残留在旧位置，直接隐藏 */
:deep(.p-tablist-active-bar) {
    display: none;
}

/* 标签页：关闭按钮默认隐藏，悬浮标签时展示 */
.workspace-tabs__label {
    margin-right: 0.5rem;
}

.workspace-tabs__close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.125rem;
    height: 1.125rem;
    border-radius: 50%;
    font-size: 0.75rem;
    color: var(--text-color-secondary);
    visibility: hidden;
}

.workspace-tabs__close:hover {
    background: var(--surface-hover);
    color: var(--text-color);
}

:deep(.p-tab:hover) .workspace-tabs__close {
    visibility: visible;
}
</style>
