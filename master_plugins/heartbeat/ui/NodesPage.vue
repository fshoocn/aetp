<template>
    <section class="nodes-page">
        <header class="nodes-page__header">
            <div>
                <h2>从模块管理</h2>
                <p class="nodes-page__hint">按「最后心跳时间 + 超时阈值」判定在线状态；点击行查看状态历史</p>
            </div>
            <div class="nodes-page__actions">
                <Button :label="auto ? '暂停自动刷新' : '恢复自动刷新'" severity="secondary" outlined size="small" @click="toggleAuto" />
                <Button label="立即刷新" icon="pi pi-refresh" size="small" :loading="loading" @click="load()" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>

        <DataTable
            :value="rows"
            dataKey="node_id"
            :filters="filters"
            :globalFilterFields="['node_id', 'node_kind', 'status', 'address', 'last_seen_text']"
            filterDisplay="row"
            :loading="loading"
            :rowHover="true"
            scrollable
            selectionMode="single"
            v-model:selection="selected"
            @row-select="onRowSelect"
        >
            <template #header>
                <div class="nodes-page__toolbar">
                    <IconField>
                        <InputIcon><i class="pi pi-search" /></InputIcon>
                        <InputText v-model="filters.global.value" placeholder="搜索节点 / 类型 / 地址…" />
                    </IconField>
                </div>
            </template>
            <Column field="node_id" header="节点" sortable filterMatchMode="contains" style="min-width: 8rem">
                <template #filter="{ filterModel }">
                    <InputText v-model="filterModel.value" placeholder="搜索" class="nodes-page__filter" />
                </template>
            </Column>
            <Column field="node_kind" header="类型" sortable filterMatchMode="contains" style="min-width: 9rem">
                <template #body="{ data }">{{ data.node_kind || '—' }}</template>
                <template #filter="{ filterModel }">
                    <InputText v-model="filterModel.value" placeholder="搜索" class="nodes-page__filter" />
                </template>
            </Column>
            <Column field="status" header="状态" sortable filterMatchMode="equals" style="min-width: 8rem">
                <template #body="{ data }">
                    <Tag :value="statusText(data.status)" :severity="statusSeverity(data.status)" />
                </template>
                <template #filter="{ filterModel }">
                    <Select v-model="filterModel.value" :options="statusFilterOptions" optionLabel="label" optionValue="value" placeholder="全部" showClear class="nodes-page__filter" />
                </template>
            </Column>
            <Column field="address" header="地址" sortable filterMatchMode="contains" style="min-width: 10rem">
                <template #body="{ data }">{{ data.address || '—' }}</template>
                <template #filter="{ filterModel }">
                    <InputText v-model="filterModel.value" placeholder="搜索" class="nodes-page__filter" />
                </template>
            </Column>
            <Column field="last_seen" filterField="last_seen_text" header="最近心跳" sortable filterMatchMode="contains" style="min-width: 10rem">
                <template #body="{ data }">{{ data.last_seen_text }}</template>
                <template #filter="{ filterModel }">
                    <InputText v-model="filterModel.value" placeholder="搜索" class="nodes-page__filter" />
                </template>
            </Column>
            <Column field="missed" header="错过周期" sortable filterMatchMode="contains" style="min-width: 8rem">
                <template #filter="{ filterModel }">
                    <InputText v-model="filterModel.value" placeholder="搜索" class="nodes-page__filter" />
                </template>
            </Column>
            <Column field="seq" header="seq" sortable filterMatchMode="contains" style="min-width: 7rem">
                <template #filter="{ filterModel }">
                    <InputText v-model="filterModel.value" placeholder="搜索" class="nodes-page__filter" />
                </template>
            </Column>
            <Column header="操作" frozen alignFrozen="right" style="min-width: 12rem">
                <template #body="{ data }">
                    <div class="nodes-page__actions" @click.stop>
                        <Button v-if="data.address" label="打开" icon="pi pi-external-link" size="small" severity="secondary" text @click="openAddress(data.address)" />
                        <Button label="日志" icon="pi pi-file" size="small" severity="secondary" text @click="openLogs(data)" />
                        <Button label="移除" icon="pi pi-trash" size="small" severity="danger" text @click="removeNode(data.node_id)" />
                    </div>
                </template>
            </Column>
            <template #empty>
                <span class="nodes-page__muted">暂无从模块上报</span>
            </template>
        </DataTable>

        <Panel v-if="detail" :header="`${detail.node.node_id} · 详情`">
            <p class="nodes-page__hint">
                首次上线 {{ formatTime(detail.node.first_seen) }}，能力
                {{ detail.node.caps_count }} 项、未识别 {{ detail.node.unparsed_count }} 项
            </p>

            <h4>状态历史</h4>
            <Timeline v-if="detail.history.length" :value="detail.history">
                <template #content="slotProps">
                    <span class="nodes-page__time">{{ formatTime(slotProps.item.at) }}</span>
                    <span>{{ describeHistory(slotProps.item) }}</span>
                </template>
            </Timeline>
            <p v-else class="nodes-page__muted">暂无状态转换</p>

            <template v-if="detail.unparsed.length">
                <h4>未识别能力条目</h4>
                <pre class="nodes-page__raw">{{ pretty(detail.unparsed) }}</pre>
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
        const message = data && data.error ? data.error.message || String(data.error) : '';
        throw new Error(message || `请求失败 (${response.status})`);
    }
    return data;
}

const STATUS_TEXT = { online: '在线', offline: '离线', lost: '失联' };

export default {
    data() {
        return {
            nodes: [],
            detail: null,
            error: '',
            auto: true,
            loading: false,
            selected: null,
            timer: null,
            disposed: false,
            statusFilterOptions: [
                { label: '在线', value: 'online' },
                { label: '离线', value: 'offline' },
                { label: '失联', value: 'lost' }
            ],
            // DataTable 的 filters 是「字段 → { value, matchMode }」元数据
            filters: {
                global: { value: null, matchMode: 'contains' },
                node_id: { value: null, matchMode: 'contains' },
                node_kind: { value: null, matchMode: 'contains' },
                status: { value: null, matchMode: 'equals' },
                address: { value: null, matchMode: 'contains' },
                last_seen_text: { value: null, matchMode: 'contains' },
                missed: { value: null, matchMode: 'contains' },
                seq: { value: null, matchMode: 'contains' }
            }
        };
    },
    computed: {
        rows() {
            // 展示行：补一个格式化后的最近心跳文本，供筛选用（排序仍按原始时间戳）
            return this.nodes.map((node) => ({ ...node, last_seen_text: this.formatTime(node.last_seen) }));
        }
    },
    mounted() {
        this.load();
        this.schedulePoll();
    },
    beforeUnmount() {
        this.disposed = true;
        if (this.timer) window.clearTimeout(this.timer);
    },
    methods: {
        async load(silent) {
            // 后台轮询（silent）不触发加载遮罩 / 转圈，仅首次进入与手动刷新显示
            if (!silent) this.loading = true;
            try {
                const data = await fetchJson(apiUrl('/api/heartbeat/nodes'));
                this.nodes = data.nodes || [];
                this.error = '';
                if (this.detail) await this.showDetail(this.detail.node.node_id, true);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.loading = false;
            }
        },
        async showDetail(nodeId, silent) {
            try {
                this.detail = await fetchJson(apiUrl(`/api/heartbeat/nodes/${encodeURIComponent(nodeId)}`));
            } catch (reason) {
                if (!silent) this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        removeNode(nodeId) {
            // 插件 SFC 拿不到 useConfirm，走宿主 app.use(ConfirmationService) 装的 $confirm
            this.$confirm.require({
                header: '移除节点',
                message: `确认移除从模块「${nodeId}」的记录？`,
                icon: 'pi pi-exclamation-triangle',
                acceptLabel: '移除',
                rejectLabel: '取消',
                accept: async () => {
                    try {
                        await fetchJson(apiUrl(`/api/heartbeat/nodes/${encodeURIComponent(nodeId)}`), { method: 'DELETE' });
                        if (this.detail && this.detail.node.node_id === nodeId) this.detail = null;
                        await this.load();
                    } catch (reason) {
                        this.error = String(reason && reason.message ? reason.message : reason);
                    }
                }
            });
        },
        schedulePoll() {
            // 超时链轮询：上一拍收完才排下一拍，慢请求不会叠加成轮询风暴
            this.timer = window.setTimeout(async () => {
                if (this.auto && !this.disposed) await this.load(true);
                if (!this.disposed) this.schedulePoll();
            }, 5000);
        },
        toggleAuto() {
            this.auto = !this.auto;
        },
        onRowSelect(event) {
            this.showDetail(event.data.node_id);
        },
        openAddress(address) {
            window.open(address, '_blank', 'noopener');
        },
        openLogs(data) {
            // 深链（日志体验 A）：预填节点 + 最近心跳前 5 分钟
            const lastSeen = Number(data.last_seen) || Date.now() / 1000;
            const query = new URLSearchParams({
                node: String(data.node_id),
                from: String(Math.floor(lastSeen - 300)),
                to: String(Math.floor(lastSeen + 60))
            });
            const url = `/logs?${query.toString()}`;
            if (this.$router && typeof this.$router.push === 'function') {
                this.$router.push(url);
            } else {
                window.location.assign(url);
            }
        },
        statusSeverity(status) {
            return { online: 'success', offline: 'warn', lost: 'danger' }[status] || 'secondary';
        },
        statusText(status) {
            return STATUS_TEXT[status] || status;
        },
        formatTime(value) {
            if (!value) return '—';
            return new Date(value * 1000).toLocaleString();
        },
        describeHistory(entry) {
            if (entry.kind === 'status') {
                return `状态 ${this.statusText(entry.from)} → ${this.statusText(entry.to)}`;
            }
            if (entry.kind === 'address') return `地址 ${entry.from} → ${entry.to}`;
            return entry.message || entry.kind;
        },
        pretty(value) {
            return JSON.stringify(value, null, 2);
        }
    }
};
</script>

<style scoped>
/* 插件 SFC 用不了运行时 Tailwind：仅保留布局所需的少量样式，
   组件外观全部来自共享 PrimeVue 组件与宿主语义变量（见开发文档 §24） */
.nodes-page {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.nodes-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.nodes-page__header h2 {
    margin: 0;
}

.nodes-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.nodes-page__muted {
    color: var(--text-color-secondary);
}

.nodes-page__actions {
    display: flex;
    gap: 0.375rem;
    white-space: nowrap;
}

.nodes-page__toolbar {
    display: flex;
    gap: 0.5rem;
}

.nodes-page__filter {
    width: 100%;
}

.nodes-page__time {
    display: inline-block;
    margin-right: 0.75rem;
    color: var(--text-color-secondary);
    font-variant-numeric: tabular-nums;
}

.nodes-page__raw {
    max-height: 16rem;
    margin: 0;
    overflow: auto;
    padding: 0.75rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    font-size: 0.75rem;
}
</style>
