<script setup>
import { computed, onMounted, ref } from 'vue';
import { request } from '@/service/backend';

const plugins = ref([]);
const loading = ref(true);
const uploading = ref(false);
const pending = ref('');
const error = ref('');
const notice = ref('');
const uploadInput = ref(null);
const dragActive = ref(false);

const enabledCount = computed(() => plugins.value.filter((plugin) => plugin.enabled).length);

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
        const data = await request('/api/plugins/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/zip' },
            body: file
        });
        notice.value = `已安装 ${data.plugin.name}，启用后刷新页面即可使用它的 UI。`;
        await loadPlugins();
    } catch (reason) {
        error.value = reason instanceof Error ? reason.message : String(reason);
    } finally {
        uploading.value = false;
        if (uploadInput.value) uploadInput.value.value = '';
    }
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

function onDrop(event) {
    dragActive.value = false;
    if (uploading.value) return;
    const file = event.dataTransfer?.files?.[0];
    if (file) uploadFile(file);
}

onMounted(loadPlugins);
</script>

<template>
    <main class="plugin-management">
        <header class="plugin-management__heading">
            <div>
                <p class="plugin-management__eyebrow">AETP / EXTENSIONS</p>
                <h1>插件管理</h1>
                <p class="plugin-management__subtitle">上传插件包，并管理插件运行状态。</p>
            </div>
            <label class="plugin-management__upload" :class="{ 'is-disabled': uploading }">
                <i :class="uploading ? 'pi pi-spin pi-spinner' : 'pi pi-upload'" aria-hidden="true" />
                <span>{{ uploading ? '正在安装…' : '上传 ZIP' }}</span>
                <input ref="uploadInput" type="file" accept=".zip,application/zip" :disabled="uploading" @change="uploadFile($event.target.files?.[0])" />
            </label>
        </header>

        <div
            class="plugin-management__dropzone"
            :class="{ 'is-active': dragActive, 'is-disabled': uploading }"
            @dragenter.prevent="dragActive = true"
            @dragover.prevent="dragActive = true"
            @dragleave.prevent="dragActive = false"
            @drop.prevent="onDrop"
        >
            <i class="pi pi-inbox" aria-hidden="true" />
            <span>将 ZIP 插件包拖放到此处，或使用“上传 ZIP”</span>
        </div>

        <p v-if="error" class="plugin-management__message plugin-management__message--error" role="alert"><i class="pi pi-exclamation-circle" aria-hidden="true" /> {{ error }}</p>
        <p v-if="notice" class="plugin-management__message plugin-management__message--success" role="status"><i class="pi pi-check-circle" aria-hidden="true" /> {{ notice }}</p>

        <section class="plugin-management__summary" aria-label="插件统计">
            <div>
                <span>已安装</span><strong>{{ plugins.length }}</strong>
            </div>
            <div>
                <span>运行中</span><strong>{{ enabledCount }}</strong>
            </div>
        </section>

        <section class="plugin-management__list">
            <div class="plugin-management__list-heading">
                <div>
                    <h2>已安装插件</h2>
                    <p>停用保留文件；卸载会移除插件包。</p>
                </div>
                <button class="plugin-management__refresh" type="button" title="刷新列表" :disabled="loading" @click="loadPlugins">
                    <i :class="loading ? 'pi pi-spin pi-spinner' : 'pi pi-refresh'" aria-hidden="true" />
                    <span>刷新</span>
                </button>
            </div>

            <div class="plugin-management__table-wrap">
                <table class="plugin-management__table">
                    <thead>
                        <tr>
                            <th>插件</th>
                            <th>版本</th>
                            <th>状态</th>
                            <th>入口</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-if="loading">
                            <td colspan="5" class="plugin-management__empty">正在读取插件列表…</td>
                        </tr>
                        <tr v-else-if="plugins.length === 0">
                            <td colspan="5" class="plugin-management__empty">尚未安装插件</td>
                        </tr>
                        <tr v-for="plugin in plugins" v-else :key="plugin.id">
                            <td>
                                <strong>{{ plugin.name }}</strong>
                                <small>{{ plugin.id }}</small>
                                <small v-if="plugin.last_error" class="plugin-management__plugin-error">{{ plugin.last_error }}</small>
                            </td>
                            <td>{{ plugin.version }}</td>
                            <td>
                                <span class="plugin-management__state" :class="plugin.enabled ? 'is-enabled' : 'is-disabled'">
                                    <i :class="plugin.enabled ? 'pi pi-check-circle' : 'pi pi-pause-circle'" aria-hidden="true" />
                                    {{ plugin.enabled ? '运行中' : '已停用' }}
                                </span>
                            </td>
                            <td>
                                <code>{{ plugin.entrypoint }}</code>
                            </td>
                            <td>
                                <div class="plugin-management__actions">
                                    <button type="button" :disabled="pending === plugin.id" @click="togglePlugin(plugin)">
                                        <i :class="plugin.enabled ? 'pi pi-stop' : 'pi pi-play'" aria-hidden="true" />
                                        {{ plugin.enabled ? '停用' : '启用' }}
                                    </button>
                                    <button class="is-danger" type="button" :disabled="pending === plugin.id" @click="uninstallPlugin(plugin)"><i class="pi pi-trash" aria-hidden="true" /> 卸载</button>
                                </div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </section>
    </main>
</template>

<style scoped>
.plugin-management {
    display: grid;
    gap: 1.25rem;
}
.plugin-management__heading,
.plugin-management__list-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
}
.plugin-management__eyebrow {
    margin: 0 0 0.3rem;
    color: var(--p-primary-color);
    font-size: 0.72rem;
    font-weight: 700;
}
.plugin-management h1 {
    margin: 0;
    font-size: 1.55rem;
}
.plugin-management__subtitle,
.plugin-management__list-heading p {
    margin: 0.3rem 0 0;
    color: var(--p-text-muted-color);
    font-size: 0.875rem;
}
.plugin-management__upload {
    display: inline-flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.7rem 1rem;
    border-radius: 0.4rem;
    background: var(--p-primary-color);
    color: var(--p-primary-contrast-color);
    font-weight: 600;
    cursor: pointer;
}
.plugin-management__upload.is-disabled {
    opacity: 0.65;
    cursor: wait;
}
.plugin-management__upload input {
    display: none;
}
.plugin-management__dropzone {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.7rem;
    min-height: 4.5rem;
    border: 1px dashed var(--p-content-border-color);
    border-radius: 0.45rem;
    color: var(--p-text-muted-color);
    font-size: 0.875rem;
}
.plugin-management__dropzone > i {
    color: var(--p-primary-color);
}
.plugin-management__dropzone.is-active {
    border-color: var(--p-primary-color);
    background: var(--p-highlight-background);
}
.plugin-management__message {
    display: flex;
    align-items: start;
    gap: 0.5rem;
    margin: 0;
    padding: 0.75rem 0.9rem;
    border-radius: 0.4rem;
    font-size: 0.875rem;
}
.plugin-management__message--error {
    background: var(--p-red-50);
    color: var(--p-red-700);
}
.plugin-management__message--success {
    background: var(--p-green-50);
    color: var(--p-green-700);
}
.plugin-management__summary {
    display: grid;
    grid-template-columns: repeat(2, minmax(8rem, 12rem));
    gap: 0.75rem;
}
.plugin-management__summary > div {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.8rem 1rem;
    border: 1px solid var(--p-content-border-color);
    border-radius: 0.45rem;
    background: var(--p-content-background);
}
.plugin-management__summary span {
    color: var(--p-text-muted-color);
    font-size: 0.875rem;
}
.plugin-management__summary strong {
    font-size: 1.15rem;
    font-variant-numeric: tabular-nums;
}
.plugin-management__list {
    display: grid;
    gap: 0.85rem;
}
.plugin-management__list-heading h2 {
    margin: 0;
    font-size: 1.05rem;
}
.plugin-management__refresh,
.plugin-management__actions button {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--p-content-border-color);
    border-radius: 0.35rem;
    background: var(--p-content-background);
    color: var(--p-text-color);
    font: inherit;
    cursor: pointer;
}
.plugin-management__refresh:disabled,
.plugin-management__actions button:disabled {
    opacity: 0.5;
    cursor: wait;
}
.plugin-management__table-wrap {
    overflow-x: auto;
    border: 1px solid var(--p-content-border-color);
    border-radius: 0.45rem;
}
.plugin-management__table {
    width: 100%;
    min-width: 740px;
    border-collapse: collapse;
    text-align: left;
    font-size: 0.875rem;
}
.plugin-management__table th,
.plugin-management__table td {
    padding: 0.75rem 0.85rem;
    border-bottom: 1px solid var(--p-content-border-color);
    vertical-align: middle;
}
.plugin-management__table th {
    color: var(--p-text-muted-color);
    font-weight: 600;
}
.plugin-management__table tr:last-child td {
    border-bottom: 0;
}
.plugin-management__table td:first-child {
    min-width: 12rem;
}
.plugin-management__table td:first-child small {
    display: block;
    margin-top: 0.2rem;
    color: var(--p-text-muted-color);
}
.plugin-management__plugin-error {
    max-width: 24rem;
    color: var(--p-red-600) !important;
    white-space: normal;
}
.plugin-management__state {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    white-space: nowrap;
}
.plugin-management__state.is-enabled {
    color: #16834a;
}
.plugin-management__state.is-disabled {
    color: var(--p-text-muted-color);
}
.plugin-management__actions {
    display: flex;
    gap: 0.4rem;
}
.plugin-management__actions .is-danger {
    color: var(--p-red-600);
}
.plugin-management__table code {
    white-space: nowrap;
}
.plugin-management__empty {
    padding: 1.5rem !important;
    color: var(--p-text-muted-color);
    text-align: center;
}
@media (max-width: 640px) {
    .plugin-management__heading,
    .plugin-management__list-heading {
        align-items: start;
        flex-direction: column;
    }
}
</style>
