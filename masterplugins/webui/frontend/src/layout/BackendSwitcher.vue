<script setup>
import { computed, ref } from 'vue';
import { getBackendUrl, normalizeBackendUrl, setBackendUrl } from '@/service/backend';

const visible = ref(false);
const input = ref('');
const message = ref('');
const isError = ref(false);
const busy = ref(false);

// 后端地址切换后整页刷新，因此这里读模块级状态即可，无需响应式
const active = computed(() => Boolean(getBackendUrl()));
const currentLabel = computed(() => getBackendUrl() || '与页面同源');

function show(text, error = false) {
    message.value = text;
    isError.value = error;
}

function open() {
    input.value = getBackendUrl();
    show('');
    visible.value = true;
}

async function testConnection() {
    busy.value = true;
    show('');
    let target = '';
    try {
        const base = normalizeBackendUrl(input.value);
        target = base ? `${base}/api/plugins` : '/api/plugins';
        const response = await fetch(target);
        if (!response.ok) {
            show(`连接失败：HTTP ${response.status}`, true);
            return;
        }
        const data = await response.json().catch(() => null);
        const count = data && Array.isArray(data.plugins) ? data.plugins.length : 0;
        show(`连接成功：后端有 ${count} 个已安装插件`);
    } catch (error) {
        if (!target) {
            show(error instanceof Error ? error.message : String(error), true);
            return;
        }
        // fetch 对「连不上」和「被 CORS 拦截」抛的都是 Failed to fetch：
        // 用 no-cors 探测可达性来区分——探得通说明后端活着，是浏览器拦的
        try {
            await fetch(target, { mode: 'no-cors' });
            show('连接失败：跨源被浏览器拦截（目标后端需允许 CORS：webapi 的 cors_origins 配置，默认 *）', true);
        } catch {
            show('连接失败：无法连接（目标后端未启动或地址有误）', true);
        }
    } finally {
        busy.value = false;
    }
}

function apply(value) {
    show('');
    try {
        setBackendUrl(value);
    } catch (error) {
        show(error instanceof Error ? error.message : String(error), true);
        return;
    }
    // 启动期的清单 / 脚本 / 样式都挂在旧后端上，整页刷新最干净
    window.location.reload();
}
</script>

<template>
    <button type="button" class="layout-topbar-action" :class="{ 'layout-topbar-action-highlight': active }" title="切换后端地址" @click="open">
        <i class="pi pi-server"></i>
    </button>

    <Dialog v-model:visible="visible" header="切换后端地址" :modal="true" :style="{ width: '26rem' }">
        <p class="m-0 mb-3 text-sm opacity-80">
            当前后端：<strong>{{ currentLabel }}</strong>。保存后插件 API 与 UI 资源改从新地址加载，页面自动刷新。
        </p>
        <InputText v-model="input" class="w-full" placeholder="http://127.0.0.1:8080（留空 = 与页面同源）" @keyup.enter="apply(input)" />
        <p v-if="message" class="m-0 mt-2 text-sm" :class="isError ? 'text-red-500' : 'text-green-600'">{{ message }}</p>
        <template #footer>
            <Button label="恢复同源" link :disabled="busy" @click="apply('')" />
            <Button label="测试连接" severity="secondary" :disabled="busy" @click="testConnection" />
            <Button label="保存并刷新" :disabled="busy" @click="apply(input)" />
        </template>
    </Dialog>
</template>
