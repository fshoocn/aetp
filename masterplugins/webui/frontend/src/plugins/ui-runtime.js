import * as Vue from 'vue';
import { h, onBeforeUnmount, shallowRef } from 'vue';
import router from '@/router';
import { pluginUiState } from './ui-state';

const scriptDisposers = [];
const moduleCache = Object.assign(Object.create(null), { vue: Vue });
const styleCache = new Map();
let runtimeSlotSequence = 0;
let loaderModule;

async function loadSfc(entry, options) {
    loaderModule ??= import('vue3-sfc-loader');
    const { loadModule } = await loaderModule;
    return loadModule(entry, options);
}

function createLoaderOptions(contribution, styles = []) {
    return {
        moduleCache,
        async getFile(url) {
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`无法加载插件 UI 资源 ${url}: ${response.status}`);
            }
            return {
                getContentData: (asBinary) => (asBinary ? response.arrayBuffer() : response.text())
            };
        },
        addStyle(textContent, scopeId) {
            const cachedStyles = styleCache.get(contribution.id) || [];
            const compiledStyle = { textContent, scopeId };
            cachedStyles.push(compiledStyle);
            styleCache.set(contribution.id, cachedStyles);
            styles.push(attachStyle(contribution.id, compiledStyle));
        }
    };
}

function attachStyle(contributionId, compiledStyle) {
    const style = document.createElement('style');
    style.textContent = compiledStyle.textContent;
    style.dataset.pluginUi = contributionId;
    if (compiledStyle.scopeId) style.dataset.scopeId = compiledStyle.scopeId;
    document.head.append(style);
    return style;
}

export async function initializePluginUi() {
    const response = await fetch('/api/web/ui');
    if (!response.ok) {
        throw new Error(`获取插件 UI 清单失败: ${response.status}`);
    }

    pluginUiState.contributions = await response.json();
    for (const contribution of pluginUiState.contributions) {
        if (contribution.kind === 'page') {
            registerPage(contribution);
        }
    }

    for (const contribution of pluginUiState.contributions) {
        if (contribution.kind === 'script') {
            await activateScript(contribution);
        }
    }
}

function registerPage(contribution) {
    const routeName = `plugin-ui-${encodeURIComponent(contribution.id)}`;
    router.addRoute('app-shell', {
        path: contribution.path,
        name: routeName,
        meta: { pluginUi: true, title: contribution.title },
        component: {
            setup() {
                return () => h(PluginLoadedSfc, { contribution });
            }
        }
    });
}

async function activateScript(contribution) {
    try {
        const module = await import(/* @vite-ignore */ contribution.entry);
        if (typeof module.activate !== 'function') {
            throw new TypeError(`插件脚本 ${contribution.id} 必须导出 activate(context)`);
        }

        const dispose = await module.activate({
            api: {
                request: (url, options) => fetch(url, options)
            },
            router,
            slots: {
                mountHtml(target, html) {
                    return mountRuntimeSlot(contribution, target, 'html', String(html));
                },
                mountVue(target, entry) {
                    return mountRuntimeSlot(contribution, target, 'vue', String(entry));
                },
                update(target, value) {
                    pluginUiState.scriptValues[`${contribution.id}:${target}`] = {
                        contributionId: contribution.id,
                        target,
                        value: String(value ?? '')
                    };
                },
                clear(target) {
                    delete pluginUiState.scriptValues[`${contribution.id}:${target}`];
                }
            }
        });

        if (typeof dispose === 'function') {
            scriptDisposers.push(dispose);
        }
    } catch (error) {
        console.error(`插件 UI 脚本加载失败: ${contribution.id}`, error);
    }
}

function mountRuntimeSlot(contribution, target, format, value) {
    if (!target) throw new TypeError('必须指定插件 UI outlet 名称');
    const id = `${contribution.id}:runtime-${++runtimeSlotSequence}`;
    pluginUiState.runtimeSlots.push({
        id,
        kind: 'slot',
        format,
        owner: contribution.owner,
        target,
        order: 0,
        ...(format === 'html' ? { content: value } : { entry: value })
    });
    return () => {
        pluginUiState.runtimeSlots = pluginUiState.runtimeSlots.filter((item) => item.id !== id);
    };
}

export function createLoadedSfc() {
    return {
        props: { contribution: { type: Object, required: true } },
        setup(props) {
            const component = shallowRef(null);
            const error = shallowRef(null);
            const styles = [];
            let disposed = false;

            for (const compiledStyle of styleCache.get(props.contribution.id) || []) {
                styles.push(attachStyle(props.contribution.id, compiledStyle));
            }

            loadSfc(props.contribution.entry, createLoaderOptions(props.contribution, styles))
                .then((loaded) => {
                    if (disposed) styles.forEach((style) => style.remove());
                    else component.value = loaded;
                })
                .catch((reason) => {
                    if (disposed) styles.forEach((style) => style.remove());
                    error.value = reason;
                    console.error(`插件 Vue 页面加载失败: ${props.contribution.id}`, reason);
                });

            onBeforeUnmount(() => {
                disposed = true;
                styles.forEach((style) => style.remove());
            });

            return () => {
                if (component.value) return h(component.value);
                if (error.value) return h('div', { class: 'plugin-ui-error' }, '插件界面加载失败');
                return h('div', { class: 'plugin-ui-loading', role: 'status' }, '正在加载插件界面…');
            };
        }
    };
}

export const PluginLoadedSfc = createLoadedSfc();

export function shutdownPluginUi() {
    for (const dispose of scriptDisposers.splice(0).reverse()) {
        try {
            dispose();
        } catch (error) {
            console.error('插件 UI 清理失败', error);
        }
    }
    for (const contribution of pluginUiState.contributions) {
        const routeName = `plugin-ui-${encodeURIComponent(contribution.id)}`;
        if (router.hasRoute(routeName)) router.removeRoute(routeName);
    }
    document.querySelectorAll('style[data-plugin-ui]').forEach((style) => style.remove());
    styleCache.clear();
    pluginUiState.contributions = [];
    pluginUiState.runtimeSlots = [];
    pluginUiState.scriptValues = {};
}

if (import.meta.hot) {
    import.meta.hot.dispose(shutdownPluginUi);
}
