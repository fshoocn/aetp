<!--
  已退役：MaterialsPanel.vue 取代（脚本管理页 slot 面板 scripts:executor-pytest）。
  本页（/pytest/materials）与 SubmitPage.vue（/pytest/submit）已随 W2-D 同批退役：
  ui_service.py 不再注册旧两页，全部能力已迁入 ui/MaterialsPanel.vue（上传自动带
  X-Material-Executor: pytest）。文件保留仅作迁移对照参考，勿再改功能。
-->
<template>
    <section class="materials-page">
        <header class="materials-page__header">
            <div>
                <h2>测试材料</h2>
                <p class="materials-page__hint">上传脚本包（zip）→ 编辑组装配方 → 分析 / 解析 → 选例 → 预推；材料内容对核心不透明，语义归 pytest 执行器族</p>
            </div>
            <div class="materials-page__actions">
                <Button label="刷新" icon="pi pi-refresh" size="small" :loading="loading" @click="load()" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="info" :closable="true" @close="notice = ''">{{ notice }}</Message>

        <Panel header="上传材料（zip）">
            <div class="materials-page__form">
                <input type="file" accept=".zip,application/zip" class="materials-page__file" @change="onFileChange" />
                <InputText v-model="uploadName" placeholder="材料名（默认取文件名）" class="materials-page__name" />
                <textarea
                    v-model="uploadRecipe"
                    class="materials-page__textarea"
                    placeholder='配方 recipe（可选，JSON 对象，如 {"layout": [], "entry": "tests"}）'
                ></textarea>
                <div class="materials-page__actions">
                    <Button label="上传" icon="pi pi-upload" size="small" :loading="uploading" :disabled="!file" @click="uploadMaterial" />
                </div>
            </div>
            <p class="materials-page__hint">
                上传按 <code>(sha256, recipe_fingerprint)</code> 去重（§4.3）：同内容同配方命中既有材料并提示
                <code>deduplicated</code>，同内容不同配方视为不同测试工程。
            </p>
        </Panel>

        <div class="materials-page__toolbar">
            <Select
                v-model="stateFilter"
                :options="stateFilterOptions"
                optionLabel="label"
                optionValue="value"
                placeholder="全部状态"
                showClear
                class="materials-page__filter"
                @change="onFilterChange"
            />
            <div class="materials-page__actions">
                <Button
                    label="上一页"
                    icon="pi pi-chevron-left"
                    size="small"
                    severity="secondary"
                    outlined
                    :disabled="offset <= 0 || loading"
                    @click="prevPage"
                />
                <span class="materials-page__muted">第 {{ offset + 1 }} 条起</span>
                <Button
                    label="下一页"
                    icon="pi pi-chevron-right"
                    size="small"
                    severity="secondary"
                    outlined
                    :disabled="!hasMore || loading"
                    @click="nextPage"
                />
            </div>
        </div>

        <DataTable
            :value="materials"
            dataKey="material_id"
            :loading="loading"
            :rowHover="true"
            scrollable
            selectionMode="single"
            v-model:selection="selected"
            @row-select="onRowSelect"
        >
            <Column field="name" header="名称" sortable style="min-width: 10rem">
                <template #body="{ data }">{{ data.name || '—' }}</template>
            </Column>
            <Column field="state" header="状态" sortable style="min-width: 8rem">
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
                <span class="materials-page__muted">暂无材料，先上传 zip 脚本包</span>
            </template>
        </DataTable>

        <Panel v-if="detail" :header="`${detail.name || materialIdOf(detail)} · 详情`">
            <div class="materials-page__actions">
                <Button label="下载 zip" icon="pi pi-download" size="small" severity="secondary" outlined @click="downloadMaterial" />
                <Select v-model="inspectMode" :options="inspectModeOptions" optionLabel="label" optionValue="value" class="materials-page__filter" />
                <Button label="开始 inspect" icon="pi pi-search" size="small" :loading="inspecting" @click="runInspect" />
                <Button label="删除" icon="pi pi-trash" size="small" severity="danger" text @click="deleteMaterial" />
                <Button label="关闭" icon="pi pi-times" size="small" severity="secondary" text @click="closeDetail" />
            </div>

            <p class="materials-page__hint">
                <span class="materials-page__mono">{{ materialIdOf(detail) }}</span>
                · {{ formatSize(detail.size) }} · 更新 {{ formatTime(detail.updated_ts) }}
                · sha256 <span class="materials-page__mono">{{ shortSha(detail.sha256) }}</span>
            </p>

            <h4>组装配方（recipe）</h4>
            <textarea v-model="recipeText" class="materials-page__textarea materials-page__textarea--tall"></textarea>
            <p class="materials-page__hint">
                保存后状态回 <code>uploaded</code> 需重新分析（配方指纹变化即新指纹）。任务下发的
                <code>recipe_override</code> 与本配方浅合并：<b>同名标量 / 对象替换，数组（含 layout）整体替换</b>。
            </p>
            <div class="materials-page__actions">
                <Button label="保存配方" icon="pi pi-save" size="small" :loading="savingRecipe" @click="saveRecipe" />
            </div>

            <h4>分析 / 解析结果（inspect）</h4>
            <p v-if="inspectRecord" class="materials-page__hint">
                <Tag :value="inspectStateText(inspectRecord.state)" :severity="inspectStateSeverity(inspectRecord.state)" />
                · {{ inspectRecord.mode }} · 节点 {{ inspectRecord.node_id || '—' }}
                · 耗时 {{ formatDuration(inspectRecord.duration_s) }}
                <template v-if="inspectRecord.late"> · 迟到应答（已收下刷新）</template>
                <template v-if="inspectRecord.error"> · {{ inspectRecord.error }}</template>
            </p>
            <p v-else class="materials-page__muted">暂无 inspect 记录（「分析」查语法 / 依赖 / 可收集性，「解析」产出用例清单）</p>
            <template v-if="problems.length">
                <div v-for="(problem, index) in problems" :key="index" class="materials-page__problem">
                    <Tag :value="problem.severity || 'info'" :severity="problemSeverity(problem.severity)" />
                    <span class="materials-page__mono">{{ problem.code }}</span>
                    <span v-if="problem.path" class="materials-page__mono materials-page__muted">{{ problem.path }}<template v-if="problem.line">:{{ problem.line }}</template></span>
                    <span>{{ problem.message }}</span>
                </div>
            </template>

            <h4>用例清单</h4>
            <div class="materials-page__toolbar">
                <Select
                    v-model="markerFilter"
                    :options="markerOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="全部 marker"
                    showClear
                    class="materials-page__filter"
                />
                <span class="materials-page__muted">已勾选 {{ selectedCases.length }} 条</span>
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
                        <span class="materials-page__mono" :title="data.case_id">{{ data.case_id }}</span>
                    </template>
                </Column>
                <Column field="path" header="文件" style="min-width: 8rem">
                    <template #body="{ data }">{{ data.path || '—' }}</template>
                </Column>
                <Column field="markers" header="markers" style="min-width: 8rem">
                    <template #body="{ data }">
                        <span class="materials-page__muted">{{ (data.markers || []).join(', ') || '—' }}</span>
                    </template>
                </Column>
                <Column field="status" header="状态" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.status || 'ok' }}</template>
                </Column>
            </DataTable>
            <p v-else class="materials-page__muted">暂无用例清单（先以 collect 模式 inspect 解析用例）</p>

            <div class="materials-page__actions">
                <InputText v-model="selectionName" placeholder="选择集名称" class="materials-page__name" />
                <Button
                    label="保存选择集"
                    icon="pi pi-save"
                    size="small"
                    :loading="savingSelection"
                    :disabled="!selectedCases.length"
                    @click="saveSelection"
                />
            </div>
            <p class="materials-page__hint">勾选用例后保存选择集 → 得到 <code>selection_id</code>（Job 只带引用，载荷恒小，§5/§11）。</p>
            <ul v-if="selections.length" class="materials-page__list">
                <li v-for="sel in selections" :key="sel.selection_id">
                    <label class="materials-page__check">
                        <input v-model="sel.include" type="checkbox" />
                        {{ sel.name }}（{{ sel.count }} 条）
                    </label>
                    <span class="materials-page__mono">{{ sel.selection_id }}</span>
                    <Button label="复制" icon="pi pi-copy" size="small" severity="secondary" text @click="copyText(sel.selection_id)" />
                </li>
            </ul>

            <h4>预推（下发前置）</h4>
            <div class="materials-page__actions">
                <Button label="预推到候选节点" icon="pi pi-upload" size="small" :loading="pushing" @click="pushMaterial" />
            </div>
            <p class="materials-page__hint">按上方勾选的 selection_ids 随材料元包同步给全部 <code>executor/pytest</code> 候选节点（§5.2）；失败节点可重试或依赖 missing 兜底。</p>
            <template v-if="pushResult">
                <p class="materials-page__hint">
                    成功节点：
                    <template v-if="pushResult.pushed.length">
                        <Tag v-for="node in pushResult.pushed" :key="node" :value="node" severity="success" class="materials-page__tag" />
                    </template>
                    <span v-else class="materials-page__muted">无</span>
                    ；失败节点：
                    <template v-if="pushResult.failed.length">
                        <Tag v-for="node in pushResult.failed" :key="node" :value="node" severity="danger" class="materials-page__tag" />
                    </template>
                    <span v-else class="materials-page__muted">无</span>
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

// HTTP 头值是 Latin-1 字节串：材料名 / 配方含非 Latin-1 字符（如中文）时
// fetch 会直接抛错，改走 API 既有的 name / recipe 查询参数兜底
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
            materials: [],
            detail: null,
            selected: null,
            error: '',
            notice: '',
            loading: false,
            stateFilter: null,
            offset: 0,
            limit: 20,
            stateFilterOptions: Object.entries(STATE_TEXT).map(([value, label]) => ({ label, value })),
            // 上传区
            file: null,
            uploadName: '',
            uploadRecipe: '',
            uploading: false,
            // 配方编辑
            recipeText: '',
            savingRecipe: false,
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
        hasMore() {
            return this.materials.length >= this.limit;
        },
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
    mounted() {
        this.load();
    },
    beforeUnmount() {
        this.disposed = true;
    },
    methods: {
        async load(silent) {
            if (!silent) this.loading = true;
            try {
                const query = new URLSearchParams({ limit: String(this.limit), offset: String(this.offset) });
                if (this.stateFilter) query.set('state', this.stateFilter);
                const data = await fetchJson(apiUrl(`/api/test/materials?${query.toString()}`));
                this.materials = (data.materials || []).map((item) => ({ ...item, material_id: this.materialIdOf(item) }));
                this.error = '';
                if (this.detail) await this.openDetail(this.materialIdOf(this.detail), true);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.loading = false;
            }
        },
        onFilterChange() {
            this.offset = 0;
            this.load();
        },
        prevPage() {
            this.offset = Math.max(0, this.offset - this.limit);
            this.load();
        },
        nextPage() {
            this.offset += this.limit;
            this.load();
        },
        onRowSelect(event) {
            this.openDetail(this.materialIdOf(event.data));
        },
        closeDetail() {
            this.detail = null;
            this.selected = null;
            this.inspectRecord = null;
            this.cases = [];
            this.selectedCases = [];
            this.markerFilter = null;
            this.pushResult = null;
        },
        async openDetail(materialId, silent) {
            if (!materialId) return;
            try {
                const data = await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}`));
                this.detail = data.material || null;
                this.recipeText = JSON.stringify((this.detail && this.detail.recipe) || {}, null, 2);
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
                if (!name) throw new Error('材料名必填');
                const recipeText = this.uploadRecipe.trim();
                if (recipeText) {
                    const parsed = JSON.parse(recipeText);
                    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                        throw new Error('配方需是 JSON 对象');
                    }
                }
                let path = '/api/test/materials';
                const headers = { 'Content-Type': 'application/zip' };
                if (isLatin1(name) && isLatin1(recipeText)) {
                    headers['X-Material-Name'] = name;
                    if (recipeText) headers['X-Material-Recipe'] = recipeText;
                } else {
                    const query = new URLSearchParams({ name });
                    if (recipeText) query.set('recipe', recipeText);
                    path = `/api/test/materials?${query.toString()}`;
                }
                const data = await fetchJson(apiUrl(path), { method: 'POST', headers, body: this.file });
                const material = data.material || {};
                const materialId = this.materialIdOf(material);
                this.notice = data.deduplicated
                    ? `内容 + 配方指纹命中既有材料（deduplicated），复用 id=${materialId}`
                    : `材料已上传：${material.name || ''}（id=${materialId}）`;
                this.file = null;
                this.uploadRecipe = '';
                await this.load();
                await this.openDetail(materialId);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.uploading = false;
            }
        },
        async saveRecipe() {
            const materialId = this.materialIdOf(this.detail);
            if (!materialId) return;
            this.savingRecipe = true;
            try {
                const recipe = JSON.parse(this.recipeText || '{}');
                if (!recipe || typeof recipe !== 'object' || Array.isArray(recipe)) {
                    throw new Error('配方需是 JSON 对象');
                }
                await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/recipe`), {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(recipe)
                });
                this.notice = '配方已保存；材料状态回 uploaded，需重新分析 / 解析';
                await this.load(true);
            } catch (reason) {
                this.error = reason instanceof SyntaxError ? `配方不是合法 JSON：${reason.message}` : String(reason && reason.message ? reason.message : reason);
            } finally {
                this.savingRecipe = false;
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
        deleteMaterial() {
            const materialId = this.materialIdOf(this.detail);
            if (!materialId) return;
            // 插件 SFC 拿不到 useConfirm，走宿主 app.use(ConfirmationService) 装的 $confirm
            this.$confirm.require({
                header: '删除材料',
                message: `确认删除材料「${materialId}」？用例清单与选择集一并删除。`,
                icon: 'pi pi-exclamation-triangle',
                acceptLabel: '删除',
                rejectLabel: '取消',
                accept: async () => {
                    try {
                        await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}`), { method: 'DELETE' });
                        this.notice = `材料已删除：${materialId}`;
                        this.closeDetail();
                        await this.load();
                    } catch (reason) {
                        if (reason && (reason.code === 'material_in_use' || reason.status === 409)) {
                            this.error = `引用冲突（material_in_use）：${reason.message}——被任务 params / 选择集引用的材料禁止删除，请先解除引用`;
                        } else {
                            this.error = String(reason && reason.message ? reason.message : reason);
                        }
                    }
                }
            });
        },
        downloadMaterial() {
            const materialId = this.materialIdOf(this.detail);
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
.materials-page {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.materials-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.materials-page__header h2 {
    margin: 0;
}

.materials-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.materials-page__muted {
    color: var(--text-color-secondary);
}

.materials-page__actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    white-space: nowrap;
    flex-wrap: wrap;
}

.materials-page__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.materials-page__filter {
    min-width: 12rem;
}

.materials-page__form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.materials-page__name {
    max-width: 20rem;
}

.materials-page__file {
    font-size: 0.875rem;
}

.materials-page__textarea {
    width: 100%;
    min-height: 5rem;
    padding: 0.5rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    color: var(--text-color);
    font-family: monospace;
    font-size: 0.8125rem;
}

.materials-page__textarea--tall {
    min-height: 10rem;
}

.materials-page__mono {
    font-family: monospace;
    font-size: 0.8125rem;
    word-break: break-all;
}

.materials-page__problem {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    flex-wrap: wrap;
    padding: 0.25rem 0;
    font-size: 0.875rem;
}

.materials-page__list {
    margin: 0.5rem 0 0;
    padding-left: 1.25rem;
}

.materials-page__list li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.125rem 0;
}

.materials-page__check {
    display: inline-flex;
    gap: 0.375rem;
    align-items: center;
}

.materials-page__tag {
    margin-right: 0.25rem;
}
</style>
