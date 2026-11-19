<template>
    <section class="scripts-page">
        <header class="scripts-page__header">
            <div>
                <h2>脚本管理</h2>
                <p class="scripts-page__hint">
                    按执行器分页管理脚本与材料；顶部为跨执行器材料总表，下方页签挂载各执行器的脚本管理面板
                </p>
            </div>
            <div class="scripts-page__actions">
                <Button label="刷新" icon="pi pi-refresh" size="small" :loading="loading" @click="reloadAll" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="tabNotice" severity="info" :closable="true" @close="tabNotice = ''">{{ tabNotice }}</Message>

        <Panel header="跨执行器材料">
            <div class="scripts-page__toolbar">
                <Select
                    v-model="executorFilter"
                    :options="executorFilterOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="全部执行器"
                    showClear
                    class="scripts-page__filter"
                    @change="loadMaterials"
                />
                <span class="scripts-page__muted">含通用材料（不归属执行器）</span>
            </div>
            <DataTable
                :value="materials"
                dataKey="material_id"
                :loading="loadingMaterials"
                :rowHover="true"
                scrollable
            >
                <Column field="name" header="名称" sortable style="min-width: 10rem">
                    <template #body="{ data }">{{ data.name || '—' }}</template>
                </Column>
                <Column field="executor_id" header="执行器" sortable style="min-width: 8rem">
                    <template #body="{ data }">
                        <Tag v-if="data.executor_id" :value="data.executor_id" severity="info" />
                        <span v-else class="scripts-page__muted">通用</span>
                    </template>
                </Column>
                <Column field="state" header="状态" sortable style="min-width: 7rem">
                    <template #body="{ data }">
                        <Tag :value="stateText(data.state)" :severity="stateSeverity(data.state)" />
                    </template>
                </Column>
                <Column field="size" header="大小" sortable style="min-width: 6rem">
                    <template #body="{ data }">{{ formatSize(data.size) }}</template>
                </Column>
                <Column field="updated_ts" header="更新时间" sortable style="min-width: 10rem">
                    <template #body="{ data }">{{ formatTime(data.updated_ts) }}</template>
                </Column>
                <template #empty>
                    <span class="scripts-page__muted">暂无材料</span>
                </template>
            </DataTable>
        </Panel>

        <Panel header="执行器脚本面板">
            <p v-if="loadingTabs" class="scripts-page__hint">页签加载中……</p>
            <p v-else-if="!tabs.length" class="scripts-page__hint">
                暂无执行器：能力信封与已注册脚本管理面板均为空（执行器上线或挂载面板后自动出现）
            </p>
            <template v-else>
                <div class="scripts-page__tabs" role="tablist" aria-label="执行器">
                    <Button
                        v-for="tab in tabs"
                        :key="tab.id"
                        :label="tab.id"
                        role="tab"
                        :aria-selected="activeId === tab.id"
                        :severity="activeId === tab.id ? 'primary' : 'secondary'"
                        :outlined="activeId !== tab.id"
                        size="small"
                        @click="activeId = tab.id"
                    />
                </div>
                <p v-if="activeInfo && (activeInfo.summary || activeInfo.version)" class="scripts-page__hint">
                    {{ activeInfo.summary || activeInfo.id }}
                    <template v-if="activeInfo.version"> · v{{ activeInfo.version }}</template>
                </p>
                <div
                    v-for="tab in tabs"
                    v-show="tab.id === activeId"
                    :key="`outlet-${tab.id}`"
                    class="scripts-page__tab-body"
                    role="tabpanel"
                >
                    <!-- tab = slot 贡献 ∪ 能力信封（M2）：每个执行器 id 都开槽，
                         空槽由 PluginUiOutlet 的 <slot/> 兜底占位 -->
                    <PluginUiOutlet :name="`scripts:${tab.id}`">
                        <span class="scripts-page__muted">该执行器未提供脚本管理面板</span>
                    </PluginUiOutlet>
                </div>
            </template>
        </Panel>
    </section>
</template>

<script>
// 与宿主 src/service/backend.js 的 `aetp.backendUrl` 约定一致（后端地址切换）；
// 插件 SFC 不能 import 宿主模块 / npm 包，故这里直接读同一个 localStorage 键。
const STORAGE_KEY = 'aetp.backendUrl';

function apiBase() {
    try {
        return window.localStorage.getItem(STORAGE_KEY) || '';
    } catch {
        return '';
    }
}

function apiUrl(path) {
    return `${apiBase()}${path}`;
}

// 插件 SFC 拿不到宿主模块，令牌直接读宿主约定的 localStorage 键
// （与 master/webui/frontend/src/service/backend.js 的 TOKEN_STORAGE_KEY 同步）
function authHeaders() {
    let token = '';
    try {
        token = window.localStorage.getItem('aetp.authToken') || '';
    } catch {
        token = '';
    }
    return token ? { 'X-AETP-Token': token } : {};
}

async function fetchJson(url, options) {
    // 先判 ok 再解析：非 JSON 错误页（网关 5xx 等）不炸成「语法错误」
    const response = await fetch(url, {
        ...(options || {}),
        headers: { ...((options && options.headers) || {}), ...authHeaders() }
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
        const payload = data && data.error ? data.error : '';
        const message = typeof payload === 'string' ? payload : payload ? payload.message || String(payload) : '';
        throw new Error(message || `请求失败 (${response.status})`);
    }
    return data;
}

const MATERIAL_STATE_TEXT = { uploaded: '待分析', analyzing: '分析中', analyzed: '已分析', failed: '失败' };

export default {
    data() {
        return {
            materials: [],
            loadingMaterials: false,
            executorFilter: null,
            slotIds: [],
            capabilities: [],
            loadingTabs: false,
            tabNotice: '',
            activeId: '',
            error: '',
            loading: false
        };
    },
    computed: {
        // tab 清单 = slot 贡献 ∪ 能力信封（评审 M2）：离线时已注册面板仍可管理，
        // 能力信息（summary/version）仅增强显示
        tabs() {
            const byId = new Map();
            for (const id of this.slotIds) {
                if (id) byId.set(id, { id, summary: '', version: '' });
            }
            for (const cap of this.capabilities) {
                const id = String((cap && cap.id) || '');
                if (!id) continue;
                const existing = byId.get(id) || { id, summary: '', version: '' };
                byId.set(id, {
                    id,
                    summary: (cap.summary || existing.summary || '') + '',
                    version: (cap.version || existing.version || '') + ''
                });
            }
            return [...byId.values()].sort((left, right) => left.id.localeCompare(right.id));
        },
        activeInfo() {
            return this.tabs.find((tab) => tab.id === this.activeId) || null;
        },
        executorFilterOptions() {
            return [{ label: '全部执行器', value: '' }, ...this.tabs.map((tab) => ({ label: tab.id, value: tab.id }))];
        }
    },
    watch: {
        tabs: {
            immediate: true,
            handler(list) {
                if (list.length && !list.some((tab) => tab.id === this.activeId)) {
                    this.activeId = list[0].id;
                }
                if (!list.length) this.activeId = '';
            }
        }
    },
    mounted() {
        this.reloadAll();
    },
    methods: {
        async reloadAll() {
            this.loading = true;
            try {
                await Promise.all([this.loadTabs(), this.loadMaterials()]);
            } finally {
                this.loading = false;
            }
        },
        async loadTabs() {
            // 能力信封 + webui 清单双源：任一失败不砸页面，降级提示后用另一源
            this.loadingTabs = true;
            const notices = [];
            try {
                const data = await fetchJson(apiUrl('/api/heartbeat/capabilities?kind=executor'));
                this.capabilities = (data.capabilities || []).filter((item) => item && item.kind === 'executor');
            } catch (reason) {
                this.capabilities = [];
                notices.push(`能力信封加载失败（${this.reasonText(reason)}），仅显示已注册面板`);
            }
            try {
                const manifest = await fetchJson(apiUrl('/api/web/ui'));
                this.slotIds = (Array.isArray(manifest) ? manifest : [])
                    .filter((item) => item && item.kind === 'slot' && String(item.target || '').startsWith('scripts:'))
                    .map((item) => String(item.target).slice('scripts:'.length))
                    .filter(Boolean);
            } catch (reason) {
                this.slotIds = [];
                notices.push(`UI 清单加载失败（${this.reasonText(reason)}），仅显示能力信封中的执行器`);
            }
            this.tabNotice = notices.join('；');
            this.loadingTabs = false;
        },
        async loadMaterials() {
            // 跨执行器材料表：executor 过滤谓词 ==X OR =''（通用材料恒可见，§4.5）
            this.loadingMaterials = true;
            try {
                const query = new URLSearchParams({ limit: '200' });
                if (this.executorFilter) query.set('executor', this.executorFilter);
                const data = await fetchJson(apiUrl(`/api/test/materials?${query.toString()}`));
                this.materials = data.materials || [];
                this.error = '';
            } catch (reason) {
                this.error = `材料列表加载失败：${this.reasonText(reason)}`;
            } finally {
                this.loadingMaterials = false;
            }
        },
        reasonText(reason) {
            return String((reason && reason.message) || reason);
        },
        stateText(state) {
            return MATERIAL_STATE_TEXT[state] || state || '—';
        },
        stateSeverity(state) {
            return { uploaded: 'info', analyzing: 'warn', analyzed: 'success', failed: 'danger' }[state] || 'secondary';
        },
        formatSize(size) {
            const value = Number(size);
            if (!(value >= 0)) return '—';
            if (value < 1024) return `${value} B`;
            if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
            return `${(value / 1024 / 1024).toFixed(1)} MB`;
        },
        formatTime(value) {
            if (!value) return '—';
            return new Date(value * 1000).toLocaleString();
        }
    }
};
</script>

<style scoped>
/* 插件 SFC 用不了运行时 Tailwind：仅保留布局所需的少量样式，
   组件外观全部来自共享 PrimeVue 组件与宿主语义变量 */
.scripts-page {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.scripts-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.scripts-page__header h2 {
    margin: 0;
}

.scripts-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.scripts-page__muted {
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.scripts-page__actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    white-space: nowrap;
}

.scripts-page__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
}

.scripts-page__filter {
    min-width: 12rem;
}

.scripts-page__tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin-bottom: 0.75rem;
}

.scripts-page__tab-body {
    min-height: 2.5rem;
}
</style>
