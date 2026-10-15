<template>
    <section class="nodes-page">
        <header class="nodes-page__header">
            <div>
                <h2>从模块管理</h2>
                <p class="nodes-page__hint">按「最后心跳时间 + 超时阈值」判定在线状态；点击行查看状态历史</p>
            </div>
            <div class="nodes-page__toolbar">
                <button type="button" class="nodes-page__button" @click="toggleAuto">
                    {{ auto ? '暂停自动刷新' : '恢复自动刷新' }}
                </button>
                <button type="button" class="nodes-page__button" @click="load">立即刷新</button>
            </div>
        </header>

        <p v-if="error" class="nodes-page__error" role="alert">{{ error }}</p>

        <table class="nodes-page__table">
            <thead>
                <tr>
                    <th>节点</th>
                    <th>类型</th>
                    <th>状态</th>
                    <th>地址</th>
                    <th>最近心跳</th>
                    <th>错过周期</th>
                    <th>seq</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                <tr v-for="item in nodes" :key="item.node_id" @click="showDetail(item.node_id)">
                    <td>{{ item.node_id }}</td>
                    <td>{{ item.node_kind || '—' }}</td>
                    <td>
                        <span class="nodes-page__status" :class="'is-' + item.status">
                            {{ statusText(item.status) }}
                        </span>
                    </td>
                    <td>{{ item.address || '—' }}</td>
                    <td>{{ formatTime(item.last_seen) }}</td>
                    <td>{{ item.missed }}</td>
                    <td>{{ item.seq }}</td>
                    <td class="nodes-page__actions" @click.stop>
                        <a
                            v-if="item.address"
                            class="nodes-page__link"
                            :href="item.address"
                            target="_blank"
                            rel="noopener"
                        >打开</a>
                        <button
                            type="button"
                            class="nodes-page__button nodes-page__button--danger"
                            @click="removeNode(item.node_id)"
                        >移除</button>
                    </td>
                </tr>
                <tr v-if="!nodes.length">
                    <td colspan="8" class="nodes-page__empty">暂无从模块上报</td>
                </tr>
            </tbody>
        </table>

        <section v-if="detail" class="nodes-page__detail">
            <h3>{{ detail.node.node_id }} · 详情</h3>
            <p class="nodes-page__hint">
                首次上线 {{ formatTime(detail.node.first_seen) }}，能力
                {{ detail.node.caps_count }} 项、未识别 {{ detail.node.unparsed_count }} 项
            </p>

            <h4>状态历史</h4>
            <ul class="nodes-page__history">
                <li v-for="(entry, index) in detail.history" :key="index">
                    <span class="nodes-page__time">{{ formatTime(entry.at) }}</span>
                    <span>{{ describeHistory(entry) }}</span>
                </li>
                <li v-if="!detail.history.length" class="nodes-page__empty">暂无状态转换</li>
            </ul>

            <h4 v-if="detail.unparsed.length">未识别能力条目</h4>
            <pre v-if="detail.unparsed.length" class="nodes-page__raw">{{ pretty(detail.unparsed) }}</pre>
        </section>
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

const STATUS_TEXT = { online: '在线', offline: '离线', lost: '失联' };

export default {
    data() {
        return {
            nodes: [],
            detail: null,
            error: '',
            auto: true,
            timer: null
        };
    },
    mounted() {
        this.load();
        this.timer = window.setInterval(() => {
            if (this.auto) this.load();
        }, 5000);
    },
    beforeUnmount() {
        if (this.timer) window.clearInterval(this.timer);
    },
    methods: {
        async load() {
            try {
                const response = await fetch(apiUrl('/api/heartbeat/nodes'));
                const data = await response.json();
                if (!response.ok) {
                    throw new Error((data.error && data.error.message) || `请求失败 (${response.status})`);
                }
                this.nodes = data.nodes || [];
                this.error = '';
                if (this.detail) await this.showDetail(this.detail.node.node_id, true);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        async showDetail(nodeId, silent) {
            try {
                const response = await fetch(
                    apiUrl(`/api/heartbeat/nodes/${encodeURIComponent(nodeId)}`)
                );
                const data = await response.json();
                if (!response.ok) {
                    throw new Error((data.error && data.error.message) || `请求失败 (${response.status})`);
                }
                this.detail = data;
            } catch (reason) {
                if (!silent) this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        async removeNode(nodeId) {
            if (!window.confirm(`确认移除从模块「${nodeId}」的记录？`)) return;
            try {
                const response = await fetch(
                    apiUrl(`/api/heartbeat/nodes/${encodeURIComponent(nodeId)}`),
                    { method: 'DELETE' }
                );
                const data = await response.json();
                if (!response.ok) {
                    throw new Error((data.error && data.error.message) || `请求失败 (${response.status})`);
                }
                if (this.detail && this.detail.node.node_id === nodeId) this.detail = null;
                await this.load();
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        toggleAuto() {
            this.auto = !this.auto;
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
.nodes-page {
    display: flex;
    flex-direction: column;
    gap: 1rem;
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

.nodes-page__toolbar {
    display: flex;
    gap: 0.5rem;
}

.nodes-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary, #64748b);
    font-size: 0.875rem;
}

.nodes-page__error {
    margin: 0;
    color: #dc2626;
}

.nodes-page__table {
    width: 100%;
    border-collapse: collapse;
}

.nodes-page__table th,
.nodes-page__table td {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--surface-border, #e2e8f0);
    text-align: left;
    font-size: 0.875rem;
}

.nodes-page__table tbody tr {
    cursor: pointer;
}

.nodes-page__table tbody tr:hover {
    background: var(--surface-hover, #f8fafc);
}

.nodes-page__actions {
    display: flex;
    gap: 0.375rem;
    white-space: nowrap;
}

.nodes-page__button,
.nodes-page__link {
    padding: 0.25rem 0.625rem;
    border: 1px solid var(--surface-border, #e2e8f0);
    border-radius: 6px;
    background: var(--surface-card, #ffffff);
    color: var(--text-color, #334155);
    font-size: 0.8125rem;
    cursor: pointer;
    text-decoration: none;
}

.nodes-page__button--danger {
    color: #dc2626;
    border-color: #fecaca;
}

.nodes-page__status {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    font-size: 0.75rem;
    color: #ffffff;
}

.nodes-page__status.is-online {
    background: #16a34a;
}

.nodes-page__status.is-offline {
    background: #d97706;
}

.nodes-page__status.is-lost {
    background: #dc2626;
}

.nodes-page__detail {
    padding: 1rem;
    border: 1px solid var(--surface-border, #e2e8f0);
    border-radius: 8px;
    background: var(--surface-card, #ffffff);
}

.nodes-page__detail h3,
.nodes-page__detail h4 {
    margin: 0.5rem 0;
}

.nodes-page__history {
    margin: 0;
    padding-left: 1.25rem;
    font-size: 0.875rem;
}

.nodes-page__time {
    margin-right: 0.5rem;
    color: var(--text-color-secondary, #64748b);
}

.nodes-page__raw {
    max-height: 16rem;
    overflow: auto;
    padding: 0.75rem;
    border-radius: 6px;
    background: var(--surface-ground, #f1f5f9);
    font-size: 0.75rem;
}

.nodes-page__empty {
    color: var(--text-color-secondary, #64748b);
    text-align: center;
}
</style>
