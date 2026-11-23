<!-- pytest 任务参数面板（设计 §6「下发任务 UI」）：测试计划编辑器步骤卡片「任务参数」区的
     slot 面板（target=plans:dispatch:pytest——能力 id 而非插件名，沿 scripts:pytest 教训）。
     契约：props.payload = { executorId, material:{...}|null, cases:{mode,case_ids}, params:{...} }
     → $emit('update', params) 输出完整 params 对象（托管 5 字段：extra_args/keyword/env/
     workdir/python；一律不产出核心管理键 material_id/selection_id/targets，宿主整替 step.params）。
     payload.params 初始回填、改动即 emit；cases 摘要（「已选 N 用例」）与脚本名只读展示。
     紧凑表单布局（嵌步骤卡片内，无大标题横幅）。
     插件 SFC 自包含：不 import 宿主 / npm 模块，仅用全局共享组件 + 原生控件（Options API）；
     多行编辑用原生 <textarea>（共享组件 Textarea 与 HTML 原生标签同名有解析坑，见
     shared-components.js 的 PvTextarea 注释，此处原生控件更稳）。 -->
<template>
    <section class="dispatch-panel">
        <p class="dispatch-panel__summary">
            <span class="dispatch-panel__muted">脚本</span>
            <span class="dispatch-panel__mono">{{ materialLabel }}</span>
            <span class="dispatch-panel__sep">·</span>
            <span class="dispatch-panel__muted">用例</span>
            <Tag :value="casesLabel" :severity="casesSeverity" />
        </p>

        <div class="dispatch-panel__field">
            <label class="dispatch-panel__label" for="dp-extra-args">额外参数（extra_args，每行一条）</label>
            <textarea
                id="dp-extra-args"
                v-model="extraArgsText"
                rows="3"
                class="dispatch-panel__textarea"
                placeholder="每行一条，如：-x&#10;--tb=short（空行忽略）"
                @input="emitUpdate"
            ></textarea>
        </div>

        <div class="dispatch-panel__field">
            <label class="dispatch-panel__label" for="dp-keyword">关键字（keyword）</label>
            <InputText
                id="dp-keyword"
                v-model="keyword"
                class="dispatch-panel__input"
                placeholder="pytest -k 表达式，留空=不过滤"
                @input="emitUpdate"
            />
        </div>

        <div class="dispatch-panel__field">
            <span class="dispatch-panel__label">环境变量（env）</span>
            <div v-for="(row, index) in envRows" :key="index" class="dispatch-panel__kv">
                <InputText v-model="row.key" placeholder="键（如 PYTHONPATH）" @input="emitUpdate" />
                <InputText v-model="row.value" placeholder="值" @input="emitUpdate" />
                <Button icon="pi pi-times" size="small" severity="danger" text @click="removeEnvRow(index)" />
            </div>
            <div class="dispatch-panel__env-actions">
                <Button label="添加环境变量" icon="pi pi-plus" size="small" severity="secondary" text @click="addEnvRow" />
            </div>
            <p v-if="envError" class="dispatch-panel__error" role="alert">{{ envError }}</p>
            <p class="dispatch-panel__hint">
                保留前缀 <code>AETP_*</code> 禁止注入（UI 拦截不进 params，执行端纵深再校验）。
            </p>
        </div>

        <div class="dispatch-panel__field">
            <label class="dispatch-panel__label" for="dp-workdir">工作目录（workdir）</label>
            <InputText
                id="dp-workdir"
                v-model="workdir"
                class="dispatch-panel__input"
                placeholder="留空=执行端默认工作目录"
                @input="emitUpdate"
            />
        </div>

        <div class="dispatch-panel__field">
            <label class="dispatch-panel__label" for="dp-python">Python 解释器（python）</label>
            <InputText
                id="dp-python"
                v-model="python"
                class="dispatch-panel__input"
                placeholder="留空=默认解释器"
                @input="emitUpdate"
            />
        </div>
    </section>
</template>

<script>
// env 键校验与 test_core PlansPage 既有校验同口径（ENV_KEY_RE + AETP_* 保留前缀拦截）
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export default {
    // payload（宿主 → 面板，设计 §6）：{ executorId, material, cases, params }
    props: ['payload'],
    emits: ['update'],
    data() {
        return {
            extraArgsText: '',
            keyword: '',
            envRows: [],
            envError: '',
            workdir: '',
            python: '',
            // 回声哨兵：记录最近一次「填入 / emit」的 params 归一化 JSON，
            // payload 回传自家改动时跳过重填，防输入光标被重置
            lastEcho: ''
        };
    },
    computed: {
        materialLabel() {
            const material = this.payload && this.payload.material;
            if (!material) return '未选择脚本';
            return String(material.name || material.material_id || '未选择脚本');
        },
        casesLabel() {
            const cases = this.payload && this.payload.cases;
            if (cases && cases.mode === 'selected') {
                const ids = Array.isArray(cases.case_ids) ? cases.case_ids : [];
                return `已选 ${ids.length} 用例`;
            }
            return '全部用例';
        },
        casesSeverity() {
            const cases = this.payload && this.payload.cases;
            return cases && cases.mode === 'selected' ? 'info' : 'secondary';
        }
    },
    watch: {
        // payload.params 外部整替（步骤切换 / raw JSON 改动等）时回填；
        // 深比较经回声哨兵去抖，自家 emit 回来的内容不重填
        payload: {
            deep: true,
            handler() {
                this.syncFromPayload();
            }
        }
    },
    created() {
        this.syncFromPayload();
    },
    methods: {
        // ---- 回填 / 回声 ---------------------------------------------------
        syncFromPayload() {
            const params = this.normalizeParams((this.payload && this.payload.params) || {});
            const echo = JSON.stringify(params);
            if (echo === this.lastEcho) return;
            this.lastEcho = echo;
            this.fillFrom(params);
        },
        fillFrom(params) {
            this.extraArgsText = Array.isArray(params.extra_args) ? params.extra_args.join('\n') : '';
            this.keyword = typeof params.keyword === 'string' ? params.keyword : '';
            this.envRows =
                params.env && typeof params.env === 'object'
                    ? Object.entries(params.env).map(([key, value]) => ({ key, value: String(value ?? '') }))
                    : [];
            this.workdir = typeof params.workdir === 'string' ? params.workdir : '';
            this.python = typeof params.python === 'string' ? params.python : '';
            this.validateEnv();
        },
        // ---- 参数组装（托管 5 字段，空值省略；核心管理键一律不产出）----------
        buildParams() {
            return this.normalizeParams({
                extra_args: String(this.extraArgsText || '').split(/\r?\n/),
                keyword: this.keyword,
                env: this.envFromRows(),
                workdir: this.workdir,
                python: this.python
            });
        },
        normalizeParams(raw) {
            // 归一化（形状固定、键序固定）：emit 产物与回声比较共用同一口径
            const params = {};
            const extra = this.extraArgsOf(raw.extra_args);
            if (extra.length) params.extra_args = extra;
            const keyword = typeof raw.keyword === 'string' ? raw.keyword.trim() : '';
            if (keyword) params.keyword = keyword;
            const env = this.envOf(raw.env);
            if (Object.keys(env).length) params.env = env;
            const workdir = typeof raw.workdir === 'string' ? raw.workdir.trim() : '';
            if (workdir) params.workdir = workdir;
            const python = typeof raw.python === 'string' ? raw.python.trim() : '';
            if (python) params.python = python;
            return params;
        },
        extraArgsOf(raw) {
            if (!Array.isArray(raw)) return [];
            return raw.map((item) => String(item ?? '').trim()).filter(Boolean);
        },
        envOf(raw) {
            const out = {};
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
            for (const [key, value] of Object.entries(raw)) {
                const name = String(key ?? '').trim();
                if (name) out[name] = String(value ?? '');
            }
            return out;
        },
        envFromRows() {
            // UI 拦截：未过校验的行不进 params（错误行留在表单里供修正）
            const out = {};
            for (const row of this.envRows) {
                const key = String(row.key || '').trim();
                if (!key || /^AETP_/i.test(key) || !ENV_KEY_RE.test(key)) continue;
                out[key] = String(row.value ?? '');
            }
            return out;
        },
        // ---- 校验（与 PlansPage env 校验同口径）-----------------------------
        validateEnv() {
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
        // ---- 事件 -----------------------------------------------------------
        addEnvRow() {
            this.envRows.push({ key: '', value: '' });
            this.emitUpdate();
        },
        removeEnvRow(index) {
            this.envRows.splice(index, 1);
            this.emitUpdate();
        },
        emitUpdate() {
            this.validateEnv();
            const params = this.buildParams();
            this.lastEcho = JSON.stringify(params);
            this.$emit('update', params);
        }
    }
};
</script>

<style scoped>
/* 插件 SFC 用不了运行时 Tailwind：仅保留布局所需的少量样式，
   组件外观全部来自共享 PrimeVue 组件与宿主语义变量（对齐 MaterialsPanel 风格）。
   紧凑表单：嵌在计划编辑器步骤卡片「任务参数」区内，不做整页布局。 */
.dispatch-panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
}

.dispatch-panel__summary {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin: 0;
    font-size: 0.875rem;
    color: var(--text-color-secondary);
}

.dispatch-panel__sep {
    color: var(--text-color-secondary);
}

.dispatch-panel__muted {
    color: var(--text-color-secondary);
}

.dispatch-panel__mono {
    font-family: monospace;
    font-size: 0.8125rem;
    color: var(--text-color);
    word-break: break-all;
}

.dispatch-panel__field {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
}

.dispatch-panel__label {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color-secondary);
}

.dispatch-panel__input {
    max-width: 24rem;
    width: 100%;
}

.dispatch-panel__textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 0.5rem 0.625rem;
    font-family: monospace;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-color);
    background: var(--p-inputtext-background, transparent);
    border: 1px solid var(--p-inputtext-border-color, var(--surface-border));
    border-radius: var(--border-radius, 6px);
    resize: vertical;
}

.dispatch-panel__textarea:focus {
    outline: none;
    border-color: var(--p-inputtext-focus-border-color, var(--primary-color));
}

.dispatch-panel__kv {
    display: grid;
    grid-template-columns: minmax(8rem, 1fr) minmax(8rem, 1.5fr) auto;
    gap: 0.375rem;
    align-items: center;
}

.dispatch-panel__env-actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    flex-wrap: wrap;
}

.dispatch-panel__hint {
    margin: 0;
    color: var(--text-color-secondary);
    font-size: 0.8125rem;
}

.dispatch-panel__error {
    margin: 0;
    color: var(--red-600, #dc2626);
    font-size: 0.8125rem;
}
</style>
