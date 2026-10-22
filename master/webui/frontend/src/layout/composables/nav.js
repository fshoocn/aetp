import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { menuModel } from './menu.js';
import { useWorkspaceTabs } from './tabs.js';

/**
 * 菜单导航模型：抽屉侧边栏用 PrimeVue PanelMenu（分组手风琴），条目按官方
 * Router 示例的 router-link 模板渲染（https://v4.primevue.org/panelmenu/）。
 * 未配置 menu_group 的页面直接作为一级条目；配置了 menu_group 的分组折叠在
 * 对应面板里。点击条目总是打开工作区 tab（即使路由未变，比如 tab 全关后
 * 重开同一页面），路由跳转由模板里的 router-link 负责。
 */
export function useNavModels() {
    const route = useRoute();
    const router = useRouter();
    const { openTab } = useWorkspaceTabs();

    /** 打开工作区 tab 并跳转；键盘激活等拿不到 navigate 的场景兜底。 */
    function open(path, title) {
        if (!path) return;
        openTab(path, title);
        if (route.path !== path) router.push(path);
    }

    /** 叶子条目点击：先开 tab，再交给 router-link 的 navigate 跳转（保留原生点击语义）。 */
    function onLeafClick(item, navigate, event) {
        if (!item.route) return;
        openTab(item.route, item.label);
        navigate(event);
    }

    function toItem(item) {
        return {
            key: item.to,
            label: item.label,
            icon: item.icon,
            disabled: item.disabled,
            route: item.to || undefined,
            command: item.to ? () => open(item.to, item.label) : undefined
        };
    }

    const panelModel = computed(() => menuModel.value.flatMap((group) => (group.path ? [{ key: group.path, label: group.label, items: group.items.map(toItem) }] : group.items.map(toItem))));

    return { panelModel, onLeafClick };
}
