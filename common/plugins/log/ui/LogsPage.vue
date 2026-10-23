<template>
    <section class="logs-page">
        <header class="logs-page__header">
            <div>
                <h2>日志</h2>
                <p class="logs-page__hint">主从全链路日志（统一时间轴）；尾随模式自动追新，上翻即暂停</p>
            </div>
            <div class="logs-page__actions">
                <Button :label="tail ? '暂停尾随' : '恢复尾随'" severity="secondary" outlined size="small" @click="toggleTail" />
                <Button label="删除…" severity="danger" outlined size="small" :disabled="!rows.length" @click="deleteFiltered" />
                <Button label="导出 .log" severity="secondary" outlined size="small" :disabled="!rows.length" @click="exportLog" />
                <Button label="立即刷新" icon="pi pi-refresh" size="small" :loading="loading" @click="reload" />
            </div>
        </header>

        <div class="logs-page__filters">
            <Select v-model="node" :options="nodeOptions" optionLabel="label" optionValue="value" placeholder="全部节点" showClear class="logs-page__filter" @change="reload" />
            <Select v-model="level" :options="levelOptions" optionLabel="label" optionValue="value" placeholder="全部级别" showClear class="logs-page__filter" @change="reload" />
            <Select v-model="window" :options="windowOptions" optionLabel="label" optionValue="value" class="logs-page__filter" @change="onWindowChange" />
            <template v-if="window === 'custom'">
                <input v-model="fromLocal" type="datetime-local" class="logs-page__filter" aria-label="起始时间" @change="onCustomRange" />
                <input v-model="toLocal" type="datetime-local" class="logs-page__filter" aria-label="结束时间" @change="onCustomRange" />
            </template>
            <IconField class="logs-page__search">
                <InputIcon><i class="pi pi-search" /></InputIcon>
                <InputText v-model="q" placeholder="关键字…" @keydown.enter="reload" />
            </IconField>
        </div>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>

        <div class="logs-page__scroll" ref="scroller" @scroll="onScroll">
            <DataTable :value="rows" dataKey="id" v-model:expandedRows="expandedRows" :rowClass="rowClass" :rowHover="true" size="small" @row-click="onRowClick">
                <Column expander style="width: 3rem" />
                <Column header="时间" style="width: 8rem">
                    <template #body="{ data }">
                        <span class="logs-page__time" :title="absolute(data.ts)">{{ relative(data.ts) }}</span>
                    </template>
                </Column>
                <Column field="node" header="节点" style="width: 9rem" />
                <Column header="级别" style="width: 5.5rem">
                    <template #body="{ data }">
                        <Tag :value="levelName(data.level)" :severity="levelSeverity(data.level)" />
                    </template>
                </Column>
                <Column header="来源" style="width: 13rem">
                    <template #body="{ data }">
                        <div class="logs-page__source" :title="data.source || ''">
                            {{ data.name }}
                            <span v-if="data.source" class="logs-page__site">{{ data.source }}</span>
                        </div>
                    </template>
                </Column>
                <Column header="信息">
                    <template #body="{ data }">
                        <Button v-if="data.op" class="logs-page__op" size="small" severity="secondary" outlined :label="data.op" title="查看本次操作" @click.stop="filterByOperation(data.op)" />
                        <span v-html="highlight(data.msg)"></span>
                    </template>
                </Column>
                <template #expansion="{ data }">
                    <div class="logs-page__detail">
                        <div class="logs-page__detail-meta">
                            <span><strong>时间：</strong>{{ absolute(data.ts) }}</span>
                            <span><strong>节点：</strong>{{ data.node }}</span>
                            <span><strong>来源：</strong>{{ data.name }}（{{ levelName(data.level) }}）</span>
                            <span v-if="data.source"><strong>调用点：</strong>{{ data.source }}</span>
                            <span v-if="data.op"><strong>操作：</strong>{{ data.op }}</span>
                        </div>
                        <pre class="logs-page__detail-msg">{{ data.msg }}</pre>
                        <div v-if="around && around.id === data.id" class="logs-page__around">
                            <div class="logs-page__around-head">
                                前后文（{{ around.scope === 'node' ? around.node : '全部节点' }}）
                                <Button label="收起前后文" severity="secondary" text size="small" @click="around = null" />
                            </div>
                            <div v-for="row in around.rows" :key="row.id" class="logs-page__around-row" :class="{ 'logs-page__around-row--anchor': row.id === around.id }">
                                <span class="logs-page__time">{{ absolute(row.ts) }}</span>
                                <span class="logs-page__around-node">{{ row.node }}</span>
                                <Tag :value="levelName(row.level)" :severity="levelSeverity(row.level)" />
                                <span v-html="highlight(row.msg)"></span>
                            </div>
                        </div>
                        <div class="logs-page__detail-actions">
                            <Button v-if="data.op" label="查看本次操作" severity="secondary" outlined size="small" @click="filterByOperation(data.op)" />
                            <Button label="前后文（本节点）" severity="secondary" outlined size="small" @click="loadAround(data, 'node')" />
                            <Button label="前后文（全部节点）" severity="secondary" outlined size="small" @click="loadAround(data, 'all')" />
                            <Button label="复制" severity="secondary" outlined size="small" @click="copyMessage(data)" />
                            <Button label="收起" severity="secondary" text size="small" @click="toggleDetail(data)" />
                        </div>
                    </div>
                </template>
                <template #empty>
                    <span class="logs-page__empty">暂无日志</span>
                </template>
            </DataTable>
        </div>

        <Button v-if="pendingNew > 0 && !follow" class="logs-page__jump" severity="primary" :label="`↓ ${pendingNew} 条新日志`" @click="jumpToLatest" />
    </section>
</template>

<script>
// 与宿主 src/service/backend.js 的 localStorage 约定一致（插件 SFC 不能 import 宿主模块）
const STORAGE_KEY = 'aetp.backendUrl';
const TOKEN_STORAGE_KEY = 'aetp.authToken';

function apiBase() {
    try {
        return window.localStorage.getItem(STORAGE_KEY) || '';
    } catch {
        return '';
    }
}

function authHeaders() {
    let token = '';
    try {
        token = window.localStorage.getItem(TOKEN_STORAGE_KEY) || '';
    } catch {
        token = '';
    }
    return token ? { 'X-AETP-Token': token } : {};
}

async function fetchJson(url) {
    const response = await fetch(url, { headers: authHeaders() });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
        const message = data && data.error ? data.error.message || String(data.error) : '';
        throw new Error(message || `请求失败 (${response.status})`);
    }
    return data;
}

const LEVELS = [
    { label: 'error 及以上', value: 0 },
    { label: 'warn 及以上', value: 1 },
    { label: 'info 及以上', value: 2 },
    { label: '全部（含 debug）', value: 3 }
];

const WINDOWS = [
    { label: '最近 5 分钟', value: 300 },
    { label: '最近 15 分钟', value: 900 },
    { label: '最近 1 小时', value: 3600 },
    { label: '今天', value: 'today' },
    { label: '自定义', value: 'custom' },
    { label: '全部', value: 0 }
];

// epoch 秒 ↔ <input type="datetime-local"> 值（本地时区）
function toLocalInput(epoch) {
    const date = new Date(Number(epoch) * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalInput(value) {
    const ms = new Date(String(value)).getTime();
    return Number.isFinite(ms) ? ms / 1000 : null;
}

function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

export default {
    data() {
        return {
            rows: [],
            node: null,
            level: null,
            q: '',
            window: 3600,
            fromLocal: '',
            toLocal: '',
            around: null,
            tail: true,
            follow: true,
            pendingNew: 0,
            loading: false,
            error: '',
            nodes: [],
            // DataTable 行展开（详情行）：设了 dataKey 时 expandedRows 是
            // 「行 key → true」的对象映射（展开箭头内部切换也用该形式），单行展开
            expandedRows: {},
            timer: null,
            disposed: false
        };
    },
    computed: {
        levelOptions() {
            return LEVELS;
        },
        windowOptions() {
            return WINDOWS;
        },
        nodeOptions() {
            return this.nodes.map((name) => ({ label: name, value: name }));
        }
    },
    created() {
        this.readDeepLink();
        this.loadNodes();
        this.reload();
        this.timer = window.setInterval(() => this.tick(), 3000);
    },
    unmounted() {
        this.disposed = true;
        if (this.timer) {
            window.clearInterval(this.timer);
        }
    },
    methods: {
        // ---- 深链（体验 A）：URL 承载过滤状态，外部可直达 ----
        readDeepLink() {
            const params = new URLSearchParams(window.location.search);
            this.node = params.get('node') || null;
            const levelRaw = params.get('level');
            this.level = levelRaw === null ? null : Number(levelRaw);
            this.q = params.get('q') || '';
            const from = params.get('from');
            const to = params.get('to');
            if (from || to) {
                this.window = 'custom';
                this.fromLocal = from ? toLocalInput(Number(from)) : '';
                this.toLocal = to ? toLocalInput(Number(to)) : '';
            }
        },
        writeDeepLink() {
            const params = new URLSearchParams();
            if (this.node) params.set('node', this.node);
            if (this.level !== null && this.level !== undefined) params.set('level', String(this.level));
            if (this.q) params.set('q', this.q);
            if (this.window === 'custom') {
                const from = fromLocalInput(this.fromLocal);
                const to = fromLocalInput(this.toLocal);
                if (from !== null) params.set('from', String(Math.floor(from)));
                if (to !== null) params.set('to', String(Math.floor(to)));
            }
            const query = params.toString();
            window.history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
        },
        buildQuery(extra) {
            const params = new URLSearchParams();
            if (this.node) params.set('node', this.node);
            if (this.level !== null && this.level !== undefined) params.set('level', String(this.level));
            if (this.q) params.set('q', this.q);
            const windowValue = this.window;
            if (windowValue === 'custom') {
                const from = fromLocalInput(this.fromLocal);
                const to = fromLocalInput(this.toLocal);
                if (from !== null) params.set('from_ts', String(from));
                if (to !== null) params.set('to_ts', String(to));
            } else if (windowValue === 'today') {
                const start = new Date();
                start.setHours(0, 0, 0, 0);
                params.set('from_ts', String(start.getTime() / 1000));
            } else if (windowValue) {
                params.set('from_ts', String(Date.now() / 1000 - Number(windowValue)));
            }
            Object.entries(extra || {}).forEach(([key, value]) => {
                if (value !== null && value !== undefined) params.set(key, String(value));
            });
            return params.toString();
        },
        async loadNodes() {
            try {
                const data = await fetchJson(`${apiBase()}/api/logs/nodes`);
                this.nodes = data.nodes || [];
            } catch {
                // 节点下拉失败不阻塞主视图
            }
        },
        async reload() {
            this.writeDeepLink();
            this.loadNodes(); // 节点清单随刷新更新（后接入的节点也能选）
            this.loading = true;
            this.error = '';
            try {
                const data = await fetchJson(`${apiBase()}/api/logs?${this.buildQuery({ limit: 500 })}`);
                // 查询返回新→旧（最新在顶）；与 tail 同一排序语义，不再翻转
                this.rows = data.logs || [];
                this.pendingNew = 0;
                this.follow = true;
                this.expandedRows = {};
                this.$nextTick(() => this.scrollToTop());
            } catch (error) {
                this.error = error instanceof Error ? error.message : String(error);
            } finally {
                this.loading = false;
            }
        },
        async tick() {
            if (this.disposed || this.loading) return;
            if (!this.rows.length) {
                await this.reload();
                return;
            }
            try {
                const newest = this.rows[0].id;
                const data = await fetchJson(`${apiBase()}/api/logs?${this.buildQuery({ after_id: newest, limit: 200 })}`);
                const fresh = data.logs || [];
                if (!fresh.length) return;
                this.rows = fresh.slice().reverse().concat(this.rows);
                if (this.follow) {
                    this.$nextTick(() => this.scrollToTop());
                } else {
                    this.pendingNew += fresh.length;
                }
            } catch {
                // 尾随轮询失败静默（网络抖动下一轮自愈）
            }
        },
        toggleTail() {
            this.tail = !this.tail;
            if (this.tail) {
                this.follow = true;
                this.tick();
            }
        },
        onScroll(event) {
            const el = event.target;
            this.follow = el.scrollTop <= 8;
            if (this.follow) this.pendingNew = 0;
        },
        scrollToTop() {
            const el = this.$refs.scroller;
            if (el) el.scrollTop = 0;
        },
        jumpToLatest() {
            this.follow = true;
            this.pendingNew = 0;
            this.scrollToTop();
        },
        // ---- 行内详情（DataTable 行展开：点行展开、再点收起） ----
        rowClass(data) {
            return `logs-page__row--${this.levelName(data.level)}`;
        },
        onRowClick(event) {
            // 展开箭头自带 @click.stop，不会触发行点击，无需去重
            this.toggleDetail(event.data);
        },
        toggleDetail(row) {
            const isOpen = Boolean(this.expandedRows && this.expandedRows[row.id]);
            this.expandedRows = isOpen ? {} : { [row.id]: true };
        },
        // ---- 操作关联（体验 C）：一键聚齐本次操作 ----
        filterByOperation(op) {
            this.q = op;
            this.expandedRows = {};
            this.reload();
        },
        // ---- 时间快捷与自定义（体验 E） ----
        onWindowChange() {
            if (this.window === 'custom') {
                if (!this.fromLocal && !this.toLocal) {
                    this.fromLocal = toLocalInput(Date.now() / 1000 - 3600);
                    this.toLocal = toLocalInput(Date.now() / 1000);
                }
            }
            this.reload();
        },
        onCustomRange() {
            this.reload();
        },
        // ---- 前后文（体验 B）：锚点行前 / 后各 N 条，统一时间轴 ----
        async loadAround(row, scope) {
            try {
                const params = new URLSearchParams();
                if (scope === 'node') params.set('node', row.node);
                if (this.level !== null && this.level !== undefined) params.set('level', String(this.level));
                if (this.q) params.set('q', this.q);
                params.set('around_id', String(row.id));
                params.set('limit', '21');
                const data = await fetchJson(`${apiBase()}/api/logs?${params.toString()}`);
                this.around = { id: row.id, rows: data.logs || [], scope, node: row.node };
            } catch (error) {
                this.error = error instanceof Error ? error.message : String(error);
            }
        },
        // ---- 单条复制（体验 E） ----
        async copyMessage(row) {
            try {
                await navigator.clipboard.writeText(row.msg);
            } catch {
                this.error = '复制失败：浏览器未授权剪贴板';
            }
        },
        // ---- 删除（按当前筛选，审计留痕） ----
        deleteFiltered() {
            const scope =
                [this.node ? `节点=${this.node}` : '', this.q ? `关键字=${this.q}` : '', this.level !== null && this.level !== undefined ? `级别≤${this.levelName(this.level)}` : '']
                    .filter(Boolean)
                    .join('、') || '全部（当前时间窗）';
            // 插件 SFC 拿不到 useConfirm，走宿主 app.use(ConfirmationService) 装的 $confirm
            this.$confirm.require({
                header: '删除日志',
                message: `按当前筛选（${scope}）删除 ${this.rows.length} 条日志？\n删除行为会记审计，不可撤销。`,
                icon: 'pi pi-exclamation-triangle',
                acceptLabel: '删除',
                rejectLabel: '取消',
                accept: async () => {
                    try {
                        const response = await fetch(`${apiBase()}/api/logs?${this.buildQuery({})}`, {
                            method: 'DELETE',
                            headers: authHeaders()
                        });
                        const data = await response.json().catch(() => null);
                        if (!response.ok) {
                            throw new Error((data && data.error) || `删除失败 (${response.status})`);
                        }
                        this.pendingNew = 0;
                        await this.reload();
                    } catch (error) {
                        this.error = error instanceof Error ? error.message : String(error);
                    }
                }
            });
        },
        // ---- 导出（体验 E） ----
        exportLog() {
            // rows 为新→旧，导出翻成时间轴序（旧→新，.log 文件惯例）
            const lines = this.rows
                .slice()
                .reverse()
                .map((row) => `${this.absolute(row.ts)} [${row.node}] ${row.name} ${this.levelName(row.level).toUpperCase()} ${row.op ? `[${row.op}] ` : ''}${row.msg}`);
            const blob = new Blob([`${lines.join('\n')}\n`], { type: 'text/plain;charset=utf-8' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `aetp-logs-${new Date().toISOString().replace(/[:.]/g, '-')}.log`;
            link.click();
            URL.revokeObjectURL(link.href);
        },
        // ---- 展示辅助 ----
        levelName(level) {
            return level === 0 ? 'error' : level === 1 ? 'warn' : level === 2 ? 'info' : 'debug';
        },
        levelSeverity(level) {
            return level === 0 ? 'danger' : level === 1 ? 'warn' : level === 2 ? 'info' : 'secondary';
        },
        absolute(ts) {
            return new Date(Number(ts) * 1000).toLocaleString();
        },
        relative(ts) {
            const diff = Date.now() / 1000 - Number(ts);
            if (diff < 60) return `${Math.max(0, Math.floor(diff))} 秒前`;
            if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
            if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
            return `${Math.floor(diff / 86400)} 天前`;
        },
        // 关键字高亮（内容先转义再标亮，杜绝注入）
        highlight(msg) {
            const escaped = escapeHtml(msg);
            const keyword = this.q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            if (!keyword) return escaped;
            return escaped.replace(new RegExp(keyword, 'gi'), (match) => `<mark>${match}</mark>`);
        }
    }
};
</script>

<style scoped>
.logs-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1rem;
}
.logs-page__hint {
    margin: 0.25rem 0 0;
    opacity: 0.7;
    font-size: 0.875rem;
}
.logs-page__actions {
    display: flex;
    gap: 0.5rem;
}
.logs-page__filters {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
}
.logs-page__filter {
    min-width: 10rem;
}
.logs-page__search {
    flex: 1;
    min-width: 14rem;
}
.logs-page__scroll {
    max-height: 65vh;
    overflow: auto;
    border: 1px solid var(--content-border, #434652);
    border-radius: 8px;
    font-size: 0.8125rem;
}
/* 行可点 + 行级状态着色（rowClass 打在 DataTable 行上，需 :deep 穿透） */
.logs-page__scroll :deep(.p-datatable-tbody > tr) {
    cursor: pointer;
}
.logs-page__scroll :deep(tr.logs-page__row--error) {
    color: var(--red-400, #ff8787);
}
.logs-page__scroll :deep(tr.logs-page__row--warn) {
    color: var(--yellow-400, #ffe066);
}
.logs-page__time {
    white-space: nowrap;
    opacity: 0.75;
}
.logs-page__source {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0.85;
}
.logs-page__site {
    display: block;
    font-size: 0.7rem;
    opacity: 0.65;
}
.logs-page__op {
    margin-right: 0.4rem;
    vertical-align: middle;
}
.logs-page__empty {
    display: inline-block;
    text-align: center;
    opacity: 0.6;
    padding: 2rem;
}
.logs-page__jump {
    position: fixed;
    right: 2.5rem;
    bottom: 2.5rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
}
/* 展开行详情（DataTable expansion slot） */
.logs-page__detail {
    padding: 0.25rem 0.25rem;
}
.logs-page__around {
    margin: 0.5rem 0;
    border: 1px solid var(--content-border, #434652);
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font-size: 0.75rem;
}
.logs-page__around-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    opacity: 0.8;
    margin-bottom: 0.25rem;
}
.logs-page__around-row {
    display: grid;
    grid-template-columns: 11rem 8rem 5rem 1fr;
    gap: 0.5rem;
    align-items: baseline;
    padding: 0.15rem 0.25rem;
    border-radius: 4px;
}
.logs-page__around-row--anchor {
    background: color-mix(in srgb, var(--primary-color, #4f9cf9) 18%, transparent);
}
.logs-page__around-node {
    opacity: 0.8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.logs-page__detail-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.5rem;
    opacity: 0.85;
}
.logs-page__detail-msg {
    white-space: pre-wrap;
    word-break: break-word;
    background: var(--content-background, #1e1f26);
    padding: 0.75rem;
    border-radius: 6px;
    margin: 0.5rem 0;
}
.logs-page__detail-actions {
    display: flex;
    gap: 0.5rem;
}
</style>