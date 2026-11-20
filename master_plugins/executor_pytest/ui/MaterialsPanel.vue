<!-- pytest 脚本处理面板（W2-D）：脚本管理页 /scripts 的 slot 面板（target=scripts:executor-pytest）。
     只处理「当前上传的脚本」：上传区 → 上传成功（含 deduplicated 复用 id）自动展开处理区
     （状态 / inspect 分析·解析 / 用例清单 / 选择集 / 预推 / 下载 / 刷新）；
     全量脚本清单归 test_core 跨执行器总表（ScriptsPage），本面板不列清单、无配方编辑。
     插件 SFC 自包含：不 import 宿主 / npm 模块，仅用全局共享组件 + 原生控件 + $confirm（Options API）。 -->
<template>
    <section class="materials-panel">
        <header class="materials-panel__header">
            <div>
                <h3>pytest 脚本处理</h3>
                <p class="materials-panel__hint">上传脚本包（zip）→ 分析 / 解析 → 选例 → 预推；材料内容对核心不透明，语义归 pytest 执行器族</p>
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="info" :closable="true" @close="notice = ''">{{ notice }}</Message>

        <!-- 上传态：未选中当前脚本时展示 -->
        <Panel v-if="!detail" header="上传脚本（zip）">
            <div class="materials-panel__form">
                <input ref="fileInput" type="file" accept=".zip,application/zip" class="materials-panel__file" @change="onFileChange" />
                <div class="materials-panel__picker">
                    <Button label="选择 zip 文件" icon="pi pi-folder-open" size="small" severity="secondary" @click="pickFile" />
                    <span class="materials-panel__muted">{{ file ? '已选：' + file.name : '未选择 zip 文件' }}</span>
                </div>
                <InputText v-model="uploadName" placeholder="脚本名（默认取文件名）" class="materials-panel__name" />
                <div class="materials-panel__actions">
                    <Button label="上传" icon="pi pi-upload" size="small" :loading="uploading" :disabled="!file" @click="uploadMaterial" />
                    <span v-if="!file" class="materials-panel__muted">请先点「选择 zip 文件」</span>
                </div>
            </div>
            <p class="materials-panel__hint">
                上传按内容指纹去重：命中既有材料会提示 <code>deduplicated</code> 并复用 id；归属执行器经
                <code>X-Material-Executor: pytest</code> 头自动标记。上传成功后在下方直接处理当前脚本。
            </p>
        </Panel>

        <!-- 处理态：上传成功（或复用既有 id）后自动展开 -->
        <Panel v-if="detail" :header="`当前脚本：${detail.name || materialIdOf(detail)}`">
            <div class="materials-panel__actions">
                <Button label="下载 zip" icon="pi pi-download" size="small" severity="secondary" outlined @click="downloadMaterial(materialIdOf(detail))" />
                <Select v-model="inspectMode" :options="inspectModeOptions" optionLabel="label" optionValue="value" class="materials-panel__filter" />
                <Button label="开始 inspect" icon="pi pi-search" size="small" :loading="inspecting" @click="runInspect" />
                <Button label="刷新" icon="pi pi-refresh" size="small" severity="secondary" :loading="loading" @click="refreshDetail" />
                <Button label="更换脚本" icon="pi pi-times" size="small" severity="secondary" text @click="closeDetail" />
            </div>

            <p class="materials-panel__hint">
                <Tag :value="stateText(detail.state)" :severity="stateSeverity(detail.state)" />
                · <span class="materials-panel__mono">{{ materialIdOf(detail) }}</span>
                · {{ formatSize(detail.size) }} · 更新 {{ formatTime(detail.updated_ts) }}
                · sha256 <span class="materials-panel__mono">{{ shortSha(detail.sha256) }}</span>
            </p>

            <h4>分析 / 解析结果（inspect）</h4>
            <p v-if="inspectRecord" class="materials-panel__hint">
                <Tag :value="inspectStateText(inspectRecord.state)" :severity="inspectStateSeverity(inspectRecord.state)" />
                · {{ inspectRecord.mode }} · 节点 {{ inspectRecord.node_id || '—' }}
                · 耗时 {{ formatDuration(inspectRecord.duration_s) }}
                <template v-if="inspectRecord.late"> · 迟到应答（已收下刷新）</template>
                <template v-if="inspectRecord.error"> · {{ inspectRecord.error }}</template>
            </p>
            <p v-else class="materials-panel__muted">暂无 inspect 记录（「分析」查语法 / 依赖 / 可收集性，「解析」产出用例清单）</p>
            <template v-if="problems.length">
                <div v-for="(problem, index) in problems" :key="index" class="materials-panel__problem">
                    <Tag :value="problem.severity || 'info'" :severity="problemSeverity(problem.severity)" />
                    <span class="materials-panel__mono">{{ problem.code }}</span>
                    <span v-if="problem.path" class="materials-panel__mono materials-panel__muted">{{ problem.path }}<template v-if="problem.line">:{{ problem.line }}</template></span>
                    <span>{{ problem.message }}</span>
                </div>
            </template>

            <h4>用例清单</h4>
            <div class="materials-panel__toolbar">
                <Select
                    v-model="markerFilter"
                    :options="markerOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="全部 marker"
                    showClear
                    class="materials-panel__filter"
                />
                <span class="materials-panel__muted">已勾选 {{ selectedCases.length }} 条</span>
            </div>
            <DataTable
                v-if="visibleCases.length"
                :value="visibleCases"
                dataKey="case_id"
                scrollable
                size="small"
                selectionMode="multiple"
                v-model:selection="selectedCases"
            >
                <Column field="case_id" header="case_id（nodeid）" style="min-width: 14rem">
                    <template #body="{ data }">
                        <span class="materials-panel__mono" :title="data.case_id">{{ data.case_id }}</span>
                    </template>
                </Column>
                <Column field="path" header="文件" style="min-width: 8rem">
                    <template #body="{ data }">{{ data.path || '—' }}</template>
                </Column>
                <Column field="markers" header="markers" style="min-width: 8rem">
                    <template #body="{ data }">
                        <span class="materials-panel__muted">{{ (data.markers || []).join(', ') || '—' }}</span>
                    </template>
                </Column>
                <Column field="status" header="状态" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.status || 'ok' }}</template>
                </Column>
            </DataTable>
            <p v-else class="materials-panel__muted">暂无用例清单（先以 collect 模式 inspect 解析用例）</p>

            <div class="materials-panel__actions">
                <InputText v-model="selectionName" placeholder="选择集名称" class="materials-panel__name" />
                <Button
                    label="保存选择集"
                    icon="pi pi-save"
                    size="small"
                    :loading="savingSelection"
                    :disabled="!selectedCases.length"
                    @click="saveSelection"
                />
            </div>
            <p class="materials-panel__hint">勾选用例后保存选择集 → 得到 <code>selection_id</code>（Job 只带引用，载荷恒小，§5/§11）。</p>
            <ul v-if="selections.length" class="materials-panel__list">
                <li v-for="sel in selections" :key="sel.selection_id">
                    <label class="materials-panel__check">
                        <input v-model="sel.include" type="checkbox" />
                        {{ sel.name }}（{{ sel.count }} 条）
                    </label>
                    <span class="materials-panel__mono">{{ sel.selection_id }}</span>
                    <Button label="复制" icon="pi pi-copy" size="small" severity="secondary" text @click="copyText(sel.selection_id)" />
                </li>
            </ul>

            <h4>预推（下发前置）</h4>
            <div class="materials-panel__actions">
                <Button label="预推到候选节点" icon="pi pi-upload" size="small" :loading="pushing" @click="pushMaterial" />
            </div>
            <p class="materials-panel__hint">按上方勾选的 selection_ids 随材料元包同步给全部 <code>executor/pytest</code> 候选节点（§5.2）；失败节点可重试或依赖 missing 兜底。</p>
            <template v-if="pushResult">
                <p class="materials-panel__hint">
                    成功节点：
                    <template v-if="pushResult.pushed.length">
                        <Tag v-for="node in pushResult.pushed" :key="node" :value="node" severity="success" class="materials-panel__tag" />
                    </template>
                    <span v-else class="materials-panel__muted">无</span>
                    ；失败节点：
                    <template v-if="pushResult.failed.length">
                        <Tag v-for="node in pushResult.failed" :key="node" :value="node" severity="danger" class="materials-panel__tag" />
                    </template>
                    <span v-else class="materials-panel__muted">无</span>
                </p>
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
        const error = new Error(message || `请求失败 (${response.status})`);
        error.status = response.status;
        error.code = (data && data.code) || (payload && payload.code) || '';
        throw error;
    }
    return data;
}

// HTTP 头值是 Latin-1 字节串：脚本名含非 Latin-1 字符（如中文）时
// fetch 会直接抛错，改走 API 既有的 name 查询参数兜底
function isLatin1(text) {
    for (let index = 0; index < text.length; index += 1) {
        if (text.charCodeAt(index) > 0xff) return false;
    }
    return true;
}

const STATE_TEXT = { uploaded: '待分析', analyzing: '分析中', analyzed: '已分析', failed: '失败' };

export default {
    data() {
        return {
            // 当前脚本（处理区）；null = 上传态
            detail: null,
            error: '',
            notice: '',
            loading: false,
            // 上传区
            file: null,
            uploadName: '',
            uploading: false,
            // inspect
            inspectMode: 'analyze',
            inspectModeOptions: [
                { label: '分析（analyze：语法 / 依赖 / 可收集性）', value: 'analyze' },
                { label: '解析（collect：用例清单）', value: 'collect' }
            ],
            inspecting: false,
            inspectRecord: null,
            // 用例清单与选择集
            cases: [],
            selectedCases: [],
            markerFilter: null,
            selectionName: '',
            savingSelection: false,
            selections: [],
            // 预推
            pushing: false,
            pushResult: null,
            disposed: false
        };
    },
    computed: {
        problems() {
            if (this.inspectRecord && (this.inspectRecord.problems || []).length) {
                return this.inspectRecord.problems;
            }
            return (this.detail && this.detail.problems) || [];
        },
        markerOptions() {
            const markers = new Set();
            for (const item of this.cases) {
                for (const marker of item.markers || []) markers.add(marker);
            }
            return [...markers].sort().map((marker) => ({ label: marker, value: marker }));
        },
        visibleCases() {
            if (!this.markerFilter) return this.cases;
            return this.cases.filter((item) => (item.markers || []).includes(this.markerFilter));
        }
    },
    beforeUnmount() {
        this.disposed = true;
    },
    methods: {
        async refreshDetail() {
            // 刷新当前脚本的状态 / 问题 / 用例清单（处理区「刷新」）
            const materialId = this.materialIdOf(this.detail);
            if (!materialId) return;
            this.loading = true;
            try {
                await this.openDetail(materialId);
            } finally {
                this.loading = false;
            }
        },
        closeDetail() {
            // 「更换脚本」：回上传态
            this.detail = null;
            this.inspectRecord = null;
            this.cases = [];
            this.selectedCases = [];
            this.markerFilter = null;
            this.selections = [];
            this.pushResult = null;
            this.error = '';
        },
        async openDetail(materialId, silent) {
            if (!materialId) return;
            try {
                const data = await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}`));
                this.detail = data.material || null;
                await this.loadCases(materialId);
                this.error = '';
            } catch (reason) {
                if (!silent) this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        async loadCases(materialId) {
            try {
                const data = await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}/cases`));
                this.cases = data.cases || [];
            } catch {
                this.cases = [];
            }
        },
        pickFile() {
            // 原生 input 被面板样式拉伸后按钮在部分浏览器/DPI 下不可见：
            // 隐藏原生 input、以显式按钮触发文件选择（跨浏览器稳定）
            this.$refs.fileInput && this.$refs.fileInput.click();
        },
        onFileChange(event) {
            const files = event.target && event.target.files;
            this.file = files && files.length ? files[0] : null;
            if (this.file && !this.uploadName) this.uploadName = this.file.name.replace(/\.zip$/i, '');
        },
        async uploadMaterial() {
            if (!this.file) return;
            this.uploading = true;
            try {
                const name = (this.uploadName || this.file.name || '').trim();
                if (!name) throw new Error('脚本名必填');
                let path = '/api/test/materials';
                // 归属执行器随上传头带上（§4.5）：pytest 族面板自动打 executor_id="pytest"
                const headers = { 'Content-Type': 'application/zip', 'X-Material-Executor': 'pytest' };
                if (isLatin1(name)) {
                    headers['X-Material-Name'] = name;
                } else {
                    const query = new URLSearchParams({ name });
                    path = `/api/test/materials?${query.toString()}`;
                }
                const data = await fetchJson(apiUrl(path), { method: 'POST', headers, body: this.file });
                const material = data.material || {};
                const materialId = this.materialIdOf(material);
                this.notice = data.deduplicated
                    ? `内容指纹命中既有材料（deduplicated），复用 id=${materialId}`
                    : `脚本已上传：${material.name || ''}（id=${materialId}）`;
                this.file = null;
                // 上传成功自动展开「当前脚本」处理区（含 deduplicated 复用 id）
                await this.openDetail(materialId);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.uploading = false;
            }
        },
        async runInspect() {
            const materialId = this.materialIdOf(this.detail);
            if (!materialId || this.inspecting) return;
            this.inspecting = true;
            this.inspectRecord = null;
            try {
                const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/inspect`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: this.inspectMode })
                });
                this.notice = `inspect 已提交（${this.inspectMode}）：request_id=${data.request_id}`;
                await this.pollInspect(data.request_id, materialId);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
                this.inspecting = false;
            }
        },
        async pollInspect(requestId, materialId) {
            // 轮询至终结态（inspecting → done / failed）；后端看门狗保证 inspect_timeout_s 内终结
            for (let attempt = 0; attempt < 150 && !this.disposed; attempt += 1) {
                await new Promise((resolve) => window.setTimeout(resolve, 2000));
                if (this.disposed) return;
                let record = null;
                try {
                    const data = await fetchJson(apiUrl(`/api/pytest/inspects/${encodeURIComponent(requestId)}`));
                    record = data.inspect || null;
                } catch (reason) {
                    this.error = String(reason && reason.message ? reason.message : reason);
                    this.inspecting = false;
                    return;
                }
                this.inspectRecord = record;
                if (record && record.state !== 'inspecting') {
                    this.inspecting = false;
                    this.notice = record.state === 'done'
                        ? `inspect 完成（${record.mode}，耗时 ${this.formatDuration(record.duration_s)}）`
                        : `inspect 失败：${record.error || '见问题列表'}`;
                    await this.openDetail(materialId, true);
                    return;
                }
            }
            this.inspecting = false;
        },
        async saveSelection() {
            const materialId = this.materialIdOf(this.detail);
            const name = (this.selectionName || '').trim();
            if (!materialId || !this.selectedCases.length) return;
            if (!name) {
                this.error = '选择集名称必填';
                return;
            }
            this.savingSelection = true;
            try {
                const caseIds = this.selectedCases.map((item) => item.case_id);
                const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/selections`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, case_ids: caseIds })
                });
                this.selections.unshift({
                    selection_id: data.selection_id,
                    name,
                    count: caseIds.length,
                    include: true
                });
                this.notice = `选择集已保存：selection_id=${data.selection_id}`;
                this.selectionName = '';
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.savingSelection = false;
            }
        },
        async pushMaterial() {
            const materialId = this.materialIdOf(this.detail);
            if (!materialId) return;
            this.pushing = true;
            try {
                const included = this.selections.filter((item) => item.include).map((item) => item.selection_id);
                const body = included.length ? JSON.stringify({ selection_ids: included }) : '';
                const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/push`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body
                });
                this.pushResult = { pushed: data.pushed || [], failed: data.failed || [] };
                this.notice = `预推完成：成功 ${this.pushResult.pushed.length} 台，失败 ${this.pushResult.failed.length} 台`;
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.pushing = false;
            }
        },
        downloadMaterial(materialId) {
            if (!materialId) return;
            this.downloadZip(materialId);
        },
        async downloadZip(materialId) {
            // <a> 带不上 X-AETP-Token 头，走 fetch blob 下载（同 TasksPage 本地产物口径）
            try {
                const response = await fetch(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}/download`), {
                    headers: authHeaders()
                });
                if (!response.ok) throw new Error(`下载失败 (${response.status})`);
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = `${materialId}.zip`;
                document.body.appendChild(link);
                link.click();
                link.remove();
                URL.revokeObjectURL(url);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        async copyText(text) {
            try {
                await navigator.clipboard.writeText(String(text || ''));
                this.notice = `已复制：${text}`;
            } catch {
                this.error = '复制失败，请手动选择文本复制';
            }
        },
        materialIdOf(material) {
            return String((material && (material.material_id || material.id)) || '');
        },
        shortSha(value) {
            const text = String(value || '');
            return text ? `${text.slice(0, 12)}…` : '—';
        },
        stateText(state) {
            return STATE_TEXT[state] || state || '—';
        },
        stateSeverity(state) {
            return { uploaded: 'info', analyzing: 'warn', analyzed: 'success', failed: 'danger' }[state] || 'secondary';
        },
        inspectStateText(state) {
            return { inspecting: '进行中', done: '完成', failed: '失败' }[state] || state || '—';
        },
        inspectStateSeverity(state) {
            return { inspecting: 'warn', done: 'success', failed: 'danger' }[state] || 'secondary';
        },
        problemSeverity(severity) {
            return { error: 'danger', warning: 'warn' }[severity] || 'info';
        },
        formatDuration(seconds) {
            const value = Number(seconds);
            if (!(value >= 0)) return '—';
            return `${value.toFixed(1)}s`;
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
/* 自适应面板容器（页面内嵌 slot）：随宿主列宽伸缩，不做整页布局 */
.materials-panel {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
}

.materials-panel__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.materials-panel__header h3 {
    margin: 0;
}

.materials-panel__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.materials-panel__muted {
    color: var(--text-color-secondary);
}

.materials-panel__actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    white-space: nowrap;
    flex-wrap: wrap;
}

.materials-panel__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.materials-panel__filter {
    min-width: 12rem;
}

.materials-panel__form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.materials-panel__name {
    max-width: 20rem;
}

.materials-panel__file {
    /* 视觉隐藏（保留键盘/无障碍可达）：选择动作走「选择 zip 文件」按钮 */
    position: absolute;
    width: 0;
    height: 0;
    opacity: 0;
    overflow: hidden;
}

.materials-panel__picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.materials-panel__mono {
    font-family: monospace;
    font-size: 0.8125rem;
    word-break: break-all;
}

.materials-panel__problem {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding: 0.25rem 0;
    font-size: 0.875rem;
}

.materials-panel__list {
    margin: 0.5rem 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
}

.materials-panel__list li {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.materials-panel__check {
    display: flex;
    align-items: center;
    gap: 0.375rem;
}

.materials-panel__tag {
    margin-right: 0.25rem;
}
</style>
