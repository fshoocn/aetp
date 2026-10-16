<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { getBackendUrl, request } from '@/service/backend';
import MarkdownDoc from '@/components/MarkdownDoc.vue';

// 非同源时在页头提示数据来源；切换后端会整页刷新，无需响应式
const backendUrl = getBackendUrl();

const plugins = ref([]);
const loading = ref(true);
const uploading = ref(false);
const pending = ref('');
const error = ref('');
const notice = ref('');
const uploadRef = ref(null);

// 描述文件弹窗（README.md，Markdown 渲染）；docsAbort 防止后完成的慢请求覆盖新内容
const docsVisible = ref(false);
const docsPlugin = ref(null);
const docsContent = ref('');
const docsLoading = ref(false);
const docsError = ref('');
let docsAbort = null;

const enabledCount = computed(() => plugins.value.filter((plugin) => plugin.enabled).length);

// 展示分组：用户安装 → 预装 → 内置（内置记录自带 protected=True，第二段须先排除 builtin）
const pluginGroups = computed(() => [
    {
        key: 'installed',
        label: '用户安装插件',
        hint: 'ZIP 上传安装，可启停 / 卸载 / 覆盖升级',
        items: plugins.value.filter((plugin) => !plugin.builtin && !plugin.protected)
    },
    {
        key: 'preinstalled',
        label: '预装插件',
        hint: '平台预装：停用 / 卸载后重启会自动重装',
        items: plugins.value.filter((plugin) => !plugin.builtin && plugin.protected)
    },
    {
        key: 'builtin',
        label: '内置插件',
        hint: '随平台直装，不可安装 / 升级 / 启停 / 卸载',
        items: plugins.value.filter((plugin) => plugin.builtin)
    }
]);

// 表头搜索框：TreeTable 自带的全局过滤会按「先命中的列字段」裁剪子节点，跨列匹配会漏行，
// 故全局搜索自己做（覆盖名称 / ID / 版本 / 状态 / 入口）；逐列筛选仍走 filters
const query = ref('');

function matchesQuery(plugin) {
    const keyword = query.value.trim().toLowerCase();
    if (!keyword) return true;
    return [plugin.name, plugin.id, plugin.version, plugin.entrypoint, plugin.enabled ? '运行中' : '已停用'].some((value) => String(value).toLowerCase().includes(keyword));
}

// 表头排序：TreeTable 的 sortNodesSingle 只排根层（分组行之间），组内插件排序自己做；
// 表头点击 / aria-sort 仍由 TreeTable 驱动，经 v-model 同步排序状态
const sortField = ref(null);
const sortOrder = ref(null);
const collator = new Intl.Collator(undefined, { numeric: true });

function toRowData(plugin) {
    return {
        ...plugin,
        type: 'plugin',
        status_text: plugin.enabled ? '运行中' : '已停用',
        search_key: `${plugin.name} ${plugin.id}`
    };
}

function compareRows(left, right) {
    const direction = sortOrder.value === -1 ? -1 : 1;
    return direction * collator.compare(left[sortField.value] ?? '', right[sortField.value] ?? '');
}

// TreeTable 分组节点：用户安装 / 预装 / 内置 各为一行可折叠的分组行，组内即插件行。
// 插件行数据平铺（含排序 / 筛选用的派生字段）；分组行缺字段，排序时各组比较结果相等、顺序稳定
const treeNodes = computed(() =>
    pluginGroups.value
        .map((group) => {
            const rows = group.items.filter(matchesQuery).map(toRowData);
            if (sortField.value && sortOrder.value) rows.sort(compareRows);
            return {
                key: `group:${group.key}`,
                data: { type: 'group', label: group.label, hint: group.hint, count: rows.length },
                children: rows.map((row) => ({ key: row.id, leaf: true, data: row }))
            };
        })
        .filter((groupNode) => !query.value.trim() || groupNode.children.length > 0)
);

// 逐列筛选：TreeTable 的 filters 是「字段 → 原始值」，matchMode 由列的 filterMatchMode 声明
const filters = ref({ search_key: null, version: null, status_text: null, entrypoint: null });
const statusOptions = ['运行中', '已停用'];

// 默认收起，「展开全部 / 收起全部」沿用模板 TableDoc 的 Row Expansion 惯例
const expandedKeys = ref({});

function expandAll() {
    expandedKeys.value = { 'group:installed': true, 'group:preinstalled': true, 'group:builtin': true };
}

function collapseAll() {
    expandedKeys.value = {};
}

async function loadPlugins({ preserveError = false } = {}) {
    loading.value = true;
    if (!preserveError) error.value = '';
    try {
        const data = await request('/api/plugins');
        plugins.value = data.plugins;
    } catch (reason) {
        if (!preserveError) error.value = reason instanceof Error ? reason.message : String(reason);
    } finally {
        loading.value = false;
    }
}

async function uploadFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.zip')) {
        error.value = '请选择 ZIP 格式的插件包';
        return;
    }

    uploading.value = true;
    error.value = '';
    notice.value = '';
    try {
        const data = await sendUpload(file, false);
        notice.value = `已安装 ${data.plugin.name}，启用后刷新页面即可使用它的 UI。`;
        await loadPlugins();
    } catch (reason) {
        const details = reason instanceof Error ? reason.details : undefined;
        if (details && details.code === 'already-installed') {
            // 同 id 替换不区分版本方向，一律显示确认
            const confirmed = window.confirm(`插件 ${details.id}（版本 ${details.installed_version}）已安装。\n替换为版本 ${details.incoming_version} 吗？\n确认后旧版本归档保留一份，可回滚。`);
            if (confirmed) {
                try {
                    const data = await sendUpload(file, true);
                    notice.value = `已替换 ${data.plugin.name}（${details.installed_version} → ${data.plugin.version}），刷新页面以更新菜单和 UI。`;
                    await loadPlugins();
                } catch (replaceReason) {
                    error.value = replaceReason instanceof Error ? replaceReason.message : String(replaceReason);
                }
            }
        } else {
            error.value = reason instanceof Error ? reason.message : String(reason);
        }
    } finally {
        uploading.value = false;
        uploadRef.value?.clear();
    }
}

function sendUpload(file, replace) {
    return request(`/api/plugins/upload${replace ? '?replace=1' : ''}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/zip' },
        body: file
    });
}

async function togglePlugin(plugin) {
    pending.value = plugin.id;
    error.value = '';
    notice.value = '';
    try {
        const action = plugin.enabled ? 'disable' : 'enable';
        const data = await request(`/api/plugins/${encodeURIComponent(plugin.id)}/${action}`, { method: 'POST' });
        notice.value = plugin.enabled ? `${plugin.name} 已停用；刷新页面以更新菜单和 UI。` : `${plugin.name} 已启用；刷新页面以更新菜单和 UI。`;
        replacePlugin(data.plugin);
    } catch (reason) {
        error.value = reason instanceof Error ? reason.message : String(reason);
        await loadPlugins({ preserveError: true });
    } finally {
        pending.value = '';
    }
}

async function uninstallPlugin(plugin) {
    if (!window.confirm(`确定卸载“${plugin.name}”吗？插件文件将被删除。`)) return;
    pending.value = plugin.id;
    error.value = '';
    notice.value = '';
    try {
        await request(`/api/plugins/${encodeURIComponent(plugin.id)}`, { method: 'DELETE' });
        plugins.value = plugins.value.filter((item) => item.id !== plugin.id);
        notice.value = `${plugin.name} 已卸载；刷新页面以更新菜单和 UI。`;
    } catch (reason) {
        error.value = reason instanceof Error ? reason.message : String(reason);
    } finally {
        pending.value = '';
    }
}

function replacePlugin(updated) {
    plugins.value = plugins.value.map((plugin) => (plugin.id === updated.id ? updated : plugin));
}

async function openDocs(plugin) {
    docsAbort?.abort(); // 切换目标：中止旧请求，慢响应不得覆盖新内容
    const controller = new AbortController();
    docsAbort = controller;
    docsPlugin.value = plugin;
    docsContent.value = '';
    docsError.value = '';
    docsLoading.value = true;
    docsVisible.value = true;
    try {
        const data = await request(`/api/plugins/${encodeURIComponent(plugin.id)}/readme`, { signal: controller.signal });
        if (docsAbort !== controller) return;
        docsContent.value = data.content;
    } catch (reason) {
        if (controller.signal.aborted) return; // 主动取消：不写回过期状态
        docsError.value = reason instanceof Error ? reason.message : String(reason);
    } finally {
        if (docsAbort === controller) docsLoading.value = false;
    }
}

onBeforeUnmount(() => docsAbort?.abort());

function onUploader(event) {
    if (uploading.value) return;
    const file = event.files?.[0];
    if (file) uploadFile(file);
}

onMounted(loadPlugins);
</script>

<template>
    <main class="flex flex-col gap-6">
        <header class="flex flex-wrap items-center justify-between gap-4">
            <div>
                <p class="text-primary font-semibold text-xs m-0 mb-2">AETP / EXTENSIONS</p>
                <h1 class="text-2xl font-semibold m-0">插件管理</h1>
                <p class="text-muted-color text-sm m-0 mt-2">上传插件包，并管理插件运行状态。</p>
                <Tag v-if="backendUrl" class="mt-3" severity="secondary" icon="pi pi-server">数据来源：{{ backendUrl }}</Tag>
            </div>
        </header>

        <FileUpload
            ref="uploadRef"
            accept=".zip,application/zip"
            :maxFileSize="33554432"
            :multiple="false"
            :showUploadButton="false"
            :showCancelButton="false"
            chooseLabel="选择 ZIP 插件包"
            customUpload
            auto
            @uploader="onUploader"
        >
            <template #empty>
                <div class="flex items-center gap-3 text-muted-color text-sm">
                    <i class="pi pi-upload text-primary" aria-hidden="true" />
                    {{ uploading ? '正在安装…' : '将 ZIP 插件包拖放到此处，或点击「选择 ZIP 插件包」' }}
                </div>
            </template>
        </FileUpload>

        <Message v-if="error" severity="error" :closable="false" role="alert">{{ error }}</Message>
        <Message v-if="notice" severity="success" :closable="false" role="status">{{ notice }}</Message>

        <section class="flex flex-wrap items-stretch gap-4" aria-label="插件统计">
            <div class="flex items-center gap-3 border border-surface-300 rounded-border px-4 py-3">
                <span class="text-muted-color text-sm">已安装</span><strong class="text-xl">{{ plugins.length }}</strong>
            </div>
            <div class="flex items-center gap-3 border border-surface-300 rounded-border px-4 py-3">
                <span class="text-muted-color text-sm">运行中</span><strong class="text-xl">{{ enabledCount }}</strong>
            </div>
        </section>

        <div class="card">
            <TreeTable
                :value="plugins.length === 0 ? [] : treeNodes"
                v-model:expandedKeys="expandedKeys"
                v-model:sortField="sortField"
                v-model:sortOrder="sortOrder"
                :filters="filters"
                dataKey="key"
                :loading="loading"
                scrollable
                tableStyle="min-width: 62rem"
            >
                <template #header>
                    <div class="flex flex-wrap items-center justify-between gap-3">
                        <div>
                            <span class="font-semibold text-xl">已安装插件</span>
                            <p class="text-muted-color text-sm m-0 mt-2">按「用户安装 → 预装 → 内置」分组，可折叠；点表头排序，筛选行逐列过滤；停用保留文件，卸载会移除插件包。</p>
                        </div>
                        <div class="flex flex-wrap items-center gap-2">
                            <IconField>
                                <InputIcon><i class="pi pi-search" /></InputIcon>
                                <InputText v-model="query" placeholder="搜索名称 / ID / 版本 / 入口" />
                            </IconField>
                            <Button text icon="pi pi-plus" label="展开全部" @click="expandAll" />
                            <Button text icon="pi pi-minus" label="收起全部" @click="collapseAll" />
                            <Button text icon="pi pi-refresh" label="刷新" :loading="loading" @click="loadPlugins" />
                        </div>
                    </div>
                </template>
                <Column field="name" filterField="search_key" filterMatchMode="contains" sortable header="插件" :expander="true" style="min-width: 16rem">
                    <template #filter>
                        <InputText v-model="filters.search_key" placeholder="名称 / ID" class="w-full" />
                    </template>
                    <template #body="{ node }">
                        <div v-if="node.data.type === 'group'" class="flex flex-wrap items-baseline gap-x-3">
                            <span class="font-semibold whitespace-nowrap">{{ node.data.label }}</span>
                            <span class="text-muted-color text-sm">{{ node.data.hint }} · {{ node.data.count }} 个</span>
                        </div>
                        <div v-else>
                            <div class="flex flex-wrap items-baseline gap-x-2">
                                <strong class="whitespace-nowrap">{{ node.data.name }}</strong>
                                <small class="text-muted-color">{{ node.data.id }}</small>
                            </div>
                            <small v-if="node.data.last_error" class="block text-red-500 mt-1">{{ node.data.last_error }}</small>
                        </div>
                    </template>
                </Column>
                <Column field="version" filterMatchMode="contains" sortable header="版本" style="min-width: 9rem">
                    <template #filter>
                        <InputText v-model="filters.version" placeholder="版本" class="w-full" />
                    </template>
                    <template #body="{ node }">
                        <template v-if="node.data.type !== 'group'">{{ node.data.version }}</template>
                    </template>
                </Column>
                <Column field="status_text" filterMatchMode="equals" sortable header="状态" style="min-width: 9rem">
                    <template #filter>
                        <Select v-model="filters.status_text" :options="statusOptions" placeholder="全部" showClear class="w-full" />
                    </template>
                    <template #body="{ node }">
                        <Tag v-if="node.data.type !== 'group'" :value="node.data.status_text" :severity="node.data.enabled ? 'success' : 'secondary'" />
                    </template>
                </Column>
                <Column field="entrypoint" filterMatchMode="contains" sortable header="入口" style="min-width: 12rem">
                    <template #filter>
                        <InputText v-model="filters.entrypoint" placeholder="入口" class="w-full" />
                    </template>
                    <template #body="{ node }">
                        <code v-if="node.data.type !== 'group'" class="whitespace-nowrap text-sm">{{ node.data.entrypoint }}</code>
                    </template>
                </Column>
                <Column header="操作" frozen alignFrozen="right" style="min-width: 16rem">
                    <template #body="{ node }">
                        <div v-if="node.data.type !== 'group'" class="flex flex-wrap items-center gap-2">
                            <Button text severity="secondary" size="small" icon="pi pi-book" label="文档" title="查看插件文档" :disabled="!node.data.has_readme" @click="openDocs(node.data)" />
                            <template v-if="!node.data.builtin">
                                <Button
                                    text
                                    severity="secondary"
                                    size="small"
                                    :icon="node.data.enabled ? 'pi pi-stop' : 'pi pi-play'"
                                    :label="node.data.enabled ? '停用' : '启用'"
                                    :disabled="pending === node.data.id"
                                    :title="node.data.protected ? '平台预装插件：重启后会自动重装' : ''"
                                    @click="togglePlugin(node.data)"
                                />
                                <Button
                                    text
                                    severity="danger"
                                    size="small"
                                    icon="pi pi-trash"
                                    label="卸载"
                                    :disabled="pending === node.data.id"
                                    :title="node.data.protected ? '平台预装插件：重启后会自动重装' : ''"
                                    @click="uninstallPlugin(node.data)"
                                />
                            </template>
                            <span v-else class="flex items-center gap-2 text-muted-color text-sm whitespace-nowrap" title="内置插件随平台直装，不可安装 / 升级 / 启停 / 卸载">
                                <i class="pi pi-lock" aria-hidden="true" /> 随平台直装
                            </span>
                        </div>
                    </template>
                </Column>
                <template #empty>
                    <span class="text-muted-color">{{ plugins.length ? '没有匹配当前搜索 / 筛选的插件' : '尚未安装插件' }}</span>
                </template>
            </TreeTable>
        </div>

        <Dialog v-model:visible="docsVisible" modal :header="docsPlugin ? `${docsPlugin.name} · v${docsPlugin.version}` : '插件文档'" :style="{ width: '44rem', maxWidth: '92vw' }">
            <div class="max-h-[min(62vh,40rem)] overflow-y-auto pr-2">
                <p v-if="docsLoading" class="flex items-center gap-2 text-muted-color"><i class="pi pi-spin pi-spinner" aria-hidden="true" /> 正在读取描述文件…</p>
                <p v-else-if="docsError" class="flex items-center gap-2 text-red-500"><i class="pi pi-exclamation-circle" aria-hidden="true" /> {{ docsError }}</p>
                <MarkdownDoc v-else :source="docsContent" />
            </div>
        </Dialog>
    </main>
</template>


