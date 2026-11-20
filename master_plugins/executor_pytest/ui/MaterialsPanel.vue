<!-- pytest 脚本处理面板（W2-D）：脚本管理页 /scripts 的 slot 面板（target=scripts:executor-pytest）。
     「上传 → 自动分析 → 取消 / 保存」向导：只处理「当前上传的脚本」——上传成功（含 deduplicated
     复用 id）自动触发 analyze inspect 并轮询至终结，只呈现「分析是否通过」（未通过附问题
     code / 条数一行摘要）；用例 / 标签查看归脚本总表「详情」，选例归计划编辑器，预推归下发流程，
     下载 / 删除归脚本总表。
     插件 SFC 自包含：不 import 宿主 / npm 模块，仅用全局共享组件 + 原生控件（Options API）。 -->
<template>
    <section class="materials-panel">
        <header class="materials-panel__header">
            <div>
                <h3>pytest 脚本处理</h3>
                <p class="materials-panel__hint">上传脚本包（zip）→ 自动分析 → 取消 / 保存；用例查看归脚本总表「详情」，选例归计划编辑器，预推归下发流程</p>
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="info" :closable="true" @close="notice = ''">{{ notice }}</Message>

        <!-- 上传态 -->
        <Panel v-if="phase === 'upload'" header="上传脚本（zip）">
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
                <code>X-Material-Executor: pytest</code> 头自动标记。上传成功后自动开始分析，只呈现「是否通过」。
            </p>
        </Panel>

        <!-- 分析态：上传成功后自动触发 analyze，轮询至终结 -->
        <Panel v-else-if="phase === 'analyzing'" :header="`正在分析：${detailTitle}`">
            <p class="materials-panel__hint">
                <Tag :value="stateText(material.state)" :severity="stateSeverity(material.state)" />
                · <span class="materials-panel__mono">{{ materialIdOf(material) }}</span>
                · {{ formatSize(material.size) }} · 更新 {{ formatTime(material.updated_ts) }}
                · sha256 <span class="materials-panel__mono">{{ shortSha(material.sha256) }}</span>
            </p>
            <div class="materials-panel__progress" role="status">
                <span class="materials-panel__spinner" aria-hidden="true"></span>
                <span>分析中…已用时 {{ analyzingSeconds }}s（语法 / 依赖 / 可收集性，自动完成）</span>
            </div>
        </Panel>

        <!-- 完成态：只告诉用户是否分析通过（明细归总表「详情」） -->
        <Panel v-else :header="`分析结果：${detailTitle}`">
            <p class="materials-panel__hint">
                <Tag :value="stateText(material.state)" :severity="stateSeverity(material.state)" />
                · <span class="materials-panel__mono">{{ materialIdOf(material) }}</span>
                · {{ formatSize(material.size) }} · 更新 {{ formatTime(material.updated_ts) }}
                · sha256 <span class="materials-panel__mono">{{ shortSha(material.sha256) }}</span>
            </p>
            <p
                class="materials-panel__result"
                :class="analyzeOk ? 'materials-panel__result--ok' : 'materials-panel__result--fail'"
                role="status"
            >
                {{ analyzeOk ? '✅ 分析通过' : '❌ 分析未通过' }}
            </p>
            <p v-if="!analyzeOk && problemSummary" class="materials-panel__hint">{{ problemSummary }}</p>
            <div class="materials-panel__actions">
                <Button v-if="!decided && analyzeOk" label="保存" icon="pi pi-save" size="small" @click="saveMaterial" />
                <Button v-if="!decided" label="取消" icon="pi pi-trash" size="small" severity="danger" outlined :loading="deleting" @click="cancelMaterial" />
                <Button label="继续上传" icon="pi pi-upload" size="small" severity="secondary" text @click="restartUpload" />
            </div>
            <p class="materials-panel__hint">{{ doneHint }}</p>
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
            // 向导阶段：upload（上传）→ analyzing（自动分析）→ done（完成：取消 / 保存）
            phase: 'upload',
            // 当前材料详情（analyzing / done 展示基本信息）
            detail: null,
            error: '',
            notice: '',
            // 上传区
            file: null,
            uploadName: '',
            uploading: false,
            // 自动分析（analyze inspect → 轮询至终结）
            analyzingSeconds: 0,
            analyzeOk: false,
            problemSummary: '',
            // 完成态决策：'' 未决策 / 'saved' 已保存 / 'deleted' 已取消并删除
            decided: '',
            deleting: false,
            disposed: false
        };
    },
    computed: {
        material() {
            return this.detail || {};
        },
        detailTitle() {
            const name = String((this.detail && this.detail.name) || '').trim();
            return name || this.materialIdOf(this.detail) || '当前脚本';
        },
        doneHint() {
            if (this.decided === 'saved') return '材料已保留，可在脚本总表查看 / 删除。点「继续上传」处理下一个脚本。';
            if (this.decided === 'deleted') return '材料已删除。点「继续上传」处理下一个脚本。';
            const keep = '「继续上传」直接处理下一个脚本（材料保留）；点弹窗 X 关闭同样保留现状（总表可见 / 可删）。';
            return this.analyzeOk
                ? `「保存」保留材料；「取消」删除该材料；${keep}`
                : `「取消」删除该材料；${keep}`;
        }
    },
    beforeUnmount() {
        this.disposed = true;
    },
    methods: {
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
                    ? `内容指纹命中既有材料（deduplicated），复用 id=${materialId}，已自动开始分析`
                    : `脚本已上传：${material.name || ''}（id=${materialId}），已自动开始分析`;
                this.file = null;
                this.error = '';
                this.resetWizard();
                this.phase = 'analyzing';
                await this.openDetail(materialId, true);
                await this.startAnalyze(materialId);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.uploading = false;
            }
        },
        resetWizard() {
            this.analyzingSeconds = 0;
            this.analyzeOk = false;
            this.problemSummary = '';
            this.decided = '';
        },
        async openDetail(materialId, silent) {
            if (!materialId) return;
            try {
                const data = await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}`));
                this.detail = data.material || null;
                this.error = '';
            } catch (reason) {
                if (!silent) this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        async startAnalyze(materialId) {
            // 上传成功（含 deduplicated 复用 id）自动触发 analyze：编译结果 + 用例 / 标签清单
            // 由后端产出（analyze 应答带 cases），UI 只轮询到 ok 字段呈现「是否通过」
            if (!materialId) return;
            this.analyzingSeconds = 0;
            try {
                const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/inspect`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: 'analyze' })
                });
                await this.pollInspect(data.request_id);
            } catch (reason) {
                this.finishAnalyze(false, `分析触发失败：${String(reason && reason.message ? reason.message : reason)}`);
            }
        },
        async pollInspect(requestId) {
            // 轮询至终结态（inspecting → done / failed）；后端看门狗保证 inspect_timeout_s 内终结
            for (let attempt = 0; attempt < 150 && !this.disposed; attempt += 1) {
                await new Promise((resolve) => window.setTimeout(resolve, 2000));
                if (this.disposed) return;
                let record = null;
                try {
                    const data = await fetchJson(apiUrl(`/api/pytest/inspects/${encodeURIComponent(requestId)}`));
                    record = data.inspect || null;
                } catch (reason) {
                    this.finishAnalyze(false, `分析状态查询失败：${String(reason && reason.message ? reason.message : reason)}`);
                    return;
                }
                if (record && record.state !== 'inspecting') {
                    // 通过口径与后端落核心一致：done + ok，且问题清单无 error 级（否则核心置 failed）
                    const problems = Array.isArray(record.problems) ? record.problems : [];
                    const hasError = problems.some((item) => String((item && item.severity) || '') === 'error');
                    const passed = record.state === 'done' && Boolean(record.ok) && !hasError;
                    this.finishAnalyze(passed, this.problemSummaryOf(record));
                    return;
                }
                this.analyzingSeconds += 2;
            }
            if (!this.disposed) this.finishAnalyze(false, '分析超时未终结，可稍后在脚本总表查看状态');
        },
        problemSummaryOf(record) {
            // 未通过摘要只报「问题 code / 条数」一行，不展开明细（明细归总表「详情」）
            const problems = record && Array.isArray(record.problems) ? record.problems : [];
            if (!problems.length) return String((record && record.error) || '未给出问题明细');
            const counts = new Map();
            for (const item of problems) {
                const code = String((item && item.code) || 'unknown');
                counts.set(code, (counts.get(code) || 0) + 1);
            }
            const parts = [...counts.entries()].map(([code, count]) => `${code} ×${count}`);
            return `问题 ${problems.length} 条：${parts.join('、')}`;
        },
        finishAnalyze(passed, summary) {
            this.analyzeOk = Boolean(passed);
            this.problemSummary = passed ? '' : summary;
            this.phase = 'done';
            // 核心已按应答刷新材料状态（analyzed / failed），静默拉新基本信息
            const materialId = this.materialIdOf(this.detail);
            if (materialId) this.openDetail(materialId, true);
        },
        saveMaterial() {
            // 「保存」= 保留材料（不删除），提示「已保存」；用例查看 / 删除归脚本总表
            this.decided = 'saved';
            this.notice = '已保存';
            this.error = '';
        },
        async cancelMaterial() {
            // 「取消」= 删除该材料（生命周期收口 core：被任务 / 计划 / 选择集引用时 409）
            const materialId = this.materialIdOf(this.detail);
            if (!materialId || this.deleting) return;
            this.deleting = true;
            try {
                await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(materialId)}`), { method: 'DELETE' });
                this.decided = 'deleted';
                this.notice = '已取消并删除';
                this.error = '';
            } catch (reason) {
                const text = String(reason && reason.message ? reason.message : reason);
                this.error = text.includes('material_in_use') || text.includes('引用')
                    ? `取消失败（引用冲突）：${text}——被任务 / 计划 / 选择集引用的材料禁止删除，请先解除引用`
                    : `取消失败（删除材料）：${text}`;
            } finally {
                this.deleting = false;
            }
        },
        restartUpload() {
            // 「继续上传」= 原「更换脚本」语义：回上传态处理下一个脚本（本材料保留）
            this.phase = 'upload';
            this.detail = null;
            this.resetWizard();
            this.error = '';
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
    margin-top: 0.5rem;
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

.materials-panel__progress {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    padding: 0.75rem 0 0.25rem;
    font-size: 0.9375rem;
}

.materials-panel__spinner {
    width: 1.125rem;
    height: 1.125rem;
    border: 2px solid var(--text-color-secondary);
    border-top-color: transparent;
    border-radius: 50%;
    flex: none;
    animation: materials-panel-spin 0.9s linear infinite;
}

@keyframes materials-panel-spin {
    to {
        transform: rotate(360deg);
    }
}

.materials-panel__result {
    margin: 0.5rem 0 0.25rem;
    font-size: 1.125rem;
    font-weight: 600;
}

.materials-panel__result--ok {
    color: var(--green-600, #16a34a);
}

.materials-panel__result--fail {
    color: var(--red-600, #dc2626);
}
</style>
