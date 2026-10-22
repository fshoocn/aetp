import { computed } from 'vue';
import { pluginUiState } from '@/plugins/ui-state';

/**
 * PrimeVue PanelMenu 侧边栏菜单模型：宿主自带页面 + 插件 UI 页面（register_ui）。
 *
 * 菜单权重即 `register_ui` 的 `menu_order`（越小越靠前）；「插件管理」权重
 * 默认 100（即最低，排最后）。组内按权重排序、分组按组内最靠前的权重排序；
 * 整个菜单权重最靠前的条目即默认首页（`/` 重定向目标，见 router/index.js）。
 * 未声明 `menu_group` 的页面归入无 path 的 AETP 桶（PluginMenu 直接铺成
 * 一级菜单）；声明了 `menu_group` 的分组带 `path`（PluginMenu 渲染为
 * 可折叠的二级菜单）。
 */

//: 「插件管理」菜单权重默认值：100 = 最低（没有插件页面时它仍是首页）
export const PLUGIN_MANAGEMENT_MENU_ORDER = 100;

const byOrder = (left, right) => left.order - right.order || left.label.localeCompare(right.label);

export const menuModel = computed(() => {
    // 宿主自己的页面与未声明 menu_group 的插件页面归入 AETP 组
    const groups = new Map([
        ['AETP', [{ label: '插件管理', icon: 'pi pi-fw pi-box', to: '/plugins', order: PLUGIN_MANAGEMENT_MENU_ORDER }]]
    ]);
    for (const page of pluginUiState.contributions.filter((item) => item.kind === 'page' && item.title && item.path)) {
        const entry = {
            label: page.title,
            icon: page.menu_icon || 'pi pi-fw pi-box',
            to: page.path,
            // 缺失 / 非法的 menu_order 会把排序算成 NaN（首页漂移）：
            // 与后端 register_ui 的默认权重 0 对齐，保证排序确定
            order: Number.isFinite(Number(page.menu_order)) ? Number(page.menu_order) : 0,
            id: page.id
        };
        const groupName = (typeof page.menu_group === 'string' ? page.menu_group.trim() : '') || 'AETP';
        if (!groups.has(groupName)) groups.set(groupName, []);
        groups.get(groupName).push(entry);
    }

    return [...groups.entries()]
        .map(([label, items]) => ({
            label,
            // 分组的 path 是子菜单展开态的路径前缀（AppMenuItem 约定）；AETP 组保持无 path
            ...(label === 'AETP' ? {} : { path: `plugins-${label}` }),
            items: items.sort(byOrder)
        }))
        .sort((left, right) => left.items[0].order - right.items[0].order || left.label.localeCompare(right.label));
});

/** 默认首页：菜单里权重最靠前的条目（兜底回插件管理页）。 */
export function homePath() {
    return menuModel.value[0]?.items[0]?.to ?? '/plugins';
}
