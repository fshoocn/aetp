<template>
    <section class="db-page">
        <header class="db-page__header">
            <div>
                <h2>数据库</h2>
                <p class="db-page__hint">每节点唯一数据库 aetp.db 的归属登记与孤儿数据自检（手动触发，不随启动回收）</p>
            </div>
            <div class="db-page__actions">
                <Button label="清理孤儿…" severity="danger" outlined size="small" :disabled="!report || !report.orphans || !report.orphans.length" :loading="busy === 'drop'" @click="runCheck('drop')" />
                <Button label="自检" icon="pi pi-search" size="small" :loading="busy === 'warn'" @click="runCheck('warn')" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="success" :closable="true" @close="notice = ''">{{ notice }}</Message>
        <Message v-if="report && report.skipped" severity="warn" :closable="false">
            自检整体跳过（宁漏勿误删）：{{ report.reason || '事实源不可读' }}
        </Message>

        <Panel v-if="report && !report.skipped" header="概况" class="db-page__panel">
            <div class="db-page__summary">
                <span><strong>检查时间：</strong>{{ formatTime(report.checked_at) }}</span>
                <span><strong>孤儿归属：</strong>{{ (report.orphans || []).length }}</span>
                <span><strong>保留归属：</strong>{{ (report.kept || []).length }}</span>
                <span><strong>本次清理：</strong>{{ (report.cleaned || []).length }}</span>
            </div>
        </Panel>

        <Panel v-if="report && !report.skipped" header="孤儿归属（登记在册、代码已不在）" class="db-page__panel">
            <DataTable :value="report.orphans || []" dataKey="owner" size="small" :rowHover="true">
                <Column field="owner" header="归属（owner）" style="width: 16rem" />
                <Column header="数据表">
                    <template #body="{ data }">
                        <Tag v-for="item in data.tables || []" :key="item.name" :value="item.name" :title="item.fp ? `结构指纹 ${item.fp}` : '暂无指纹'" severity="secondary" class="db-page__tag" />
                    </template>
                </Column>
                <template #empty>
                    <span class="db-page__empty">没有孤儿归属 —— 无需清理</span>
                </template>
            </DataTable>
        </Panel>

        <Panel v-if="report && !report.skipped && (report.kept || []).length" header="保留归属（keep_data，跳过清理）" class="db-page__panel">
            <DataTable :value="report.kept || []" dataKey="owner" size="small" :rowHover="true">
                <Column field="owner" header="归属（owner）" style="width: 16rem" />
                <Column header="数据表">
                    <template #body="{ data }">
                        <Tag v-for="item in data.tables || []" :key="item.name" :value="item.name" severity="info" class="db-page__tag" />
                    </template>
                </Column>
            </DataTable>
        </Panel>

        <Panel v-if="report && (report.cleaned || []).length" header="本次已清理" class="db-page__panel">
            <div class="db-page__cleaned">
                <Tag v-for="owner in report.cleaned" :key="owner" :value="owner" severity="success" class="db-page__tag" />
            </div>
        </Panel>
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

async function postJson(url, body) {
    const response = await fetch(url, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
        const message = data && data.error ? data.error.message || String(data.error) : '';
        throw new Error(message || `请求失败 (${response.status})`);
    }
    return data;
}

export default {
    data() {
        return {
            report: null,
            error: '',
            notice: '',
            busy: ''
        };
    },
    created() {
        this.runCheck('warn');
    },
    methods: {
        formatTime(epoch) {
            const value = Number(epoch);
            return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString() : '—';
        },
        async runCheck(policy) {
            if (policy === 'drop') {
                const count = this.report && this.report.orphans ? this.report.orphans.length : 0;
                const ok = window.confirm(`将删除 ${count} 个孤儿归属的全部数据表，不可恢复。确定继续？`);
                if (!ok) return;
            }
            this.busy = policy;
            this.error = '';
            this.notice = '';
            try {
                const data = await postJson(`${apiBase()}/api/db/self-check`, { policy });
                this.report = data;
                if (policy === 'drop' && !data.skipped) {
                    const cleaned = data.cleaned || [];
                    this.notice = cleaned.length ? `已清理：${cleaned.join('、')}` : '没有需要清理的孤儿归属';
                }
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            } finally {
                this.busy = '';
            }
        }
    }
};
</script>

<style scoped>
.db-page {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.db-page__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
}

.db-page__header h2 {
    margin: 0 0 0.25rem;
}

.db-page__hint {
    margin: 0;
    color: var(--text-muted-color, #64748b);
    font-size: 0.9rem;
}

.db-page__actions {
    display: flex;
    gap: 0.5rem;
}

.db-page__summary {
    display: flex;
    gap: 1.5rem;
    flex-wrap: wrap;
    font-size: 0.95rem;
}

.db-page__tag {
    margin-right: 0.35rem;
}

.db-page__empty {
    color: var(--text-muted-color, #64748b);
}
</style>
