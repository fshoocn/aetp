<!--
  测试计划（/plans，测试计划设计 §5.3）：列表 ⇄ 编辑器 ⇄ 下发面板单页。
  视图切换 query 驱动（?edit=<plan_id|new>，M1 裁决），不注册独立菜单页。
  动态参数表单迁移自已退役的 executor_pytest/ui/SubmitPage.vue（能力信封
  detail.schema 生成、表单 ⇄ raw JSON 双向同步、raw 优先），并按 M3/S5 裁决
  补 number/boolean/enum 分支；未知类型提示走 raw JSON。
-->
<template>
    <section class="plans-page">
        <!-- ============================== 列表视图 ============================== -->
        <template v-if="!editorOpen">
            <header class="plans-page__header">
                <div>
                    <h2>测试计划</h2>
                    <p class="plans-page__hint">
                        计划 = 可复用的多执行器步骤序列；下发时实例化为任务（labels 自动注入 plan_id / plan_name）
                    </p>
                </div>
                <div class="plans-page__actions">
                    <Button label="新建计划" icon="pi pi-plus" size="small" @click="openNew" />
                    <Button label="刷新" icon="pi pi-refresh" size="small" severity="secondary" outlined :loading="loadingPlans" @click="loadPlans()" />
                </div>
            </header>

            <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
            <Message v-if="notice" severity="info" :closable="true" @close="notice = ''">{{ notice }}</Message>

            <DataTable :value="rows" dataKey="plan_id" :loading="loadingPlans" :rowHover="true" scrollable>
                <Column field="name" header="名称" sortable style="min-width: 10rem">
                    <template #body="{ data }">{{ data.name || '—' }}</template>
                </Column>
                <Column field="step_count" header="步骤数" sortable style="min-width: 5rem">
                    <template #body="{ data }">{{ data.step_count }}</template>
                </Column>
                <Column header="执行器组合" style="min-width: 8rem">
                    <template #body="{ data }">{{ data.executors_text || '—' }}</template>
                </Column>
                <Column header="标签" style="min-width: 8rem">
                    <template #body="{ data }">
                        <span class="plans-page__muted">{{ data.labels_text || '—' }}</span>
                    </template>
                </Column>
                <Column field="updated_ts" header="更新时间" sortable style="min-width: 10rem">
                    <template #body="{ data }">{{ formatTime(data.updated_ts) }}</template>
                </Column>
                <Column header="操作" style="min-width: 16rem">
                    <template #body="{ data }">
                        <div class="plans-page__actions">
                            <Button label="编辑" icon="pi pi-pencil" size="small" severity="secondary" text @click="openEdit(data.plan_id)" />
                            <Button label="复制" icon="pi pi-copy" size="small" severity="secondary" text @click="cloneFromList(data)" />
                            <Button label="下发" icon="pi pi-send" size="small" severity="secondary" text @click="openDispatch(data.plan_id)" />
                            <Button label="删除" icon="pi pi-trash" size="small" severity="danger" text @click="confirmDelete(data)" />
                        </div>
                    </template>
                </Column>
                <template #empty>
                    <span class="plans-page__muted">暂无测试计划，点「新建计划」创建</span>
                </template>
            </DataTable>
        </template>

        <!-- ============================== 编辑器视图 ============================== -->
        <template v-else>
            <header class="plans-page__header">
                <div>
                    <h2>{{ editorTitle }}</h2>
                    <p class="plans-page__hint">
                        步骤按序线性执行（Stage 屏障）；参数表单随执行器能力信封 detail.schema 自动切换，raw JSON 双向同步、冲突 <b>raw 优先</b>
                    </p>
                </div>
                <div class="plans-page__actions">
                    <Button label="返回列表" icon="pi pi-arrow-left" size="small" severity="secondary" outlined @click="goList" />
                </div>
            </header>

            <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
            <Message v-if="notice" severity="info" :closable="true" @close="notice = ''">{{ notice }}</Message>

            <!-- 基本信息 -->
            <Panel header="基本信息">
                <div class="plans-page__grid">
                    <label class="plans-page__field">
                        <span class="plans-page__label">名称（必填）</span>
                        <InputText v-model="editor.name" placeholder="计划名称" class="plans-page__input" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">默认优先级（大者先出队）</span>
                        <input v-model.number="editor.priority" type="number" class="plans-page__native" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">失败策略</span>
                        <Select v-model="editor.failurePolicy" :options="failurePolicyOptions" optionLabel="label" optionValue="value" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">默认超时（秒，0 = 不限）</span>
                        <input v-model.number="editor.timeoutS" type="number" min="0" class="plans-page__native" />
                    </label>
                </div>
                <label class="plans-page__field">
                    <span class="plans-page__label">说明</span>
                    <textarea v-model="editor.description" class="plans-page__textarea" placeholder="计划用途、适用场景……"></textarea>
                </label>
                <h4>标签（labels，随下发注入任务）</h4>
                <div v-for="(row, index) in editor.labelsRows" :key="index" class="plans-page__kv">
                    <InputText v-model="row.key" placeholder="键" />
                    <InputText v-model="row.value" placeholder="值" />
                    <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(editor.labelsRows, index)" />
                </div>
                <Button label="添加标签" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(editor.labelsRows)" />
            </Panel>

            <!-- 步骤列表 -->
            <Panel header="步骤列表（自上而下顺序执行）">
                <div v-for="(step, index) in editor.steps" :key="index" class="plans-page__step">
                    <div class="plans-page__step-head">
                        <Tag :value="`#${index + 1}`" severity="secondary" />
                        <InputText v-model="step.name" placeholder="步骤名（必填，stage_params 按此名覆盖）" class="plans-page__step-name" />
                        <div class="plans-page__actions">
                            <Button icon="pi pi-arrow-up" size="small" severity="secondary" text :disabled="index === 0" @click="moveStep(index, -1)" />
                            <Button icon="pi pi-arrow-down" size="small" severity="secondary" text :disabled="index === editor.steps.length - 1" @click="moveStep(index, 1)" />
                            <Button icon="pi pi-times" size="small" severity="danger" text @click="removeStep(index)" />
                        </div>
                    </div>

                    <div class="plans-page__grid">
                        <label class="plans-page__field">
                            <span class="plans-page__label">执行器（能力信封 ∪ 已知 id，可手输）</span>
                            <input
                                v-model="step.executorId"
                                list="plans-executor-ids"
                                class="plans-page__native"
                                placeholder="如 pytest"
                                @change="onStepExecutorChange(step)"
                            />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">脚本材料（含通用材料）</span>
                            <Select
                                v-model="step.materialId"
                                :options="materialOptionsOf(step)"
                                optionLabel="label"
                                optionValue="value"
                                placeholder="无（不指定材料）"
                                class="plans-page__input"
                            />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">分片模式</span>
                            <Select v-model="step.shardMode" :options="shardModeOptions" optionLabel="label" optionValue="value" />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">分片数 hint（0 = 执行器自定）</span>
                            <input v-model.number="step.shardHint" type="number" min="0" class="plans-page__native" />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">步骤超时（秒，0 = 不限）</span>
                            <input v-model.number="step.timeoutS" type="number" min="0" class="plans-page__native" />
                        </label>
                    </div>

                    <h5>参数（按能力信封 detail.schema 动态生成）</h5>
                    <p v-if="!schemaReadyOf(step)" class="plans-page__hint">能力信封 schema 加载中……可先编辑 raw JSON。</p>
                    <template v-else>
                        <p v-if="!schemaPropsOf(step).length" class="plans-page__hint">
                            {{ schemaHintOf(step) }}
                        </p>
                        <div v-for="prop in schemaPropsOf(step)" :key="prop.key" class="plans-page__field">
                            <span class="plans-page__label">
                                {{ prop.key }}<span v-if="prop.title" class="plans-page__muted"> — {{ prop.title }}</span>
                            </span>
                            <InputText
                                v-if="prop.type === 'string'"
                                v-model="step.form.values[prop.key]"
                                class="plans-page__input"
                                @input="syncStepFormToRaw(step)"
                            />
                            <input
                                v-else-if="prop.type === 'number'"
                                v-model.number="step.form.values[prop.key]"
                                type="number"
                                class="plans-page__native"
                                @input="syncStepFormToRaw(step)"
                            />
                            <select
                                v-else-if="prop.type === 'boolean'"
                                v-model="step.form.values[prop.key]"
                                class="plans-page__native"
                                @change="syncStepFormToRaw(step)"
                            >
                                <option :value="null">（未设）</option>
                                <option :value="true">true</option>
                                <option :value="false">false</option>
                            </select>
                            <select
                                v-else-if="prop.type === 'enum'"
                                v-model="step.form.values[prop.key]"
                                class="plans-page__native"
                                @change="syncStepFormToRaw(step)"
                            >
                                <option value="">（未设）</option>
                                <option v-for="opt in prop.options" :key="String(opt)" :value="opt">{{ opt }}</option>
                            </select>
                            <textarea
                                v-else-if="prop.type === 'array'"
                                v-model="step.form.arrayText[prop.key]"
                                class="plans-page__textarea"
                                placeholder="每行一条"
                                @input="syncStepFormToRaw(step)"
                            ></textarea>
                            <template v-else-if="prop.type === 'object'">
                                <div v-for="(row, rowIndex) in step.form.objectRows[prop.key] || []" :key="rowIndex" class="plans-page__kv">
                                    <InputText v-model="row.key" placeholder="键" @input="syncStepFormToRaw(step)" />
                                    <InputText v-model="row.value" placeholder="值" @input="syncStepFormToRaw(step)" />
                                    <Button
                                        icon="pi pi-times"
                                        size="small"
                                        severity="danger"
                                        text
                                        @click="removeKv(step.form.objectRows[prop.key], rowIndex); syncStepFormToRaw(step)"
                                    />
                                </div>
                                <Button
                                    label="添加"
                                    icon="pi pi-plus"
                                    size="small"
                                    severity="secondary"
                                    text
                                    @click="addKv(step.form.objectRows[prop.key]); syncStepFormToRaw(step)"
                                />
                            </template>
                            <p v-else class="plans-page__hint">
                                未知类型（{{ prop.rawType || '—' }}）：请用下方 raw JSON 编辑此参数。
                            </p>
                        </div>
                    </template>
                    <h5>raw JSON（params 兜底 ⇄ 表单双向同步，冲突 raw 优先）</h5>
                    <textarea
                        v-model="step.paramsRaw"
                        class="plans-page__textarea plans-page__textarea--tall"
                        placeholder="{}"
                        @input="syncStepRawToForm(step)"
                    ></textarea>
                    <p v-if="step.paramsRawError" class="plans-page__error-text">{{ step.paramsRawError }}</p>
                </div>
                <Button label="添加步骤" icon="pi pi-plus" size="small" severity="secondary" outlined @click="addStep" />
            </Panel>

            <!-- 底栏 -->
            <div class="plans-page__footer">
                <div class="plans-page__actions">
                    <Button label="保存" icon="pi pi-save" size="small" :loading="saving" @click="savePlan" />
                    <Button
                        label="另存为（副本）"
                        icon="pi pi-clone"
                        size="small"
                        severity="secondary"
                        outlined
                        :disabled="!editor.planId"
                        @click="saveAsCopy"
                    />
                    <Button
                        label="直接下发（不保存）"
                        icon="pi pi-send"
                        size="small"
                        severity="warn"
                        outlined
                        :loading="tempSubmitting"
                        @click="submitTempTask"
                    />
                    <Button label="取消" icon="pi pi-times" size="small" severity="secondary" text @click="goList" />
                </div>
                <template v-if="validationErrors.length">
                    <Message v-for="(item, index) in validationErrors" :key="index" severity="error" :closable="false">
                        {{ item }}
                    </Message>
                </template>
            </div>

            <!-- 下发面板（§5.5：计划下发保存后可用；字段同时供「直接下发（不保存）」用） -->
            <div ref="dispatchPanel">
                <Panel header="下发（运行时覆盖 overrides）">
                    <p class="plans-page__hint">
                        「② 下发」按计划实例化任务（labels 自动注入 plan_id / plan_name）；「直接下发（不保存）」组装临时任务（无 plan_id 标签，幂等键 UI 生成）。
                        覆盖次序：stage_params[步骤] 浅合并 → env 增量注入每步 params.env（后者胜）。
                    </p>
                    <div class="plans-page__grid">
                        <label class="plans-page__field">
                            <span class="plans-page__label">任务名（空 = 计划名）</span>
                            <InputText v-model="dispatch.name" placeholder="如：今晚回归" class="plans-page__input" />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">优先级（空/0 = 计划默认）</span>
                            <input v-model.number="dispatch.priority" type="number" class="plans-page__native" />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">幂等键（空 = 每次新建；头优先于 body）</span>
                            <InputText v-model="dispatch.idempotencyKey" placeholder="可选，重复触发去重键" class="plans-page__input" />
                        </label>
                    </div>

                    <h4>标签（labels 覆盖）</h4>
                    <div v-for="(row, index) in dispatch.labelsRows" :key="index" class="plans-page__kv">
                        <InputText v-model="row.key" placeholder="键" />
                        <InputText v-model="row.value" placeholder="值" />
                        <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(dispatch.labelsRows, index)" />
                    </div>
                    <Button label="添加标签" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(dispatch.labelsRows)" />

                    <h4>环境变量（env，增量注入每步 params.env）</h4>
                    <div v-for="(row, index) in dispatch.envRows" :key="index" class="plans-page__kv">
                        <InputText v-model="row.key" placeholder="键（如 PYTHONPATH）" @input="validateEnv" />
                        <InputText v-model="row.value" placeholder="值" />
                        <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(dispatch.envRows, index)" />
                    </div>
                    <Button label="添加环境变量" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(dispatch.envRows)" />
                    <p v-if="dispatch.envError" class="plans-page__error-text">{{ dispatch.envError }}</p>
                    <p class="plans-page__hint">
                        保留前缀 <code>AETP_*</code> 禁止注入（UI 拦截，执行端纵深再校验）。
                    </p>

                    <h4>选例（stage_params[步骤名] 覆盖：selection_id 或 targets 二选一/可并存）</h4>
                    <div v-for="(step, index) in editor.steps" :key="index" class="plans-page__step-override">
                        <span class="plans-page__label">{{ step.name || `步骤 ${index + 1}` }}</span>
                        <div class="plans-page__grid">
                            <label class="plans-page__field">
                                <span class="plans-page__label">selection_id（可选）</span>
                                <InputText v-model="step.override.selectionId" placeholder="sel-…" class="plans-page__input" />
                            </label>
                            <label class="plans-page__field">
                                <span class="plans-page__label">targets（可选，逐行 nodeid / 文件 / 目录）</span>
                                <textarea
                                    v-model="step.override.targetsText"
                                    class="plans-page__textarea"
                                    placeholder="tests/test_a.py::TestX::test_y"
                                ></textarea>
                            </label>
                        </div>
                    </div>

                    <div class="plans-page__actions">
                        <Button
                            label="① 预推材料（best-effort）"
                            icon="pi pi-upload"
                            size="small"
                            severity="secondary"
                            outlined
                            :loading="dispatch.pushing"
                            @click="runPrePush"
                        />
                        <Button
                            label="② 下发（按计划）"
                            icon="pi pi-send"
                            size="small"
                            :disabled="!editor.planId"
                            :loading="dispatch.submitting"
                            @click="dispatchPlan"
                        />
                        <span v-if="!editor.planId" class="plans-page__hint">保存计划后可用</span>
                    </div>

                    <template v-if="dispatch.pushNotes.length">
                        <h4>预推结果</h4>
                        <p class="plans-page__hint">
                            <template v-for="(note, index) in dispatch.pushNotes" :key="index">
                                <Tag :value="note.text" :severity="note.severity" class="plans-page__tag" />
                            </template>
                        </p>
                    </template>
                    <template v-if="dispatch.result">
                        <p class="plans-page__hint">
                            任务已下发：task_id = <span class="plans-page__mono">{{ dispatch.result.task_id }}</span>
                            <Tag v-if="dispatch.result.deduplicated" value="幂等去重（复用既有任务）" severity="warn" class="plans-page__tag" />
                            <Button label="查看结果" icon="pi pi-external-link" size="small" severity="secondary" text @click="goTasks" />
                        </p>
                    </template>
                </Panel>
            </div>
        </template>

        <!-- 执行器候选（能力信封 ∪ 手输，datalist 原生下拉提示） -->
        <datalist id="plans-executor-ids">
            <option v-for="entry in executors" :key="entry.id" :value="entry.id">{{ entry.summary || entry.id }}</option>
        </datalist>
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

// 动态表单不重复渲染的保留键（专用区块装配 / raw JSON 直改，同 SubmitPage 口径）
const RESERVED_PARAMS = ['material_id', 'selection_id', 'targets', 'env'];
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const SCALAR_TYPES = ['string', 'number', 'boolean', 'enum'];

function newIdempotencyKey() {
    // 临时任务幂等键 UI 生成（裁决 G）：uuid，重复触发保护
    try {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
    } catch {
        // 继续走兜底
    }
    return `tmp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export default {
    data() {
        return {
            error: '',
            notice: '',
            validationErrors: [],
            // 列表视图
            plans: [],
            loadingPlans: false,
            // 视图切换（query ?edit=<plan_id|new>；无 $router 时的兜底镜像）
            editorKey: '',
            fallbackEdit: '',
            // 编辑器
            editor: null,
            saving: false,
            tempSubmitting: false,
            focusDispatch: false,
            // 能力信封 / 材料
            executors: [],
            schemas: {},
            materialsByExecutor: {},
            capabilitiesFailed: false,
            // 下发面板
            dispatch: {
                name: '',
                priority: 0,
                labelsRows: [],
                envRows: [],
                envError: '',
                idempotencyKey: '',
                pushing: false,
                pushNotes: [],
                submitting: false,
                result: null
            },
            failurePolicyOptions: [
                { label: 'fail_fast（任一 Job 失败即停）', value: 'fail_fast' },
                { label: 'continue（跑完全部 Job）', value: 'continue' }
            ],
            shardModeOptions: [
                { label: 'none（不分片）', value: 'none' },
                { label: 'plan（执行器规划分片）', value: 'plan' }
            ]
        };
    },
    computed: {
        editQuery() {
            // query 驱动视图（M1）：路由 query 优先，无路由时读 window.location.search
            const route = this.$route;
            if (route && route.query && route.query.edit !== undefined && route.query.edit !== null) {
                const value = route.query.edit;
                return String(Array.isArray(value) ? value[0] || '' : value);
            }
            return this.fallbackEdit;
        },
        editorOpen() {
            return this.editorKey !== '';
        },
        editorTitle() {
            if (!this.editor) return '测试计划';
            return this.editor.planId ? `编辑计划 · ${this.editor.name || this.editor.planId}` : '新建计划';
        },
        rows() {
            return this.plans.map((plan) => {
                const steps = Array.isArray(plan.steps) ? plan.steps : [];
                const executors = [];
                for (const step of steps) {
                    const executor = step && typeof step.executor === 'object' ? step.executor : {};
                    const id = String((executor && executor.id) || '');
                    if (id && !executors.includes(id)) executors.push(id);
                }
                return {
                    ...plan,
                    plan_id: String(plan.plan_id || plan.id || ''),
                    step_count: steps.length,
                    executors_text: executors.join(' + '),
                    labels_text: this.formatLabels(plan.labels)
                };
            });
        }
    },
    watch: {
        editQuery: {
            immediate: true,
            handler(value) {
                this.enterEditorFor(String(value || ''));
            }
        }
    },
    created() {
        // 无 $router 环境的 query 兜底镜像
        try {
            const params = new URLSearchParams(window.location.search);
            this.fallbackEdit = params.get('edit') || '';
        } catch {
            this.fallbackEdit = '';
        }
    },
    mounted() {
        this.loadPlans();
        this.loadCapabilities();
    },
    methods: {
        // ============================ 视图切换（query 驱动） ============================
        enterEditorFor(value) {
            const target = value || '';
            if (target === this.editorKey) return;
            this.editorKey = target;
            if (!target) {
                this.closeEditorState();
                this.loadPlans(true);
                return;
            }
            if (target === 'new') {
                this.openNewEditor();
                return;
            }
            this.openExistingEditor(target);
        },
        navigateEdit(value, replace) {
            const target = value || '';
            const query = target ? { edit: target } : {};
            if (this.$router && typeof this.$router.push === 'function') {
                const location = { path: '/plans', query };
                if (replace && typeof this.$router.replace === 'function') this.$router.replace(location);
                else this.$router.push(location);
                return;
            }
            const url = target ? `/plans?edit=${encodeURIComponent(target)}` : '/plans';
            try {
                window.history[replace ? 'replaceState' : 'pushState']({}, '', url);
            } catch {
                // 历史 API 受限时只切内存视图
            }
            this.fallbackEdit = target;
        },
        openNew() {
            this.navigateEdit('new');
        },
        openEdit(planId) {
            this.navigateEdit(String(planId || ''));
        },
        openDispatch(planId) {
            // 列表「下发」：进编辑器并定位下发面板
            this.focusDispatch = true;
            this.navigateEdit(String(planId || ''));
        },
        goList() {
            this.navigateEdit('');
        },
        closeEditorState() {
            this.editor = null;
            this.validationErrors = [];
            this.resetDispatch();
        },
        // ================================ 列表 =================================
        async loadPlans(silent) {
            if (!silent) this.loadingPlans = true;
            try {
                const data = await fetchJson(apiUrl('/api/test/plans?limit=200'));
                this.plans = data.plans || [];
                this.error = '';
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.loadingPlans = false;
            }
        },
        planIdOf(plan) {
            return String((plan && (plan.plan_id || plan.id)) || '');
        },
        confirmDelete(plan) {
            const planId = this.planIdOf(plan);
            if (!planId) return;
            // 插件 SFC 拿不到 useConfirm，走宿主 app.use(ConfirmationService) 装的 $confirm
            this.$confirm.require({
                header: '删除计划',
                message: `确认删除计划「${plan.name || planId}」？已下发任务的 plan_id / plan_name 标签仍可回溯显示。`,
                icon: 'pi pi-exclamation-triangle',
                acceptLabel: '删除',
                rejectLabel: '取消',
                accept: async () => {
                    try {
                        await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}`), { method: 'DELETE' });
                        this.notice = `计划已删除：${plan.name || planId}`;
                        this.error = '';
                        await this.loadPlans(true);
                    } catch (reason) {
                        // 409 / 404 等错误明示，不静默
                        this.error = `删除失败：${String(reason && reason.message ? reason.message : reason)}`;
                    }
                }
            });
        },
        async cloneFromList(plan) {
            const planId = this.planIdOf(plan);
            try {
                const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}/clone`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}'
                });
                const copy = data.plan || {};
                this.notice = `已复制为「${copy.name || ''}」`;
                this.error = '';
                await this.loadPlans(true);
            } catch (reason) {
                this.error = `复制失败：${String(reason && reason.message ? reason.message : reason)}`;
            }
        },
        // ================================ 编辑器 =================================
        makeStep(raw) {
            const step = raw && typeof raw === 'object' ? raw : {};
            const executor = step.executor && typeof step.executor === 'object' ? step.executor : {};
            const shard = step.shard && typeof step.shard === 'object' ? step.shard : {};
            const params =
                step.params && typeof step.params === 'object' && !Array.isArray(step.params) ? step.params : {};
            return {
                name: String(step.name || ''),
                executorId: String((executor && executor.id) || ''),
                executorKind: String((executor && executor.kind) || 'executor'),
                executorLabels:
                    executor.labels && typeof executor.labels === 'object' && !Array.isArray(executor.labels)
                        ? { ...executor.labels }
                        : {},
                materialId: String(step.material_id || ''),
                requires:
                    step.requires && typeof step.requires === 'object' && !Array.isArray(step.requires)
                        ? { ...step.requires }
                        : {},
                paramsRaw: JSON.stringify(params, null, 2),
                paramsRawError: '',
                form: { values: {}, arrayText: {}, objectRows: {} },
                shardMode: String(shard.mode || 'none'),
                shardHint: Number(shard.hint || 0),
                timeoutS: Number(step.timeout_s || 0),
                // 下发覆盖（stage_params）：selection_id 或 targets 文本（不入计划存储）
                override: { selectionId: '', targetsText: '' }
            };
        },
        openNewEditor() {
            this.editor = {
                planId: '',
                name: '',
                description: '',
                labelsRows: [],
                priority: 0,
                failurePolicy: 'fail_fast',
                timeoutS: 0,
                steps: [this.makeStep({ name: '步骤 1', executor: { id: '' } })]
            };
            this.validationErrors = [];
            this.resetDispatch();
            this.error = '';
        },
        async openExistingEditor(planId) {
            try {
                const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}`));
                this.applyPlanToEditor(data.plan || {});
                this.error = '';
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
                this.navigateEdit('', true);
            }
        },
        applyPlanToEditor(plan) {
            const defaults =
                plan.defaults && typeof plan.defaults === 'object' && !Array.isArray(plan.defaults) ? plan.defaults : {};
            const steps = Array.isArray(plan.steps) ? plan.steps : [];
            this.editor = {
                planId: String(plan.plan_id || plan.id || ''),
                name: String(plan.name || ''),
                description: String(plan.description || ''),
                labelsRows: this.kvRowsOf(plan.labels),
                priority: Number(defaults.priority || 0),
                failurePolicy: String(defaults.failure_policy || 'fail_fast'),
                timeoutS: Number(defaults.timeout_s || 0),
                steps: steps.length ? steps.map((step) => this.makeStep(step)) : [this.makeStep({ name: '步骤 1' })]
            };
            this.validationErrors = [];
            this.resetDispatch();
            for (const step of this.editor.steps) {
                this.ensureSchema(step);
                this.ensureMaterials(step);
            }
            if (this.focusDispatch) {
                this.focusDispatch = false;
                this.$nextTick(() => {
                    const target = this.$refs.dispatchPanel;
                    if (target && typeof target.scrollIntoView === 'function') {
                        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    }
                });
            }
        },
        kvRowsOf(source) {
            let obj = source;
            if (typeof obj === 'string') {
                try {
                    obj = JSON.parse(obj) || {};
                } catch {
                    obj = {};
                }
            }
            if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return [];
            return Object.entries(obj).map(([key, value]) => ({ key, value: String(value ?? '') }));
        },
        addStep() {
            const step = this.makeStep({ name: `步骤 ${this.editor.steps.length + 1}` });
            this.editor.steps.push(step);
            this.ensureSchema(step);
        },
        removeStep(index) {
            this.editor.steps.splice(index, 1);
        },
        moveStep(index, delta) {
            const target = index + delta;
            if (target < 0 || target >= this.editor.steps.length) return;
            const steps = this.editor.steps;
            const [step] = steps.splice(index, 1);
            steps.splice(target, 0, step);
        },
        onStepExecutorChange(step) {
            step.executorId = String(step.executorId || '').trim();
            step.materialId = ''; // 材料归属随执行器，切换即清空避免归属不符
            this.ensureSchema(step);
            this.ensureMaterials(step);
            this.syncStepRawToForm(step); // raw 优先：按新 schema 回填表单
        },
        // ======================= 能力信封 / 材料 / 动态表单 =======================
        async loadCapabilities() {
            // 能力信封（SubmitPage.vue:387-394 解析法）：executor 条目 → 执行器候选 + schema
            try {
                const data = await fetchJson(apiUrl('/api/heartbeat/capabilities?kind=executor'));
                const seen = {};
                for (const entry of data.capabilities || []) {
                    if (!entry || entry.kind !== 'executor') continue;
                    const id = String(entry.id || '');
                    if (!id) continue;
                    const schema = entry.detail && entry.detail.schema ? entry.detail.schema : null;
                    if (!seen[id]) {
                        seen[id] = true;
                        this.executors.push({ id, summary: String(entry.summary || entry.provider || '') });
                    }
                    if (schema && !this.schemas[id]) this.schemas[id] = schema;
                    else if (!(id in this.schemas)) this.schemas[id] = schema || null;
                }
                this.capabilitiesFailed = false;
                for (const step of (this.editor && this.editor.steps) || []) this.syncStepRawToForm(step);
            } catch (reason) {
                this.capabilitiesFailed = true;
                this.notice = `能力信封加载失败（${reason && reason.message ? reason.message : reason}），执行器可手输 id、参数走 raw JSON。`;
            }
        },
        ensureSchema(step) {
            const id = String(step.executorId || '').trim();
            if (!id) return;
            if (!(id in this.schemas)) {
                if (this.capabilitiesFailed) {
                    this.loadCapabilities();
                } else {
                    // 能力信封里没有该 id（手输 / 离线）：schema 记空，走 raw JSON
                    this.schemas[id] = null;
                }
            }
            this.$nextTick(() => this.syncStepRawToForm(step));
        },
        async ensureMaterials(step) {
            // 脚本下拉（§4.5 过滤谓词 ==id OR ='' 通用可见）
            const id = String(step.executorId || '').trim();
            if (!id || id in this.materialsByExecutor) return;
            try {
                const data = await fetchJson(
                    apiUrl(`/api/test/materials?executor=${encodeURIComponent(id)}&limit=200`)
                );
                this.materialsByExecutor[id] = data.materials || [];
            } catch {
                this.materialsByExecutor[id] = [];
            }
        },
        materialOptionsOf(step) {
            const list = this.materialsByExecutor[String(step.executorId || '').trim()] || [];
            const options = [{ label: '无（不指定材料）', value: '' }];
            for (const item of list) {
                const id = String((item && (item.material_id || item.id)) || '');
                if (!id) continue;
                const generic = String(item.executor_id || '') === '' ? '·通用' : '';
                options.push({ label: `${item.name || id}${generic}（${item.state || '—'}）`, value: id });
            }
            if (step.materialId && !options.some((option) => option.value === step.materialId)) {
                options.push({ label: `${step.materialId}（不在当前列表）`, value: step.materialId });
            }
            return options;
        },
        schemaOf(step) {
            const id = String(step.executorId || '').trim();
            return id ? this.schemas[id] : null;
        },
        schemaReadyOf(step) {
            return this.schemaOf(step) !== undefined;
        },
        schemaPropsOf(step) {
            const schema = this.schemaOf(step);
            if (!schema || typeof schema !== 'object') return [];
            const props = schema.properties || {};
            const reserved = new Set(RESERVED_PARAMS);
            return Object.entries(props)
                .filter(([key]) => !reserved.has(key))
                .map(([key, def]) => {
                    const raw = def && typeof def === 'object' ? def : {};
                    const options = Array.isArray(raw.enum) ? raw.enum : Array.isArray(raw.options) ? raw.options : null;
                    let type = raw.type || (options ? 'enum' : 'string');
                    if (options && type !== 'array' && type !== 'object') type = 'enum';
                    if (type === 'integer') type = 'number';
                    if (!SCALAR_TYPES.includes(type) && type !== 'array' && type !== 'object') type = 'unknown';
                    return {
                        key,
                        type,
                        options: options || [],
                        rawType: String(raw.type || (options ? 'enum' : '')),
                        title: String(raw.title || raw.description || '')
                    };
                });
        },
        schemaHintOf(step) {
            const id = String(step.executorId || '').trim();
            if (!id) return '请先选择 / 输入执行器 id，或直接编辑 raw JSON。';
            if (this.schemaOf(step)) return '';
            return '该执行器无能力信封 detail.schema（离线或未上报），参数请用 raw JSON 编辑。';
        },
        initStepForm(step) {
            // 按 schema 建立表单槽位（值从 raw JSON 回填，raw 优先）
            step.form = { values: {}, arrayText: {}, objectRows: {} };
            for (const prop of this.schemaPropsOf(step)) {
                if (prop.type === 'array') step.form.arrayText[prop.key] = '';
                else if (prop.type === 'object') step.form.objectRows[prop.key] = [];
                else if (prop.type === 'boolean') step.form.values[prop.key] = null;
                else if (prop.type === 'number') step.form.values[prop.key] = '';
                else step.form.values[prop.key] = '';
            }
        },
        // ---- 参数表单 ⇄ raw 同步（raw 优先，SubmitPage 迁移 + number/boolean/enum 分支） ----
        parseStepRaw(step) {
            const text = String(step.paramsRaw || '').trim();
            if (!text) return null;
            try {
                const parsed = JSON.parse(text);
                if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
                return parsed;
            } catch {
                return null;
            }
        },
        buildStepParamsFromForm(step) {
            // 先并入 raw 里表单不认识的键（保留手改），再让表单值覆盖 schema 键
            const out = {};
            const rawParsed = this.parseStepRaw(step);
            if (rawParsed) Object.assign(out, rawParsed);
            for (const prop of this.schemaPropsOf(step)) {
                const key = prop.key;
                if (prop.type === 'array') {
                    const lines = String(step.form.arrayText[key] || '')
                        .split('\n')
                        .map((line) => line.trim())
                        .filter(Boolean);
                    if (lines.length) out[key] = lines;
                    else delete out[key];
                } else if (prop.type === 'object') {
                    const obj = {};
                    for (const row of step.form.objectRows[key] || []) {
                        if (String(row.key || '').trim()) obj[String(row.key).trim()] = row.value;
                    }
                    if (Object.keys(obj).length) out[key] = obj;
                    else delete out[key];
                } else if (prop.type === 'number') {
                    const value = step.form.values[key];
                    if (value === '' || value === null || value === undefined || Number.isNaN(Number(value))) {
                        delete out[key];
                    } else {
                        out[key] = Number(value);
                    }
                } else if (prop.type === 'boolean') {
                    const value = step.form.values[key];
                    if (value === null || value === undefined || value === '') delete out[key];
                    else out[key] = value === true || value === 'true';
                } else if (prop.type === 'enum') {
                    const value = step.form.values[key];
                    if (value === '' || value === null || value === undefined) delete out[key];
                    else out[key] = value;
                } else {
                    // string / 未知类型（未知类型不渲染控件，此处只保留 raw 值）
                    if (prop.type !== 'unknown') {
                        const value = String(step.form.values[key] ?? '').trim();
                        if (value) out[key] = value;
                        else delete out[key];
                    }
                }
            }
            return out;
        },
        syncStepFormToRaw(step) {
            step.paramsRaw = JSON.stringify(this.buildStepParamsFromForm(step), null, 2);
            step.paramsRawError = '';
        },
        syncStepRawToForm(step) {
            const text = String(step.paramsRaw || '').trim();
            if (!text) {
                step.paramsRawError = '';
                return;
            }
            let parsed = null;
            try {
                parsed = JSON.parse(text);
            } catch (reason) {
                step.paramsRawError = `raw 参数不是合法 JSON：${reason.message}`;
                return;
            }
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                step.paramsRawError = 'raw 参数需是 JSON 对象';
                return;
            }
            step.paramsRawError = '';
            this.initStepForm(step);
            for (const prop of this.schemaPropsOf(step)) {
                const value = parsed[prop.key];
                if (value === undefined) continue;
                if (prop.type === 'array') {
                    step.form.arrayText[prop.key] = Array.isArray(value) ? value.join('\n') : String(value);
                } else if (prop.type === 'object') {
                    const source = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
                    step.form.objectRows[prop.key] = Object.entries(source).map(([key, item]) => ({
                        key,
                        value: String(item ?? '')
                    }));
                } else if (prop.type === 'number') {
                    const num = Number(value);
                    step.form.values[prop.key] = value === null || Number.isNaN(num) ? '' : num;
                } else if (prop.type === 'boolean') {
                    step.form.values[prop.key] =
                        typeof value === 'boolean' ? value : value === 'true' ? true : value === 'false' ? false : null;
                } else if (prop.type === 'enum') {
                    step.form.values[prop.key] = value;
                } else if (prop.type !== 'unknown') {
                    step.form.values[prop.key] = typeof value === 'string' ? value : JSON.stringify(value);
                }
            }
        },
        finalStepParams(step) {
            // 下发 / 保存口径：表单先组装，raw 冲突值覆盖（raw 优先）
            const params = this.buildStepParamsFromForm(step);
            const rawParsed = this.parseStepRaw(step);
            return rawParsed ? { ...params, ...rawParsed } : params;
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
            for (const row of this.dispatch.envRows) {
                const key = String(row.key || '').trim();
                if (!key) {
                    if (String(row.value ?? '').trim()) {
                        this.dispatch.envError = '环境变量键不能为空';
                        return false;
                    }
                    continue;
                }
                if (/^AETP_/i.test(key)) {
                    this.dispatch.envError = `环境变量键 ${key} 使用保留前缀 AETP_*，已拦截`;
                    return false;
                }
                if (!ENV_KEY_RE.test(key)) {
                    this.dispatch.envError = `环境变量键 ${key} 不合法（需匹配 [A-Za-z_][A-Za-z0-9_]*）`;
                    return false;
                }
            }
            this.dispatch.envError = '';
            return true;
        },
        // ============================ 校验 / 组装 ============================
        validateEditor() {
            const problems = [];
            const editor = this.editor;
            if (!editor) return ['编辑器未就绪'];
            if (!String(editor.name || '').trim()) problems.push('基本信息：名称必填');
            if (!editor.steps.length) problems.push('steps 不能为空');
            const names = new Set();
            editor.steps.forEach((step, index) => {
                const where = `steps[${index}]`;
                const name = String(step.name || '').trim();
                if (!name) problems.push(`${where}.name 步骤名必填`);
                else if (names.has(name)) problems.push(`${where}.name 步骤名重复：${name}`);
                else names.add(name);
                if (!String(step.executorId || '').trim()) problems.push(`${where}.executor.id 执行器 id 必填`);
                const rawText = String(step.paramsRaw || '').trim();
                if (rawText && !this.parseStepRaw(step)) {
                    problems.push(`${where}.params raw JSON 不是合法对象：${step.paramsRawError || '无法解析'}`);
                }
                const params = this.finalStepParams(step);
                const inner = params.material_id;
                if (
                    step.materialId &&
                    typeof inner === 'string' &&
                    inner &&
                    inner !== step.materialId
                ) {
                    problems.push(
                        `${where}.material_id 与 params.material_id 同名冲突：${step.materialId} ≠ ${inner}`
                    );
                }
            });
            return problems;
        },
        stepToSpec(step) {
            const name = String(step.name || '').trim();
            const executorId = String(step.executorId || '').trim();
            return {
                name,
                executor: {
                    id: executorId,
                    kind: String(step.executorKind || 'executor'),
                    labels: step.executorLabels || {}
                },
                material_id: String(step.materialId || '').trim(),
                params: this.finalStepParams(step),
                shard: { mode: step.shardMode || 'none', hint: Number(step.shardHint) || 0 },
                requires: step.requires || {},
                timeout_s: Number(step.timeoutS) || 0
            };
        },
        showValidationProblems(problems) {
            this.validationErrors = problems;
        },
        handleSaveError(reason) {
            const message = String(reason && reason.message ? reason.message : reason);
            if (reason && reason.status === 400) {
                // 校验错误逐条展示（400 的 error 消息按「；」拆行）
                const lines = message
                    .replace(/^计划规格无效：/, '')
                    .split('；')
                    .map((line) => line.trim())
                    .filter(Boolean);
                this.showValidationProblems(lines.length ? lines : [message]);
            } else {
                this.error = message;
            }
        },
        // ============================ 保存 / 另存为 ============================
        async savePlan() {
            this.error = '';
            this.notice = '';
            this.validationErrors = [];
            const problems = this.validateEditor();
            if (problems.length) {
                this.showValidationProblems(problems);
                return;
            }
            const editor = this.editor;
            const body = {
                name: String(editor.name || '').trim(),
                description: String(editor.description || ''),
                labels: this.collectKv(editor.labelsRows),
                defaults: {
                    priority: Number(editor.priority) || 0,
                    failure_policy: editor.failurePolicy,
                    timeout_s: Number(editor.timeoutS) || 0
                },
                steps: editor.steps.map((step) => this.stepToSpec(step))
            };
            const isNew = !editor.planId;
            this.saving = true;
            try {
                const data = await fetchJson(apiUrl(isNew ? '/api/test/plans' : `/api/test/plans/${encodeURIComponent(editor.planId)}`), {
                    method: isNew ? 'POST' : 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const plan = data.plan || {};
                editor.planId = this.planIdOf(plan);
                this.editorKey = editor.planId;
                this.navigateEdit(editor.planId, true);
                this.notice = `计划已保存：${plan.name || editor.name}`;
                await this.loadPlans(true);
            } catch (reason) {
                this.handleSaveError(reason);
            } finally {
                this.saving = false;
            }
        },
        async saveAsCopy() {
            // 另存为：先落当前内容（保证副本 = 编辑内容），再走 clone（name 加「副本」后缀）
            if (!this.editor.planId) return;
            await this.savePlan();
            if (this.validationErrors.length) return;
            try {
                const data = await fetchJson(
                    apiUrl(`/api/test/plans/${encodeURIComponent(this.editor.planId)}/clone`),
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
                );
                const copy = data.plan || {};
                this.applyPlanToEditor(copy);
                this.editorKey = this.planIdOf(copy);
                this.navigateEdit(this.editorKey, true);
                this.notice = `已另存为副本：${copy.name || this.editorKey}`;
                await this.loadPlans(true);
            } catch (reason) {
                this.error = `另存为失败：${String(reason && reason.message ? reason.message : reason)}`;
            }
        },
        // ============================ 下发（§5.5） ============================
        resetDispatch() {
            this.dispatch = {
                name: '',
                priority: 0,
                labelsRows: [],
                envRows: [],
                envError: '',
                idempotencyKey: '',
                pushing: false,
                pushNotes: [],
                submitting: false,
                result: null
            };
        },
        async runPrePush() {
            // ① 预推材料（best-effort）：对每个 material 调 pytest 族 push 口，
            // 404 = 族未提供推送端点跳过提示；其余错误收集不阻断（评审 #16）
            const notes = [];
            const byMaterial = {};
            for (const step of this.editor.steps) {
                const materialId = String(step.materialId || '').trim();
                if (!materialId) continue;
                if (!byMaterial[materialId]) byMaterial[materialId] = [];
                const selectionId = String((step.override && step.override.selectionId) || '').trim();
                if (selectionId) byMaterial[materialId].push(selectionId);
            }
            this.dispatch.pushing = true;
            try {
                for (const materialId of Object.keys(byMaterial)) {
                    const selectionIds = byMaterial[materialId];
                    const payload = selectionIds.length ? { selection_ids: selectionIds } : {};
                    try {
                        const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/push`), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });
                        const pushed = (data.pushed || []).length;
                        const failed = (data.failed || []).length;
                        notes.push({
                            text: `${materialId}：预推成功 ${pushed} 台${failed ? ` / 失败 ${failed} 台（走 missing 兜底）` : ''}`,
                            severity: failed ? 'warn' : 'success'
                        });
                    } catch (reason) {
                        if (reason && reason.status === 404) {
                            notes.push({
                                text: `${materialId}：族未提供推送端点（404），跳过`,
                                severity: 'secondary'
                            });
                        } else {
                            notes.push({
                                text: `${materialId}：预推失败（${reason && reason.message ? reason.message : reason}），不阻断下发`,
                                severity: 'danger'
                            });
                        }
                    }
                }
                if (!notes.length) {
                    notes.push({ text: '本计划步骤未指定材料，无需预推', severity: 'secondary' });
                }
            } finally {
                this.dispatch.pushNotes = notes;
                this.dispatch.pushing = false;
            }
        },
        buildOverrides() {
            // overrides 按 §5.5：name / priority / labels / env / stage_params / idempotency_key
            const overrides = {};
            const name = String(this.dispatch.name || '').trim();
            if (name) overrides.name = name;
            const priority = Number(this.dispatch.priority);
            if (!Number.isNaN(priority) && priority !== 0) overrides.priority = priority;
            const labels = this.collectKv(this.dispatch.labelsRows);
            if (Object.keys(labels).length) overrides.labels = labels;
            const env = this.collectKv(this.dispatch.envRows);
            if (Object.keys(env).length) overrides.env = env;
            const stageParams = {};
            for (const step of this.editor.steps) {
                const stepName = String(step.name || '').trim();
                if (!stepName) continue;
                const entry = {};
                const selectionId = String((step.override && step.override.selectionId) || '').trim();
                if (selectionId) entry.selection_id = selectionId;
                const targets = String((step.override && step.override.targetsText) || '')
                    .split('\n')
                    .map((line) => line.trim())
                    .filter(Boolean);
                if (targets.length) entry.targets = targets;
                if (Object.keys(entry).length) stageParams[stepName] = entry;
            }
            if (Object.keys(stageParams).length) overrides.stage_params = stageParams;
            const idempotencyKey = String(this.dispatch.idempotencyKey || '').trim();
            if (idempotencyKey) overrides.idempotency_key = idempotencyKey;
            return overrides;
        },
        async dispatchPlan() {
            // ② 下发：POST /api/test/plans/{id}/dispatch（保存后可用）
            this.error = '';
            this.notice = '';
            this.validationErrors = [];
            this.dispatch.result = null;
            const planId = this.editor.planId;
            if (!planId) return;
            const problems = this.validateEditor();
            if (problems.length) {
                this.showValidationProblems(problems);
                return;
            }
            if (!this.validateEnv()) return;
            this.dispatch.submitting = true;
            try {
                await this.runPrePush();
                const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}/dispatch`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.buildOverrides())
                });
                this.dispatch.result = {
                    task_id: String(data.task_id || ''),
                    deduplicated: Boolean(data.deduplicated)
                };
                this.notice = `任务已下发：task_id=${data.task_id}`;
            } catch (reason) {
                this.handleSaveError(reason);
            } finally {
                this.dispatch.submitting = false;
            }
        },
        async submitTempTask() {
            // 临时任务（裁决 G）：不保存直接下发 = 组装 TaskSpec 直接 POST /api/test/tasks
            // （labels 不带 plan_id、幂等键 UI 生成 uuid），同样走预推流程
            this.error = '';
            this.notice = '';
            this.validationErrors = [];
            this.dispatch.result = null;
            const problems = this.validateEditor();
            if (problems.length) {
                this.showValidationProblems(problems);
                return;
            }
            if (!this.validateEnv()) return;
            const editor = this.editor;
            const envOverride = this.collectKv(this.dispatch.envRows);
            const stages = editor.steps.map((step) => {
                const params = this.finalStepParams(step);
                const stage = this.stepToSpec(step);
                const entry = {};
                const selectionId = String((step.override && step.override.selectionId) || '').trim();
                if (selectionId) entry.selection_id = selectionId;
                const targets = String((step.override && step.override.targetsText) || '')
                    .split('\n')
                    .map((line) => line.trim())
                    .filter(Boolean);
                if (targets.length) entry.targets = targets;
                const merged = { ...params, ...entry };
                if (Object.keys(envOverride).length) {
                    merged.env = { ...(merged.env && typeof merged.env === 'object' ? merged.env : {}), ...envOverride };
                }
                return {
                    name: stage.name,
                    executor: stage.executor,
                    params: merged,
                    shard: stage.shard,
                    requires: stage.requires,
                    timeout_s: stage.timeout_s
                };
            });
            const name = String(this.dispatch.name || '').trim() || String(editor.name || '').trim();
            const priority = Number(this.dispatch.priority);
            const spec = {
                name,
                stages,
                priority: Number.isNaN(priority) ? 0 : priority,
                labels: { ...this.collectKv(editor.labelsRows), ...this.collectKv(this.dispatch.labelsRows) },
                idempotency_key: newIdempotencyKey(),
                failure_policy: editor.failurePolicy,
                timeout_s: Number(editor.timeoutS) || 0
            };
            this.tempSubmitting = true;
            try {
                await this.runPrePush();
                const data = await fetchJson(apiUrl('/api/test/tasks'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(spec)
                });
                this.dispatch.result = {
                    task_id: String(data.task_id || ''),
                    deduplicated: Boolean(data.deduplicated)
                };
                this.notice = `临时任务已下发（未保存计划）：task_id=${data.task_id}`;
            } catch (reason) {
                this.handleSaveError(reason);
            } finally {
                this.tempSubmitting = false;
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
        // ============================ 展示辅助 ============================
        formatLabels(labels) {
            let source = labels;
            if (typeof labels === 'string') {
                try {
                    source = JSON.parse(labels) || {};
                } catch {
                    return labels;
                }
            }
            if (!source || typeof source !== 'object' || Array.isArray(source)) return '';
            return Object.entries(source)
                .map(([key, value]) => `${key}=${value}`)
                .join(', ');
        },
        formatTime(value) {
            if (!value) return '—';
            return new Date(value * 1000).toLocaleString();
        }
    }
};
</script>

<style scoped>
/* 插件 SFC 用不了运行时 Tailwind：仅保留布局所需的少量样式（风格对齐 TasksPage.vue），
   组件外观全部来自共享 PrimeVue 组件与宿主语义变量 */
.plans-page {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.plans-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.plans-page__header h2 {
    margin: 0;
}

.plans-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.plans-page__muted {
    color: var(--text-color-secondary);
}

.plans-page__error-text {
    margin: 0.25rem 0 0;
    color: var(--red-500);
    font-size: 0.875rem;
}

.plans-page__actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    white-space: nowrap;
    flex-wrap: wrap;
}

.plans-page__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
    gap: 0.75rem;
}

.plans-page__field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
}

.plans-page__label {
    font-size: 0.8125rem;
    color: var(--text-color-secondary);
}

.plans-page__input {
    width: 100%;
}

.plans-page__native {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    color: var(--text-color);
    font: inherit;
}

.plans-page__textarea {
    width: 100%;
    min-height: 4.5rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    color: var(--text-color);
    font: inherit;
    resize: vertical;
}

.plans-page__textarea--tall {
    min-height: 8rem;
    font-family: monospace;
    font-size: 0.8125rem;
}

.plans-page__kv {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    margin-bottom: 0.375rem;
}

.plans-page__kv .p-inputtext {
    flex: 1;
}

.plans-page__step {
    margin-bottom: 1rem;
    padding: 1rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
}

.plans-page__step-head {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin-bottom: 0.75rem;
}

.plans-page__step-name {
    flex: 1;
}

.plans-page__step h5 {
    margin: 0.75rem 0 0.25rem;
}

.plans-page__step-override {
    margin-bottom: 0.75rem;
    padding: 0.75rem;
    border: 1px dashed var(--surface-border);
    border-radius: var(--content-border-radius);
}

.plans-page__footer {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 1rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
}

.plans-page__mono {
    font-family: monospace;
    font-size: 0.8125rem;
}

.plans-page__tag {
    margin-right: 0.25rem;
}
</style>
