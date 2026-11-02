<template>
    <section class="flow-page">
        <header class="flow-page__header">
            <div>
                <h2>流程</h2>
                <p class="flow-page__hint">模板 + 用户流程（版本化覆盖）：关闭可选节点、插入步骤、切换实现、改参数，保存即生成新版本</p>
            </div>
            <div class="flow-page__actions">
                <Button label="刷新" icon="pi pi-refresh" size="small" @click="reload" :loading="busy === 'load'" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="success" :closable="true" @close="notice = ''">{{ notice }}</Message>

        <Panel header="流程（模板与用户流程）" class="flow-page__panel">
            <DataTable :value="flows" dataKey="id" size="small" :rowHover="true" selectionMode="single"
                v-model:selection="selectedFlow" @row-select="onFlowSelect">
                <Column field="id" header="ID" style="width: 12rem" />
                <Column field="title" header="名称" />
                <Column header="类型" style="width: 7rem">
                    <template #body="{ data }">
                        <Tag :value="data.kind === 'user' ? '用户' : '模板'"
                            :severity="data.kind === 'user' ? 'info' : 'secondary'" />
                    </template>
                </Column>
                <Column header="版本 / 派生" style="width: 12rem">
                    <template #body="{ data }">
                        <span>v{{ data.version }}</span>
                        <span v-if="data.extends" class="flow-page__muted"> ← {{ data.extends }}</span>
                    </template>
                </Column>
                <Column header="状态" style="width: 10rem">
                    <template #body="{ data }">
                        <Tag v-if="data.needs_attention" value="需重新校验" severity="warn" />
                        <Tag v-else-if="!data.enabled" value="已停用" severity="secondary" />
                        <Tag v-else value="正常" severity="success" />
                    </template>
                </Column>
                <template #empty>
                    <span class="flow-page__muted">还没有流程——插件模板随引擎注册，用户流程可在此保存</span>
                </template>
            </DataTable>
        </Panel>

        <Panel v-if="detail" :header="`流程详情：${detail.id}`" class="flow-page__panel">
            <div class="flow-page__summary">
                <span><strong>类型：</strong>{{ detail.kind === 'user' ? '用户流程' : '模板' }}</span>
                <span v-if="detail.extends"><strong>派生自：</strong>{{ detail.extends }}</span>
                <span><strong>版本：</strong>v{{ detail.version }}</span>
                <span v-if="detail.created_by"><strong>创建者：</strong>{{ detail.created_by }}</span>
                <span><strong>历史版本：</strong>{{ (detail.versions || []).length }}</span>
            </div>

            <Message v-if="(detail.problems || []).length" severity="warn" :closable="false">
                <div v-for="p in detail.problems" :key="p.code + p.node">{{ p.node }}：{{ p.message }}</div>
            </Message>

            <h3>生效节点</h3>
            <DataTable :value="detail.effective_nodes || []" dataKey="id" size="small" :rowHover="true">
                <Column field="id" header="节点" style="width: 10rem" />
                <Column field="stage" header="阶段" style="width: 8rem" />
                <Column header="实现 / 标签">
                    <template #body="{ data }">
                        <Tag v-if="data.pin" :value="`pin: ${data.pin}`" severity="info" class="flow-page__tag" />
                        <Tag v-for="(v, k) in (data.labels || {})" :key="k" :value="`${k}=${v}`" severity="secondary"
                            class="flow-page__tag" />
                    </template>
                </Column>
                <Column header="策略" style="width: 14rem">
                    <template #body="{ data }">
                        <Tag v-if="data.locked" value="locked" severity="warn" class="flow-page__tag" />
                        <Tag :value="data.required ? '必选' : '可选'" severity="secondary" class="flow-page__tag" />
                        <Tag v-if="data.disabled" value="已关闭" severity="danger" class="flow-page__tag" />
                    </template>
                </Column>
            </DataTable>

            <h3>覆盖编辑（JSON：disable / insert / params / pin / labels）</h3>
            <textarea class="flow-page__editor" v-model="overridesText" rows="8" spellcheck="false"></textarea>
            <div class="flow-page__actions">
                <Button label="预检" icon="pi pi-search" size="small" :loading="busy === 'validate'"
                    @click="validateFlow" />
                <Button label="保存为新版本" icon="pi pi-save" size="small" :disabled="detail.kind !== 'user'"
                    :loading="busy === 'save'" @click="saveFlow" />
                <Button label="启动运行" icon="pi pi-play" size="small" severity="success"
                    :disabled="detail.needs_attention" :loading="busy === 'start'" @click="startRun" />
                <Button v-if="detail.kind === 'user'" label="删除…" icon="pi pi-trash" size="small" severity="danger"
                    outlined :loading="busy === 'delete'" @click="deleteFlow" />
                <Button label="导出 JSON" icon="pi pi-download" size="small" severity="secondary" outlined
                    @click="exportFlow" />
            </div>

            <h3 v-if="problems.length">预检结果</h3>
            <DataTable v-if="problems.length" :value="problems" dataKey="message" size="small">
                <Column header="级别" style="width: 7rem">
                    <template #body="{ data }">
                        <Tag :value="data.level" :severity="data.level === 'error' ? 'danger' : 'warn'" />
                    </template>
                </Column>
                <Column field="node" header="节点" style="width: 10rem" />
                <Column field="code" header="代码" style="width: 10rem" />
                <Column field="message" header="说明" />
            </DataTable>
        </Panel>

        <Panel header="运行" class="flow-page__panel">
            <DataTable :value="runs" dataKey="run_id" size="small" :rowHover="true" selectionMode="single"
                v-model:selection="selectedRun" @row-select="onRunSelect">
                <Column field="run_id" header="运行" style="width: 12rem" />
                <Column field="flow_id" header="流程" style="width: 10rem" />
                <Column header="状态" style="width: 8rem">
                    <template #body="{ data }">
                        <Tag :value="data.state" :severity="stateSeverity(data.state)" />
                    </template>
                </Column>
                <Column field="initiator" header="发起方" style="width: 8rem" />
                <Column header="时间">
                    <template #body="{ data }">
                        <span class="flow-page__muted">{{ formatTime(data.created_at) }}</span>
                    </template>
                </Column>
                <template #empty>
                    <span class="flow-page__muted">暂无运行</span>
                </template>
            </DataTable>

            <div v-if="runDetail" class="flow-page__rundetail">
                <h3>运行详情：{{ runDetail.run_id }}</h3>
                <div class="flow-page__summary">
                    <span><strong>状态：</strong>{{ runDetail.state }}</span>
                    <span><strong>发起方：</strong>{{ runDetail.initiator }}</span>
                    <span v-if="runDetail.error"><strong>错误：</strong>{{ runDetail.error }}</span>
                </div>
                <DataTable :value="runDetail.nodes || []" dataKey="node_id" size="small">
                    <Column field="node_id" header="节点" style="width: 10rem" />
                    <Column field="state" header="状态" style="width: 8rem" />
                    <Column field="provider" header="提供者" style="width: 10rem" />
                    <Column field="attempts" header="尝试" style="width: 6rem" />
                    <Column field="error" header="错误" />
                </DataTable>
                <div class="flow-page__actions">
                    <Button v-if="['pending', 'running'].includes(runDetail.state)" label="取消…" icon="pi pi-ban"
                        size="small" severity="danger" outlined :loading="busy === 'cancel'" @click="cancelRun" />
                    <Button v-if="runDetail.state === 'interrupted'" label="重放（retry）" icon="pi pi-replay"
                        size="small" :loading="busy === 'resume'" @click="resumeRun('retry')" />
                    <Button v-if="runDetail.state === 'interrupted'" label="跳过（skip）" icon="pi pi-step-forward"
                        size="small" severity="secondary" outlined :loading="busy === 'resume'"
                        @click="resumeRun('skip')" />
                    <Button v-if="runDetail.state === 'interrupted'" label="终止（abort）" icon="pi pi-times"
                        size="small" severity="danger" outlined :loading="busy === 'resume'"
                        @click="resumeRun('abort')" />
                </div>
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

async function request(path, { method = 'GET', body } = {}) {
    const response = await fetch(`${apiBase()}${path}`, {
        method,
        headers: body ? { ...authHeaders(), 'Content-Type': 'application/json' } : { ...authHeaders() },
        body: body ? JSON.stringify(body) : undefined
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
        const message = data && data.error ? (data.error.message || String(data.error)) : '';
        const error = new Error(message || `请求失败 (${response.status})`);
        error.payload = data;
        throw error;
    }
    return data;
}

export default {
    data() {
        return {
            flows: [],
            runs: [],
            selectedFlow: null,
            selectedRun: null,
            detail: null,
            runDetail: null,
            overridesText: '{}',
            problems: [],
            error: '',
            notice: '',
            busy: ''
        };
    },
    created() {
        this.reload();
    },
    methods: {
        formatTime(epoch) {
            const value = Number(epoch);
            return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toLocaleString() : '—';
        },
        stateSeverity(state) {
            return { succeeded: 'success', failed: 'danger', cancelled: 'warn', interrupted: 'warn' }[state] || 'info';
        },
        async reload() {
            this.busy = 'load';
            this.error = '';
            try {
                const [flowsData, runsData] = await Promise.all([
                    request('/api/flow/flows'),
                    request('/api/flow/runs?limit=50')
                ]);
                this.flows = flowsData.flows || [];
                this.runs = runsData.runs || [];
                if (this.detail) {
                    const again = this.flows.find((f) => f.id === this.detail.id);
                    if (again) await this.onFlowSelect({ data: again });
                }
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            } finally {
                this.busy = '';
            }
        },
        async onFlowSelect(event) {
            const row = event && event.data ? event.data : event;
            if (!row) return;
            this.busy = 'load';
            this.error = '';
            this.problems = [];
            try {
                const detail = await request(`/api/flow/flows/${encodeURIComponent(row.id)}`);
                this.detail = detail;
                this.overridesText = JSON.stringify(
                    detail.extends ? (detail.overrides || {}) : (detail.export && detail.export.def) || {},
                    null, 2
                );
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            } finally {
                this.busy = '';
            }
        },
        parseOverrides() {
            try {
                return JSON.parse(this.overridesText || '{}');
            } catch {
                this.error = '覆盖 JSON 不合法';
                return null;
            }
        },
        async validateFlow() {
            const overrides = this.parseOverrides();
            if (!overrides) return;
            this.busy = 'validate';
            this.error = '';
            try {
                const data = await request(`/api/flow/flows/${encodeURIComponent(this.detail.id)}/validate`, {
                    method: 'POST', body: { overrides }
                });
                this.problems = data.problems || [];
                if (!this.problems.length) this.notice = '预检通过：没有问题';
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            } finally {
                this.busy = '';
            }
        },
        async saveFlow() {
            const overrides = this.parseOverrides();
            if (!overrides) return;
            const body = this.detail.extends
                ? { title: this.detail.title, extends: this.detail.extends, overrides }
                : { title: this.detail.title, def: overrides };
            this.busy = 'save';
            this.error = '';
            try {
                await request(`/api/flow/flows/${encodeURIComponent(this.detail.id)}`, { method: 'PUT', body });
                this.notice = '已保存为新版本';
                await this.reload();
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
                const payload = err && err.payload && err.payload.details ? err.payload.details.problems : null;
                if (payload) this.problems = payload;
            } finally {
                this.busy = '';
            }
        },
        async startRun() {
            const overrides = this.parseOverrides();
            if (!overrides) return;
            this.busy = 'start';
            this.error = '';
            try {
                const data = await request(`/api/flow/flows/${encodeURIComponent(this.detail.id)}/runs`, {
                    method: 'POST', body: { overrides, input: {} }
                });
                this.notice = `已启动：${data.run_id}`;
                await this.reload();
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
                const payload = err && err.payload && err.payload.details ? err.payload.details.problems : null;
                if (payload) this.problems = payload;
            } finally {
                this.busy = '';
            }
        },
        async deleteFlow() {
            const ok = window.confirm(`删除用户流程 ${this.detail.id} 的全部版本？历史运行不受影响。`);
            if (!ok) return;
            this.busy = 'delete';
            this.error = '';
            try {
                await request(`/api/flow/flows/${encodeURIComponent(this.detail.id)}`, { method: 'DELETE' });
                this.notice = '已删除';
                this.detail = null;
                await this.reload();
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            } finally {
                this.busy = '';
            }
        },
        exportFlow() {
            if (!this.detail || !this.detail.export) return;
            const blob = new Blob([JSON.stringify(this.detail.export, null, 2)], { type: 'application/json' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `${this.detail.id}.flow.json`;
            link.click();
            URL.revokeObjectURL(link.href);
        },
        async onRunSelect(event) {
            const row = event && event.data ? event.data : event;
            if (!row) return;
            try {
                this.runDetail = await request(`/api/flow/runs/${encodeURIComponent(row.run_id || row.id)}`);
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            }
        },
        async cancelRun() {
            const ok = window.confirm(`取消运行 ${this.runDetail.run_id}？`);
            if (!ok) return;
            this.busy = 'cancel';
            try {
                await request(`/api/flow/runs/${encodeURIComponent(this.runDetail.run_id)}/cancel`, {
                    method: 'POST', body: { reason: 'ui' }
                });
                this.notice = '已请求取消';
                await this.reload();
                await this.onRunSelect(this.runDetail);
            } catch (err) {
                this.error = err && err.message ? err.message : String(err);
            } finally {
                this.busy = '';
            }
        },
        async resumeRun(mode) {
            this.busy = 'resume';
            try {
                await request(`/api/flow/runs/${encodeURIComponent(this.runDetail.run_id)}/resume`, {
                    method: 'POST', body: { mode }
                });
                this.notice = `已处置（${mode}）`;
                await this.reload();
                await this.onRunSelect(this.runDetail);
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
.flow-page {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.flow-page__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
}

.flow-page__header h2 {
    margin: 0 0 0.25rem;
}

.flow-page__hint {
    margin: 0;
    color: var(--text-muted-color, #64748b);
    font-size: 0.9rem;
}

.flow-page__actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin: 0.75rem 0;
}

.flow-page__summary {
    display: flex;
    gap: 1.5rem;
    flex-wrap: wrap;
    font-size: 0.95rem;
    margin-bottom: 0.5rem;
}

.flow-page__editor {
    width: 100%;
    font-family: ui-monospace, Consolas, monospace;
    font-size: 0.85rem;
    padding: 0.5rem;
    border: 1px solid var(--content-border-color, #cbd5e1);
    border-radius: 6px;
    background: var(--content-background, transparent);
    color: var(--text-color, inherit);
}

.flow-page__tag {
    margin-right: 0.35rem;
}

.flow-page__muted {
    color: var(--text-muted-color, #64748b);
    font-size: 0.9rem;
}

.flow-page__rundetail {
    margin-top: 1rem;
}
</style>
