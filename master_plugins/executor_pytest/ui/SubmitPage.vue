<template>
    <section class="submit-page">
        <header class="submit-page__header">
            <div>
                <h2>下发测试任务</h2>
                <p class="submit-page__hint">
                    按按钮顺序提交：① 保存选择集（有勾选时）→ ② 预推材料 → ③ 组装 StageSpec 下发；
                    预推失败的节点仍可提交（失败节点走 missing 兜底，§5.2）
                </p>
            </div>
            <div class="submit-page__actions">
                <Button label="刷新材料 / 能力" icon="pi pi-refresh" size="small" :loading="loadingMaterials" @click="reloadAll" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="info" :closable="true" @close="notice = ''">{{ notice }}</Message>

        <Panel header="基本信息">
            <div class="submit-page__grid">
                <label class="submit-page__field">
                    <span class="submit-page__label">名称（必填）</span>
                    <InputText v-model="form.name" placeholder="任务名称" class="submit-page__input" />
                </label>
                <label class="submit-page__field">
                    <span class="submit-page__label">优先级（大者先出队）</span>
                    <input v-model.number="form.priority" type="number" class="submit-page__native" />
                </label>
                <label class="submit-page__field">
                    <span class="submit-page__label">失败策略</span>
                    <Select v-model="form.failurePolicy" :options="failurePolicyOptions" optionLabel="label" optionValue="value" />
                </label>
                <label class="submit-page__field">
                    <span class="submit-page__label">任务总超时（秒，0 = 不限）</span>
                    <input v-model.number="form.timeoutS" type="number" min="0" class="submit-page__native" />
                </label>
            </div>
            <h4>标签（labels）</h4>
            <div v-for="(row, index) in labelsRows" :key="index" class="submit-page__kv">
                <InputText v-model="row.key" placeholder="键" />
                <InputText v-model="row.value" placeholder="值" />
                <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(labelsRows, index)" />
            </div>
            <Button label="添加标签" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(labelsRows)" />
        </Panel>

        <Panel header="材料与选例">
            <div class="submit-page__grid">
                <label class="submit-page__field">
                    <span class="submit-page__label">材料（material_id）</span>
                    <Select
                        v-model="materialId"
                        :options="materialOptions"
                        optionLabel="label"
                        optionValue="value"
                        placeholder="选择材料"
                        showClear
                        class="submit-page__input"
                        @change="onMaterialChange"
                    />
                </label>
                <label class="submit-page__field">
                    <span class="submit-page__label">既有选择集（selection_id，可手填）</span>
                    <InputText v-model="selectionIdInput" placeholder="sel-…" class="submit-page__input" />
                </label>
            </div>
            <h4>手填 targets（可选，逐行 nodeid / 文件 / 目录）</h4>
            <textarea v-model="targetsText" class="submit-page__textarea" placeholder="tests/test_a.py::TestX::test_y"></textarea>
            <p class="submit-page__hint">
                执行目标优先级：分片 <code>unit_refs</code> → <code>params.targets</code> → <code>params.selection_id</code> 展开 →
                配方 <code>entry</code> 兜底；<code>material_id / selection_id / targets</code> 至少填其一。
            </p>

            <h4>用例清单勾选</h4>
            <div class="submit-page__toolbar">
                <Select
                    v-model="markerFilter"
                    :options="markerOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="全部 marker"
                    showClear
                    class="submit-page__filter"
                />
                <span class="submit-page__muted">已勾选 {{ selectedCases.length }} 条</span>
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
                        <span class="submit-page__mono" :title="data.case_id">{{ data.case_id }}</span>
                    </template>
                </Column>
                <Column field="path" header="文件" style="min-width: 8rem">
                    <template #body="{ data }">{{ data.path || '—' }}</template>
                </Column>
                <Column field="markers" header="markers" style="min-width: 8rem">
                    <template #body="{ data }">
                        <span class="submit-page__muted">{{ (data.markers || []).join(', ') || '—' }}</span>
                    </template>
                </Column>
            </DataTable>
            <p v-else class="submit-page__muted">暂无用例（选材料后自动加载；未解析用例的材料可直接手填 targets）</p>

            <div class="submit-page__actions">
                <InputText v-model="selectionName" placeholder="选择集名称" class="submit-page__input" />
                <Button
                    label="保存选择集"
                    icon="pi pi-save"
                    size="small"
                    :loading="savingSelection"
                    :disabled="!selectedCases.length"
                    @click="saveSelection"
                />
            </div>
            <p v-if="savedSelectionId" class="submit-page__hint">
                已保存 selection_id：<span class="submit-page__mono">{{ savedSelectionId }}</span>
                <Button label="复制" icon="pi pi-copy" size="small" severity="secondary" text @click="copyText(savedSelectionId)" />
            </p>
        </Panel>

        <Panel header="参数（按能力信封 detail.schema 动态生成）">
            <p class="submit-page__hint">
                表单由 <code>executor/pytest</code> 能力信封的 <code>detail.schema</code>（§11.1）生成，换执行器自动跟随；
                <code>material_id / selection_id / targets / env / recipe_override</code> 由专用区块装配，不在下方重复。
                表单与 raw JSON 双向同步，冲突时 <b>raw 优先</b>。
            </p>
            <p v-if="schemaHint" class="submit-page__hint">{{ schemaHint }}</p>
            <div v-for="prop in schemaProps" :key="prop.key" class="submit-page__field">
                <span class="submit-page__label">{{ prop.key }}<span v-if="prop.title" class="submit-page__muted"> — {{ prop.title }}</span></span>
                <InputText v-if="prop.type === 'string'" v-model="dynamicValues[prop.key]" class="submit-page__input" @input="syncFormToRaw" />
                <textarea
                    v-else-if="prop.type === 'array'"
                    v-model="arrayText[prop.key]"
                    class="submit-page__textarea"
                    placeholder="每行一条"
                    @input="syncFormToRaw"
                ></textarea>
                <template v-else-if="prop.type === 'object'">
                    <div v-for="(row, index) in objectRows[prop.key]" :key="index" class="submit-page__kv">
                        <InputText v-model="row.key" placeholder="键" @input="syncFormToRaw" />
                        <InputText v-model="row.value" placeholder="值" @input="syncFormToRaw" />
                        <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(objectRows[prop.key], index)" />
                    </div>
                    <Button label="添加" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(objectRows[prop.key]); syncFormToRaw()" />
                </template>
            </div>
            <h4>raw JSON 兜底（params）</h4>
            <textarea v-model="paramsRaw" class="submit-page__textarea submit-page__textarea--tall" @input="syncRawToForm"></textarea>
            <p v-if="paramsRawError" class="submit-page__error-text">{{ paramsRawError }}</p>
        </Panel>

        <Panel header="环境变量（params.env）">
            <div v-for="(row, index) in envRows" :key="index" class="submit-page__kv">
                <InputText v-model="row.key" placeholder="键（如 PYTHONPATH）" @input="validateEnv" />
                <InputText v-model="row.value" placeholder="值" />
                <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(envRows, index)" />
            </div>
            <Button label="添加环境变量" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(envRows)" />
            <p v-if="envError" class="submit-page__error-text">{{ envError }}</p>
            <p class="submit-page__hint">
                环境变量为增量覆盖（argv 化注入，不进 shell）；<b>保留前缀 <code>AETP_*</code> 拒绝</b>（UI 拦截，执行端纵深再校验）。
            </p>
        </Panel>

        <Panel header="分片与配方覆盖">
            <div class="submit-page__grid">
                <label class="submit-page__field">
                    <span class="submit-page__label">分片模式</span>
                    <Select v-model="shardMode" :options="shardModeOptions" optionLabel="label" optionValue="value" />
                </label>
                <label class="submit-page__field">
                    <span class="submit-page__label">分片数 hint（0 = 执行器自定）</span>
                    <input v-model.number="shardHint" type="number" min="0" class="submit-page__native" />
                </label>
            </div>
            <h4>配方覆盖（params.recipe_override）</h4>
            <textarea
                v-model="recipeOverrideRaw"
                class="submit-page__textarea"
                placeholder='{"args": ["-q", "-x"]}'
            ></textarea>
            <p v-if="recipeOverrideError" class="submit-page__error-text">{{ recipeOverrideError }}</p>
            <p class="submit-page__hint">
                与材料配方浅合并：<b>同名标量 / 对象替换，数组（含 layout）整体替换</b>（临时覆盖，不回写材料）。
            </p>
        </Panel>

        <Panel header="提交流程">
            <div class="submit-page__actions">
                <Button
                    label="① 保存选择集"
                    icon="pi pi-save"
                    size="small"
                    severity="secondary"
                    outlined
                    :loading="savingSelection"
                    :disabled="!selectedCases.length"
                    @click="saveSelection"
                />
                <Button
                    label="② 预推材料"
                    icon="pi pi-upload"
                    size="small"
                    severity="secondary"
                    outlined
                    :loading="pushing"
                    :disabled="!materialId"
                    @click="pushMaterial"
                />
                <Button label="③ 下发任务" icon="pi pi-send" size="small" :loading="submitting" @click="submitTask" />
            </div>
            <template v-if="pushResult">
                <p class="submit-page__hint">
                    预推成功节点：
                    <template v-if="pushResult.pushed.length">
                        <Tag v-for="node in pushResult.pushed" :key="node" :value="node" severity="success" class="submit-page__tag" />
                    </template>
                    <span v-else class="submit-page__muted">无</span>
                    ；失败节点：
                    <template v-if="pushResult.failed.length">
                        <Tag v-for="node in pushResult.failed" :key="node" :value="node" severity="danger" class="submit-page__tag" />
                    </template>
                    <span v-else class="submit-page__muted">无</span>
                </p>
            </template>
            <template v-if="submitResult">
                <p class="submit-page__hint">
                    任务已下发：task_id = <span class="submit-page__mono">{{ submitResult.task_id }}</span>
                    <Button label="打开任务页" icon="pi pi-external-link" size="small" severity="secondary" text @click="goTasks" />
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

// 专用区块装配的保留键：动态表单不再重复渲染（§9.2 表格末列）
const RESERVED_PARAMS = ['material_id', 'selection_id', 'targets', 'env', 'recipe_override'];
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export default {
    data() {
        return {
            error: '',
            notice: '',
            // 基本信息
            form: { name: '', priority: 0, failurePolicy: 'fail_fast', timeoutS: 0 },
            failurePolicyOptions: [
                { label: 'fail_fast（任一 Job 失败即停）', value: 'fail_fast' },
                { label: 'continue（跑完全部 Job）', value: 'continue' }
            ],
            labelsRows: [],
            // 材料与选例
            materials: [],
            materialId: null,
            loadingMaterials: false,
            cases: [],
            selectedCases: [],
            markerFilter: null,
            targetsText: '',
            selectionIdInput: '',
            selectionName: '',
            savedSelectionId: '',
            // 参数（动态表单 + raw 兜底）
            schema: null,
            schemaProps: [],
            schemaHint: '',
            dynamicValues: {},
            arrayText: {},
            objectRows: {},
            paramsRaw: '',
            paramsRawError: '',
            // 环境变量
            envRows: [],
            envError: '',
            // 分片
            shardMode: 'none',
            shardModeOptions: [
                { label: 'none（不分片）', value: 'none' },
                { label: 'plan（执行器规划分片）', value: 'plan' }
            ],
            shardHint: 0,
            // 配方覆盖
            recipeOverrideRaw: '',
            recipeOverrideError: '',
            // 提交流程
            savingSelection: false,
            pushing: false,
            pushResult: null,
            submitting: false,
            submitResult: null
        };
    },
    computed: {
        materialOptions() {
            return this.materials.map((item) => ({
                label: `${item.name || this.materialIdOf(item)}（${item.state || '—'}）`,
                value: this.materialIdOf(item)
            }));
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
        this.reloadAll();
    },
    methods: {
        async reloadAll() {
            await Promise.all([this.loadMaterials(), this.loadSchema()]);
        },
        async loadMaterials() {
            this.loadingMaterials = true;
            try {
                const data = await fetchJson(apiUrl('/api/test/materials?limit=200'));
                this.materials = data.materials || [];
                this.error = '';
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.loadingMaterials = false;
            }
        },
        async loadSchema() {
            // 参数 schema 动态发现（D17）：从心跳能力信封找 executor/pytest 的 detail.schema
            try {
                const data = await fetchJson(apiUrl('/api/heartbeat/capabilities?kind=executor'));
                const entry = (data.capabilities || []).find(
                    (item) => item.kind === 'executor' && item.id === 'pytest' && item.detail && item.detail.schema
                );
                if (!entry) {
                    this.schemaHint = '未发现 executor/pytest 能力信封（无在线执行节点或心跳未上报），参数请直接编辑 raw JSON。';
                    return;
                }
                this.schema = entry.detail.schema || null;
                this.initSchemaFields();
                this.schemaHint = `动态表单已按能力信封生成（节点 ${entry.node_id || '—'}）。`;
            } catch (reason) {
                this.schemaHint = `能力信封加载失败（${reason && reason.message ? reason.message : reason}），参数请直接编辑 raw JSON。`;
            }
        },
        initSchemaFields() {
            const props = (this.schema && this.schema.properties) || {};
            const reserved = new Set(RESERVED_PARAMS);
            this.schemaProps = Object.entries(props)
                .filter(([key]) => !reserved.has(key))
                .map(([key, def]) => ({
                    key,
                    type: (def && def.type) || 'string',
                    title: (def && def.title) || ''
                }));
            for (const prop of this.schemaProps) {
                if (prop.type === 'array') this.arrayText[prop.key] = this.arrayText[prop.key] || '';
                else if (prop.type === 'object') this.objectRows[prop.key] = this.objectRows[prop.key] || [];
                else this.dynamicValues[prop.key] = this.dynamicValues[prop.key] || '';
            }
            this.syncFormToRaw();
        },
        // ---- 参数表单 ⇄ raw 同步（raw 优先） ------------------------------
        buildParamsFromForm() {
            // 先并入 raw 里表单不认识的键（保留手改），再让表单值覆盖 schema 键
            const out = {};
            const rawParsed = this.parseRaw();
            if (rawParsed) Object.assign(out, rawParsed);
            for (const prop of this.schemaProps) {
                const key = prop.key;
                if (prop.type === 'array') {
                    const lines = String(this.arrayText[key] || '')
                        .split('\n')
                        .map((line) => line.trim())
                        .filter(Boolean);
                    if (lines.length) out[key] = lines;
                    else delete out[key];
                } else if (prop.type === 'object') {
                    const obj = {};
                    for (const row of this.objectRows[key] || []) {
                        if (String(row.key || '').trim()) obj[String(row.key).trim()] = row.value;
                    }
                    if (Object.keys(obj).length) out[key] = obj;
                    else delete out[key];
                } else {
                    const value = String(this.dynamicValues[key] || '').trim();
                    if (value) out[key] = value;
                    else delete out[key];
                }
            }
            return out;
        },
        parseRaw() {
            const text = String(this.paramsRaw || '').trim();
            if (!text) return null;
            try {
                const parsed = JSON.parse(text);
                if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
                return parsed;
            } catch {
                return null;
            }
        },
        syncFormToRaw() {
            this.paramsRaw = JSON.stringify(this.buildParamsFromForm(), null, 2);
            this.paramsRawError = '';
        },
        syncRawToForm() {
            const text = String(this.paramsRaw || '').trim();
            if (!text) {
                this.paramsRawError = '';
                return;
            }
            let parsed = null;
            try {
                parsed = JSON.parse(text);
            } catch (reason) {
                this.paramsRawError = `raw 参数不是合法 JSON：${reason.message}`;
                return;
            }
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                this.paramsRawError = 'raw 参数需是 JSON 对象';
                return;
            }
            this.paramsRawError = '';
            for (const prop of this.schemaProps) {
                const value = parsed[prop.key];
                if (value === undefined) continue;
                if (prop.type === 'array') {
                    this.arrayText[prop.key] = Array.isArray(value) ? value.join('\n') : String(value);
                } else if (prop.type === 'object') {
                    const source = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
                    this.objectRows[prop.key] = Object.entries(source).map(([key, item]) => ({ key, value: String(item) }));
                } else {
                    this.dynamicValues[prop.key] = typeof value === 'string' ? value : JSON.stringify(value);
                }
            }
        },
        // ---- kv 编辑 ------------------------------------------------------
        addKv(rows) {
            rows.push({ key: '', value: '' });
        },
        removeKv(rows, index) {
            rows.splice(index, 1);
        },
        collectKv(rows) {
            const out = {};
            for (const row of rows) {
                const key = String(row.key || '').trim();
                if (key) out[key] = String(row.value ?? '');
            }
            return out;
        },
        validateEnv() {
            // UI 拦截：保留前缀 AETP_* 与非法标识符键直接拒绝（执行端再纵深校验）
            for (const row of this.envRows) {
                const key = String(row.key || '').trim();
                if (!key) {
                    if (String(row.value ?? '').trim()) {
                        this.envError = '环境变量键不能为空';
                        return false;
                    }
                    continue;
                }
                if (/^AETP_/i.test(key)) {
                    this.envError = `环境变量键 ${key} 使用保留前缀 AETP_*，已拦截`;
                    return false;
                }
                if (!ENV_KEY_RE.test(key)) {
                    this.envError = `环境变量键 ${key} 不合法（需匹配 [A-Za-z_][A-Za-z0-9_]*）`;
                    return false;
                }
            }
            this.envError = '';
            return true;
        },
        parseRecipeOverride() {
            const text = String(this.recipeOverrideRaw || '').trim();
            if (!text) {
                this.recipeOverrideError = '';
                return null;
            }
            let parsed = null;
            try {
                parsed = JSON.parse(text);
            } catch (reason) {
                this.recipeOverrideError = `配方覆盖不是合法 JSON：${reason.message}`;
                return null;
            }
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                this.recipeOverrideError = '配方覆盖需是 JSON 对象';
                return null;
            }
            this.recipeOverrideError = '';
            return parsed;
        },
        // ---- 材料与选例 ---------------------------------------------------
        async onMaterialChange() {
            this.cases = [];
            this.selectedCases = [];
            this.markerFilter = null;
            this.savedSelectionId = '';
            this.pushResult = null;
            if (!this.materialId) return;
            try {
                const data = await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(this.materialId)}/cases`));
                this.cases = data.cases || [];
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        async saveSelection() {
            const name = String(this.selectionName || '').trim();
            if (!this.materialId) {
                this.error = '请先选择材料再保存选择集';
                return;
            }
            if (!this.selectedCases.length) {
                this.error = '请先勾选用例';
                return;
            }
            if (!name) {
                this.error = '选择集名称必填';
                return;
            }
            this.savingSelection = true;
            try {
                const caseIds = this.selectedCases.map((item) => item.case_id);
                const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(this.materialId)}/selections`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, case_ids: caseIds })
                });
                this.savedSelectionId = data.selection_id;
                this.selectionIdInput = data.selection_id;
                this.notice = `选择集已保存：selection_id=${data.selection_id}（已填入 selection_id）`;
                this.error = '';
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.savingSelection = false;
            }
        },
        async pushMaterial() {
            if (!this.materialId) return;
            this.pushing = true;
            try {
                const selectionIds = [];
                const current = (this.savedSelectionId || this.selectionIdInput).trim();
                if (current) selectionIds.push(current);
                const body = selectionIds.length ? JSON.stringify({ selection_ids: selectionIds }) : '';
                const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(this.materialId)}/push`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body
                });
                this.pushResult = { pushed: data.pushed || [], failed: data.failed || [] };
                this.notice = `预推完成：成功 ${this.pushResult.pushed.length} 台，失败 ${this.pushResult.failed.length} 台（失败节点可继续提交，走 missing 兜底）`;
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.pushing = false;
            }
        },
        // ---- 下发 ---------------------------------------------------------
        async submitTask() {
            this.error = '';
            this.submitResult = null;
            const name = String(this.form.name || '').trim();
            if (!name) {
                this.error = '任务名称必填';
                return;
            }
            if (!this.validateEnv()) return;
            const recipeOverride = this.parseRecipeOverride();
            if (this.recipeOverrideError) return;
            // raw 优先：raw 缺省/非法时以动态表单为准
            const params = this.buildParamsFromForm();
            const rawParsed = this.parseRaw();
            if (String(this.paramsRaw || '').trim() && !rawParsed) {
                this.paramsRawError = 'raw 参数不是合法 JSON 对象，无法下发';
                return;
            }
            if (rawParsed) Object.assign(params, rawParsed);
            const targets = String(this.targetsText || '')
                .split('\n')
                .map((line) => line.trim())
                .filter(Boolean);
            const selectionId = (this.savedSelectionId || this.selectionIdInput || '').trim();
            if (this.materialId) params.material_id = this.materialId;
            if (selectionId) params.selection_id = selectionId;
            if (targets.length) params.targets = targets;
            const env = this.collectKv(this.envRows);
            if (Object.keys(env).length) params.env = env;
            if (recipeOverride) params.recipe_override = recipeOverride;
            if (!params.material_id && !params.selection_id && !(Array.isArray(params.targets) && params.targets.length)) {
                this.error = 'material_id / selection_id / targets 至少填其一';
                return;
            }
            // 幂等键 UI 生成（§9.2）：重复触发保护，改名重发即新键
            const idempotencyKey = `submit-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
            const spec = {
                name,
                stages: [
                    {
                        name: 'pytest-run',
                        executor: { id: 'pytest', kind: 'executor', labels: {} },
                        params,
                        shard: { mode: this.shardMode, hint: Number(this.shardHint) || 0 },
                        requires: {},
                        timeout_s: 0
                    }
                ],
                priority: Number(this.form.priority) || 0,
                labels: this.collectKv(this.labelsRows),
                idempotency_key: idempotencyKey,
                failure_policy: this.form.failurePolicy,
                timeout_s: Number(this.form.timeoutS) || 0
            };
            this.submitting = true;
            try {
                const data = await fetchJson(apiUrl('/api/test/tasks'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(spec)
                });
                this.submitResult = { task_id: data.task_id };
                this.notice = `任务已下发：task_id=${data.task_id}`;
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.submitting = false;
            }
        },
        goTasks() {
            // 深链到「测试任务」页（宿主路由；无路由时整页跳转）
            if (this.$router && typeof this.$router.push === 'function') {
                this.$router.push('/tasks');
            } else {
                window.location.assign('/tasks');
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
        }
    }
};
</script>

<style scoped>
/* 插件 SFC 用不了运行时 Tailwind：仅保留布局所需的少量样式，
   组件外观全部来自共享 PrimeVue 组件与宿主语义变量 */
.submit-page {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.submit-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.submit-page__header h2 {
    margin: 0;
}

.submit-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.submit-page__muted {
    color: var(--text-color-secondary);
}

.submit-page__error-text {
    margin: 0.25rem 0 0;
    color: var(--red-500);
    font-size: 0.875rem;
}

.submit-page__actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    white-space: nowrap;
    flex-wrap: wrap;
}

.submit-page__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.submit-page__filter {
    min-width: 12rem;
}

.submit-page__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
    gap: 0.75rem;
}

.submit-page__field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
}

.submit-page__label {
    font-size: 0.8125rem;
    color: var(--text-color-secondary);
}

.submit-page__input {
    width: 100%;
}

.submit-page__native {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    color: var(--text-color);
    font-size: 0.875rem;
}

.submit-page__textarea {
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

.submit-page__textarea--tall {
    min-height: 10rem;
}

.submit-page__kv {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    margin-bottom: 0.375rem;
}

.submit-page__kv .p-inputtext {
    flex: 1;
}

.submit-page__mono {
    font-family: monospace;
    font-size: 0.8125rem;
    word-break: break-all;
}

.submit-page__tag {
    margin-right: 0.25rem;
}
</style>
