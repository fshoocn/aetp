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
                <Button
                    label="上传脚本"
                    icon="pi pi-upload"
                    size="small"
                    @mouseenter="showUploadMenu"
                    @click="toggleUploadMenu"
                />
                <PvMenu ref="uploadMenu" :model="uploadMenuItems" popup @show="menuOpen = true" @hide="menuOpen = false" />
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
                <Column header="操作" style="min-width: 9rem">
                    <template #body="{ data }">
                        <div class="scripts-page__row-actions">
                            <Button label="下载" icon="pi pi-download" size="small" severity="secondary" text @click="downloadMaterial(materialIdOf(data))" />
                            <Button label="删除" icon="pi pi-trash" size="small" severity="danger" text @click="deleteMaterial(materialIdOf(data))" />
                        </div>
                    </template>
                </Column>
                <template #empty>
                    <span class="scripts-page__muted">暂无材料</span>
                </template>
            </DataTable>
        </Panel>

        <!-- 执行器上传 UI 按需弹出（需求：上传脚本按钮 → 悬浮菜单选执行器 → 弹窗）；
             内容仍经 slot 挂载各执行器面板（scripts:<id>），空槽兜底占位 -->
        <PvDialog
            v-model:visible="uploadDialogVisible"
            modal
            :header="`上传脚本 · ${uploadTarget}`"
            :style="{ width: '90vw', maxWidth: '1200px' }"
        >
            <div v-if="uploadTarget" :key="uploadTarget" class="scripts-page__dialog-body">
                <PluginUiOutlet :name="`scripts:${uploadTarget}`">
                    <span class="scripts-page__muted">该执行器未提供脚本管理面板</span>
                </PluginUiOutlet>
            </div>
        </PvDialog>
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
            menuOpen: false,
            uploadDialogVisible: false,
            uploadTarget: '',
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
        // 「上传脚本」悬浮菜单：执行器类型 = slot 贡献 ∪ 能力信封（与原页签同源）
        uploadMenuItems() {
            if (!this.tabs.length) return [{ label: '暂无执行器', disabled: true }];
            return this.tabs.map((tab) => ({
                label: tab.summary ? `${tab.id} — ${tab.summary}` : tab.id,
                icon: 'pi pi-box',
                command: () => this.openUploadDialog(tab.id)
            }));
        },
        executorFilterOptions() {
            return [{ label: '全部执行器', value: '' }, ...this.tabs.map((tab) => ({ label: tab.id, value: tab.id }))];
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
        showUploadMenu(event) {
            // 悬浮即弹出执行器类型菜单（PrimeVue Menu popup 仅暴露 toggle）
            if (this.menuOpen) return;
            this.$refs.uploadMenu && this.$refs.uploadMenu.toggle(event);
            this.menuOpen = true;
        },
        toggleUploadMenu(event) {
            this.$refs.uploadMenu && this.$refs.uploadMenu.toggle(event);
            this.menuOpen = !this.menuOpen;
        },
        openUploadDialog(executorId) {
            this.uploadTarget = String(executorId || '');
            if (this.uploadTarget) this.uploadDialogVisible = true;
        },
        materialIdOf(row) {
            return String((row && (row.material_id || row.id)) || '');
        },
        async downloadMaterial(materialId) {
            // 材料生命周期管理归 test_core 总表（§7.2 裁决）：<a> 带不上鉴权头，走 fetch blob
            if (!materialId) return;
            try {
                const response = await fetch(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}/download`), {
                    headers: authHeaders()
                });
                if (!response.ok) throw new Error(`下载失败 (${response.status})`);
                const blob = await response.blob();
                const link = document.createElement('a');
                link.href = URL.createObjectURL(blob);
                link.download = `${materialId}.zip`;
                link.click();
                URL.revokeObjectURL(link.href);
            } catch (reason) {
                this.error = `下载失败：${this.reasonText(reason)}`;
            }
        },
        deleteMaterial(materialId) {
            // 删除是材料生命周期语义（引用保护 / 级联清理在 core 服务面）：
            // 统一收口到本总表，执行器面板不放删除入口
            if (!materialId) return;
            this.$confirm.require({
                header: '删除材料',
                message: `确认删除材料「${materialId}」？用例清单与选择集一并删除。`,
                icon: 'pi pi-exclamation-triangle',
                acceptLabel: '删除',
                rejectLabel: '取消',
                accept: async () => {
                    try {
                        await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}`), { method: 'DELETE' });
                        this.error = '';
                        await this.loadMaterials();
                    } catch (reason) {
                        const text = this.reasonText(reason);
                        this.error = text.includes('material_in_use') || text.includes('引用')
                            ? `引用冲突：${text}——被任务 / 计划 / 选择集引用的材料禁止删除，请先解除引用`
                            : `删除失败：${text}`;
                    }
                }
            });
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

.scripts-page__row-actions {
    display: flex;
    align-items: center;
    gap: 0.125rem;
    flex-wrap: wrap;
}

.scripts-page__dialog-body {
    min-width: 0;
    max-height: 75vh;
    overflow: auto;
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
