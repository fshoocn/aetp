import { computed, reactive } from 'vue';
import { menuModel } from './menu.js';

/**
 * 工作区标签页（IDE 式多页签）：菜单 / 直链导航都会在主区打开对应 tab，
 * 标签页支持悬浮关闭按钮与右键菜单（关闭当前 / 左右侧全部 / 非当前）。
 */
const state = reactive({
    tabs: [], // { path, title }
    activePath: null
});

/** 菜单里的页面标题（tab 标签文字），查不到回退调用方给的标题。 */
export function menuTitleFor(path) {
    for (const group of menuModel.value) {
        for (const item of group.items) {
            if (item.to === path) return item.label;
        }
    }
    return null;
}

export function useWorkspaceTabs() {
    const tabs = computed(() => state.tabs);
    const activePath = computed(() => state.activePath);

    function openTab(path, title) {
        if (!path) return;
        let tab = state.tabs.find((item) => item.path === path);
        if (!tab) {
            tab = { path, title: title || menuTitleFor(path) || path };
            state.tabs.push(tab);
        } else if (title && tab.title !== title) {
            tab.title = title;
        }
        state.activePath = path;
    }

    // 关闭激活页后应跳转的相邻页（右侧优先，其次左侧）；一个不剩时留空（主区显示空白）
    function neighborPath(removedIndex) {
        if (!state.tabs.length) return null;
        const neighbor = state.tabs[removedIndex] ?? state.tabs[removedIndex - 1] ?? state.tabs[state.tabs.length - 1];
        return neighbor.path;
    }

    /** 关闭单个 tab；返回需要跳转的路径（关闭的不是当前页时返回 null）。 */
    function closeTab(path) {
        const index = state.tabs.findIndex((item) => item.path === path);
        if (index === -1) return null;
        const wasActive = state.activePath === path;
        state.tabs.splice(index, 1);
        if (!wasActive) return null;
        const next = neighborPath(index);
        state.activePath = next;
        return next;
    }

    /** 关闭指定页左侧全部；返回需要跳转的路径（当前页被关掉时 = 指定页）。 */
    function closeLeft(path) {
        const index = state.tabs.findIndex((item) => item.path === path);
        if (index <= 0) return null;
        const activeRemoved = state.tabs.slice(0, index).some((item) => item.path === state.activePath);
        state.tabs.splice(0, index);
        if (!activeRemoved) return null;
        state.activePath = path;
        return path;
    }

    /** 关闭指定页右侧全部；返回需要跳转的路径（当前页被关掉时 = 指定页）。 */
    function closeRight(path) {
        const index = state.tabs.findIndex((item) => item.path === path);
        if (index === -1 || index === state.tabs.length - 1) return null;
        const activeRemoved = state.tabs.slice(index + 1).some((item) => item.path === state.activePath);
        state.tabs.splice(index + 1);
        if (!activeRemoved) return null;
        state.activePath = path;
        return path;
    }

    /** 只保留指定页；返回需要跳转的路径（当前页不是指定页时 = 指定页）。 */
    function closeOthers(path) {
        const tab = state.tabs.find((item) => item.path === path);
        if (!tab) return null;
        const activeRemoved = state.activePath !== path;
        state.tabs.splice(0, state.tabs.length, tab);
        state.activePath = path;
        return activeRemoved ? path : null;
    }

    return { tabs, activePath, openTab, closeTab, closeLeft, closeRight, closeOthers };
}
