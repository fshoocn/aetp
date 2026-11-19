<template>
    <section class="tasks-page">
        <header class="tasks-page__header">
            <div>
                <h2>测试任务</h2>
                <p class="tasks-page__hint">任务状态机与阶段 / 作业明细；点击行查看时间线、结果与日志视图</p>
            </div>
            <div class="tasks-page__actions">
                <Button label="刷新" icon="pi pi-refresh" size="small" :loading="loading" @click="load()" />
            </div>
        </header>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>

        <div class="tasks-page__toolbar">
            <div class="tasks-page__actions">
                <Select
                    v-model="stateFilter"
                    :options="stateFilterOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="全部状态"
                    showClear
                    class="tasks-page__filter"
                    @change="onFilterChange"
                />
                <Select
                    v-model="planFilter"
                    :options="plans"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="全部计划"
                    showClear
                    class="tasks-page__filter"
                    @change="onFilterChange"
                />
            </div>
            <div class="tasks-page__actions">
                <Button
                    label="上一页"
                    icon="pi pi-chevron-left"
                    size="small"
                    severity="secondary"
                    outlined
                    :disabled="offset <= 0 || loading"
                    @click="prevPage"
                />
                <span class="tasks-page__muted">第 {{ offset + 1 }} 条起</span>
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
            :value="rows"
            dataKey="id"
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
            <Column field="priority" header="优先级" sortable style="min-width: 5rem">
                <template #body="{ data }">{{ data.priority }}</template>
            </Column>
            <Column field="plan_text" header="计划" sortable style="min-width: 8rem">
                <template #body="{ data }">
                    <span class="tasks-page__muted">{{ data.plan_text }}</span>
                </template>
            </Column>
            <Column field="labels" header="标签" style="min-width: 9rem">
                <template #body="{ data }">
                    <span class="tasks-page__muted">{{ data.labels_text || '—' }}</span>
                </template>
            </Column>
            <Column field="created_ts" header="创建时间" sortable style="min-width: 10rem">
                <template #body="{ data }">{{ formatTime(data.created_ts) }}</template>
            </Column>
            <Column header="耗时" style="min-width: 7rem">
                <template #body="{ data }">{{ formatDuration(durationOf(data)) }}</template>
            </Column>
            <template #empty>
                <span class="tasks-page__muted">暂无任务</span>
            </template>
        </DataTable>

        <Panel v-if="detail" :header="`${detailTitle} · 详情`">
            <div class="tasks-page__actions">
                <InputText
                    v-model="cancelReason"
                    placeholder="取消原因（可选）"
                    class="tasks-page__reason"
                    :disabled="!canCancel"
                />
                <Button
                    label="取消任务"
                    icon="pi pi-ban"
                    size="small"
                    severity="danger"
                    :disabled="!canCancel || busy"
                    @click="cancelTask"
                />
                <Button
                    label="重跑失败 Job"
                    icon="pi pi-replay"
                    size="small"
                    severity="secondary"
                    :disabled="!isFailed || busy"
                    @click="rerunTask"
                />
                <Button label="刷新" icon="pi pi-refresh" size="small" severity="secondary" outlined :loading="busy" @click="refreshDetail" />
                <Button label="关闭" icon="pi pi-times" size="small" severity="secondary" text @click="closeDetail" />
            </div>

            <p class="tasks-page__hint">
                任务 {{ taskIdOf(detail.task) }} · 优先级 {{ detail.task.priority }}
                · {{ formatLabels(detail.task.labels) }}
                · 创建 {{ formatTime(detail.task.created_ts) }}
                <template v-if="detail.task.finished_ts"> · 结束 {{ formatTime(detail.task.finished_ts) }}</template>
            </p>
            <p v-if="detail.task.error" class="tasks-page__error-text">{{ detail.task.error }}</p>

            <div class="tasks-page__view-tabs">
                <Button
                    label="时间线"
                    icon="pi pi-history"
                    size="small"
                    :severity="detailView === 'timeline' ? 'primary' : 'secondary'"
                    :outlined="detailView !== 'timeline'"
                    @click="setDetailView('timeline')"
                />
                <Button
                    label="结果"
                    icon="pi pi-chart-bar"
                    size="small"
                    :severity="detailView === 'result' ? 'primary' : 'secondary'"
                    :outlined="detailView !== 'result'"
                    @click="setDetailView('result')"
                />
                <Button
                    label="日志"
                    icon="pi pi-align-left"
                    size="small"
                    :severity="detailView === 'logs' ? 'primary' : 'secondary'"
                    :outlined="detailView !== 'logs'"
                    @click="setDetailView('logs')"
                />
            </div>

            <template v-if="detailView === 'timeline'">
            <h4>阶段</h4>
            <DataTable v-if="stages.length" :value="stages" dataKey="name" scrollable size="small">
                <Column field="seq" header="#" style="min-width: 3rem">
                    <template #body="{ data }">{{ data.seq }}</template>
                </Column>
                <Column field="name" header="名称" style="min-width: 8rem">
                    <template #body="{ data }">{{ data.name }}</template>
                </Column>
                <Column header="类型" style="min-width: 5rem">
                    <template #body="{ data }">{{ kindText(data.kind) }}</template>
                </Column>
                <Column field="state" header="状态" style="min-width: 7rem">
                    <template #body="{ data }">
                        <Tag :value="stateText(data.state)" :severity="stateSeverity(data.state)" />
                    </template>
                </Column>
                <Column header="执行器" style="min-width: 7rem">
                    <template #body="{ data }">{{ executorId(data.executor) }}</template>
                </Column>
                <Column header="分片" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.shard_mode || 'none' }}</template>
                </Column>
                <Column header="超时(秒)" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.timeout_s || 0 }}</template>
                </Column>
                <template #empty>
                    <span class="tasks-page__muted">暂无阶段</span>
                </template>
            </DataTable>
            <p v-else class="tasks-page__muted">暂无阶段</p>

            <h4>作业（Job）</h4>
            <DataTable v-if="jobs.length" :value="jobs" dataKey="id" scrollable size="small">
                <Column header="Job" style="min-width: 9rem">
                    <template #body="{ data }">
                        <span class="tasks-page__mono" :title="jobIdOf(data)">{{ shortId(jobIdOf(data)) }}</span>
                    </template>
                </Column>
                <Column header="阶段" style="min-width: 7rem">
                    <template #body="{ data }">{{ stageNameOf(data) }}</template>
                </Column>
                <Column field="attempt" header="attempt" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.attempt }}</template>
                </Column>
                <Column field="state" header="状态" style="min-width: 7rem">
                    <template #body="{ data }">
                        <Tag :value="jobStateText(data.state)" :severity="jobStateSeverity(data.state)" />
                    </template>
                </Column>
                <Column header="节点" style="min-width: 7rem">
                    <template #body="{ data }">{{ data.node_id || '—' }}</template>
                </Column>
                <Column header="耗时" style="min-width: 6rem">
                    <template #body="{ data }">{{ formatDuration(jobDuration(data)) }}</template>
                </Column>
                <Column header="错误" style="min-width: 10rem">
                    <template #body="{ data }">
                        <span class="tasks-page__muted">{{ data.error || '—' }}</span>
                    </template>
                </Column>
                <template #empty>
                    <span class="tasks-page__muted">暂无作业</span>
                </template>
            </DataTable>
            <p v-else class="tasks-page__muted">暂无作业</p>

            <h4>状态时间线</h4>
            <Timeline v-if="history.length" :value="history">
                <template #content="slotProps">
                    <span class="tasks-page__time">{{ formatTime(slotProps.item.ts) }}</span>
                    <span>
                        {{ stateText(slotProps.item.from_state) }} → {{ stateText(slotProps.item.to_state) }}
                        <span v-if="slotProps.item.reason" class="tasks-page__muted">（{{ slotProps.item.reason }}）</span>
                    </span>
                </template>
            </Timeline>
            <p v-else class="tasks-page__muted">暂无状态转换</p>

            <h4>产物</h4>
            <DataTable v-if="artifacts.length" :value="artifacts" dataKey="id" scrollable size="small">
                <Column header="名称" style="min-width: 9rem">
                    <template #body="{ data }">{{ data.name || '—' }}</template>
                </Column>
                <Column header="类型" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.kind || '—' }}</template>
                </Column>
                <Column header="大小" style="min-width: 5rem">
                    <template #body="{ data }">{{ formatSize(data.size) }}</template>
                </Column>
                <Column header="状态" style="min-width: 5rem">
                    <template #body="{ data }">{{ data.state || '—' }}</template>
                </Column>
                <Column header="URI" style="min-width: 12rem">
                    <template #body="{ data }">
                        <span class="tasks-page__mono tasks-page__muted" :title="data.uri">{{ data.uri || '—' }}</span>
                    </template>
                </Column>
                <Column header="操作" style="min-width: 5rem">
                    <template #body="{ data }">
                        <div class="tasks-page__actions" @click.stop>
                            <Button label="下载" icon="pi pi-download" size="small" severity="secondary" text @click="downloadArtifact(data)" />
                        </div>
                    </template>
                </Column>
                <template #empty>
                    <span class="tasks-page__muted">暂无产物</span>
                </template>
            </DataTable>
            <p v-else class="tasks-page__muted">暂无产物</p>

            <template v-if="extensionKeys.length">
                <h4>扩展信息</h4>
                <div v-for="key in extensionKeys" :key="key" class="tasks-page__extension">
                    <h5>{{ key }}</h5>
                    <pre class="tasks-page__raw">{{ pretty(detail.extensions[key]) }}</pre>
                </div>
            </template>
            <template v-if="extensionErrors.length">
                <h4>扩展错误</h4>
                <pre class="tasks-page__raw">{{ extensionErrors.join('\n') }}</pre>
            </template>
            </template>

            <Panel v-else-if="detailView === 'result'" header="结果">
                <Message v-if="resultError" severity="error" :closable="false" role="alert">{{ resultError }}</Message>
                <p v-if="resultLoading" class="tasks-page__muted">结果加载中…</p>
                <template v-if="result">
                    <div class="tasks-page__stats">
                        <div class="tasks-page__stat">
                            <span class="tasks-page__stat-label">状态</span>
                            <Tag :value="stateText(result.state)" :severity="stateSeverity(result.state)" />
                        </div>
                        <div class="tasks-page__stat">
                            <span class="tasks-page__stat-label">耗时</span>
                            <strong>{{ formatDuration(result.duration_s) }}</strong>
                        </div>
                        <div v-for="entry in resultCounts" :key="entry.key" class="tasks-page__stat">
                            <span class="tasks-page__stat-label">{{ entry.key }}</span>
                            <strong>{{ entry.value }}</strong>
                        </div>
                        <div class="tasks-page__stat">
                            <span class="tasks-page__stat-label">用例</span>
                            <strong>{{ resultCaseCount }}</strong>
                        </div>
                        <div class="tasks-page__stat">
                            <span class="tasks-page__stat-label">产物</span>
                            <strong>{{ result.artifacts || 0 }}</strong>
                        </div>
                    </div>

                    <h4>Jobs 明细</h4>
                    <DataTable v-if="resultJobs.length" :value="resultJobs" dataKey="job_id" scrollable size="small">
                        <Column header="Job" style="min-width: 9rem">
                            <template #body="{ data }">
                                <span class="tasks-page__mono" :title="data.job_id">{{ shortId(data.job_id) }}</span>
                            </template>
                        </Column>
                        <Column header="attempt" style="min-width: 5rem">
                            <template #body="{ data }">{{ data.attempt }}</template>
                        </Column>
                        <Column header="状态" style="min-width: 7rem">
                            <template #body="{ data }">
                                <Tag :value="jobStateText(data.status)" :severity="jobStateSeverity(data.status)" />
                            </template>
                        </Column>
                        <Column header="节点" style="min-width: 7rem">
                            <template #body="{ data }">{{ data.node_id || '—' }}</template>
                        </Column>
                        <Column header="summary" style="min-width: 10rem">
                            <template #body="{ data }">
                                <span class="tasks-page__muted">{{ formatLabels(data.summary) || '—' }}</span>
                            </template>
                        </Column>
                        <template #empty>
                            <span class="tasks-page__muted">暂无作业</span>
                        </template>
                    </DataTable>
                    <p v-else class="tasks-page__muted">暂无作业</p>

                    <div class="tasks-page__toolbar">
                        <h4>失败用例</h4>
                        <Select
                            v-model="caseStatus"
                            :options="caseStatusOptions"
                            optionLabel="label"
                            optionValue="value"
                            class="tasks-page__filter"
                            @change="loadCases"
                        />
                    </div>
                    <DataTable v-if="cases.length" :value="cases" dataKey="id" scrollable size="small">
                        <Column header="用例" style="min-width: 10rem">
                            <template #body="{ data }">
                                <span class="tasks-page__mono" :title="data.case_id">{{ data.case_id || '—' }}</span>
                            </template>
                        </Column>
                        <Column header="耗时" style="min-width: 5rem">
                            <template #body="{ data }">{{ formatDuration(data.duration_s) }}</template>
                        </Column>
                        <Column header="message" style="min-width: 14rem">
                            <template #body="{ data }">
                                <div class="tasks-page__case-message">
                                    <span class="tasks-page__clamp" :title="data.message || ''">{{ data.message || '—' }}</span>
                                    <Button
                                        v-if="data.message"
                                        :label="caseExpanded[data.id] ? '收起' : '展开'"
                                        size="small"
                                        severity="secondary"
                                        text
                                        @click="toggleCaseMessage(data.id)"
                                    />
                                    <pre v-if="caseExpanded[data.id]" class="tasks-page__raw">{{ data.message }}</pre>
                                </div>
                            </template>
                        </Column>
                        <Column header="detail_ref" style="min-width: 8rem">
                            <template #body="{ data }">
                                <span class="tasks-page__mono tasks-page__muted" :title="data.detail_ref || ''">{{ data.detail_ref || '—' }}</span>
                            </template>
                        </Column>
                        <template #empty>
                            <span class="tasks-page__muted">暂无{{ caseStatus === 'failed' ? '失败' : '错误' }}用例</span>
                        </template>
                    </DataTable>
                    <p v-else class="tasks-page__muted">暂无{{ caseStatus === 'failed' ? '失败' : '错误' }}用例</p>

                    <h4>产物</h4>
                    <DataTable v-if="artifacts.length" :value="artifacts" dataKey="artifact_id" scrollable size="small">
                        <Column header="类型" style="min-width: 5rem">
                            <template #body="{ data }">{{ data.kind || '—' }}</template>
                        </Column>
                        <Column header="名称" style="min-width: 9rem">
                            <template #body="{ data }">{{ data.name || '—' }}</template>
                        </Column>
                        <Column header="大小" style="min-width: 5rem">
                            <template #body="{ data }">{{ formatSize(data.size) }}</template>
                        </Column>
                        <Column header="操作" style="min-width: 5rem">
                            <template #body="{ data }">
                                <div class="tasks-page__actions" @click.stop>
                                    <Button label="下载" icon="pi pi-download" size="small" severity="secondary" text @click="downloadArtifact(data)" />
                                </div>
                            </template>
                        </Column>
                        <template #empty>
                            <span class="tasks-page__muted">暂无产物</span>
                        </template>
                    </DataTable>
                    <p v-else class="tasks-page__muted">暂无产物</p>
                </template>
                <p v-else-if="!resultLoading" class="tasks-page__muted">暂无结果</p>
            </Panel>

            <Panel v-else header="日志">
                <div class="tasks-page__actions">
                    <Select
                        v-model="logJobId"
                        :options="jobOptions"
                        optionLabel="label"
                        optionValue="value"
                        placeholder="选择 Job"
                        class="tasks-page__filter"
                        @change="onLogJobChange"
                    />
                    <Tag v-if="selectedJob" :value="`attempt ${selectedJob.attempt}`" severity="info" />
                    <span class="tasks-page__muted">已加载 {{ logRows.length }} 行 · 游标 {{ logSinceId }}</span>
                </div>
                <Message v-if="!logAvailable" severity="warn" :closable="false">日志服务未启用</Message>
                <Message v-else-if="logError" severity="error" :closable="false" role="alert">{{ logError }}</Message>
                <div ref="logScroller" class="tasks-page__log">
                    <div
                        v-for="row in logRows"
                        :key="row.id"
                        class="tasks-page__log-row"
                        :class="{ 'tasks-page__log-row--case': isCaseRow(row) }"
                    >
                        <span class="tasks-page__log-ts">{{ formatLogTime(row.ts) }}</span>
                        <span class="tasks-page__log-level">{{ logLevelText(row.level) }}</span>
                        <span class="tasks-page__log-source" :title="row.name || ''">{{ row.source || '—' }}</span>
                        <span class="tasks-page__log-msg">{{ row.msg }}</span>
                    </div>
                    <p v-if="!logRows.length" class="tasks-page__muted">{{ logJobId ? '暂无日志' : '请选择 Job 后开始拉取日志' }}</p>
                </div>
            </Panel>
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
        throw new Error(message || `请求失败 (${response.status})`);
    }
    return data;
}

const TASK_STATE_TEXT = {
    pending: '待调度',
    planning: '规划中',
    running: '执行中',
    canceling: '取消中',
    succeeded: '成功',
    failed: '失败',
    canceled: '已取消',
    error: '错误'
};

const JOB_STATE_TEXT = {
    queued: '排队中',
    leased: '已租约',
    running: '执行中',
    succeeded: '成功',
    failed: '失败',
    canceled: '已取消',
    timeout: '超时',
    lost: '失联'
};

const FINISHED_STATES = ['succeeded', 'failed', 'canceled', 'error'];

// 日志游标轮询节律（§5.6 / S6）：基础 2s；连续空轮询退避 2s→4s→8s→10s 封顶，有新行复位
const LOG_POLL_DELAYS = [2000, 4000, 8000, 10000];
const LOG_POLL_LIMIT = 200;

export default {
    data() {
        return {
            tasks: [],
            detail: null,
            jobs: [],
            artifacts: [],
            error: '',
            loading: false,
            busy: false,
            selected: null,
            stateFilter: null,
            planFilter: null,
            offset: 0,
            limit: 20,
            cancelReason: '',
            plans: [],
            detailView: 'timeline',
            result: null,
            resultLoading: false,
            resultError: '',
            cases: [],
            caseStatus: 'failed',
            caseExpanded: {},
            logJobId: '',
            logRows: [],
            logSinceId: 0,
            logEmptyStreak: 0,
            logAvailable: true,
            logError: '',
            logTimer: null,
            visibilityHandler: null,
            disposed: false,
            caseStatusOptions: [
                { label: '失败（failed）', value: 'failed' },
                { label: '错误（error）', value: 'error' }
            ],
            stateFilterOptions: Object.entries(TASK_STATE_TEXT).map(([value, label]) => ({ label, value }))
        };
    },
    computed: {
        rows() {
            // 展示行：补一个格式化后的标签文本与计划名，供表格与筛选共用
            return this.tasks.map((task) => ({
                ...task,
                id: this.taskIdOf(task),
                labels_text: this.formatLabels(task.labels),
                plan_text: this.planText(task.labels)
            }));
        },
        hasMore() {
            return this.tasks.length >= this.limit;
        },
        task() {
            return (this.detail && this.detail.task) || {};
        },
        stages() {
            return (this.detail && this.detail.stages) || [];
        },
        history() {
            return (this.detail && (this.detail.history || this.detail.task_history)) || [];
        },
        detailTitle() {
            return this.task.name || this.taskIdOf(this.task) || '任务';
        },
        canCancel() {
            return ['pending', 'planning', 'running'].includes(this.task.state);
        },
        isFailed() {
            return this.task.state === 'failed';
        },
        extensionKeys() {
            return Object.keys((this.detail && this.detail.extensions) || {});
        },
        extensionErrors() {
            return (this.detail && this.detail.extension_errors) || [];
        },
        resultCounts() {
            // counts 键序：常见状态键固定在前，其余按名称排，展示稳定
            const counts = (this.result && this.result.counts) || {};
            const preferred = ['total', 'passed', 'failed', 'skipped', 'error'];
            const keys = Object.keys(counts).sort((a, b) => {
                const ia = preferred.indexOf(a);
                const ib = preferred.indexOf(b);
                if (ia !== ib) return (ia === -1 ? preferred.length : ia) - (ib === -1 ? preferred.length : ib);
                return a < b ? -1 : a > b ? 1 : 0;
            });
            return keys.map((key) => ({ key, value: counts[key] }));
        },
        resultJobs() {
            return (this.result && this.result.jobs) || [];
        },
        resultCaseCount() {
            const counts = (this.result && this.result.cases) || {};
            return Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
        },
        jobOptions() {
            return this.jobs.map((job) => {
                const id = this.jobIdOf(job);
                return {
                    value: id,
                    label: `${this.shortId(id)} · ${this.stageNameOf(job)} · 第 ${job.attempt} 次`,
                    attempt: job.attempt
                };
            });
        },
        selectedJob() {
            return this.jobs.find((job) => this.jobIdOf(job) === this.logJobId) || null;
        }
    },
    mounted() {
        this.load();
        this.loadPlans();
        // 后台标签页暂停日志轮询，回到前台恢复（§5.6 / S6）
        this.visibilityHandler = () => {
            if (this.disposed) return;
            if (document.hidden) {
                this.stopLogPolling();
            } else if (this.detailView === 'logs' && this.logJobId) {
                this.startLogPolling();
            }
        };
        document.addEventListener('visibilitychange', this.visibilityHandler);
    },
    beforeUnmount() {
        this.disposed = true;
        this.stopLogPolling();
        if (this.visibilityHandler) document.removeEventListener('visibilitychange', this.visibilityHandler);
    },
    methods: {
        async load(silent) {
            if (!silent) this.loading = true;
            try {
                const query = new URLSearchParams({ limit: String(this.limit), offset: String(this.offset) });
                if (this.stateFilter) query.set('state', this.stateFilter);
                if (this.planFilter) query.set('labels', JSON.stringify({ plan_id: this.planFilter }));
                const data = await fetchJson(apiUrl(`/api/test/tasks?${query.toString()}`));
                this.tasks = data.tasks || [];
                this.error = '';
                if (this.detail) await this.refreshDetail(true);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.loading = false;
            }
        },
        async refreshDetail(silent) {
            if (this.detail) await this.showDetail(this.taskIdOf(this.task), silent);
        },
        async showDetail(taskId, silent) {
            if (!taskId) return;
            this.busy = !silent;
            try {
                const base = `/api/test/tasks/${encodeURIComponent(taskId)}`;
                const detail = await fetchJson(apiUrl(base));
                const jobsData = await fetchJson(apiUrl(`${base}/jobs`));
                const artifactsData = await fetchJson(
                    apiUrl(`/api/test/artifacts?task_id=${encodeURIComponent(taskId)}`)
                );
                this.detail = detail;
                this.jobs = jobsData.jobs || [];
                this.artifacts = artifactsData.artifacts || [];
                this.error = '';
                if (this.detailView === 'result') await this.loadResult();
                if (this.detailView === 'logs') {
                    this.ensureLogSelection();
                    this.startLogPolling();
                }
            } catch (reason) {
                if (!silent) this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.busy = false;
            }
        },
        onRowSelect(event) {
            this.cancelReason = '';
            this.resetResultView();
            this.resetLogView();
            this.showDetail(this.taskIdOf(event.data));
        },
        closeDetail() {
            this.detail = null;
            this.jobs = [];
            this.artifacts = [];
            this.selected = null;
            this.cancelReason = '';
            this.detailView = 'timeline';
            this.resetResultView();
            this.resetLogView();
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
        async cancelTask() {
            const taskId = this.taskIdOf(this.task);
            this.busy = true;
            try {
                await fetchJson(apiUrl(`/api/test/tasks/${encodeURIComponent(taskId)}/cancel`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ reason: this.cancelReason })
                });
                this.cancelReason = '';
                await this.load(true);
                await this.showDetail(taskId, true);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.busy = false;
            }
        },
        async rerunTask() {
            const taskId = this.taskIdOf(this.task);
            this.busy = true;
            try {
                await fetchJson(apiUrl(`/api/test/tasks/${encodeURIComponent(taskId)}/rerun`), {
                    method: 'POST'
                });
                await this.load(true);
                await this.showDetail(taskId);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.busy = false;
            }
        },
        // ============================ 详情视图切换（时间线 / 结果 / 日志） ========
        setDetailView(view) {
            if (this.detailView === view) return;
            this.detailView = view;
            if (view === 'result') {
                this.stopLogPolling();
                this.loadResult();
            } else if (view === 'logs') {
                this.ensureLogSelection();
                this.startLogPolling();
            } else {
                this.stopLogPolling();
            }
        },
        async loadPlans() {
            // 计划下拉 id→name 映射（§5.6）；失败仅清空下拉，不影响任务列表
            try {
                const data = await fetchJson(apiUrl('/api/test/plans'));
                this.plans = (data.plans || []).map((plan) => ({
                    label: String(plan.name || plan.plan_id || plan.id || '未命名计划'),
                    value: String(plan.plan_id || plan.id || '')
                }));
            } catch {
                this.plans = [];
            }
        },
        // ============================ 结果视图 ================================
        async loadResult() {
            const taskId = this.taskIdOf(this.task);
            if (!taskId) return;
            this.resultLoading = true;
            try {
                const data = await fetchJson(apiUrl(`/api/test/tasks/${encodeURIComponent(taskId)}/result`));
                this.result = (data && data.result) || null;
                this.resultError = '';
                await this.loadCases();
            } catch (reason) {
                this.result = null;
                this.resultError = String(reason && reason.message ? reason.message : reason);
            } finally {
                this.resultLoading = false;
            }
        },
        async loadCases() {
            const taskId = this.taskIdOf(this.task);
            if (!taskId) return;
            this.caseExpanded = {};
            try {
                const query = new URLSearchParams({ task_id: taskId, status: this.caseStatus, limit: '200' });
                const data = await fetchJson(apiUrl(`/api/test/cases?${query.toString()}`));
                this.cases = (data && data.cases) || [];
            } catch (reason) {
                this.cases = [];
                this.resultError = String(reason && reason.message ? reason.message : reason);
            }
        },
        toggleCaseMessage(caseId) {
            this.caseExpanded = { ...this.caseExpanded, [caseId]: !this.caseExpanded[caseId] };
        },
        resetResultView() {
            this.result = null;
            this.resultError = '';
            this.resultLoading = false;
            this.cases = [];
            this.caseStatus = 'failed';
            this.caseExpanded = {};
        },
        // ============================ 日志视图（游标轮询） ====================
        ensureLogSelection() {
            // Job 明细变化后选中项可能失效：自动落到第一个 Job
            const known = this.jobs.some((job) => this.jobIdOf(job) === this.logJobId);
            if (!known) {
                this.logJobId = this.jobs.length ? this.jobIdOf(this.jobs[0]) : '';
                this.resetLogRows();
            }
        },
        onLogJobChange() {
            this.resetLogRows();
            this.startLogPolling();
        },
        resetLogRows() {
            this.logRows = [];
            this.logSinceId = 0;
            this.logEmptyStreak = 0;
            this.logError = '';
            this.logAvailable = true;
        },
        resetLogView() {
            this.stopLogPolling();
            this.logJobId = '';
            this.resetLogRows();
        },
        startLogPolling() {
            this.stopLogPolling();
            if (this.disposed || this.detailView !== 'logs' || !this.logJobId || !this.logAvailable) return;
            if (typeof document !== 'undefined' && document.hidden) return; // 后台标签页暂停，visibilitychange 恢复
            this.scheduleLogPoll(0); // 进入视图先立即拉一拍
        },
        stopLogPolling() {
            if (this.logTimer) {
                window.clearTimeout(this.logTimer);
                this.logTimer = null;
            }
        },
        scheduleLogPoll(delayMs) {
            if (this.disposed || this.detailView !== 'logs' || !this.logJobId || !this.logAvailable) return;
            if (typeof document !== 'undefined' && document.hidden) return;
            // 超时链轮询：上一拍收完才排下一拍；连续空轮询退避 2s→4s→8s→10s 封顶
            const wait = typeof delayMs === 'number'
                ? delayMs
                : LOG_POLL_DELAYS[Math.min(this.logEmptyStreak, LOG_POLL_DELAYS.length - 1)];
            this.logTimer = window.setTimeout(async () => {
                await this.pollLogs();
                if (!this.disposed) this.scheduleLogPoll();
            }, wait);
        },
        async pollLogs() {
            const jobId = this.logJobId;
            if (!jobId) return;
            try {
                const query = new URLSearchParams({
                    since_id: String(this.logSinceId),
                    limit: String(LOG_POLL_LIMIT)
                });
                const data = await fetchJson(
                    apiUrl(`/api/test/jobs/${encodeURIComponent(jobId)}/logs?${query.toString()}`)
                );
                if (this.disposed || this.logJobId !== jobId) return;
                if (data.log_available === false) {
                    this.logAvailable = false;
                    this.stopLogPolling();
                    return;
                }
                this.logAvailable = true;
                this.logError = '';
                const rows = data.logs || [];
                if (rows.length) {
                    this.logRows = this.logRows.concat(rows); // API 恒返回旧 → 新，直接追加
                    this.logEmptyStreak = 0; // 有新行复位
                    this.$nextTick(() => this.scrollLogsToBottom());
                } else {
                    this.logEmptyStreak += 1;
                }
                const next = Number(data.next_since_id);
                if (Number.isFinite(next) && next > this.logSinceId) this.logSinceId = next;
            } catch (reason) {
                if (this.disposed || this.logJobId !== jobId) return;
                this.logError = String(reason && reason.message ? reason.message : reason);
                this.logEmptyStreak += 1; // 拉取失败同样退避，避免打爆故障端点
            }
        },
        scrollLogsToBottom() {
            const box = this.$refs.logScroller;
            if (box) box.scrollTop = box.scrollHeight;
        },
        downloadArtifact(artifact) {
            const uri = String(artifact.uri || '');
            // 外部 URI（oss:// 等，消费插件回填）直接新窗口打开；
            // 本地产物走带令牌的下载（<a> 带不上 X-AETP-Token 头）
            if (uri && !uri.startsWith('local://') && uri.includes('://')) {
                window.open(uri, '_blank', 'noopener');
                return;
            }
            this.downloadLocal(artifact);
        },
        async downloadLocal(artifact) {
            const artifactId = this.artifactId(artifact);
            try {
                const response = await fetch(
                    apiUrl(`/api/test/artifacts/${encodeURIComponent(artifactId)}/download`),
                    { headers: authHeaders() }
                );
                if (!response.ok) throw new Error(`下载失败 (${response.status})`);
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = artifact.name || artifactId;
                document.body.appendChild(link);
                link.click();
                link.remove();
                URL.revokeObjectURL(url);
            } catch (reason) {
                this.error = String(reason && reason.message ? reason.message : reason);
            }
        },
        taskIdOf(task) {
            return String((task && (task.id || task.task_id)) || '');
        },
        jobIdOf(job) {
            return String((job && (job.id || job.job_id)) || '');
        },
        artifactId(artifact) {
            return String((artifact && (artifact.id || artifact.artifact_id)) || '');
        },
        shortId(value) {
            const text = String(value || '');
            return text.length > 14 ? `${text.slice(0, 12)}…` : text;
        },
        stageNameOf(job) {
            return job.stage_name || job.stage || job.stage_id || '—';
        },
        executorId(executor) {
            if (executor && typeof executor === 'object') return executor.id || '—';
            if (typeof executor === 'string' && executor.trim()) {
                try {
                    const parsed = JSON.parse(executor);
                    return (parsed && parsed.id) || '—';
                } catch {
                    return executor;
                }
            }
            return '—';
        },
        kindText(kind) {
            return { run: '执行', approval: '确认门' }[kind] || kind || '—';
        },
        stateText(state) {
            return TASK_STATE_TEXT[state] || state || '—';
        },
        stateSeverity(state) {
            return (
                {
                    pending: 'secondary',
                    planning: 'info',
                    running: 'warn',
                    canceling: 'warn',
                    succeeded: 'success',
                    failed: 'danger',
                    canceled: 'secondary',
                    error: 'danger'
                }[state] || 'secondary'
            );
        },
        jobStateText(state) {
            return JOB_STATE_TEXT[state] || state || '—';
        },
        jobStateSeverity(state) {
            return (
                {
                    queued: 'secondary',
                    leased: 'info',
                    running: 'warn',
                    succeeded: 'success',
                    failed: 'danger',
                    canceled: 'secondary',
                    timeout: 'danger',
                    lost: 'danger'
                }[state] || 'secondary'
            );
        },
        formatLabels(labels) {
            if (!labels) return '';
            let source = labels;
            if (typeof labels === 'string') {
                try {
                    source = JSON.parse(labels) || {};
                } catch {
                    return labels;
                }
            }
            return Object.entries(source)
                .map(([key, value]) => `${key}=${value}`)
                .join(', ');
        },
        planText(labels) {
            // 计划列（§5.6）：labels.plan_name 显示，缺省 labels.plan_id，再缺省「—」
            let source = labels;
            if (typeof labels === 'string') {
                try {
                    source = JSON.parse(labels) || {};
                } catch {
                    source = {};
                }
            }
            const map = source && typeof source === 'object' ? source : {};
            return map.plan_name || map.plan_id || '—';
        },
        isCaseRow(row) {
            // case 归属高亮依据行 source 字段（评审 S6）：非 stream 值即用例行
            const source = String((row && row.source) || '');
            return source !== '' && source !== 'stdout' && source !== 'stderr';
        },
        logLevelText(level) {
            return { 0: '错误', 1: '警告', 2: '信息', 3: '调试' }[level] || '调试';
        },
        formatLogTime(value) {
            if (!value) return '—';
            const date = new Date(value * 1000);
            const ms = String(date.getMilliseconds()).padStart(3, '0');
            return `${date.toLocaleTimeString()}.${ms}`;
        },
        durationOf(task) {
            const created = Number(task.created_ts) || 0;
            if (!created) return null;
            const end =
                Number(task.finished_ts) ||
                Number(task.updated_ts) ||
                (FINISHED_STATES.includes(task.state) ? 0 : Date.now() / 1000);
            return end > created ? end - created : null;
        },
        jobDuration(job) {
            const started = Number(job.started_ts) || 0;
            const finished = Number(job.finished_ts) || 0;
            return started && finished > started ? finished - started : null;
        },
        formatDuration(seconds) {
            if (seconds === null || seconds === undefined || !(seconds >= 0)) return '—';
            if (seconds < 60) return `${seconds.toFixed(1)}s`;
            const minutes = Math.floor(seconds / 60);
            const rest = Math.round(seconds % 60);
            if (minutes < 60) return `${minutes}m ${rest}s`;
            return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
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
        },
        pretty(value) {
            return JSON.stringify(value, null, 2);
        }
    }
};
</script>

<style scoped>
/* 插件 SFC 用不了运行时 Tailwind：仅保留布局所需的少量样式，
   组件外观全部来自共享 PrimeVue 组件与宿主语义变量 */
.tasks-page {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.tasks-page__header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.tasks-page__header h2 {
    margin: 0;
}

.tasks-page__hint {
    margin: 0.25rem 0 0;
    color: var(--text-color-secondary);
    font-size: 0.875rem;
}

.tasks-page__muted {
    color: var(--text-color-secondary);
}

.tasks-page__error-text {
    color: var(--red-500);
    font-size: 0.875rem;
}

.tasks-page__actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
    white-space: nowrap;
}

.tasks-page__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
}

.tasks-page__filter {
    min-width: 12rem;
}

.tasks-page__reason {
    width: 14rem;
}

.tasks-page__time {
    display: inline-block;
    margin-right: 0.75rem;
    color: var(--text-color-secondary);
    font-variant-numeric: tabular-nums;
}

.tasks-page__mono {
    font-family: monospace;
    font-size: 0.8125rem;
}

.tasks-page__extension h5 {
    margin: 0.5rem 0 0.25rem;
}

.tasks-page__raw {
    max-height: 16rem;
    margin: 0;
    overflow: auto;
    padding: 0.75rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    font-size: 0.75rem;
}

.tasks-page__view-tabs {
    display: flex;
    gap: 0.375rem;
    margin: 0.75rem 0 0.25rem;
}

/* 结果视图：聚合卡片 */
.tasks-page__stats {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-bottom: 1rem;
}

.tasks-page__stat {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 5.5rem;
    padding: 0.625rem 0.875rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
}

.tasks-page__stat-label {
    color: var(--text-color-secondary);
    font-size: 0.75rem;
}

/* 失败用例：message 截断 + 展开 */
.tasks-page__case-message {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    align-items: flex-start;
}

.tasks-page__clamp {
    display: block;
    max-width: 22rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* 日志视图：游标追加滚动区 */
.tasks-page__log {
    max-height: 24rem;
    overflow: auto;
    padding: 0.5rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    font-family: monospace;
    font-size: 0.75rem;
}

.tasks-page__log-row {
    display: flex;
    gap: 0.5rem;
    padding: 0.125rem 0.25rem;
    white-space: pre-wrap;
    word-break: break-word;
}

/* case 归属高亮（评审 S6：依据行 source 字段） */
.tasks-page__log-row--case {
    background: color-mix(in srgb, var(--primary-color, #4f9cf9) 18%, transparent);
    font-weight: 700;
}

.tasks-page__log-ts {
    flex: none;
    color: var(--text-color-secondary);
    font-variant-numeric: tabular-nums;
}

.tasks-page__log-level {
    flex: none;
    min-width: 2.25rem;
}

.tasks-page__log-source {
    flex: none;
    max-width: 10rem;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-color-secondary);
}

.tasks-page__log-msg {
    flex: 1;
    min-width: 0;
}
</style>
