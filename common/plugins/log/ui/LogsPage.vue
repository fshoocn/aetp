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
            <Select v-model="window" :options="windowOptions" optionLabel="label" optionValue="value" class="logs-page__filter" @change="reload" />
            <IconField class="logs-page__search">
                <InputIcon><i class="pi pi-search" /></InputIcon>
                <InputText v-model="q" placeholder="关键字…" @keydown.enter="reload" />
            </IconField>
        </div>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>

        <div class="logs-page__scroll" ref="scroller" @scroll="onScroll">
            <table class="logs-page__table">
                <tbody>
                    <template v-for="row in rows" :key="row.id">
                        <tr class="logs-page__row" :class="`logs-page__row--${levelName(row.level)}`" @click="toggleDetail(row)">
                            <td class="logs-page__time" :title="absolute(row.ts)">{{ relative(row.ts) }}</td>
                            <td class="logs-page__node">{{ row.node }}</td>
                            <td class="logs-page__level"><Tag :value="levelName(row.level)" :severity="levelSeverity(row.level)" /></td>
                            <td class="logs-page__source" :title="row.source || ''">
                                {{ row.name }}
                                <span v-if="row.source" class="logs-page__site">{{ row.source }}</span>
                            </td>
                            <td class="logs-page__msg">
                                <button v-if="row.op" class="logs-page__op" title="查看本次操作" @click.stop="filterByOperation(row.op)">{{ row.op }}</button>
                                <span v-html="highlight(row.msg)"></span>
                            </td>
                        </tr>
                        <tr v-if="selected && selected.id === row.id" class="logs-page__detail">
                            <td colspan="5">
                                <div class="logs-page__detail-meta">
                                    <span><strong>时间：</strong>{{ absolute(selected.ts) }}</span>
                                    <span><strong>节点：</strong>{{ selected.node }}</span>
                                    <span><strong>来源：</strong>{{ selected.name }}（{{ levelName(selected.level) }}）</span>
                                    <span v-if="selected.source"><strong>调用点：</strong>{{ selected.source }}</span>
                                    <span v-if="selected.op"><strong>操作：</strong>{{ selected.op }}</span>
                                </div>
                                <pre class="logs-page__detail-msg">{{ selected.msg }}</pre>
                                <div class="logs-page__detail-actions">
                                    <Button v-if="selected.op" label="查看本次操作" severity="secondary" outlined size="small" @click="filterByOperation(selected.op)" />
                                    <Button label="收起" severity="secondary" text size="small" @click="selected = null" />
                                </div>
                            </td>
                        </tr>
                    </template>
                    <tr v-if="!rows.length && !loading">
                        <td colspan="5" class="logs-page__empty">暂无日志</td>
                    </tr>
                </tbody>
            </table>
        </div>

        <button v-if="pendingNew > 0 && !follow" class="logs-page__jump" @click="jumpToLatest">
            ↓ {{ pendingNew }} 条新日志
        </button>
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
    { label: '全部', value: 0 }
];

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
            tail: true,
            follow: true,
            pendingNew: 0,
            loading: false,
            error: '',
            nodes: [],
            selected: null,
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
        },
        writeDeepLink() {
            const params = new URLSearchParams();
            if (this.node) params.set('node', this.node);
            if (this.level !== null && this.level !== undefined) params.set('level', String(this.level));
            if (this.q) params.set('q', this.q);
            const query = params.toString();
            window.history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
        },
        buildQuery(extra) {
            const params = new URLSearchParams();
            if (this.node) params.set('node', this.node);
            if (this.level !== null && this.level !== undefined) params.set('level', String(this.level));
            if (this.q) params.set('q', this.q);
            const windowValue = this.window;
            if (windowValue === 'today') {
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
            this.loading = true;
            this.error = '';
            try {
                const data = await fetchJson(`${apiBase()}/api/logs?${this.buildQuery({ limit: 500 })}`);
                // 查询返回新→旧（最新在顶）；与 tail 同一排序语义，不再翻转
                this.rows = data.logs || [];
                this.pendingNew = 0;
                this.follow = true;
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
        // ---- 行内详情（点开展开、再点收起） ----
        toggleDetail(row) {
            this.selected = this.selected && this.selected.id === row.id ? null : row;
        },
        // ---- 操作关联（体验 C）：一键聚齐本次操作 ----
        filterByOperation(op) {
            this.q = op;
            this.detailVisible = false;
            this.reload();
        },
        // ---- 删除（按当前筛选，审计留痕） ----
        async deleteFiltered() {
            const scope =
                [this.node ? `节点=${this.node}` : '', this.q ? `关键字=${this.q}` : '', this.level !== null && this.level !== undefined ? `级别≤${this.levelName(this.level)}` : '']
                    .filter(Boolean)
                    .join('、') || '全部（当前时间窗）';
            if (!window.confirm(`按当前筛选（${scope}）删除 ${this.rows.length} 条日志？\n删除行为会记审计，不可撤销。`)) return;
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
}
.logs-page__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
}
.logs-page__row {
    cursor: pointer;
}
.logs-page__row:hover {
    background: var(--content-hover-background, rgba(255, 255, 255, 0.04));
}
.logs-page__row--error {
    color: var(--red-400, #ff8787);
}
.logs-page__row--warn {
    color: var(--yellow-400, #ffe066);
}
.logs-page__row td {
    padding: 0.35rem 0.6rem;
    border-bottom: 1px solid var(--content-border, #434652);
    vertical-align: top;
}
.logs-page__time {
    white-space: nowrap;
    opacity: 0.75;
    width: 7rem;
}
.logs-page__node {
    white-space: nowrap;
    width: 8rem;
}
.logs-page__level {
    width: 5rem;
}
.logs-page__source {
    white-space: nowrap;
    width: 12rem;
    opacity: 0.85;
}
.logs-page__site {
    display: block;
    font-size: 0.7rem;
    opacity: 0.65;
}
.logs-page__op {
    display: inline-block;
    margin-right: 0.4rem;
    padding: 0 0.45rem;
    border-radius: 999px;
    border: 1px solid var(--primary-color, #4f46e5);
    background: transparent;
    color: var(--primary-color, #4f46e5);
    font-size: 0.75rem;
    cursor: pointer;
    vertical-align: middle;
}
.logs-page__op:hover {
    background: rgba(79, 70, 229, 0.18);
}
.logs-page__empty {
    text-align: center;
    opacity: 0.6;
    padding: 2rem !important;
}
.logs-page__jump {
    position: fixed;
    right: 2.5rem;
    bottom: 2.5rem;
    padding: 0.5rem 1rem;
    border-radius: 999px;
    border: none;
    background: var(--primary-color, #4f46e5);
    color: #fff;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
}
.logs-page__detail-msg {
    white-space: pre-wrap;
    word-break: break-word;
    background: var(--content-background, #1e1f26);
    padding: 0.75rem;
    border-radius: 6px;
    margin: 0.5rem 0;
}
.logs-page__detail td {
    background: var(--content-hover-background, rgba(255, 255, 255, 0.03));
    padding: 0.75rem 1rem;
}
.logs-page__detail-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1.25rem;
    font-size: 0.8125rem;
    opacity: 0.9;
}
.logs-page__detail-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
    flex-wrap: wrap;
}
</style>
