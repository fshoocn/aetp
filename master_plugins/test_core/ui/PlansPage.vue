<!--
  测试计划（/plans）：列表 ⇄ 编辑器单页，视图切换 query 驱动（?edit=<plan_id|new>）。
  编辑器 v2.1（docs/测试计划编辑器v2设计.md §7.1 修订）：步骤区两行摘要卡（行1 #N Tag +
  执行器 Tag + 脚本名 v短sha + 用例 Tag（全部用例 / 指定 N/M）+ 旧版警告 Tag；行2 参数
  键值 digest · 台架摘要 · 分片 · 超时；操作 [编辑][删除][↑][↓]，摘要纯插值防 XSS），
  「添加测试脚本」为悬浮 PvMenu（菜单项 = 脚本名称 name · v短sha，多版本同名并列 label 加
  上传时间）→ PvDialog「配置脚本」（① 勾选用例 → ② 任务参数（plans:dispatch:<能力 id>
  slot，payload={executorId,material,cases,params} → update=完整 params 整替，无面板回落
  schema 表单 ⇄ raw JSON）→ ③ 执行设置；新增 / 二次修改共用，状态收敛 dialog 对象
  （editingIndex=-1 新增），[完成配置] 过 validateEditor 单步口径）。下发动作上移顶部栏
  [下发]（仅已保存计划，body {} 无 overrides；runPrePush 静默 best-effort 结果并入通知条，
  成功通知带 task_id + [查看结果] 跳 /tasks）；[直接下发（不保存）] 组装临时任务（
  _plan_stage_specs 注入镜像 + 幂等键 UI 生成，只用步骤自身数据）；列表 [下发] 确认后
  直接 POST dispatch。触发 / 通知并入「基本信息」（cron 展开 5 段表达式 + 轻量预检，
  深度校验归服务端 InvalidSpec 逐条；event / 非 none 通知标「预留」+ config kv 折叠）。
  步骤 material 必填 + 指纹版本绑定（§3 换脚本 case diff，消失用例红字不静默丢）；台架
  定向（§4）：自动 / 指定台架（executor.nodes）/ 按能力标签（executor.labels + 随机）。
  旧格式步骤（顶层 material_id / 无 cases）载入归一 v2 并提示「请重新确认用例」，保存恒出 v2。
  插件 SFC 自包含：不 import 宿主 / npm 模块，Options API + 全局共享组件（PvDialog/
  PvMenu/PvTextarea 别名规避 HTML 同名标签坑），样式走 scoped css + PrimeVue 语义变量。
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
            <Message v-if="notice" severity="info" :closable="true" @close="clearNotice">
                {{ notice }}
                <Button v-if="noticeAction === 'tasks'" label="查看结果" icon="pi pi-external-link" size="small" severity="secondary" text @click="goTasks" />
            </Message>

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
                <Column header="脚本信息" style="min-width: 12rem">
                    <template #body="{ data }">
                        <span class="plans-page__mono" :title="data.scripts_text">{{ data.scripts_text || '—' }}</span>
                    </template>
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
                            <Button label="下发" icon="pi pi-send" size="small" severity="secondary" text @click="confirmDispatchFromList(data)" />
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
        <!-- v-else-if 守卫：编辑器数据未就绪（异步载入中）时不渲染，避免模板裸访问 editor 崩 render -->
        <template v-else-if="editor">
            <!-- 顶部栏：返回 / 标题 / 保存类操作 -->
            <header class="plans-page__topbar">
                <div class="plans-page__topbar-title">
                    <Button label="返回列表" icon="pi pi-arrow-left" size="small" severity="secondary" outlined @click="goList" />
                    <div>
                        <h2>{{ editorTitle }}</h2>
                        <p class="plans-page__hint">
                            流程：① 选脚本 → ② 勾选用例 → ③ 填参数；步骤自上而下线性执行（Stage 屏障）
                        </p>
                    </div>
                </div>
                <div class="plans-page__actions">
                    <Button label="保存" icon="pi pi-save" size="small" :loading="saving" @click="savePlan" />
                    <Button
                        label="另存为"
                        icon="pi pi-clone"
                        size="small"
                        severity="secondary"
                        outlined
                        :disabled="!editor || !editor.planId"
                        @click="saveAsCopy"
                    />
                    <Button
                        label="下发"
                        icon="pi pi-send"
                        size="small"
                        :disabled="!editor || !editor.planId"
                        :loading="dispatching"
                        title="按已保存计划下发任务（无覆盖参数；保存计划后可用）"
                        @click="dispatchPlan"
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
            </header>

            <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
            <Message v-if="notice" severity="info" :closable="true" @close="clearNotice">
                {{ notice }}
                <Button v-if="noticeAction === 'tasks'" label="查看结果" icon="pi pi-external-link" size="small" severity="secondary" text @click="goTasks" />
            </Message>
            <Message v-if="legacyStepExists" severity="warn" :closable="false">
                旧版计划（步骤缺材料绑定 / 用例快照），已归一为新版结构，请重新确认用例后再保存
            </Message>
            <template v-if="validationErrors.length">
                <Message v-for="(item, index) in validationErrors" :key="index" severity="error" :closable="false">
                    {{ item }}
                </Message>
            </template>

            <!-- ============================== 基本信息 ============================== -->
            <Panel header="基本信息">
                <div class="plans-page__grid">
                    <label class="plans-page__field">
                        <span class="plans-page__label">名称（必填）</span>
                        <InputText v-model="editor.name" placeholder="计划名称" class="plans-page__input" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">默认优先级（大者先出队）</span>
                        <InputNumber v-model="editor.priority" :maxFractionDigits="0" class="plans-page__input" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">失败策略</span>
                        <Select v-model="editor.failurePolicy" :options="failurePolicyOptions" optionLabel="label" optionValue="value" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">默认超时（秒，0 = 不限）</span>
                        <InputNumber v-model="editor.timeoutS" :min="0" class="plans-page__input" />
                    </label>
                </div>
                <label class="plans-page__field">
                    <span class="plans-page__label">说明</span>
                    <PvTextarea v-model="editor.description" rows="3" class="plans-page__textarea" placeholder="计划用途、适用场景……" />
                </label>
                <h4>标签（labels，随下发注入任务）</h4>
                <div v-for="(row, index) in editor.labelsRows" :key="index" class="plans-page__kv">
                    <InputText v-model="row.key" placeholder="键" />
                    <InputText v-model="row.value" placeholder="值" />
                    <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(editor.labelsRows, index)" />
                </div>
                <Button label="添加标签" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(editor.labelsRows)" />

                <!-- 触发 / 通知（v2.1 §7.1.2：并入基本信息，两行紧凑排布） -->
                <h4>触发方式 / 结果通知</h4>
                <div class="plans-page__grid">
                    <label class="plans-page__field">
                        <span class="plans-page__label">触发方式</span>
                        <span class="plans-page__inline">
                            <Select v-model="editor.triggerType" :options="triggerTypeOptions" optionLabel="label" optionValue="value" />
                            <Tag v-if="editor.triggerType === 'event'" value="预留" severity="warn" />
                        </span>
                    </label>
                    <label v-if="editor.triggerType === 'cron'" class="plans-page__field">
                        <span class="plans-page__label">cron 表达式</span>
                        <InputText v-model="editor.triggerExpr" placeholder="5 段：分 时 日 月 周，如 */10 * * * *" class="plans-page__input" />
                    </label>
                    <label class="plans-page__field">
                        <span class="plans-page__label">结果通知</span>
                        <span class="plans-page__inline">
                            <Select v-model="editor.notifyType" :options="notifyTypeOptions" optionLabel="label" optionValue="value" />
                            <Tag v-if="editor.notifyType !== 'none'" value="预留" severity="warn" />
                        </span>
                    </label>
                </div>
                <p v-if="editor.triggerType === 'cron'" class="plans-page__hint">
                    标准 5 段 cron（分 时 日 月 周；支持 *、*/n、a、a-b、a-b/n、逗号列表）；触发循环约 15s
                    扫描一次，错过即跳过。前端仅做 5 段轻量预检，深度校验由服务端执行（保存时 InvalidSpec 逐条展示）。
                </p>
                <Button
                    v-if="editor.triggerType === 'event'"
                    :label="editor.triggerConfigOpen ? '收起触发配置（config）' : '展开触发配置（config）'"
                    :icon="editor.triggerConfigOpen ? 'pi pi-chevron-up' : 'pi pi-chevron-down'"
                    size="small"
                    severity="secondary"
                    text
                    @click="editor.triggerConfigOpen = !editor.triggerConfigOpen"
                />
                <template v-if="editor.triggerType === 'event' && editor.triggerConfigOpen">
                    <div v-for="(row, index) in editor.triggerConfigRows" :key="index" class="plans-page__kv">
                        <InputText v-model="row.key" placeholder="键" />
                        <InputText v-model="row.value" placeholder="值" />
                        <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(editor.triggerConfigRows, index)" />
                    </div>
                    <Button label="添加配置项" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(editor.triggerConfigRows)" />
                    <p class="plans-page__hint">event 为预留接口（照存照显）；当前仅 manual / cron 实际生效。</p>
                </template>
                <Button
                    :label="editor.notifyConfigOpen ? '收起通知配置（config）' : '展开通知配置（config）'"
                    :icon="editor.notifyConfigOpen ? 'pi pi-chevron-up' : 'pi pi-chevron-down'"
                    size="small"
                    severity="secondary"
                    text
                    @click="editor.notifyConfigOpen = !editor.notifyConfigOpen"
                />
                <template v-if="editor.notifyConfigOpen">
                    <div v-for="(row, index) in editor.notifyConfigRows" :key="index" class="plans-page__kv">
                        <InputText v-model="row.key" placeholder="键" />
                        <InputText v-model="row.value" placeholder="值" />
                        <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(editor.notifyConfigRows, index)" />
                    </div>
                    <Button label="添加配置项" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(editor.notifyConfigRows)" />
                    <p class="plans-page__hint">当前仅 none 不通知；其余类型照存照显（预留接口，通知插件监听 EVENT_TASK_FINISHED 后读 plan.notify 分发）。</p>
                </template>
            </Panel>

            <!-- ============================== 测试脚本（主体） ============================== -->
            <Panel header="测试脚本（自上而下 = 执行顺序）">
                <template #header>
                    <div class="plans-page__panel-head">
                        <span>测试脚本（自上而下 = 执行顺序）</span>
                        <Button
                            label="添加测试脚本"
                            icon="pi pi-plus"
                            size="small"
                            @mouseenter="showAddMenu"
                            @click="toggleAddMenu"
                        />
                        <PvMenu ref="addMenu" :model="addMenuItems" popup @show="addMenuOpen = true" @hide="addMenuOpen = false" />
                    </div>
                </template>

                <div v-for="(step, index) in editor.steps" :key="step.uid" class="plans-page__card">
                    <!-- 行1：#N / 执行器 / 脚本 v短sha / 用例 Tag / 旧版警告（v2.1 §7.1.4） -->
                    <div class="plans-page__step-line">
                        <div class="plans-page__step-main">
                            <div class="plans-page__step-row">
                                <Tag :value="`#${index + 1}`" severity="secondary" />
                                <Tag :value="step.executorId || '未选执行器'" severity="info" />
                                <span class="plans-page__card-title">
                                    {{ materialTitleOf(step) }}<span v-if="materialShortShaOf(step) !== '—'" class="plans-page__mono"> v{{ materialShortShaOf(step) }}</span>
                                </span>
                                <Tag
                                    :value="step.casesMode === 'selected' ? `指定 ${step.caseIds.length}/${totalCasesOf(step)}` : '全部用例'"
                                    :severity="step.casesMode === 'selected' && !step.caseIds.length ? 'warn' : 'success'"
                                />
                                <Tag v-if="step.legacy" value="旧版" severity="warn" />
                            </div>
                            <!-- 行2：参数摘要 · 台架摘要 · 分片 · 超时 -->
                            <div class="plans-page__step-row plans-page__step-row--summary">
                                <span class="plans-page__mono" :title="paramsDigestOf(step)">{{ clipText(paramsDigestOf(step), 80) }}</span>
                                <span>·</span>
                                <span>{{ targetSummaryOf(step) }}</span>
                                <span>·</span>
                                <span>分片 {{ shardSummaryOf(step) }}</span>
                                <span>·</span>
                                <span>{{ timeoutSummaryOf(step) }}</span>
                            </div>
                        </div>
                        <div class="plans-page__actions">
                            <Button label="编辑" icon="pi pi-pencil" size="small" severity="secondary" text @click="openStepDialogAt(index)" />
                            <Button label="删除" icon="pi pi-trash" size="small" severity="danger" text @click="removeStep(index)" />
                            <Button icon="pi pi-arrow-up" size="small" severity="secondary" text :disabled="index === 0" @click="moveStep(index, -1)" />
                            <Button icon="pi pi-arrow-down" size="small" severity="secondary" text :disabled="index === editor.steps.length - 1" @click="moveStep(index, 1)" />
                        </div>
                    </div>
                </div>

                <div v-if="!editor.steps.length" class="plans-page__muted">暂无步骤，悬浮「添加测试脚本」选择脚本开始</div>
                <Button label="添加测试脚本" icon="pi pi-plus" size="small" severity="secondary" outlined @mouseenter="showAddMenu" @click="toggleAddMenu" />
            </Panel>

            <!-- 配置脚本对话框（v2.1 §7.1.3）：新增 / 二次修改共用，状态收敛在 dialog 对象 -->
            <PvDialog v-model:visible="dialog.open" modal header="配置脚本" :style="{ width: '86vw', maxWidth: '1100px' }" @hide="closeStepDialog">
                <div v-if="dialogStep" class="plans-page__dialog">
                    <template v-if="dialog.errors.length">
                        <Message v-for="(item, index) in dialog.errors" :key="index" severity="error" :closable="false">{{ item }}</Message>
                    </template>

                    <!-- 顶部：脚本（可更换，换脚本走 case diff）+ 执行器（随材料推导，可手改）+ 版本行 -->
                    <div class="plans-page__grid">
                        <label class="plans-page__field">
                            <span class="plans-page__label">脚本（材料，可更换——自动做用例 diff）</span>
                            <Select
                                v-model="dialogStep.materialId"
                                :options="dialogMaterialOptions"
                                optionLabel="label"
                                optionValue="value"
                                placeholder="选择脚本材料"
                                showClear
                                class="plans-page__input"
                                @change="onMaterialChange(dialogStep)"
                            />
                        </label>
                        <label class="plans-page__field">
                            <span class="plans-page__label">执行器（能力信封 ∪ 已知 id，可手输）</span>
                            <input
                                v-model="dialogStep.executorId"
                                list="plans-executor-ids"
                                class="plans-page__native"
                                placeholder="如 pytest"
                                @change="onStepExecutorChange(dialogStep)"
                            />
                        </label>
                    </div>
                    <p class="plans-page__hint">
                        <template v-if="dialogStep.materialId">
                            sha256 <span class="plans-page__mono">{{ materialShortShaOf(dialogStep) }}</span>
                            · 上传 {{ formatTime(materialTimeOf(dialogStep)) }}
                            <Tag v-if="materialBoundOf(dialogStep)" value="已绑定版本" severity="success" class="plans-page__tag" />
                            <Tag v-else value="未绑定指纹" severity="warn" class="plans-page__tag" />
                        </template>
                        <span v-else class="plans-page__muted">尚未选择脚本</span>
                    </p>
                    <Message v-if="dialogStep.legacy" severity="warn" :closable="false">
                        旧版步骤：绑定 / 用例快照缺失，已按「全部用例」归一，请重新确认用例
                    </Message>
                    <Message v-if="dialogStep.caseDiffRemoved.length" severity="error" :closable="true" @close="dialogStep.caseDiffRemoved = []">
                        {{ dialogStep.caseDiffRemoved.length }} 个用例在新脚本中不存在，已从勾选中剔除：
                        <span v-for="cid in dialogStep.caseDiffRemoved" :key="cid" class="plans-page__removed">{{ cid }}</span>
                    </Message>

                    <!-- ① 勾选用例（模式 + 筛选 + 多选表 + 批量操作） -->
                    <div class="plans-page__section">
                        <h5>① 勾选用例</h5>
                        <div class="plans-page__radios">
                            <span class="plans-page__label">用例模式：</span>
                            <div class="plans-page__radio">
                                <RadioButton v-model="dialogStep.casesMode" inputId="plans-dlg-casemode-all" value="all" @change="onCaseModeChange(dialogStep)" />
                                <label for="plans-dlg-casemode-all">全部用例</label>
                            </div>
                            <div class="plans-page__radio">
                                <RadioButton v-model="dialogStep.casesMode" inputId="plans-dlg-casemode-selected" value="selected" @change="onCaseModeChange(dialogStep)" />
                                <label for="plans-dlg-casemode-selected">指定用例</label>
                            </div>
                        </div>

                        <template v-if="dialogStep.casesMode === 'selected'">
                            <!-- 筛选栏 -->
                            <div class="plans-page__filters">
                                <IconField class="plans-page__filter">
                                    <InputIcon class="pi pi-search" />
                                    <InputText v-model="dialogStep.filters.keyword" placeholder="关键字（case_id / 名称）" class="plans-page__input" />
                                </IconField>
                                <Select
                                    v-model="dialogStep.filters.module"
                                    :options="moduleOptionsOf(dialogStep)"
                                    optionLabel="label"
                                    optionValue="value"
                                    placeholder="全部模块"
                                    showClear
                                    class="plans-page__filter"
                                />
                                <MultiSelect
                                    v-model="dialogStep.filters.markers"
                                    :options="markerOptionsOf(dialogStep)"
                                    optionLabel="label"
                                    optionValue="value"
                                    placeholder="全部标签"
                                    showClear
                                    class="plans-page__filter"
                                />
                            </div>

                            <!-- 用例表（多选） -->
                            <DataTable
                                :value="filteredCasesOf(dialogStep)"
                                dataKey="case_id"
                                :loading="caseLoadingOf(dialogStep)"
                                :rowHover="true"
                                scrollable
                                scrollHeight="18rem"
                                size="small"
                            >
                                <Column style="width: 3rem">
                                    <template #header>
                                        <Checkbox
                                            :modelValue="allFilteredCheckedOf(dialogStep)"
                                            binary
                                            :disabled="!filteredCasesOf(dialogStep).length"
                                            @update:modelValue="toggleAllFiltered(dialogStep, $event)"
                                        />
                                    </template>
                                    <template #body="{ data }">
                                        <Checkbox
                                            :modelValue="isCaseChecked(dialogStep, data.case_id)"
                                            binary
                                            @update:modelValue="toggleCase(dialogStep, data.case_id, $event)"
                                        />
                                    </template>
                                </Column>
                                <Column field="case_id" header="case_id" style="min-width: 14rem">
                                    <template #body="{ data }">
                                        <span class="plans-page__mono" :title="data.case_id">{{ data.case_id }}</span>
                                    </template>
                                </Column>
                                <Column field="name" header="名称" style="min-width: 10rem">
                                    <template #body="{ data }">{{ data.name || '—' }}</template>
                                </Column>
                                <Column field="path" header="模块" style="min-width: 8rem">
                                    <template #body="{ data }">{{ data.path || '—' }}</template>
                                </Column>
                                <Column header="标签" style="min-width: 8rem">
                                    <template #body="{ data }">
                                        <template v-if="(data.markers || []).length">
                                            <Tag v-for="marker in data.markers" :key="marker" :value="marker" severity="info" class="plans-page__tag" />
                                        </template>
                                        <span v-else class="plans-page__muted">—</span>
                                    </template>
                                </Column>
                                <template #empty>
                                    <span class="plans-page__muted">{{ caseEmptyTextOf(dialogStep) }}</span>
                                </template>
                            </DataTable>

                            <!-- 底部：已选计数 + 批量操作 -->
                            <div class="plans-page__case-foot">
                                <span class="plans-page__label">已选 {{ dialogStep.caseIds.length }} / {{ totalCasesOf(dialogStep) }}</span>
                                <Button label="全选筛选结果" icon="pi pi-check-square" size="small" severity="secondary" outlined @click="selectAllFiltered(dialogStep)" />
                                <Button label="清空" icon="pi pi-eraser" size="small" severity="secondary" outlined @click="clearCases(dialogStep)" />
                                <Button label="反选" icon="pi pi-sync" size="small" severity="secondary" outlined @click="invertCases(dialogStep)" />
                            </div>
                            <p v-if="!dialogStep.caseIds.length" class="plans-page__error-text">指定用例模式下至少勾选 1 条用例</p>
                        </template>
                        <p v-else class="plans-page__hint">跟随脚本全部用例（执行端按脚本清单全量执行，脚本更新自动生效）</p>
                    </div>

                    <!-- ② 任务参数：执行器「下发任务 UI」slot，无面板回落 schema 表单 -->
                    <div class="plans-page__section">
                        <h5>② 任务参数</h5>
                        <p v-if="!dialogStep.executorId" class="plans-page__hint">请先选择脚本 / 输入执行器 id。</p>
                        <PluginUiOutlet
                            v-else
                            :name="`plans:dispatch:${dialogStep.executorId}`"
                            :payload="dispatchPayload(dialogStep)"
                            @update="onPanelParams(dialogStep, $event)"
                        >
                            <!-- 空槽回落（§6）：清单判定无专属面板 → 通用 schema 表单 -->
                            <div v-if="slotModeOf(dialogStep) !== 'panel'" class="plans-page__fallback">
                                <p class="plans-page__hint">
                                    该执行器暂无「下发任务 UI」面板，使用通用参数表单（按能力信封 detail.schema
                                    动态生成，raw JSON 双向同步、冲突 <b>raw 优先</b>）
                                </p>
                                <p v-if="!schemaReadyOf(dialogStep)" class="plans-page__hint">能力信封 schema 加载中……可先编辑 raw JSON。</p>
                                <template v-else>
                                    <p v-if="!schemaPropsOf(dialogStep).length" class="plans-page__hint">
                                        {{ schemaHintOf(dialogStep) }}
                                    </p>
                                    <div v-for="prop in schemaPropsOf(dialogStep)" :key="prop.key" class="plans-page__field">
                                        <span class="plans-page__label">
                                            {{ prop.key }}<span v-if="prop.title" class="plans-page__muted"> — {{ prop.title }}</span>
                                        </span>
                                        <InputText
                                            v-if="prop.type === 'string'"
                                            v-model="dialogStep.form.values[prop.key]"
                                            class="plans-page__input"
                                            @input="syncStepFormToRaw(dialogStep)"
                                        />
                                        <input
                                            v-else-if="prop.type === 'number'"
                                            v-model.number="dialogStep.form.values[prop.key]"
                                            type="number"
                                            class="plans-page__native"
                                            @input="syncStepFormToRaw(dialogStep)"
                                        />
                                        <select
                                            v-else-if="prop.type === 'boolean'"
                                            v-model="dialogStep.form.values[prop.key]"
                                            class="plans-page__native"
                                            @change="syncStepFormToRaw(dialogStep)"
                                        >
                                            <option :value="null">（未设）</option>
                                            <option :value="true">true</option>
                                            <option :value="false">false</option>
                                        </select>
                                        <select
                                            v-else-if="prop.type === 'enum'"
                                            v-model="dialogStep.form.values[prop.key]"
                                            class="plans-page__native"
                                            @change="syncStepFormToRaw(dialogStep)"
                                        >
                                            <option value="">（未设）</option>
                                            <option v-for="opt in prop.options" :key="String(opt)" :value="opt">{{ opt }}</option>
                                        </select>
                                        <PvTextarea
                                            v-else-if="prop.type === 'array'"
                                            v-model="dialogStep.form.arrayText[prop.key]"
                                            rows="3"
                                            class="plans-page__textarea"
                                            placeholder="每行一条"
                                            @input="syncStepFormToRaw(dialogStep)"
                                        />
                                        <template v-else-if="prop.type === 'object'">
                                            <div v-for="(row, rowIndex) in dialogStep.form.objectRows[prop.key] || []" :key="rowIndex" class="plans-page__kv">
                                                <InputText v-model="row.key" placeholder="键" @input="syncStepFormToRaw(dialogStep)" />
                                                <InputText v-model="row.value" placeholder="值" @input="syncStepFormToRaw(dialogStep)" />
                                                <Button
                                                    icon="pi pi-times"
                                                    size="small"
                                                    severity="danger"
                                                    text
                                                    @click="removeKv(dialogStep.form.objectRows[prop.key], rowIndex); syncStepFormToRaw(dialogStep)"
                                                />
                                            </div>
                                            <Button
                                                label="添加"
                                                icon="pi pi-plus"
                                                size="small"
                                                severity="secondary"
                                                text
                                                @click="addKv(dialogStep.form.objectRows[prop.key]); syncStepFormToRaw(dialogStep)"
                                            />
                                        </template>
                                        <p v-else class="plans-page__hint">
                                            未知类型（{{ prop.rawType || '—' }}）：请用下方 raw JSON 编辑此参数。
                                        </p>
                                    </div>
                                </template>
                                <h6>raw JSON（params 兜底 ⇄ 表单双向同步，冲突 raw 优先）</h6>
                                <PvTextarea
                                    v-model="dialogStep.paramsRaw"
                                    rows="6"
                                    class="plans-page__textarea plans-page__textarea--tall"
                                    placeholder="{}"
                                    @input="syncStepRawToForm(dialogStep)"
                                />
                                <p v-if="dialogStep.paramsRawError" class="plans-page__error-text">{{ dialogStep.paramsRawError }}</p>
                            </div>
                        </PluginUiOutlet>
                    </div>

                    <!-- ③ 执行设置 -->
                    <div class="plans-page__section">
                        <h5>③ 执行设置</h5>
                        <div class="plans-page__grid">
                            <label class="plans-page__field">
                                <span class="plans-page__label">步骤名（必填，全计划唯一）</span>
                                <InputText v-model="dialogStep.name" placeholder="如：功能回归" class="plans-page__input" />
                            </label>
                            <label class="plans-page__field">
                                <span class="plans-page__label">分片模式</span>
                                <Select v-model="dialogStep.shardMode" :options="shardModeOptions" optionLabel="label" optionValue="value" />
                            </label>
                            <label class="plans-page__field">
                                <span class="plans-page__label">分片数 hint（0 = 执行器自定）</span>
                                <InputNumber
                                    v-model="dialogStep.shardHint"
                                    :min="0"
                                    :maxFractionDigits="0"
                                    :disabled="dialogStep.shardMode !== 'plan'"
                                    class="plans-page__input"
                                />
                            </label>
                            <label class="plans-page__field">
                                <span class="plans-page__label">步骤超时（秒，0 = 不限）</span>
                                <InputNumber v-model="dialogStep.timeoutS" :min="0" class="plans-page__input" />
                            </label>
                        </div>

                        <h6>台架定向</h6>
                        <div class="plans-page__radios">
                            <div v-for="opt in targetModeOptions" :key="opt.value" class="plans-page__radio">
                                <RadioButton
                                    v-model="dialogStep.targetMode"
                                    :inputId="'plans-dlg-target-' + opt.value"
                                    :value="opt.value"
                                    @change="onTargetModeChange(dialogStep)"
                                />
                                <label :for="'plans-dlg-target-' + opt.value">{{ opt.label }}</label>
                            </div>
                        </div>

                        <template v-if="dialogStep.targetMode === 'nodes'">
                            <div class="plans-page__grid">
                                <label class="plans-page__field">
                                    <span class="plans-page__label">指定台架（不限数量，数据源：节点心跳）</span>
                                    <MultiSelect
                                        v-model="dialogStep.nodes"
                                        :options="nodeOptions"
                                        optionLabel="label"
                                        optionValue="value"
                                        placeholder="选择台架"
                                        showClear
                                        class="plans-page__input"
                                    />
                                </label>
                                <label class="plans-page__field">
                                    <span class="plans-page__label">派发策略</span>
                                    <Select v-model="dialogStep.strategy" :options="strategyOptions" optionLabel="label" optionValue="value" />
                                </label>
                            </div>
                        </template>
                        <template v-else-if="dialogStep.targetMode === 'labels'">
                            <h6>能力 / 标签筛选（executor.labels）</h6>
                            <div v-for="(row, rowIndex) in dialogStep.labelRows" :key="rowIndex" class="plans-page__kv">
                                <InputText v-model="row.key" placeholder="键（如 lang）" />
                                <InputText v-model="row.value" placeholder="值（如 python）" />
                                <Button icon="pi pi-times" size="small" severity="danger" text @click="removeKv(dialogStep.labelRows, rowIndex)" />
                            </div>
                            <div class="plans-page__case-foot">
                                <Button label="添加标签" icon="pi pi-plus" size="small" severity="secondary" text @click="addKv(dialogStep.labelRows)" />
                                <label class="plans-page__field plans-page__field--inline">
                                    <span class="plans-page__label">派发策略</span>
                                    <Select v-model="dialogStep.strategy" :options="strategyOptions" optionLabel="label" optionValue="value" />
                                </label>
                            </div>
                        </template>
                        <p v-else class="plans-page__hint">按能力信封自动匹配（strategy=balanced，最少在飞优先）</p>
                    </div>
                </div>
                <template #footer>
                    <div class="plans-page__actions">
                        <Button label="取消" icon="pi pi-times" size="small" severity="secondary" text @click="closeStepDialog" />
                        <Button label="完成配置" icon="pi pi-check" size="small" @click="confirmDialogStep" />
                    </div>
                </template>
            </PvDialog>
        </template>
        <template v-else>
            <!-- 编辑器数据异步就绪中（editor=null 窗口）：给加载态而非裸渲染崩掉 -->
            <p class="plans-page__hint" role="status">正在加载计划…</p>
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

// 动态表单不重复渲染的保留键（核心管理键 material_id/selection_id/targets 属
// material/cases 同名语义，表单排除、校验冲突；env 由执行器下发 UI / raw JSON 管理）
const RESERVED_PARAMS = ['material_id', 'selection_id', 'targets', 'env'];
const SCALAR_TYPES = ['string', 'number', 'boolean', 'enum'];
const MATERIAL_STATE_TEXT = { uploaded: '待分析', analyzing: '分析中', analyzed: '已分析', failed: '失败' };

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

function isPlainObject(value) {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function sameStrList(left, right) {
    // 同名冲突比较（服务端 _same_str_list 同口径）：同为字符串数组且同集（顺序无关）
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    const a = left.map((item) => String(item)).sort();
    const b = right.map((item) => String(item)).sort();
    return a.length === b.length && a.every((item, index) => item === b[index]);
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
            stepUid: 0,
            // 能力信封 / 材料 / 用例 / 台架 / slot 清单
            executors: [],
            schemas: {},
            capabilitiesFailed: false,
            materialsByExecutor: {},
            casesByMaterial: {},
            nodes: [],
            dispatchSlotIds: [],
            uiManifestLoaded: false,
            // 配置脚本对话框（v2.1 §7.1.3）：新增 / 二次修改共用；editingIndex=-1 为新增
            dialog: {
                open: false,
                editingIndex: -1,
                step: null,
                errors: []
            },
            addMenuOpen: false,
            dispatching: false,
            noticeAction: '',
            failurePolicyOptions: [
                { label: 'fail_fast（任一 Job 失败即停）', value: 'fail_fast' },
                { label: 'continue（跑完全部 Job）', value: 'continue' }
            ],
            shardModeOptions: [
                { label: 'none（不分片）', value: 'none' },
                { label: 'plan（执行器规划分片）', value: 'plan' }
            ],
            strategyOptions: [
                { label: 'balanced（最少在飞优先）', value: 'balanced' },
                { label: 'random（匹配集合内随机）', value: 'random' }
            ],
            targetModeOptions: [
                { label: '自动', value: 'auto' },
                { label: '指定台架', value: 'nodes' },
                { label: '按能力标签', value: 'labels' }
            ],
            triggerTypeOptions: [
                { label: 'manual（手动）', value: 'manual' },
                { label: 'cron（定时）', value: 'cron' },
                { label: 'event（事件）', value: 'event' }
            ],
            notifyTypeOptions: [
                { label: 'none（不通知）', value: 'none' },
                { label: 'jira', value: 'jira' },
                { label: 'webhook', value: 'webhook' },
                { label: 'email', value: 'email' }
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
        legacyStepExists() {
            return Boolean(this.editor && this.editor.steps.some((step) => step.legacy));
        },
        nodeOptions() {
            return (this.nodes || []).map((node) => {
                const id = String((node && node.node_id) || '');
                const status = String((node && node.status) || '—');
                return { value: id, label: `${id} · ${status}` };
            });
        },
        dialogStep() {
            return this.dialog ? this.dialog.step : null;
        },
        allMaterialRows() {
            // 「添加测试脚本」菜单 / 对话框脚本 Select 共用全量材料源（无 executor 过滤）
            const entry = this.materialsByExecutor[''];
            return entry && entry.rows ? entry.rows : [];
        },
        addMenuItems() {
            // 悬浮菜单项 = 脚本名称（name · v短sha；多版本同名并列，label 加上传时间区分）
            const rows = this.allMaterialRows
                .slice()
                .sort((left, right) => String(left.name || '').localeCompare(String(right.name || '')));
            if (!rows.length) return [{ label: '暂无脚本，请先到脚本管理上传', disabled: true }];
            const nameCount = {};
            for (const row of rows) {
                const name = String(row.name || this.materialIdOfRow(row));
                nameCount[name] = (nameCount[name] || 0) + 1;
            }
            return rows.map((row) => {
                const id = this.materialIdOfRow(row);
                const name = String(row.name || id);
                const base = `${name} · v${this.shortSha8(row.sha256)}`;
                return {
                    label: nameCount[name] > 1 ? `${base} · ${this.formatTime(row.updated_ts)}` : base,
                    icon: 'pi pi-file',
                    command: () => this.openStepDialogForMaterial(id)
                };
            });
        },
        dialogMaterialOptions() {
            // 对话框脚本 Select：全量材料（名称 · 版本 · 执行器 · 用例数 · 状态）
            const options = [];
            for (const row of this.allMaterialRows) {
                const id = this.materialIdOfRow(row);
                if (!id) continue;
                const caseEntry = this.casesByMaterial[id];
                const count = caseEntry && caseEntry.loaded && !caseEntry.error ? `${caseEntry.rows.length} 用例` : '用例 …';
                options.push({
                    value: id,
                    label: `${row.name || id} · ${this.shortSha8(row.sha256)} · ${row.executor_id || '通用'} · ${count} · ${this.stateText(row.state)}`
                });
            }
            const step = this.dialog && this.dialog.step;
            if (step && step.materialId && !options.some((option) => option.value === step.materialId)) {
                options.push({ value: step.materialId, label: `${step.materialId}（不在当前列表）` });
            }
            return options;
        },
        rows() {
            return this.plans.map((plan) => {
                const steps = Array.isArray(plan.steps) ? plan.steps : [];
                const executors = [];
                const scripts = [];
                for (const step of steps) {
                    const executor = isPlainObject(step && step.executor) ? step.executor : {};
                    const id = String(executor.id || '');
                    if (id && !executors.includes(id)) executors.push(id);
                    scripts.push(this.scriptTextOf(step));
                }
                return {
                    ...plan,
                    plan_id: String(plan.plan_id || plan.id || ''),
                    step_count: steps.length,
                    executors_text: executors.join(' + '),
                    scripts_text: scripts.join('，'),
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
        this.loadNodes();
        this.loadUiManifest();
        this.ensureMaterials(''); // 「添加测试脚本」菜单 / 对话框脚本 Select 全量材料源
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
        confirmDispatchFromList(plan) {
            // 列表「下发」（v2.1 §7.1.1）：确认后直接 POST dispatch（body {}，无 overrides），不再跳编辑器
            const planId = this.planIdOf(plan);
            if (!planId) return;
            this.$confirm.require({
                header: '下发计划',
                message: `确认下发计划「${plan.name || planId}」？将按已保存内容实例化为任务（labels 自动注入 plan_id / plan_name）。`,
                icon: 'pi pi-send',
                acceptLabel: '下发',
                rejectLabel: '取消',
                accept: async () => {
                    try {
                        const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}/dispatch`), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: '{}'
                        });
                        this.notice = `任务已下发 task_id=${data.task_id}${data.deduplicated ? '（幂等去重，复用既有任务）' : ''}`;
                        this.noticeAction = 'tasks';
                        this.error = '';
                    } catch (reason) {
                        this.error = `下发失败：${this.reasonText(reason)}`;
                    }
                }
            });
        },
        goList() {
            this.navigateEdit('');
        },
        closeEditorState() {
            this.editor = null;
            this.validationErrors = [];
        },
        // ================================ 列表 =================================
        async loadPlans(silent) {
            if (!silent) this.loadingPlans = true;
            try {
                const data = await fetchJson(apiUrl('/api/test/plans?limit=200'));
                this.plans = data.plans || [];
                this.error = '';
            } catch (reason) {
                this.error = this.reasonText(reason);
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
                        this.noticeAction = '';
                        this.error = '';
                        await this.loadPlans(true);
                    } catch (reason) {
                        // 409 / 404 等错误明示，不静默
                        this.error = `删除失败：${this.reasonText(reason)}`;
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
                this.noticeAction = '';
                this.error = '';
                await this.loadPlans(true);
            } catch (reason) {
                this.error = `复制失败：${this.reasonText(reason)}`;
            }
        },
        // ================================ 编辑器 =================================
        makeStep(raw, fromSaved) {
            const step = isPlainObject(raw) ? raw : {};
            const executor = isPlainObject(step.executor) ? step.executor : {};
            const shard = isPlainObject(step.shard) ? step.shard : {};
            const params = isPlainObject(step.params) ? step.params : {};
            const materialRaw = isPlainObject(step.material) ? step.material : null;
            const topMaterialId = typeof step.material_id === 'string' ? step.material_id : '';
            const materialId = materialRaw ? String(materialRaw.material_id || '') : topMaterialId;
            const casesRaw = isPlainObject(step.cases) ? step.cases : null;
            const casesMode = casesRaw && casesRaw.mode === 'selected' ? 'selected' : 'all';
            const caseIds = casesRaw && Array.isArray(casesRaw.case_ids) ? casesRaw.case_ids.map((id) => String(id)) : [];
            const nodes = Array.isArray(executor.nodes) ? executor.nodes.map((id) => String(id)) : [];
            const strategy = executor.strategy === 'random' ? 'random' : 'balanced';
            const executorLabels = isPlainObject(executor.labels) ? executor.labels : {};
            const materialMeta = materialRaw
                ? {
                      material_id: materialId,
                      sha256: String(materialRaw.sha256 || ''),
                      recipe_fingerprint: String(materialRaw.recipe_fingerprint || ''),
                      name: String(materialRaw.name || '')
                  }
                : topMaterialId
                  ? { material_id: topMaterialId, sha256: '', recipe_fingerprint: '', name: '' }
                  : null;
            this.stepUid += 1;
            return {
                uid: this.stepUid,
                name: String(step.name || ''),
                executorId: String(executor.id || ''),
                executorKind: String(executor.kind || 'executor'),
                labelRows: this.kvRowsOf(executorLabels),
                nodes,
                strategy,
                targetMode: nodes.length ? 'nodes' : Object.keys(executorLabels).length ? 'labels' : 'auto',
                materialId,
                materialMeta,
                // 旧格式（顶层 material_id / 无 cases）归一 v2 后显式提示（§7 兼容）
                legacy: Boolean(fromSaved) && (!materialRaw || !casesRaw),
                casesMode,
                caseIds,
                caseDiffRemoved: [],
                filters: { keyword: '', module: '', markers: [] },
                requires: isPlainObject(step.requires) ? { ...step.requires } : {},
                paramsRaw: JSON.stringify(params, null, 2),
                paramsRawError: '',
                form: { values: {}, arrayText: {}, objectRows: {} },
                shardMode: String(shard.mode || 'none') === 'plan' ? 'plan' : 'none',
                shardHint: Number(shard.hint) || 0,
                timeoutS: Number(step.timeout_s) || 0
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
                triggerType: 'manual',
                triggerExpr: '',
                triggerConfigRows: [],
                triggerConfigOpen: false,
                notifyType: 'none',
                notifyConfigRows: [],
                notifyConfigOpen: false,
                steps: []
            };
            this.validationErrors = [];
            this.error = '';
        },
        async openExistingEditor(planId) {
            try {
                const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}`));
                this.applyPlanToEditor(data.plan || {});
                this.error = '';
            } catch (reason) {
                this.error = this.reasonText(reason);
                this.navigateEdit('', true);
            }
        },
        applyPlanToEditor(plan) {
            const defaults = isPlainObject(plan.defaults) ? plan.defaults : {};
            const trigger = isPlainObject(plan.trigger) ? plan.trigger : {};
            const notify = isPlainObject(plan.notify) ? plan.notify : {};
            const steps = Array.isArray(plan.steps) ? plan.steps : [];
            const triggerType = String(trigger.type || 'manual');
            const notifyType = String(notify.type || 'none');
            const triggerConfig = isPlainObject(trigger.config) ? trigger.config : {};
            this.editor = {
                planId: String(plan.plan_id || plan.id || ''),
                name: String(plan.name || ''),
                description: String(plan.description || ''),
                labelsRows: this.kvRowsOf(plan.labels),
                priority: Number(defaults.priority) || 0,
                failurePolicy: String(defaults.failure_policy || 'fail_fast'),
                timeoutS: Number(defaults.timeout_s) || 0,
                triggerType: this.triggerTypeOptions.some((option) => option.value === triggerType) ? triggerType : 'manual',
                triggerExpr: String(triggerConfig.expr || ''),
                triggerConfigRows: this.kvRowsOf(triggerConfig),
                triggerConfigOpen: false,
                notifyType: this.notifyTypeOptions.some((option) => option.value === notifyType) ? notifyType : 'none',
                notifyConfigRows: this.kvRowsOf(notify.config),
                notifyConfigOpen: false,
                steps: steps.map((step) => this.makeStep(step, true))
            };
            this.validationErrors = [];
            for (const step of this.editor.steps) {
                this.ensureSchema(step);
                this.ensureMaterials(step);
                if (step.materialId) this.ensureCases(step.materialId);
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
            if (!isPlainObject(obj)) return [];
            return Object.entries(obj).map(([key, value]) => ({ key, value: String(value ?? '') }));
        },
        // ================ 添加测试脚本悬浮菜单 / 配置脚本对话框（v2.1 §7.1.3） ================
        showAddMenu(event) {
            // 悬浮即弹出脚本菜单（PrimeVue Menu popup 仅暴露 toggle，沿 ScriptsPage 上传菜单交互）
            if (this.addMenuOpen) return;
            this.$refs.addMenu && this.$refs.addMenu.toggle(event);
            this.addMenuOpen = true;
        },
        toggleAddMenu(event) {
            this.$refs.addMenu && this.$refs.addMenu.toggle(event);
            this.addMenuOpen = !this.addMenuOpen;
        },
        async openStepDialogForMaterial(materialId) {
            // 菜单项点击：新建步骤并预选该材料（指定用例 + 默认全选），打开「配置脚本」对话框
            const id = String(materialId || '').trim();
            if (!id) return;
            await this.ensureMaterials('');
            const step = this.makeStep({ name: `步骤 ${this.editor.steps.length + 1}` }, false);
            step.materialId = id;
            step.casesMode = 'selected';
            await this.onMaterialChange(step);
            this.ensureSchema(step);
            this.dialog = { open: true, editingIndex: -1, step, errors: [] };
        },
        openStepDialogAt(index) {
            // [编辑]：载入该步骤现状（spec 往返深拷贝），二次修改后整步写回
            const source = this.editor.steps[index];
            if (!source) return;
            const step = this.makeStep(this.stepToSpec(source), false);
            step.legacy = Boolean(source.legacy);
            this.ensureSchema(step);
            this.ensureMaterials(step);
            if (step.materialId) this.ensureCases(step.materialId);
            this.dialog = { open: true, editingIndex: index, step, errors: [] };
        },
        closeStepDialog() {
            this.dialog = { open: false, editingIndex: -1, step: null, errors: [] };
        },
        confirmDialogStep() {
            // [完成配置]：过 validateEditor 单步口径校验后追加 / 更新步骤并关对话框
            const step = this.dialog.step;
            const editingIndex = this.dialog.editingIndex;
            if (!step) return;
            const names = new Set();
            this.editor.steps.forEach((item, index) => {
                if (index === editingIndex) return;
                const name = String(item.name || '').trim();
                if (name) names.add(name);
            });
            const where = editingIndex >= 0 ? `steps[${editingIndex}]` : '新步骤';
            const problems = this.validateStepProblems(step, where, names);
            if (problems.length) {
                this.dialog.errors = problems;
                return;
            }
            if (editingIndex >= 0) this.editor.steps.splice(editingIndex, 1, step);
            else this.editor.steps.push(step);
            this.closeStepDialog();
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
            // 对话框内材料主导，执行器手改仅影响面板挂载 / schema 回落，不丢已选材料与用例
            this.ensureSchema(step);
            this.ensureMaterials(step);
            this.syncStepRawToForm(step); // raw 优先：按新 schema 回填表单
        },
        // ======================= 能力信封 / 材料 / 用例 / slot 清单 =======================
        async loadCapabilities() {
            // 能力信封：executor 条目 → 执行器候选 + detail.schema（回落表单用）
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
                this.notice = `能力信封加载失败（${this.reasonText(reason)}），执行器可手输 id、参数走 raw JSON。`;
                this.noticeAction = '';
            }
        },
        async loadUiManifest() {
            // UI 清单（§6 回落判定）：plans:dispatch:<能力 id> 槽位存在 → 挂执行器面板，
            // 不存在 → 通用 schema 表单；清单不可用时走 PluginUiOutlet 空槽 fallback 兜底
            try {
                const manifest = await fetchJson(apiUrl('/api/web/ui'));
                this.dispatchSlotIds = (Array.isArray(manifest) ? manifest : [])
                    .filter((item) => item && item.kind === 'slot' && String(item.target || '').startsWith('plans:dispatch:'))
                    .map((item) => String(item.target).slice('plans:dispatch:'.length))
                    .filter(Boolean);
                this.uiManifestLoaded = true;
            } catch {
                this.dispatchSlotIds = [];
                this.uiManifestLoaded = false;
            }
        },
        async loadNodes() {
            // 台架多选数据源（§4：指定台架）
            try {
                const data = await fetchJson(apiUrl('/api/heartbeat/nodes'));
                this.nodes = Array.isArray(data.nodes) ? data.nodes : [];
            } catch {
                this.nodes = [];
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
            // 脚本下拉数据源：executor 过滤谓词 ==X OR =''（通用材料恒可见）；空执行器列全部
            const key = String(step.executorId || '').trim();
            const cached = this.materialsByExecutor[key];
            if (cached && (cached.loading || cached.done)) return cached.promise;
            const entry = { loading: true, done: false, rows: [], error: '', promise: null };
            this.materialsByExecutor[key] = entry;
            entry.promise = (async () => {
                // 经响应式代理改（裸 entry 直改不触发视图更新——Vue3 响应式陷阱）
                const slot = this.materialsByExecutor[key] || entry;
                try {
                    const query = new URLSearchParams({ limit: '200' });
                    if (key) query.set('executor', key);
                    const data = await fetchJson(apiUrl(`/api/test/materials?${query.toString()}`));
                    slot.rows = Array.isArray(data.materials) ? data.materials : [];
                    this.prefetchCaseCounts(slot.rows);
                } catch (reason) {
                    slot.error = this.reasonText(reason);
                } finally {
                    slot.loading = false;
                    slot.done = true;
                }
            })();
            return entry.promise;
        },
        prefetchCaseCounts(rows) {
            // 用例数进选项标签：后台预取前 N 个材料的用例清单（缓存复用，换脚本 diff 即时）
            for (const row of (rows || []).slice(0, 20)) {
                const id = this.materialIdOfRow(row);
                if (id) this.ensureCases(id);
            }
        },
        async ensureCases(materialId) {
            const id = String(materialId || '').trim();
            if (!id) return;
            const cached = this.casesByMaterial[id];
            if (cached && (cached.loading || cached.loaded)) return cached.promise;
            const entry = { loading: true, loaded: false, rows: [], error: '', promise: null };
            this.casesByMaterial[id] = entry;
            entry.promise = (async () => {
                // 经响应式代理改（裸 entry 直改不触发视图更新——Vue3 响应式陷阱）
                const slot = this.casesByMaterial[id] || entry;
                try {
                    const data = await fetchJson(apiUrl(`/api/test/materials/${encodeURIComponent(id)}/cases`));
                    slot.rows = (Array.isArray(data.cases) ? data.cases : []).map((item) => ({
                        case_id: String((item && item.case_id) || ''),
                        name: String((item && item.name) || ''),
                        path: String((item && item.path) || ''),
                        markers: Array.isArray(item && item.markers) ? item.markers.map((m) => String(m)) : [],
                        status: String((item && item.status) || '')
                    }));
                    slot.error = '';
                } catch (reason) {
                    slot.error = this.reasonText(reason);
                } finally {
                    slot.loading = false;
                    slot.loaded = true;
                }
            })();
            return entry.promise;
        },
        materialsOf(step) {
            const key = String(step.executorId || '').trim();
            const entry = this.materialsByExecutor[key];
            return entry && entry.rows ? entry.rows : [];
        },
        materialIdOfRow(row) {
            return String((row && (row.material_id || row.id)) || '');
        },
        materialRowOf(step) {
            const id = String(step.materialId || '').trim();
            if (!id) return null;
            for (const row of this.materialsOf(step)) {
                if (this.materialIdOfRow(row) === id) return row;
            }
            for (const key of Object.keys(this.materialsByExecutor)) {
                const entry = this.materialsByExecutor[key];
                for (const row of (entry && entry.rows) || []) {
                    if (this.materialIdOfRow(row) === id) return row;
                }
            }
            return null;
        },
        async onMaterialChange(step) {
            // 更换脚本（§3）：case diff——交集保持勾选、消失项红字列出并剔除
            const oldIds = step.caseIds.slice();
            const row = this.materialRowOf(step);
            if (row) {
                step.materialMeta = {
                    material_id: this.materialIdOfRow(row),
                    sha256: String(row.sha256 || ''),
                    recipe_fingerprint: String(row.recipe_fingerprint || ''),
                    name: String(row.name || '')
                };
                // executor 自动推导（随材料，可手改）
                const owner = String(row.executor_id || '');
                if (owner) {
                    step.executorId = owner;
                    this.ensureSchema(step);
                    this.ensureMaterials(step);
                }
            } else if (step.materialId) {
                step.materialMeta = { material_id: String(step.materialId), sha256: '', recipe_fingerprint: '', name: '' };
            } else {
                step.materialMeta = null;
            }
            await this.ensureCases(step.materialId);
            const rows = this.casesOf(step);
            const idSet = new Set(rows.map((item) => item.case_id));
            const kept = oldIds.filter((id) => idSet.has(id));
            const removed = oldIds.filter((id) => !idSet.has(id));
            step.caseDiffRemoved = removed;
            if (step.casesMode === 'selected') {
                step.caseIds = oldIds.length ? kept : rows.map((item) => item.case_id);
            } else {
                step.caseIds = rows.map((item) => item.case_id);
            }
            step.filters = { keyword: '', module: '', markers: [] };
            if (step.materialId) step.legacy = false;
            if (removed.length) {
                this.notice = `${removed.length} 个用例在新脚本中不存在，已从勾选中剔除（红字列出）`;
                this.noticeAction = '';
            }
        },
        // ============================ 用例筛选 / 勾选 ============================
        casesOf(step) {
            const entry = this.casesByMaterial[String(step.materialId || '').trim()];
            return entry && entry.rows ? entry.rows : [];
        },
        caseLoadingOf(step) {
            const entry = this.casesByMaterial[String(step.materialId || '').trim()];
            return Boolean(entry && entry.loading);
        },
        caseEmptyTextOf(step) {
            const entry = this.casesByMaterial[String(step.materialId || '').trim()];
            if (!step.materialId) return '请先选择脚本';
            if (entry && entry.loading) return '用例清单加载中……';
            if (entry && entry.error) return `用例清单加载失败：${entry.error}`;
            if (!this.casesOf(step).length) return '该脚本尚无用例清单（分析未产出）';
            return '无匹配用例（试试放宽筛选）';
        },
        filteredCasesOf(step) {
            const rows = this.casesOf(step);
            const filters = step.filters || {};
            const keyword = String(filters.keyword || '').trim().toLowerCase();
            const module = String(filters.module || '');
            const markers = Array.isArray(filters.markers) ? filters.markers : [];
            return rows.filter((item) => {
                if (
                    keyword &&
                    !String(item.case_id || '').toLowerCase().includes(keyword) &&
                    !String(item.name || '').toLowerCase().includes(keyword)
                ) {
                    return false;
                }
                if (module && String(item.path || '') !== module) return false;
                if (markers.length && !markers.some((marker) => item.markers.includes(String(marker)))) return false;
                return true;
            });
        },
        moduleOptionsOf(step) {
            // 模块 = path 去重集合
            const set = new Set();
            for (const item of this.casesOf(step)) {
                const path = String(item.path || '');
                if (path) set.add(path);
            }
            return [...set].sort().map((path) => ({ label: path, value: path }));
        },
        markerOptionsOf(step) {
            // 标签 = markers 并集
            const set = new Set();
            for (const item of this.casesOf(step)) {
                for (const marker of item.markers) set.add(String(marker));
            }
            return [...set].sort().map((marker) => ({ label: marker, value: marker }));
        },
        isCaseChecked(step, caseId) {
            return step.caseIds.includes(String(caseId));
        },
        toggleCase(step, caseId, on) {
            const id = String(caseId);
            const set = new Set(step.caseIds);
            if (on) set.add(id);
            else set.delete(id);
            step.caseIds = [...set];
            step.legacy = false;
        },
        toggleAllFiltered(step, on) {
            const set = new Set(step.caseIds);
            for (const item of this.filteredCasesOf(step)) {
                if (on) set.add(item.case_id);
                else set.delete(item.case_id);
            }
            step.caseIds = [...set];
            step.legacy = false;
        },
        allFilteredCheckedOf(step) {
            const rows = this.filteredCasesOf(step);
            if (!rows.length) return false;
            return rows.every((item) => step.caseIds.includes(item.case_id));
        },
        selectAllFiltered(step) {
            const set = new Set(step.caseIds);
            for (const item of this.filteredCasesOf(step)) set.add(item.case_id);
            step.caseIds = [...set];
            step.legacy = false;
        },
        clearCases(step) {
            step.caseIds = [];
            step.legacy = false;
        },
        invertCases(step) {
            const selected = new Set(step.caseIds);
            step.caseIds = this.casesOf(step)
                .map((item) => item.case_id)
                .filter((id) => !selected.has(id));
            step.legacy = false;
        },
        totalCasesOf(step) {
            return this.casesOf(step).length;
        },
        onCaseModeChange(step) {
            // 默认「指定用例且全选」：切到指定用例时空选集自动全选
            if (step.casesMode === 'selected' && !step.caseIds.length) {
                step.caseIds = this.casesOf(step).map((item) => item.case_id);
            }
            step.legacy = false;
        },
        onTargetModeChange(step) {
            if (step.targetMode === 'labels') step.strategy = 'random';
            else if (step.targetMode === 'nodes' && step.strategy !== 'random') step.strategy = 'balanced';
        },
        // ======================= 参数：slot 面板 ⇄ schema 表单 =======================
        slotModeOf(step) {
            // §6 回落判定：清单已加载按注册情况分流；清单不可用 → hybrid（挂面板 + 空槽兜底表单）
            const id = String(step.executorId || '').trim();
            if (!id) return 'form';
            if (!this.uiManifestLoaded) return 'hybrid';
            return this.dispatchSlotIds.includes(id) ? 'panel' : 'form';
        },
        dispatchPayload(step) {
            // payload（宿主 → 面板，设计 §6）：{ executorId, material, cases, params }
            return {
                executorId: String(step.executorId || ''),
                material: this.materialMetaOf(step),
                cases: { mode: step.casesMode, case_ids: step.caseIds.slice() },
                params: this.finalStepParams(step)
            };
        },
        onPanelParams(step, params) {
            // update（面板 → 宿主）：完整 params 对象整替（核心管理键不产出，raw 镜像同步）
            const obj = isPlainObject(params) ? params : {};
            step.paramsRaw = JSON.stringify(obj, null, 2);
            step.paramsRawError = '';
        },
        materialMetaOf(step) {
            const id = String(step.materialId || '').trim();
            if (!id) return null;
            const meta = isPlainObject(step.materialMeta) ? step.materialMeta : {};
            return {
                material_id: id,
                name: String(meta.name || ''),
                sha256: String(meta.sha256 || ''),
                recipe_fingerprint: String(meta.recipe_fingerprint || '')
            };
        },
        materialTitleOf(step) {
            const meta = isPlainObject(step.materialMeta) ? step.materialMeta : {};
            return String(meta.name || step.materialId || '未选脚本');
        },
        materialShortShaOf(step) {
            const meta = isPlainObject(step.materialMeta) ? step.materialMeta : {};
            return meta.sha256 ? String(meta.sha256).slice(0, 8) : '—';
        },
        materialBoundOf(step) {
            const meta = isPlainObject(step.materialMeta) ? step.materialMeta : {};
            return Boolean(meta.sha256 && meta.recipe_fingerprint);
        },
        materialTimeOf(step) {
            const row = this.materialRowOf(step);
            return row ? row.updated_ts : 0;
        },
        // ====================== schema 动态表单（回落路径，raw 优先） ======================
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
                    const raw = isPlainObject(def) ? def : {};
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
                if (!isPlainObject(parsed)) return null;
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
            if (!isPlainObject(parsed)) {
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
                    const source = isPlainObject(value) ? value : {};
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
        // ==================== 步骤摘要（v2.1 §7.1.4 两行摘要） / 通知条辅助 ====================
        paramsDigestOf(step) {
            // 参数键值 digest：递归点路径展平（数组逗号串），纯插值渲染防 XSS，截断见 clipText
            const params = this.finalStepParams(step);
            const parts = [];
            const walk = (value, path) => {
                if (isPlainObject(value)) {
                    for (const [key, item] of Object.entries(value)) walk(item, path ? `${path}.${key}` : key);
                } else if (Array.isArray(value)) {
                    parts.push(`${path}=${value.map((item) => String(item)).join(',')}`);
                } else if (value !== undefined && value !== null && value !== '') {
                    parts.push(`${path}=${String(value)}`);
                }
            };
            for (const [key, value] of Object.entries(params)) walk(value, key);
            return parts.join(' · ') || '参数 —';
        },
        clipText(text, max) {
            const value = String(text || '');
            return value.length > max ? `${value.slice(0, max)}…` : value;
        },
        targetSummaryOf(step) {
            // 台架摘要：自动 / 指定台架 a,b×N（strategy） / 按标签 k=v（strategy）
            if (step.targetMode === 'nodes') {
                const names = step.nodes.map((node) => String(node || '')).filter(Boolean);
                return `台架 ${names.join(',')}×${names.length}（${step.strategy}）`;
            }
            if (step.targetMode === 'labels') {
                const kv = this.collectKv(step.labelRows);
                const text = Object.entries(kv)
                    .map(([key, value]) => `${key}=${value}`)
                    .join(',');
                return `${text || '—'}（${step.strategy}）`;
            }
            return '自动';
        },
        shardSummaryOf(step) {
            return step.shardMode === 'plan' ? `plan(+${Number(step.shardHint) || 0})` : 'none';
        },
        timeoutSummaryOf(step) {
            const timeout = Number(step.timeoutS) || 0;
            return timeout > 0 ? `超时 ${timeout}s` : '超时 不限';
        },
        clearNotice() {
            this.notice = '';
            this.noticeAction = '';
        },
        triggerConfigOut() {
            // cron 只出 expr；event 照存照显 config（§5.1 预留）；manual 恒空
            const editor = this.editor;
            if (editor.triggerType === 'cron') return { expr: String(editor.triggerExpr || '').trim() };
            if (editor.triggerType === 'event') return this.collectKv(editor.triggerConfigRows);
            return {};
        },
        // ============================ 校验 / 组装（前端预检，§8 同口径） ============================
        validateEditor() {
            const problems = [];
            const editor = this.editor;
            if (!editor) return ['编辑器未就绪'];
            if (!String(editor.name || '').trim()) problems.push('基本信息：名称必填');
            const priority = Number(editor.priority);
            if (!Number.isInteger(priority)) problems.push('defaults.priority 需是整数');
            const defaultTimeout = Number(editor.timeoutS);
            if (!Number.isFinite(defaultTimeout) || defaultTimeout < 0) {
                problems.push('defaults.timeout_s 需是有限非负数（0=不限）');
            }
            // 触发轻量预检（v2.1 §7.1.2）：cron 表达式 5 段、各段非空；深度校验归服务端 InvalidSpec
            if (editor.triggerType === 'cron') {
                const expr = String(editor.triggerExpr || '').trim();
                const segments = expr ? expr.split(/\s+/) : [];
                if (segments.length !== 5 || segments.some((segment) => !segment)) {
                    problems.push('trigger.config.expr 需是 5 段 cron 表达式（分 时 日 月 周），如 */10 * * * *');
                }
            }
            if (!editor.steps.length) problems.push('steps 不能为空');
            const names = new Set();
            editor.steps.forEach((step, index) => {
                problems.push(...this.validateStepProblems(step, `steps[${index}]`, names));
            });
            return problems;
        },
        validateStepProblems(step, where, names) {
            // 单步口径（§8）：编辑器全量校验与「配置脚本」[完成配置] 共用；names 由调用方维护唯一性
            const problems = [];
            const name = String(step.name || '').trim();
            if (!name) problems.push(`${where}.name 步骤名必填`);
            else if (names && names.has(name)) problems.push(`${where}.name 步骤名重复：${name}`);
            else if (names) names.add(name);
            if (!String(step.executorId || '').trim()) problems.push(`${where}.executor.id 需是非空字符串`);
            // material 必填（§2.1/§8.2）
            if (!String(step.materialId || '').trim()) {
                problems.push(`${where}.material.material_id 必填（计划步骤必须绑定脚本）`);
            }
            // cases（§8.3）
            if (step.casesMode !== 'all' && step.casesMode !== 'selected') {
                problems.push(`${where}.cases.mode 非法（可选：all / selected）`);
            }
            if (step.casesMode === 'selected' && !step.caseIds.length) {
                problems.push(`${where}.cases.mode=selected 时 case_ids 不能为空`);
            }
            // params raw + 核心管理键同名冲突（§8.4）
            const rawText = String(step.paramsRaw || '').trim();
            if (rawText && !this.parseStepRaw(step)) {
                problems.push(`${where}.params raw JSON 不是合法对象：${step.paramsRawError || '无法解析'}`);
            }
            const params = this.finalStepParams(step);
            const innerMaterial = params.material_id;
            if (
                typeof innerMaterial === 'string' &&
                innerMaterial &&
                step.materialId &&
                innerMaterial !== step.materialId
            ) {
                problems.push(`${where}.material_id 与 params.material_id 同名冲突：${step.materialId} ≠ ${innerMaterial}`);
            }
            const targets = params.targets;
            if (step.casesMode === 'selected' && targets !== undefined && targets !== null) {
                if (!sameStrList(targets, step.caseIds)) {
                    problems.push(`${where}.cases.case_ids 与 params.targets 同名冲突：${step.caseIds.join(', ')} ≠ ${JSON.stringify(targets)}`);
                }
            }
            if (step.casesMode === 'selected' && params.selection_id) {
                problems.push(
                    `${where}.params.selection_id 与 cases 选例同名冲突（计划选例走 cases.case_ids，请移除 selection_id）`
                );
            }
            // 台架定向（§8.5）
            if (step.targetMode === 'nodes') {
                if (!step.nodes.length) problems.push(`${where}.executor.nodes 指定台架模式下不能为空`);
                for (const node of step.nodes) {
                    if (!String(node || '').trim()) problems.push(`${where}.executor.nodes 含空串（台架名需非空）`);
                }
            }
            if (step.targetMode !== 'auto' && step.strategy !== 'balanced' && step.strategy !== 'random') {
                problems.push(`${where}.executor.strategy 非法（可选：balanced / random）`);
            }
            // shard / timeout（§8.6）
            const hint = Number(step.shardHint);
            if (!Number.isInteger(hint) || hint < 0) problems.push(`${where}.shard.hint 需是非负整数`);
            const timeout = Number(step.timeoutS);
            if (!Number.isFinite(timeout) || timeout < 0) {
                problems.push(`${where}.timeout_s 需是有限非负数（0=不限）`);
            }
            return problems;
        },
        materialOut(step) {
            const meta = this.materialMetaOf(step);
            return meta
                ? {
                      material_id: meta.material_id,
                      sha256: meta.sha256,
                      recipe_fingerprint: meta.recipe_fingerprint,
                      name: meta.name
                  }
                : { material_id: '', sha256: '', recipe_fingerprint: '', name: '' };
        },
        stepToSpec(step) {
            // 步骤 v2（设计 §2.1）：保存 / 临时任务组装共用
            return {
                name: String(step.name || '').trim(),
                executor: {
                    id: String(step.executorId || '').trim(),
                    kind: String(step.executorKind || 'executor'),
                    labels: step.targetMode === 'labels' ? this.collectKv(step.labelRows) : {},
                    nodes: step.targetMode === 'nodes' ? step.nodes.map((node) => String(node)).filter(Boolean) : [],
                    strategy: step.targetMode === 'auto' ? 'balanced' : step.strategy
                },
                material: this.materialOut(step),
                cases: {
                    mode: step.casesMode === 'selected' ? 'selected' : 'all',
                    case_ids: step.casesMode === 'selected' ? step.caseIds.slice() : []
                },
                params: this.finalStepParams(step),
                shard: { mode: step.shardMode === 'plan' ? 'plan' : 'none', hint: Number(step.shardHint) || 0 },
                requires: step.requires || {},
                timeout_s: Number(step.timeoutS) || 0
            };
        },
        showValidationProblems(problems) {
            this.validationErrors = problems;
        },
        handleSaveError(reason) {
            const message = this.reasonText(reason);
            if (reason && reason.status === 400) {
                // 校验错误逐条展示（400 的 InvalidSpec 消息按「；」拆行）
                const lines = message
                    .replace(/^(计划|任务)规格无效：/, '')
                    .split('；')
                    .map((line) => line.trim())
                    .filter(Boolean);
                this.showValidationProblems(lines.length ? lines : [message]);
            } else {
                this.error = message;
            }
        },
        // ============================ 保存 / 另存为 ============================
        planBody() {
            const editor = this.editor;
            return {
                name: String(editor.name || '').trim(),
                description: String(editor.description || ''),
                labels: this.collectKv(editor.labelsRows),
                defaults: {
                    priority: Number(editor.priority) || 0,
                    failure_policy: editor.failurePolicy,
                    timeout_s: Number(editor.timeoutS) || 0
                },
                trigger: {
                    type: editor.triggerType,
                    config: this.triggerConfigOut()
                },
                notify: {
                    type: editor.notifyType,
                    config: this.collectKv(editor.notifyConfigRows)
                },
                steps: editor.steps.map((step) => this.stepToSpec(step))
            };
        },
        async savePlan() {
            this.error = '';
            this.clearNotice();
            this.validationErrors = [];
            const problems = this.validateEditor();
            if (problems.length) {
                this.showValidationProblems(problems);
                return;
            }
            const editor = this.editor;
            const body = this.planBody();
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
                this.noticeAction = '';
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
                const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(this.editor.planId)}/clone`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}'
                });
                const copy = data.plan || {};
                this.applyPlanToEditor(copy);
                this.editorKey = this.planIdOf(copy);
                this.navigateEdit(this.editorKey, true);
                this.notice = `已另存为副本：${copy.name || this.editorKey}`;
                this.noticeAction = '';
                await this.loadPlans(true);
            } catch (reason) {
                this.error = `另存为失败：${this.reasonText(reason)}`;
            }
        },
        // ============================ 下发（§5.5） ============================
        async runPrePush() {
            // 预推材料（best-effort，静默）：对每个 material 调 pytest 族 push 口，
            // 404 = 族未提供推送端点跳过；其余错误收集不阻断（评审 #16）。结果由调用方并入通知条。
            const notes = [];
            const materialIds = [];
            for (const step of (this.editor && this.editor.steps) || []) {
                const materialId = String(step.materialId || '').trim();
                if (materialId && !materialIds.includes(materialId)) materialIds.push(materialId);
            }
            for (const materialId of materialIds) {
                try {
                    const data = await fetchJson(apiUrl(`/api/pytest/materials/${encodeURIComponent(materialId)}/push`), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: '{}'
                    });
                    const pushed = (data.pushed || []).length;
                    const failed = (data.failed || []).length;
                    notes.push({
                        text: `${materialId}：预推成功 ${pushed} 台${failed ? ` / 失败 ${failed} 台（走 missing 兜底）` : ''}`,
                        severity: failed ? 'warn' : 'success'
                    });
                } catch (reason) {
                    if (reason && reason.status === 404) {
                        notes.push({ text: `${materialId}：族未提供推送端点（404），跳过`, severity: 'secondary' });
                    } else {
                        notes.push({
                            text: `${materialId}：预推失败（${this.reasonText(reason)}），不阻断下发`,
                            severity: 'danger'
                        });
                    }
                }
            }
            return notes;
        },
        async dispatchPlan() {
            // 下发（v2.1 §7.1.1）：POST /api/test/plans/{id}/dispatch body {}（无 overrides），
            // 预推静默 best-effort 并入通知条；成功通知带 task_id + [查看结果]
            this.clearNotice();
            this.error = '';
            this.validationErrors = [];
            const planId = this.editor && this.editor.planId;
            if (!planId) return;
            this.dispatching = true;
            try {
                const notes = await this.runPrePush();
                const data = await fetchJson(apiUrl(`/api/test/plans/${encodeURIComponent(planId)}/dispatch`), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}'
                });
                const prepush = notes.length ? `；预推：${notes.map((note) => note.text).join('，')}` : '';
                this.notice = `任务已下发 task_id=${data.task_id}${data.deduplicated ? '（幂等去重，复用既有任务）' : ''}${prepush}`;
                this.noticeAction = 'tasks';
            } catch (reason) {
                this.handleSaveError(reason);
            } finally {
                this.dispatching = false;
            }
        },
        tempStageOf(step) {
            // 临时任务阶段：镜像 _plan_stage_specs 注入（§2.3）——params.material_id ←
            // material.material_id、selected 且未显式给 targets/selection_id 时注入
            // params.targets = cases.case_ids；只用步骤自身数据（v2.1 无下发覆盖面板）
            const spec = this.stepToSpec(step);
            const params = { ...spec.params };
            if (!params.material_id && spec.material.material_id) {
                params.material_id = spec.material.material_id;
            }
            if (spec.cases.mode === 'selected' && !('targets' in params) && !('selection_id' in params)) {
                params.targets = [...spec.cases.case_ids];
            }
            return {
                name: spec.name,
                executor: spec.executor,
                params,
                shard: spec.shard,
                requires: spec.requires,
                timeout_s: spec.timeout_s
            };
        },
        async submitTempTask() {
            // 直接下发（不保存）：组装 TaskSpec 直接 POST /api/test/tasks
            // （labels 不带 plan_id、幂等键 UI 生成 uuid）；只用步骤自身数据 + 计划级默认值
            this.clearNotice();
            this.error = '';
            this.validationErrors = [];
            const problems = this.validateEditor();
            if (problems.length) {
                this.showValidationProblems(problems);
                return;
            }
            const editor = this.editor;
            const stages = editor.steps.map((step) => this.tempStageOf(step));
            const spec = {
                name: String(editor.name || '').trim(),
                stages,
                priority: Number(editor.priority) || 0,
                labels: this.collectKv(editor.labelsRows),
                idempotency_key: newIdempotencyKey(),
                failure_policy: editor.failurePolicy,
                timeout_s: Number(editor.timeoutS) || 0
            };
            this.tempSubmitting = true;
            try {
                const notes = await this.runPrePush();
                const data = await fetchJson(apiUrl('/api/test/tasks'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(spec)
                });
                const prepush = notes.length ? `；预推：${notes.map((note) => note.text).join('，')}` : '';
                this.notice = `临时任务已下发（未保存计划）：task_id=${data.task_id}${prepush}`;
                this.noticeAction = 'tasks';
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
        scriptTextOf(rawStep) {
            // 列表「脚本信息」列：各步骤「脚本名@短sha」逗号串（旧格式归一显示）
            const step = isPlainObject(rawStep) ? rawStep : {};
            const material = isPlainObject(step.material) ? step.material : null;
            const topMaterialId = typeof step.material_id === 'string' ? step.material_id : '';
            const materialId = material ? String(material.material_id || '') : topMaterialId;
            const name = String((material && material.name) || materialId || '未选脚本');
            const sha = String((material && material.sha256) || '');
            return `${name}@${sha ? sha.slice(0, 8) : '未绑定'}`;
        },
        formatLabels(labels) {
            let source = labels;
            if (typeof labels === 'string') {
                try {
                    source = JSON.parse(labels) || {};
                } catch {
                    return labels;
                }
            }
            if (!isPlainObject(source)) return '';
            return Object.entries(source)
                .map(([key, value]) => `${key}=${value}`)
                .join(', ');
        },
        formatTime(value) {
            if (!value) return '—';
            return new Date(value * 1000).toLocaleString();
        },
        shortSha8(value) {
            const text = String(value || '');
            return text ? text.slice(0, 8) : '—';
        },
        stateText(state) {
            return MATERIAL_STATE_TEXT[state] || state || '—';
        },
        reasonText(reason) {
            return String((reason && reason.message) || reason);
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

.plans-page__topbar {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    flex-wrap: wrap;
}

.plans-page__topbar-title {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
}

.plans-page__topbar-title h2 {
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

.plans-page__removed {
    display: block;
    font-family: monospace;
    font-size: 0.8125rem;
    color: var(--red-500);
    word-break: break-all;
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
    min-width: 0;
}

.plans-page__field--inline {
    flex-direction: row;
    align-items: center;
    gap: 0.5rem;
}

.plans-page__label {
    font-size: 0.8125rem;
    color: var(--text-color-secondary);
}

.plans-page__input {
    width: 100%;
    min-width: 0;
}

.plans-page__inline {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
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

/* 测试脚本步骤卡（v2.1 §7.1.4：两行摘要 + 操作列，表单移入配置对话框） */
.plans-page__panel-head {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
}

.plans-page__card {
    margin-bottom: 1rem;
    border: 1px solid var(--surface-border);
    border-radius: var(--content-border-radius);
    background: var(--surface-card);
    overflow: hidden;
}

.plans-page__card-title {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 20rem;
}

.plans-page__step-line {
    display: flex;
    gap: 0.75rem;
    align-items: flex-start;
    justify-content: space-between;
    padding: 0.625rem 1rem;
}

.plans-page__step-main {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
    flex: 1;
}

.plans-page__step-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    min-width: 0;
}

.plans-page__step-row--summary {
    font-size: 0.8125rem;
    color: var(--text-color-secondary);
}

.plans-page__dialog {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    max-height: 72vh;
    overflow-y: auto;
    padding-right: 0.25rem;
}

.plans-page__section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
}

.plans-page__section h5 {
    margin: 0;
    font-size: 0.9375rem;
}

.plans-page__section h6 {
    margin: 0.5rem 0 0;
    font-size: 0.875rem;
    color: var(--text-color-secondary);
}

.plans-page__section + .plans-page__section {
    border-top: 1px dashed var(--surface-border);
    padding-top: 1rem;
}

.plans-page__radios {
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
}

.plans-page__radio {
    display: flex;
    align-items: center;
    gap: 0.375rem;
}

.plans-page__filters {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
}

.plans-page__filter {
    flex: 1 1 12rem;
    min-width: 10rem;
}

.plans-page__case-foot {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 0.25rem;
}

.plans-page__fallback {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
}

.plans-page__mono {
    font-family: monospace;
    font-size: 0.8125rem;
    word-break: break-all;
}

.plans-page__tag {
    margin-right: 0.25rem;
}
</style>
